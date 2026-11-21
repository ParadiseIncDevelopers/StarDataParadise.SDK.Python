"""PEP 249 exceptions and Server error diagnostics."""

class Warning(Exception):
    pass


class Error(Exception):
    def __init__(self, message, *, code=None, sqlstate=None, correlation_id=None, request_token=None,
                 unknown_outcome=False, line=None, column=None, native_errno=None, phase=None,
                 commit_state=None, run_state=None, run_id=None, retry=None, call_id=None,
                 action_commit_state=None, cause_native_errno=None, cause_sqlstate=None):
        super().__init__(message)
        self.code = code
        self.sqlstate = sqlstate
        self.correlation_id = correlation_id
        self.request_token = request_token
        self.unknown_outcome = unknown_outcome
        self.line = line
        self.column = column
        self.native_errno = native_errno
        self.phase = phase
        self.commit_state = commit_state
        self.run_state = run_state
        self.run_id = run_id
        self.retry = retry
        self.call_id = call_id
        self.action_commit_state = action_commit_state
        self.cause_native_errno = cause_native_errno
        self.cause_sqlstate = cause_sqlstate


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
                  unknown_outcome=uncertain, line=error.get("line"), column=error.get("column"),
                  **{name:error.get(name) for name in ("native_errno","phase","commit_state","run_state","run_id","retry",
                                                     "call_id","action_commit_state","cause_native_errno","cause_sqlstate")})
