"""PEP 249 exceptions and Server error diagnostics."""

class Warning(Exception):
    pass


class Error(Exception):
    def __init__(self, message, *, code=None, sqlstate=None, correlation_id=None, request_token=None,
                 unknown_outcome=False, line=None, column=None):
        super().__init__(message)
        self.code = code
        self.sqlstate = sqlstate
        self.correlation_id = correlation_id
        self.request_token = request_token
        self.unknown_outcome = unknown_outcome
        self.line = line
        self.column = column


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


class OutcomeUnknown(OperationalError):
    """The original token can be resolved through outcome() on another connection."""


def server_error(error, token):
    state = error["sqlstate"]
    code = error["code"]
    uncertain = error["unknown_outcome"]
    if uncertain:
        target = OutcomeUnknown
    elif code in (1004, 1007) or state.startswith("0A"):
        target = NotSupportedError
    elif state.startswith("23"):
        target = IntegrityError
    elif state.startswith("22"):
        target = DataError
    elif code in (1001, 1015) or state.startswith("XX"):
        target = InternalError
    elif state.startswith("42") and code != 1006:
        target = ProgrammingError
    else:
        target = OperationalError
    return target(error["message"], code=code, sqlstate=state,
                  correlation_id=error.get("correlation_id"), request_token=token,
                  unknown_outcome=uncertain, line=error.get("line"), column=error.get("column"))
