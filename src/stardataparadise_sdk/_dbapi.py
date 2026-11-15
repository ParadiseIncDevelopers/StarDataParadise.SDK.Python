"""PEP 249 connection, transaction, and pull-based result lifecycle."""
from collections.abc import Mapping
import math
import os
import re
import secrets

from ._errors import (DataError, Error, InterfaceError, InternalError, NotSupportedError, OperationalError,
                      ProgrammingError)
from ._types import Bind, _name, bind, decode
from ._wire import Wire


_READ = {"SELECT", "WITH", "EXPLAIN"}
_ROUTINE = {"CALL", "BEGIN", "DECLARE"}
_WRITE = {"CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "GRANT", "REVOKE",
          "ANALYZE", "REINDEX"}
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_DECIMAL = re.compile(r"DECIMAL\s*\((\d+),\s*(\d+)\)\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*){0,2}\Z")


def _verb(sql):
    if not isinstance(sql, str):
        raise ProgrammingError("SQL must be text")
    rest = sql.lstrip()
    while rest.startswith(("--", "/*")):
        end = rest.find("\n") if rest.startswith("--") else rest.find("*/") + 1
        if end < 1:
            raise ProgrammingError("The input contains only or ends inside a comment")
        rest = rest[end + 1:].lstrip()
    match = re.match(r"[A-Za-z]+", rest)
    if not match:
        raise ProgrammingError("SQL is empty or has no recognized statement name")
    verb = match.group(0).upper()
    if verb not in _READ | _ROUTINE | _WRITE:
        raise NotSupportedError("Unsupported SQL command category")
    if verb in {"CREATE", "ALTER"} and re.match(r"(?i)^(CREATE|ALTER)\s+MODEL\b", rest):
        raise NotSupportedError("Use an explicit typed model operation for catalog writes")
    return verb


