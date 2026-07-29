"""Strict database-neutral canonical serialization for logical exports."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal, DecimalException, InvalidOperation, localcontext

from django.db import models

from .exceptions import (
    LogicalExportRegistryError,
    UnsafeMediaReference,
    UnsupportedLogicalExportField,
)
from .logical_export_registry import JsonPolicy

LOGICAL_RECORD_SCHEMA = "nexa.logical-record.v1"
LOGICAL_MEDIA_REFERENCE_SCHEMA = "nexa.logical-media-reference.v1"
DETERMINISTIC_ORDERING_VERSION = "nexa.logical-order.v1"

_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_DENOMINATION_NAME = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_DATABASE_ID_PREFIXES = (
    "assignment",
    "branch",
    "business",
    "category",
    "customer",
    "employee",
    "location",
    "membership",
    "order",
    "product",
    "purchase",
    "register",
    "role",
    "sale",
    "shift",
    "supplier",
    "user",
    "variant",
    "warehouse",
)
_SEPARATOR_CONFUSABLES = frozenset(
    {
        "\u2044",  # fraction slash
        "\u2215",  # division slash
        "\u2571",  # box drawings light diagonal
        "\u2572",  # box drawings light diagonal
        "\u29f5",  # reverse solidus operator
        "\u29f8",  # big solidus
        "\u29f9",  # big reverse solidus
        "\ufe68",  # small reverse solidus
        "\uff0f",  # fullwidth solidus
        "\uff3c",  # fullwidth reverse solidus
    }
)
_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
        *(f"COM{number}" for number in "\u00b9\u00b2\u00b3"),
        *(f"LPT{number}" for number in "\u00b9\u00b2\u00b3"),
    }
)
_JSON_STRING_CHUNK_CHARACTERS = 4096
_MAXIMUM_JSON_INPUT_BYTES = 65_536
_MAXIMUM_JSON_NODES = 4096
_MAXIMUM_JSON_CONTAINER_MEMBERS = 1024
_MAXIMUM_JSON_STRING_BYTES = 16_384
_MAXIMUM_JSON_TOTAL_STRING_BYTES = 65_536


def _strict_unicode_string(value):
    if not isinstance(value, str):
        raise UnsupportedLogicalExportField()
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise UnsupportedLogicalExportField() from None
    return value


def canonical_uuid(value) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if type(value) is not str:
        raise UnsupportedLogicalExportField()
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise UnsupportedLogicalExportField() from None


def canonical_datetime(value) -> str:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            normalized = value.strip()
            if normalized.endswith("Z"):
                normalized = f"{normalized[:-1]}+00:00"
            parsed = datetime.fromisoformat(normalized)
        else:
            raise TypeError
        # Django's SQLite backend persists aware values as naive UTC text.
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (OverflowError, TypeError, ValueError):
        raise UnsupportedLogicalExportField() from None


def canonical_date(value) -> str:
    try:
        if isinstance(value, datetime):
            raise TypeError
        if isinstance(value, date):
            parsed = value
        elif isinstance(value, str):
            parsed = date.fromisoformat(value)
        else:
            raise TypeError
        if isinstance(parsed, datetime):
            raise TypeError
        return parsed.isoformat()
    except (TypeError, ValueError):
        raise UnsupportedLogicalExportField() from None


def canonical_time(value) -> str:
    try:
        if isinstance(value, time):
            parsed = value
        elif isinstance(value, str):
            parsed = time.fromisoformat(value)
        else:
            raise TypeError
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            raise ValueError
        return parsed.isoformat(timespec="microseconds")
    except (TypeError, ValueError):
        raise UnsupportedLogicalExportField() from None


def canonical_decimal(value, *, decimal_places: int) -> str:
    if isinstance(value, (float, bool)) or value is None or type(value) not in (Decimal, int, str):
        raise UnsupportedLogicalExportField()
    try:
        parsed = Decimal(value)
        if not parsed.is_finite():
            raise InvalidOperation
        quantum = Decimal(1).scaleb(-int(decimal_places))
        sign, digits, exponent = parsed.as_tuple()
        del sign
        required_precision = len(digits) + max(0, int(exponent)) + max(0, int(decimal_places)) + 2
        with localcontext() as context:
            context.prec = max(28, required_precision)
            normalized = parsed.quantize(quantum)
        if normalized != parsed:
            raise InvalidOperation
        if normalized.is_zero():
            normalized = normalized.copy_abs()
        rendered = format(normalized, f".{int(decimal_places)}f")
        if len(rendered.lstrip("-").replace(".", "")) > 64:
            raise InvalidOperation
        return rendered
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise UnsupportedLogicalExportField() from None


def _consume_json_budget(budget, *, string_value=None):
    budget["nodes"] += 1
    if budget["nodes"] > _MAXIMUM_JSON_NODES:
        raise UnsupportedLogicalExportField()
    if string_value is not None:
        encoded_length = len(_strict_unicode_string(string_value).encode("utf-8"))
        if encoded_length > _MAXIMUM_JSON_STRING_BYTES:
            raise UnsupportedLogicalExportField()
        budget["string_bytes"] += encoded_length
        if budget["string_bytes"] > _MAXIMUM_JSON_TOTAL_STRING_BYTES:
            raise UnsupportedLogicalExportField()


def _canonical_json_value(value, *, depth, maximum_depth, budget):
    if depth > maximum_depth:
        raise UnsupportedLogicalExportField()
    _consume_json_budget(
        budget,
        string_value=value if isinstance(value, str) else None,
    )
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _strict_unicode_string(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise UnsupportedLogicalExportField()
        return value
    if isinstance(value, float):
        # Binary floating-point cannot be serialized as an exact logical value.
        raise UnsupportedLogicalExportField()
    if isinstance(value, list):
        if len(value) > _MAXIMUM_JSON_CONTAINER_MEMBERS:
            raise UnsupportedLogicalExportField()
        return [
            _canonical_json_value(
                item,
                depth=depth + 1,
                maximum_depth=maximum_depth,
                budget=budget,
            )
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > _MAXIMUM_JSON_CONTAINER_MEMBERS:
            raise UnsupportedLogicalExportField()
        for key in value:
            _consume_json_budget(budget, string_value=key)
            if _is_database_id_json_key(key):
                raise UnsupportedLogicalExportField()
        return {
            key: _canonical_json_value(
                value[key],
                depth=depth + 1,
                maximum_depth=maximum_depth,
                budget=budget,
            )
            for key in sorted(value)
        }
    raise UnsupportedLogicalExportField()


def _reject_json_constant(_value):
    raise ValueError


def _is_database_id_json_key(value):
    normalized = unicodedata.normalize("NFKC", _strict_unicode_string(value))
    camel_separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    tokens = tuple(token.lower() for token in re.split(r"[^A-Za-z0-9]+", camel_separated) if token)
    compact = "".join(tokens)
    if not tokens:
        return False
    if any(token in {"id", "ids", "pk", "pks"} for token in tokens):
        return True
    if compact in {"primarykey", "primarykeys"}:
        return True
    return any(
        compact.startswith(f"{prefix}id") or compact.startswith(f"{prefix}pk")
        for prefix in _DATABASE_ID_PREFIXES
    )


def _strict_json_object(pairs):
    parsed = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError
        parsed[key] = value
    return parsed


def canonical_json(
    value,
    *,
    maximum_depth,
    policy=JsonPolicy.CANONICAL,
    allowed_values=(),
):
    if isinstance(value, str):
        try:
            if len(value.encode("utf-8", errors="strict")) > _MAXIMUM_JSON_INPUT_BYTES:
                raise UnsupportedLogicalExportField()
        except UnicodeError:
            raise UnsupportedLogicalExportField() from None
    try:
        parsed = (
            json.loads(
                value,
                parse_float=Decimal,
                parse_int=int,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_strict_json_object,
            )
            if isinstance(value, str)
            else value
        )
    except (
        DecimalException,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise UnsupportedLogicalExportField() from None
    canonical = _canonical_json_value(
        parsed,
        depth=0,
        maximum_depth=maximum_depth,
        budget={"nodes": 0, "string_bytes": 0},
    )
    selected_policy = JsonPolicy(policy)
    if selected_policy == JsonPolicy.SORTED_STRING_SET:
        if not isinstance(canonical, list):
            raise UnsupportedLogicalExportField()
        if any(not isinstance(item, str) or not item for item in canonical):
            raise UnsupportedLogicalExportField()
        if len(canonical) != len(set(canonical)):
            raise UnsupportedLogicalExportField()
        allowed = frozenset(allowed_values)
        if allowed and not set(canonical).issubset(allowed):
            raise UnsupportedLogicalExportField()
        return sorted(canonical)
    if selected_policy in {
        JsonPolicy.FLAT_STRING_MAP,
        JsonPolicy.INDEXED_STRING_MAP,
    }:
        if not isinstance(canonical, dict):
            raise UnsupportedLogicalExportField()
        if any(not key or not isinstance(value, str) for key, value in canonical.items()):
            raise UnsupportedLogicalExportField()
        allowed = frozenset(allowed_values)
        if selected_policy == JsonPolicy.INDEXED_STRING_MAP and not allowed:
            raise UnsupportedLogicalExportField()
        if allowed and not set(canonical).issubset(allowed):
            raise UnsupportedLogicalExportField()
        return canonical
    if selected_policy == JsonPolicy.DENOMINATION_MAP:
        if canonical is None:
            return None
        if not isinstance(canonical, dict):
            raise UnsupportedLogicalExportField()
        seen_denominations = set()
        for key, item in canonical.items():
            if not _DENOMINATION_NAME.fullmatch(key):
                raise UnsupportedLogicalExportField()
            try:
                denomination = Decimal(key)
            except (DecimalException, ValueError):
                raise UnsupportedLogicalExportField() from None
            if not denomination.is_finite() or denomination <= 0:
                raise UnsupportedLogicalExportField()
            normalized_denomination = denomination.normalize()
            if normalized_denomination in seen_denominations:
                raise UnsupportedLogicalExportField()
            seen_denominations.add(normalized_denomination)
            if isinstance(item, bool) or not isinstance(item, (int, Decimal)):
                raise UnsupportedLogicalExportField()
            if isinstance(item, Decimal) and not item.is_finite():
                raise UnsupportedLogicalExportField()
            if item < 0:
                raise UnsupportedLogicalExportField()
        return canonical
    return canonical


def _canonical_json_decimal_token(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise UnsupportedLogicalExportField()
    sign, digits, exponent = value.as_tuple()
    rendered_digits = "".join(str(digit) for digit in digits)
    rendered_digits = rendered_digits.lstrip("0")
    if not rendered_digits:
        return "0"
    while rendered_digits.endswith("0"):
        rendered_digits = rendered_digits[:-1]
        exponent += 1
    adjusted = exponent + len(rendered_digits) - 1
    if -6 <= adjusted < 21:
        if exponent >= 0:
            token = f"{rendered_digits}{'0' * exponent}"
        else:
            split = len(rendered_digits) + exponent
            if split > 0:
                token = f"{rendered_digits[:split]}.{rendered_digits[split:]}"
            else:
                token = f"0.{'0' * -split}{rendered_digits}"
    else:
        fraction = rendered_digits[1:]
        coefficient = f"{rendered_digits[0]}.{fraction}" if fraction else rendered_digits[0]
        token = f"{coefficient}e{adjusted}"
    return f"-{token}" if sign else token


def _iter_json_string_bytes(value):
    _strict_unicode_string(value)
    yield b'"'
    for offset in range(0, len(value), _JSON_STRING_CHUNK_CHARACTERS):
        fragment = value[offset : offset + _JSON_STRING_CHUNK_CHARACTERS]
        rendered = json.dumps(
            fragment,
            ensure_ascii=False,
            allow_nan=False,
        )[1:-1]
        yield rendered.encode("utf-8", errors="strict")
    yield b'"'


def _iter_canonical_json_bytes(value):
    if value is None:
        yield b"null"
        return
    if value is True:
        yield b"true"
        return
    if value is False:
        yield b"false"
        return
    if isinstance(value, int) and not isinstance(value, bool):
        yield str(value).encode("ascii")
        return
    if isinstance(value, Decimal):
        yield _canonical_json_decimal_token(value).encode("ascii")
        return
    if isinstance(value, str):
        yield from _iter_json_string_bytes(value)
        return
    if isinstance(value, (list, tuple)):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _iter_canonical_json_bytes(item)
        yield b"]"
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise UnsupportedLogicalExportField()
        yield b"{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield b","
            yield from _iter_json_string_bytes(key)
            yield b":"
            yield from _iter_canonical_json_bytes(value[key])
        yield b"}"
        return
    raise UnsupportedLogicalExportField()


def validate_media_storage_name(value, *, maximum_length) -> str:
    if not isinstance(value, str):
        raise UnsafeMediaReference()
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise UnsafeMediaReference() from None
    if (
        not value
        or not value.strip()
        or len(value) > maximum_length
        or len(encoded) > maximum_length
        or "\x00" in value
        or "\\" in value
        or "%" in value
        or value.startswith(("/", "//"))
        or _WINDOWS_DRIVE.match(value)
        or _URL_SCHEME.match(value)
        or any(
            character != "/"
            and (
                character in _SEPARATOR_CONFUSABLES
                or unicodedata.normalize("NFKC", character) in {"/", "\\"}
            )
            for character in value
        )
        or any(character in '<>:"|?*#' for character in value)
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in value
        )
    ):
        raise UnsafeMediaReference()
    segments = value.split("/")
    for segment in segments:
        if (
            segment in {"", ".", ".."}
            or segment != segment.strip()
            or segment != segment.rstrip(" .")
            or not segment.rstrip(" .")
        ):
            raise UnsafeMediaReference()
        device_stem = segment.rstrip(" .").split(".", 1)[0].rstrip(" ")
        if device_stem.upper() in _WINDOWS_DEVICE_NAMES:
            raise UnsafeMediaReference()
    return value


def iter_canonical_document(value, *, trailing_lf=False):
    """Yield canonical JSON document bytes without generic coercion."""

    if type(trailing_lf) is not bool:
        raise UnsupportedLogicalExportField()
    try:
        yield from _iter_canonical_json_bytes(value)
        if trailing_lf:
            yield b"\n"
    except UnsupportedLogicalExportField:
        raise
    except (RecursionError, TypeError, ValueError, UnicodeError):
        raise UnsupportedLogicalExportField() from None


def encode_canonical_document(value, *, trailing_lf=False) -> bytes:
    """Return exact canonical JSON bytes for hashes and serialized documents."""

    return b"".join(
        iter_canonical_document(
            value,
            trailing_lf=trailing_lf,
        )
    )


class CanonicalLogicalSerializer:
    """Serialize reviewed Django field values without generic fallbacks."""

    def __init__(self, *, maximum_json_depth, maximum_media_name_length):
        self.maximum_json_depth = int(maximum_json_depth)
        self.maximum_media_name_length = int(maximum_media_name_length)
        if self.maximum_json_depth <= 0 or self.maximum_media_name_length <= 0:
            raise LogicalExportRegistryError()

    def scalar(self, field, value):
        if value is None:
            if getattr(field, "null", False):
                return None
            raise UnsupportedLogicalExportField()
        if isinstance(field, models.UUIDField):
            return canonical_uuid(value)
        if isinstance(field, models.DecimalField):
            return canonical_decimal(
                value,
                decimal_places=field.decimal_places,
            )
        if isinstance(field, models.DateTimeField):
            return canonical_datetime(value)
        if isinstance(field, models.DateField):
            return canonical_date(value)
        if isinstance(field, models.TimeField):
            return canonical_time(value)
        if isinstance(field, models.BooleanField):
            if type(value) is bool:
                return value
            if type(value) is int and value in (0, 1):
                return bool(value)
            raise UnsupportedLogicalExportField()
        if isinstance(
            field,
            (
                models.AutoField,
                models.BigAutoField,
                models.SmallAutoField,
                models.IntegerField,
            ),
        ):
            if type(value) is not int:
                raise UnsupportedLogicalExportField()
            return value
        if isinstance(
            field,
            (
                models.CharField,
                models.TextField,
                models.EmailField,
                models.GenericIPAddressField,
                models.SlugField,
            ),
        ):
            return _strict_unicode_string(value)
        raise UnsupportedLogicalExportField()

    def json(self, json_spec, value):
        return canonical_json(
            value,
            maximum_depth=self.maximum_json_depth,
            policy=json_spec.policy,
            allowed_values=json_spec.allowed_values,
        )

    def media_name(self, value):
        return validate_media_storage_name(
            value,
            maximum_length=self.maximum_media_name_length,
        )

    @staticmethod
    def iter_encoded_line(payload):
        try:
            yield from _iter_canonical_json_bytes(payload)
            yield b"\n"
        except UnsupportedLogicalExportField:
            raise
        except (RecursionError, TypeError, ValueError, UnicodeError):
            raise UnsupportedLogicalExportField() from None

    @classmethod
    def encode_line(cls, payload) -> bytes:
        return b"".join(cls.iter_encoded_line(payload))


__all__ = [
    "CanonicalLogicalSerializer",
    "DETERMINISTIC_ORDERING_VERSION",
    "LOGICAL_MEDIA_REFERENCE_SCHEMA",
    "LOGICAL_RECORD_SCHEMA",
    "canonical_date",
    "canonical_datetime",
    "canonical_decimal",
    "canonical_json",
    "canonical_time",
    "canonical_uuid",
    "encode_canonical_document",
    "iter_canonical_document",
    "validate_media_storage_name",
]
