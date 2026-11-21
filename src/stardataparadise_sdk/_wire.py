"""Independent, authenticated wire v1 transport."""
import json
import socket
import ssl
import struct
import time

from ._errors import Error, InterfaceError, InternalError, OperationalError, OutcomeUnknown, server_error

MAX_FRAME = 1024 * 1024


def _keys(pairs):
    mapping = {}
    for key, value in pairs:
        if key in mapping:
            raise ValueError("Duplicate JSON key")
        mapping[key] = value
    return mapping


def _literal(_):
    raise ValueError("Non-finite JSON number")


def _check_depth(value, depth=0):
    if isinstance(value, (dict, list)):
        if depth >= 32:
            raise ValueError("JSON nesting exceeds the Server frame limit")
        for child in value.values() if isinstance(value, dict) else value:
            _check_depth(child, depth + 1)


class Wire:
    def __init__(self, host, port, user, password, *, ca_file, tls_name, timeout):
        self._socket = None
        self._last_id = 0
        self.pending = 0
        self._timeout = timeout
        self.session_id = ""
        self.product_version = ""
        try:
            context = ssl.create_default_context(cafile=ca_file)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            with socket.create_connection((host, port), timeout) as raw:
                self._socket = context.wrap_socket(raw, server_hostname=tls_name or host,
                                                   suppress_ragged_eofs=False)
            hello = self.call("hello")
            if hello["protocol_version"] != 1 or not hello["session_id"]:
                raise InterfaceError("Unsupported StarDataParadise protocol", code=1003, sqlstate="0A000")
            self.session_id = hello["session_id"]
            self.product_version = hello["product_version"]
            if self.call("auth", {"username": user, "password": password})["authenticated"] is not True:
                raise OperationalError("Authentication failed", code=1005, sqlstate="28000")
        except ssl.SSLError as error:
            self.close()
            raise OperationalError("Unable to validate the TLS connection", code=1011, sqlstate="08001") from error
        except TimeoutError as error:
            self.close()
            raise OperationalError("Connection timed out", code=1008, sqlstate="HYT00") from error
        except OSError as error:
            self.close()
            raise OperationalError("Connection could not be opened", code=1010, sqlstate="08006") from error
        except (KeyError, TypeError, ValueError) as error:
            self.close()
            raise InternalError("Malformed Server handshake", code=1001, sqlstate="08P01") from error
        except Error:
            self.close()
            raise

    @property
    def open(self):
        return self._socket is not None

    def close(self):
        current, self._socket = self._socket, None
        if current is not None:
            current.close()

    def __enter__(self):
        return self

    def __exit__(self, typ, value, trace):
        self.close()
        return False

    @staticmethod
    def _receive(sock, count, deadline):
        result = bytearray(count)
        pending = memoryview(result)
        while len(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Response deadline elapsed")
            sock.settimeout(remaining)
            read = sock.recv_into(pending)
            if not read:
                raise EOFError("Truncated Server message")
            pending = pending[read:]
        return result

    def call(self, operation, body=None, token=None, *, uncertain=False):
        sock = self._socket
        if sock is None:
            raise InterfaceError("Connection closed", code=1010, sqlstate="08006")
        if self._last_id >= 0xFFFFFFFF:
            self.close()
            raise InterfaceError("Request ID limit reached", code=1013, sqlstate="55000")
        self._last_id += 1
        request_id = self._last_id
        body = {} if body is None else body
        try:
            request = {"v": 1, "id": request_id, "op": operation, "body": body}
            _check_depth(request)
            encoded = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise InterfaceError("Request cannot be encoded", code=1012, sqlstate="22023") from error
        if len(encoded) > MAX_FRAME:
            raise InterfaceError("Request exceeds the Server frame limit", code=1002, sqlstate="54000")
        self.pending = request_id
        deadline = time.monotonic() + self._timeout
        try:
            sock.settimeout(self._timeout)
            sock.sendall(struct.pack("!I", len(encoded)) + encoded)
            data, = struct.unpack("!I", self._receive(sock, 4, deadline))
            if not 0 < data <= MAX_FRAME:
                raise ValueError("Wrong Server frame length")
            payload = self._receive(sock, data, deadline).decode("utf-8", errors="strict")
            response = json.loads(payload, object_pairs_hook=_keys, parse_constant=_literal)
            _check_depth(response)
            if type(response["v"]) is not int or type(response["id"]) is not int or (
                    response["v"], response["id"]) != (1, request_id):
                raise ValueError("Mismatched Server response")
            if response["ok"] is True and isinstance(response.get("body"), dict):
                return response["body"]
            if response["ok"] is False and isinstance(response.get("error"), dict):
                raise server_error(response["error"], token)
            raise ValueError("Wrong Server response envelope")
        except KeyboardInterrupt as error:
            self.close()
            if (token is not None or uncertain):
                raise OutcomeUnknown("Operation interrupted", code=1009, sqlstate="57014",
                                     request_token=token, unknown_outcome=True) from error
            raise
        except TimeoutError as error:
            self.close()
            target = OutcomeUnknown if (token is not None or uncertain) else OperationalError
            raise target("Operation timed out", code=1008, sqlstate="HYT00", request_token=token,
                         unknown_outcome=(token is not None or uncertain)) from error
        except (OSError, EOFError) as error:
            self.close()
            target = OutcomeUnknown if (token is not None or uncertain) else OperationalError
            raise target("Connection closed during the exchange", code=1010, sqlstate="08006",
                         request_token=token, unknown_outcome=(token is not None or uncertain)) from error
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            self.close()
            target = OutcomeUnknown if (token is not None or uncertain) else InternalError
            raise target("Malformed Server reply", code=1001, sqlstate="08P01", request_token=token,
                         unknown_outcome=(token is not None or uncertain)) from error
        finally:
            self.pending = 0