def _key(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ProgrammingError("A request token is 32 lowercase hexadecimal digits")
    return value


def _secret(value, name):
    if not isinstance(value, str) or not value or "\0" in value:
        raise InterfaceError(f"{name} must be a nonempty text setting")
    try:
        if len(value.encode("utf-8")) > 1024:
            raise InterfaceError(f"{name} is outside the Server limit")
    except UnicodeError as error:
        raise InterfaceError(f"{name} must contain valid UTF-8 scalar values") from error
    return value


class Connection:
    def __init__(self, host, port, user, password, *, ca_file, tls_name, timeout, autocommit, retry_reads):
        if not isinstance(host, str) or not host or "\0" in host:
            raise InterfaceError("Invalid Server host")
        if type(port) is not int or not 1 <= port <= 65535:
            raise InterfaceError("Server port must be between 1 and 65535")
        if tls_name is not None and (not isinstance(tls_name, str) or not tls_name or "\0" in tls_name):
            raise InterfaceError("Invalid TLS server name")
        if (type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 300):
            raise InterfaceError("Timeout must be a finite number between 0 and 300 seconds")
        if type(autocommit) is not bool or type(retry_reads) is not bool:
            raise InterfaceError("autocommit and retry_reads must be booleans")
        if ca_file is not None:
            try:
                ca_file = os.fspath(ca_file)
            except TypeError as error:
                raise InterfaceError("CA file must be a path") from error
        self._connection_args = (host, port, _secret(user, "User"), _secret(password, "Password"))
        self._connection_options = {"ca_file": ca_file, "tls_name": tls_name, "timeout": timeout}
        self._autocommit = autocommit
        self._retry_reads = retry_reads
        self._closed = False
        self._transaction = False
        self._cursor = None
        self.last_request_token = None
        self._wire = self._connect()

    def _connect(self):
        return Wire(*self._connection_args, **self._connection_options)

    def _require_open(self):
        if self._closed:
            raise InterfaceError("Connection was closed")
        if not self._wire.open:
            raise InterfaceError("Connection is closed by transport failure", code=1010, sqlstate="08006")

    def _call(self, operation, body=None, token=None):
        self._require_open()
        try:
            return self._wire.call(operation, body, token)
        finally:
            if not self._wire.open:
                self._transaction = False

    @property
    def session_id(self):
        self._require_open()
        return self._wire.session_id

    @property
    def server_version(self):
        self._require_open()
        return self._wire.product_version

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, enabled):
        self._require_open()
        if type(enabled) is not bool:
            raise InterfaceError("autocommit must be a boolean")
        if enabled != self._autocommit and (self._transaction or self._cursor is not None):
            raise ProgrammingError("Finish the transaction and result before changing autocommit")
        self._autocommit = enabled

    def reconnect(self):
        if self._closed:
            raise InterfaceError("An explicitly closed connection cannot be reopened")
        if self._wire.open:
            if self._transaction or self._cursor is not None:
                raise ProgrammingError("Finish the transaction and result before reconnecting")
            self._wire.close()
        self._cursor = None
        self._transaction = False
        self._wire = self._connect()

    def cancel(self):
        """Send a cancellation request without disturbing the outstanding TLS exchange."""
        self._require_open()
        current = self._wire
        request_id = current.pending
        if not request_id:
            return False
        with self._connect() as control:
            if self._wire is not current or current.pending != request_id:
                return False
            reply = control.call("query_cancel", {"session_id": current.session_id, "request_id": request_id})
            return reply["accepted"] is True

    def _begin(self):
        if not self._transaction:
            self._call("tx_begin")
            self._transaction = True

    def _finish_transaction(self, commit, request_token=None):
        if not self._transaction:
            return
        try:
            if commit:
                request_token = _key(request_token) if request_token is not None else secrets.token_hex(16)
                self.last_request_token = request_token
                self._call("tx_commit", {"request_token": request_token}, request_token)
            else:
                self._call("tx_rollback")
        except Error:
            if self._wire.open:
                try:
                    self._call("tx_rollback")
                except Error:
                    self._wire.close()
            raise
        finally:
            self._transaction = False

    def commit(self, *, request_token=None):
        self._require_open()
        if request_token is not None:
            _key(request_token)
        if self._cursor is not None:
            self._cursor._end(commit=True, request_token=request_token)
        self._finish_transaction(True, request_token)

    def rollback(self):
        self._require_open()
        if self._cursor is not None:
            self._cursor._end(commit=False)
        self._finish_transaction(False)

    def close(self):
        if self._closed:
            return
        try:
            if self._wire.open:
                self.rollback()
        finally:
            self._wire.close()
            self._closed = True
            self._connection_args = ()
            self._cursor = None
            self._transaction = False

    def cursor(self):
        self._require_open()
        return Cursor(self)

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, typ, value, trace):
        try:
            if typ is None:
                self.commit()
        finally:
            self.close()
        return False

    def _control(self, operation, body=None, request_token=None):
        if self._cursor is not None:
            raise ProgrammingError("Close the active cursor result first")
        return self._call(operation, body, request_token)

    def capabilities(self):
        return self._control("capabilities")["features"]

    def health(self):
        return self._control("health")

    def metadata(self):
        return self._control("metadata")

    def outcome(self, request_token):
        return self._control("tx_outcome", {"request_token": _key(request_token)})

    def history(self, target, *, limit=128):
        return self._control("history_log", {"target": target, "limit": limit})

    def validate_decision(self, sql):
        return self._control("decision_validate", {"sql": sql})

    def predict(self, name, features):
        return self._control("model_predict", {"name": name, "features": features})

    def dataset(self, dataset_id):
        return self._control("dataset_get", {"dataset_id": dataset_id})

    def job(self, job_id):
        return self._control("job_status", {"job_id": job_id})

    def submit_compute(self, count):
        key = secrets.token_hex(16)
        self.last_request_token = key
        return self._control("job_submit", {"request_token": key, "kind": "compute", "count": count}, key)

    def cancel_job(self, job_id):
        return self._control("job_cancel", {"job_id": job_id})

    def administration_health(self):
        return self._control("admin_health")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.arraysize = 128
        self.set_name = None
        self.last_request_token = None
        self._types = {}
        self._closed = False
        self._statement = None
        self._result = None
        self._owns_routine = False
        self._failed = False
        self._batch_index = 0
        self._set_index = 0
        self._end_set = False
        self._end_results = False
        self._rows = ()
        self._offset = 0

    def _require(self):
        if self._closed:
            raise InterfaceError("Cursor was closed")
        self.connection._require_open()

    def _reset(self):
        self._statement = self._result = self.set_name = self.description = self.last_request_token = None
        self._batch_index = self._set_index = self._offset = 0
        self._owns_routine = self._failed = self._end_set = self._end_results = False
        self._rows = ()
        self.rowcount = -1

    def execute(self, operation, parameters=None, *, request_token=None):
        self._require()
        self._end(commit=True)
        if self.connection._cursor is not None:
            raise ProgrammingError("Another cursor owns the active result")
        self._reset()
        verb = _verb(operation)
        declarations, encoded = bind(parameters, self._types)
        if request_token is not None:
            _key(request_token)
        for attempt in range(2):
            try:
                self._execute(operation, verb, declarations, encoded, request_token)
                return self
            except OperationalError as error:
                if (attempt or verb not in _READ or not self.connection.autocommit or not self.connection._retry_reads
                        or error.code != 1010 or error.unknown_outcome or self.connection._wire.open):
                    raise
                self.connection.reconnect()
                self._reset()
        raise InternalError("Unexpected retry state")

    def _execute(self, sql, verb, declarations, encoded, request_token):
        connection = self.connection
        self._owns_routine = connection.autocommit and verb in _ROUTINE and not connection._transaction
        if request_token is not None and (verb in _READ or connection._transaction or
                                          not connection.autocommit):
            raise ProgrammingError("Token requires an independent autocommit mutation")
        if self._owns_routine or not connection.autocommit:
            connection._begin()
        key = None
        if (verb in _WRITE or self._owns_routine) and (self._owns_routine or not connection._transaction):
            key = request_token if request_token is not None else secrets.token_hex(16)
        self.last_request_token = key
        if key is not None:
            connection.last_request_token = key
        try:
            prepared = connection._call("query.prepare", {"sql": sql, "parameter_types": declarations})
            self._statement = prepared["statement_id"]
            body = {"statement_id": self._statement, "parameters": encoded}
            operation_token = None if self._owns_routine else key
            if operation_token is not None:
                body["request_token"] = operation_token
            result = connection._call("execute", body, operation_token)
            self.rowcount = result["affected_rows"]
            self._result = result.get("result_id")
            if self._result is not None:
                connection._cursor = self
                if not self.rowcount:
                    self.rowcount = -1
                self._read_columns(result["columns"])
            else:
                self._end(commit=True)
        except BaseException:
            self._failed = True
            try:
                self._end(commit=False)
            except Error:
                connection._wire.close()
            raise

    def executemany(self, operation, seq_of_parameters):
        self._require()
        if _verb(operation) not in {"INSERT", "UPDATE", "DELETE"}:
            raise NotSupportedError("executemany requires a DML statement without a result set")
        self._end(commit=True)
        connection = self.connection
        if connection._cursor is not None:
            raise ProgrammingError("Another cursor owns the active result")
        owned = connection.autocommit and not connection._transaction
        connection._begin()
        save = None if owned else "sdp_many_" + secrets.token_hex(8)
        count = 0
        saved = False
        try:
            if save:
                connection._call("tx_savepoint", {"name": save})
                saved = True
            for parameters in seq_of_parameters:
                self.execute(operation, parameters)
                count += self.rowcount
            if save:
                connection._call("tx_release", {"name": save})
            if owned:
                connection._finish_transaction(True)
            self.rowcount = count
            return self
        except BaseException:
            if connection._wire.open and connection._transaction:
                try:
                    if saved:
                        connection._call("tx_rollback_to", {"name": save})
                        connection._call("tx_release", {"name": save})
                    elif owned:
                        connection._finish_transaction(False)
                except Error:
                    connection._wire.close()
            raise

    def callproc(self, procedure, parameters=()):
        if not isinstance(procedure, str) or not _IDENTIFIER.fullmatch(procedure):
            raise ProgrammingError("Use an ordinary qualified procedure identifier")
        inputs, markers = {}, []
        for index, parameter in enumerate(parameters):
            # A literal NULL is used only for an unspecified pure OUT argument.
            if parameter is None:
                markers.append("NULL")
            else:
                name = f"p{index}"
                markers.append(":" + name)
                inputs[name] = parameter
        self.execute(f"CALL {procedure}({','.join(markers)})", inputs)
        return parameters

    def _read_columns(self, metadata):
        result = []
        for column in metadata:
            name, type_name = column["name"], column["type"]
            shape = _DECIMAL.fullmatch(type_name)
            precision, scale = map(int, shape.groups()) if shape else (None, None)
            result.append((name, type_name, None, None, precision, scale, column.get("nullable")))
        self.description = tuple(result)

    def _fill(self):
        try:
            data = self.connection._call("fetch", {"result_id": self._result,
                                                  "batch_rows": max(1, min(1024, self.arraysize))})["batch"]
            if (data["result_set"], data["batch_index"]) != (self._set_index, self._batch_index):
                raise InternalError("Out-of-sequence results")
            if self._batch_index == 0:
                self._read_columns(data["columns"])
                self.set_name = data.get("set_name")
            self._batch_index += 1
            self._end_set, self._end_results = data["end_of_set"], data["end_of_results"]
            if (self._end_results and not self._end_set) or (not self._end_set and not data["rows"]):
                raise InternalError("A result batch did not advance")
            metadata = self.description
            if any(len(fields) != len(metadata) for fields in data["rows"]):
                raise InternalError("Result row width does not match its columns")
            self._rows = tuple(tuple(decode(field, column[1]) for field, column in zip(fields, metadata, strict=True))
                               for fields in data["rows"])
            self._offset = 0
        except BaseException:
            self._failed = True
            self._end_set = self._end_results = True
            raise

    def fetchone(self):
        self._require()
        if self.description is None:
            raise ProgrammingError("Cursor does not have a result")
        if self._result is None:
            return None
        while self._offset == len(self._rows) and not self._end_set:
            self._fill()
        if self._offset == len(self._rows):
            if self._end_results:
                self._end(commit=True)
            return None
        row = self._rows[self._offset]
        self._offset += 1
        return row

    def fetchmany(self, size=None):
        count = self.arraysize if size is None else size
        if type(count) is not int or count < 0:
            raise ProgrammingError("fetchmany requires a nonnegative integer")
        result = []
        for _ in range(count):
            row = self.fetchone()
            if row is None:
                break
            result.append(row)
        return result

    def fetchall(self):
        return list(self)

    def nextset(self):
        self._require()
        if self._result is None:
            return None
        while not self._end_set:
            self._fill()
        if self._end_results:
            self._end(commit=True)
            self.description = None
            return None
        self._set_index += 1
        self._batch_index = 0
        self._fill()
        return True

    def setinputsizes(self, sizes):
        if not isinstance(sizes, Mapping):
            raise NotSupportedError("Pass a mapping from named parameters to SQL declarations")
        definitions = {}
        for name, definition in sizes.items():
            if isinstance(definition, str):
                definition = Bind(None, definition)
            if not isinstance(definition, Bind):
                raise ProgrammingError("Declare input SQL types with Bind")
            definition.descriptor()
            definitions[_name(name)] = definition
        self._types = definitions

    def setoutputsize(self, size, column=None):
        self._require()
        # Fetch size is bounded by the Server; this optional DB-API hint is not used.

    def _end(self, *, commit, request_token=None):
        connection = self.connection
        try:
            if connection._wire.open:
                if self._result is not None and not self._end_results:
                    connection._call("result_cancel", {"result_id": self._result})
                if self._statement is not None:
                    connection._call("statement_close", {"statement_id": self._statement})
                if self._owns_routine:
                    connection._finish_transaction(commit and not self._failed, request_token or self.last_request_token)
        except Error:
            connection._wire.close()
            raise
        finally:
            if connection._cursor is self:
                connection._cursor = None
            self._statement = self._result = None
            self._owns_routine = False
            self._rows = ()

    def close(self):
        if not self._closed:
            try:
                self._end(commit=True)
            finally:
                self._closed = True

    def __enter__(self):
        self._require()
        return self

    def __exit__(self, typ, value, trace):
        if not self._closed:
            try:
                self._end(commit=typ is None)
            finally:
                self._closed = True
        return False

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row