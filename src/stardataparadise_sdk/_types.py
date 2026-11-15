"""Lossless SQL and routine value mappings for wire v1."""
import datetime as _time
import decimal
import re
from dataclasses import dataclass

from ._errors import DataError, NotSupportedError

_EPOCH = _time.datetime(1970, 1, 1, tzinfo=_time.timezone.utc)


@dataclass(frozen=True)
class Timestamp:
    seconds: int
    nanos: int
    offset_minutes: int
    zone: str | None = None

    def to_datetime(self):
        if self.nanos % 1000:
            raise DataError("Sub-microsecond timestamp requires its exact Timestamp value")
        offset = _time.timezone(_time.timedelta(minutes=self.offset_minutes))
        return (_EPOCH + _time.timedelta(seconds=self.seconds, microseconds=self.nanos // 1000)).astimezone(offset)


@dataclass(frozen=True)
class TimeValue:
    hour: int
    minute: int
    second: int
    nanos: int = 0

    def __post_init__(self):
        if not all(type(value) is int for value in (self.hour, self.minute, self.second, self.nanos)) or not (
                0 <= self.hour <= 23 and 0 <= self.minute <= 59 and 0 <= self.second <= 59
                and 0 <= self.nanos < 1_000_000_000):
            raise DataError("Time is outside the Server limits")

    def to_time(self):
        if self.nanos % 1000:
            raise DataError("Sub-microsecond time requires its exact TimeValue value")
        return _time.time(self.hour, self.minute, self.second, self.nanos // 1000)

    def __str__(self):
        return f"{self.hour:02}:{self.minute:02}:{self.second:02}.{self.nanos:09}"


@dataclass(frozen=True)
class Record:
    fields: tuple

    def __post_init__(self):
        fields = tuple((key, value) for key, value in self.fields)
        if any(not isinstance(key, str) or not key for key, _ in fields) or len({key for key, _ in fields}) != len(fields):
            raise DataError("Record fields require unique nonempty names")
        object.__setattr__(self, "fields", fields)


@dataclass(frozen=True)
class Collection:
    items: tuple

    def __post_init__(self):
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class Bind:
    value: object
    type_name: str
    precision: int | None = None
    scale: int | None = None

    def descriptor(self):
        if not isinstance(self.type_name, str):
            raise DataError("Declare a valid SQL parameter type")
        name = self.type_name.strip().upper()
        allowed = {"BOOLEAN", "BIGINT", "DECIMAL", "TEXT", "BLOB", "DATE", "TIME", "TIMESTAMP WITH TIME ZONE", "RECORD", "COLLECTION"}
        if name not in allowed:
            raise NotSupportedError("Unsupported declared parameter type")
        result = {"type": name}
        if name == "DECIMAL":
            if type(self.precision) is not int or type(self.scale) is not int or not 0 <= self.scale <= self.precision <= 38 or not self.precision:
                raise DataError("DECIMAL precision/scale is outside the Server limits")
            result.update(precision=self.precision, scale=self.scale)
        elif self.precision is not None or self.scale is not None:
            raise DataError("Precision and scale require DECIMAL")
        return result


def _as_timestamp(value):
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() % 60:
        raise DataError("Timestamp requires a whole-minute UTC offset")
    delta = value.astimezone(_time.timezone.utc) - _EPOCH
    return Timestamp(delta.days * 86400 + delta.seconds, delta.microseconds * 1000, int(offset.total_seconds() // 60))


def _decimal(value):
    if not value.is_finite():
        raise DataError("Decimal must be finite")
    sign, digits, exponent = value.as_tuple()
    if exponent > 0:
        if len(digits) + exponent > 38:
            raise DataError("DECIMAL exceeds 38 digits")
        digits = digits + (0,) * exponent
    scale = max(0, -exponent)
    if max(scale, len(digits), 1) > 38:
        raise DataError("DECIMAL exceeds 38 digits")
    text = "".join(map(str, digits)).lstrip("0") or "0"
    return ("-" + text if sign and text != "0" else text), scale


def encode(value, *, compound=False):
    if value is None:
        return {"t": "null"}
    if type(value) is bool:
        return {"t": "bool", "v": value}
    if type(value) is int:
        if not -(2 ** 63) <= value <= 2 ** 63 - 1:
            raise DataError("Integer is outside signed BIGINT")
        return {"t": "i64", "v": str(value)}
    if isinstance(value, decimal.Decimal):
        unscaled, scale = _decimal(value)
        return {"t": "decimal", "unscaled": unscaled, "scale": scale}
    if isinstance(value, str):
        return {"t": "text", "v": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"t": "binary", "hex": bytes(value).hex()}
    if isinstance(value, _time.datetime):
        value = _as_timestamp(value)
    if isinstance(value, Timestamp):
        if not -(2 ** 63) <= value.seconds <= 2 ** 63 - 1 or not (
                0 <= value.nanos < 1_000_000_000 and -840 <= value.offset_minutes <= 840):
            raise DataError("Timestamp is outside the Server limits")
        timestamp = {"t": "timestamp", "seconds": str(value.seconds), "nanos": value.nanos,
                     "offset_minutes": value.offset_minutes}
        if value.zone is not None:
            timestamp["zone"] = value.zone
        return timestamp
    if isinstance(value, _time.date):
        return {"t": "date" if compound else "text", "v": value.isoformat()}
    if isinstance(value, _time.time):
        if value.tzinfo is not None:
            raise DataError("TIME has no time zone")
        value = TimeValue(value.hour, value.minute, value.second, value.microsecond * 1000)
    if isinstance(value, TimeValue):
        return {"t": "time" if compound else "text", "v": str(value)}
    if isinstance(value, Record):
        return {"t": "record", "fields": [{"name": key, "value": encode(child, compound=True)} for key, child in value.fields]}
    if isinstance(value, Collection):
        return {"t": "collection", "items": [encode(child, compound=True) for child in value.items]}
    raise DataError("Unsupported Python bind value")


def decode(value, declared=None):
    kind = value["t"]
    if kind == "null":
        return None
    if kind == "bool":
        return value["v"]
    if kind == "i64":
        return int(value["v"])
    if kind == "decimal":
        raw = value["unscaled"]
        digits = raw[1:] if raw.startswith("-") else raw
        return decimal.Decimal((int(raw.startswith("-")), tuple(map(int, digits)), -value["scale"]))
    if kind in {"text", "date", "time"}:
        if declared == "DATE" or kind == "date":
            return _time.date.fromisoformat(value["v"])
        if declared == "TIME" or kind == "time":
            main, sep, fraction = value["v"].partition(".")
            hour, minute, second = map(int, main.split(":"))
            exact = TimeValue(hour, minute, second, int(fraction.ljust(9, "0")) if sep else 0)
            return exact if exact.nanos % 1000 else exact.to_time()
        return value["v"]
    if kind == "binary":
        return bytes.fromhex(value["hex"])
    if kind == "timestamp":
        return Timestamp(int(value["seconds"]), value["nanos"], value["offset_minutes"], value.get("zone"))
    if kind == "record":
        return Record(tuple((item["name"], decode(item["value"])) for item in value["fields"]))
    if kind == "collection":
        return Collection(tuple(map(decode, value["items"])))
    raise DataError("Unsupported Server result value type")


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


def _name(name):
    if not isinstance(name, str):
        raise DataError("Named parameter keys must be strings")
    if name.startswith(":"):
        name = name[1:]
    if not _NAME.fullmatch(name):
        raise DataError("Named parameters require ordinary SQL identifiers")
    return name.lower()


_TAGS = {"bool": "BOOLEAN", "i64": "BIGINT", "text": "TEXT", "binary": "BLOB",
         "timestamp": "TIMESTAMP WITH TIME ZONE", "record": "RECORD", "collection": "COLLECTION"}


def _coerce_decimal(encoded, descriptor):
    text = encoded["unscaled"]
    sign = "-" if text.startswith("-") else ""
    digits = text.lstrip("-")
    delta = descriptor["scale"] - encoded["scale"]
    if delta >= 0:
        digits += "0" * delta
    elif digits == "0":
        pass
    else:
        loss = -delta
        if loss >= len(digits) or any(digit != "0" for digit in digits[-loss:]):
            raise DataError("DECIMAL cannot be rescaled without losing digits")
        digits = digits[:-loss]
    digits = digits.lstrip("0") or "0"
    if len(digits) > descriptor["precision"]:
        raise DataError("DECIMAL exceeds the declared precision")
    return {"t": "decimal", "unscaled": (sign if digits != "0" else "") + digits,
            "scale": descriptor["scale"]}


def bind(values, declared_sizes=None):
    if values is None:
        return {}, {}
    if not hasattr(values, "items"):
        raise DataError("Use a mapping for named parameters")
    types, encoded = {}, {}
    for original, param in values.items():
        name = _name(original)
        if name in types:
            raise DataError("Duplicate canonical parameter name")
        if param is None and declared_sizes and name in declared_sizes:
            definition = declared_sizes[name]
            param = Bind(None, definition.type_name, definition.precision, definition.scale)
        value = param.value if isinstance(param, Bind) else param
        wire = encode(value)
        if isinstance(param, Bind):
            descriptor = param.descriptor()
            if value is not None:
                expected = descriptor["type"]
                if expected == "DECIMAL" and wire["t"] == "i64":
                    wire = {"t": "decimal", "unscaled": wire["v"], "scale": 0}
                if expected == "DECIMAL" and wire["t"] == "decimal":
                    wire = _coerce_decimal(wire, descriptor)
                elif expected in {"DATE", "TIME"} and wire["t"] == "text":
                    pass
                elif _TAGS.get(wire["t"]) != expected:
                    raise DataError("Bind value does not match the declared SQL type")
        elif value is None:
            raise DataError("Use Bind(None, SQL type) or setinputsizes to declare a NULL parameter")
        elif isinstance(value, (_time.time, TimeValue)):
            descriptor = {"type": "TIME"}
        elif isinstance(value, _time.date) and not isinstance(value, _time.datetime):
            descriptor = {"type": "DATE"}
        elif wire["t"] == "decimal":
            digits = len(wire["unscaled"].lstrip("-"))
            scale = wire["scale"]
            descriptor = {"type": "DECIMAL", "precision": max(digits, scale), "scale": scale}
        else:
            descriptor = {"type": _TAGS[wire["t"]]}
        types[name], encoded[name] = descriptor, wire
    return types, encoded
