"""StarDataParadise wire v1 DB-API client."""
from datetime import date as Date, datetime, time as Time, timezone
from importlib.metadata import version
from importlib.resources import files
import hashlib

from ._dbapi import Connection, Cursor
from ._errors import (Warning, Error, InterfaceError, DatabaseError, DataError, OperationalError, IntegrityError,
                      InternalError, ProgrammingError, NotSupportedError, OutcomeUnknown)
from ._types import Bind, Collection, Record, TimeValue, Timestamp as TimestampValue
from ._types import decode as decode_value

__version__ = version("stardataparadise-sdk")
apilevel = "2.0"
paramstyle = "named"
threadsafety = 1
WIRE_CONTRACT_SHA256 = "e6500900a3fa34910781ea92d5e41fe8716511fac6f6d6c689ff9cce15d578ab"


class _TypeGroup:
    def __init__(self, *tags):
        self._tags = frozenset(tags)

    def __eq__(self, sql_type):
        return isinstance(sql_type, str) and sql_type.partition("(")[0] in self._tags

    def __ne__(self, sql_type):
        return not self == sql_type


STRING = _TypeGroup("TEXT")
BINARY = _TypeGroup("BLOB")
NUMBER = _TypeGroup("BOOLEAN", "BIGINT", "DECIMAL")
DATETIME = _TypeGroup("DATE", "TIME", "TIMESTAMP WITH TIME ZONE")
ROWID = _TypeGroup("ROWID")
Binary = bytes


def Timestamp(year, month, day, hour, minute, second):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def DateFromTicks(ticks):
    return datetime.fromtimestamp(ticks, tz=timezone.utc).date()


def TimeFromTicks(ticks):
    return datetime.fromtimestamp(ticks, tz=timezone.utc).time()


def TimestampFromTicks(ticks):
    return datetime.fromtimestamp(ticks, tz=timezone.utc)


def connect(host="127.0.0.1", port=7443, *, user, password, ca_file=None, tls_name=None,
            timeout=15, autocommit=False, retry_reads=False):
    """Open an authenticated TLS connection. The default is manual commit."""
    return Connection(host, port, user, password, ca_file=ca_file, tls_name=tls_name, timeout=timeout,
                      autocommit=autocommit, retry_reads=retry_reads)


def wire_contract():
    """Return the immutable embedded Server wire v1 archive, checking its digest."""
    content = files(__package__).joinpath("contracts", "sdp-contract-v1.zip").read_bytes()
    if hashlib.sha256(content).hexdigest() != WIRE_CONTRACT_SHA256:
        raise InternalError("Wire contract archive checksum mismatch")
    return content