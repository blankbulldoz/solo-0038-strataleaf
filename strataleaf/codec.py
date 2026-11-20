"""Logical JSON value model, canonical text rendering and the on-disk payload codec.

Two codecs share one logical model:

``JSON`` (format 1, legacy)
    UTF-8 canonical text: ``json.dumps`` with sorted keys and no whitespace.

``PACK`` (format 2, current)
    MessagePack 1.2.4 binary produced by a deterministic subset of the
    MessagePack 2.0 data model.  Maps are written with sorted keys, UTF-8
    strings are written as ``str`` (never ``bin``), floats are always IEEE-754
    float64 and integers outside the signed 64-bit range use a private ext
    type so Python ``int`` survives a round trip without loss.

Payloads are stored as SQLite BLOBs that begin with a one-byte magic
(``\\x00``) followed by the format revision.  Legacy JSON payloads are stored
as TEXT and therefore can never be confused with a versioned binary payload.
"""
import hashlib
import json
import math
import re

try:  # The runtime dependency is optional; legacy databases stay readable without it.
    import msgpack as _msgpack
except ImportError:  # pragma: no cover - exercised only on installations without the wheel
    _msgpack = None

from .errors import DatabaseError, InvalidDocument

NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MISSING = object()

JSON_FORMAT = 1
PACK_FORMAT = 2
CURRENT_FORMAT = PACK_FORMAT
FORMAT_NAMES = {JSON_FORMAT: "json-text", PACK_FORMAT: "msgpack"}

#: One byte that can never introduce a UTF-8 text payload, followed by the format number.
BLOB_MAGIC = b"\x00"
BLOB_HEADER = BLOB_MAGIC + bytes([PACK_FORMAT])
#: Private MessagePack ext type carrying integers outside the signed/unsigned 64-bit range.
BIGINT_EXT = 45


class FormatError(DatabaseError):
    """A stored payload cannot be interpreted with the format it claims."""


def msgpack_available():
    return _msgpack is not None


def _require_msgpack():
    if _msgpack is None:
        raise FormatError("MessagePack support requires the msgpack 1.2.4 runtime dependency")
    return _msgpack


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise InvalidDocument("names must match [a-z][a-z0-9_]{0,62}")
    return value


def key(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 512:
        raise InvalidDocument("document key must be a nonempty string of at most 512 bytes")
    return value


def validate_json(value, depth=0):
    if depth > 32:
        raise InvalidDocument("document nesting exceeds 32 levels")
    if value is None or isinstance(value, str):
        return
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            validate_json(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values():
            validate_json(item, depth + 1)
        return
    raise InvalidDocument("only finite JSON values and string object keys are supported")


# --------------------------------------------------------------------------
# Logical document model
# --------------------------------------------------------------------------

def copy_value(value):
    """A type-exact deep copy of a validated JSON value.

    ``json.loads(json.dumps(...))`` is not type preserving: such a round trip
    turns every non-string key into text and cannot represent large integers.
    """
    if isinstance(value, dict):
        return {key: copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_value(item) for item in value]
    return value


def render(value):
    """Canonical JSON text of a logical value (used for documents and receipts)."""
    validate_json(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    """Stable logical digest, independent of the payload codec.

    The digest is computed over canonical JSON text rather than over stored
    bytes, so request fingerprints, query identities and content fingerprints
    do not change when a database changes payload format.
    """
    return hashlib.sha256(render(value).encode()).hexdigest()


def loads(value):
    """Parse JSON text or a binary payload from any supported format revision."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return decode(value)
    return json.loads(value)


def field(document, path):
    if not isinstance(path, str) or not path or any(not p for p in path.split(".")):
        raise InvalidDocument("field path must have nonempty dot-separated components")
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def scalar_token(value):
    if value is MISSING or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "b:" + str(int(value))
    if isinstance(value, int):
        return "n:" + str(value)
    if isinstance(value, float):
        return "n:" + (str(int(value)) if value.is_integer() else repr(value))
    return "v:" + render(value)


# --------------------------------------------------------------------------
# Deterministic MessagePack subset
# --------------------------------------------------------------------------

def _bigint_default(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return _msgpack.ExtType(BIGINT_EXT, str(value).encode("ascii"))
    raise TypeError("unsupported MessagePack value type: " + type(value).__name__)


def _bigint_hook(code, data):
    if code != BIGINT_EXT:
        raise FormatError(f"unsupported MessagePack extension type {code}")
    try:
        text = data.decode("ascii")
        if not text or text.strip() != text or text[0] not in "-0123456789" or not text.lstrip("-").isdigit():
            raise ValueError("not a decimal integer")
        return int(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise FormatError("malformed big integer extension payload") from exc


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def encode(value):
    """Encode a validated logical value to the current binary payload format."""
    validate_json(value)
    packer = _require_msgpack()
    try:
        return BLOB_HEADER + packer.packb(_canonical(value), use_bin_type=True, use_single_float=False,
                                          strict_types=True, default=_bigint_default)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidDocument("value cannot be encoded as MessagePack: " + str(exc)) from exc


def decode(payload):
    """Decode a payload from any supported format revision into logical values."""
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
            validate_json(value)
            return value
        except (ValueError, TypeError, InvalidDocument) as exc:
            raise FormatError("corrupt or invalid JSON payload") from exc
    if not isinstance(payload, bytes):
        raise FormatError("payload is neither text nor bytes")
    if payload.startswith(BLOB_HEADER):
        return _decode_pack(payload[len(BLOB_HEADER):])
    if payload.startswith(BLOB_MAGIC):
        raise FormatError(f"unsupported binary payload format revision {payload[1:2].hex() or 'missing'}")
    raise FormatError("unrecognised binary payload")


def _decode_pack(body):
    packer = _require_msgpack()
    try:
        value = packer.unpackb(body, raw=False, strict_map_key=True, use_list=True,
                               ext_hook=_bigint_hook)
        validate_json(value)
        return value
    except (ValueError, TypeError, OverflowError, UnicodeDecodeError, InvalidDocument) as exc:
        raise FormatError("corrupt or invalid MessagePack payload") from exc


# --------------------------------------------------------------------------
# Stored-payload classification used by the migrator
# --------------------------------------------------------------------------

TEXT = "json-text"
BLOB = "msgpack"
UNKNOWN = "unknown"


def payload_kind(payload):
    """Classify one stored payload by inspecting its representation.

    Returns ``"json-text"`` for legacy canonical JSON text, ``"msgpack"`` for
    a payload carrying the format-2 header, and ``"unknown"`` for anything
    else.  The SQL ``typeof`` distinction is included so a migration can tell
    a legitimate JSON string starting with U+0002 from binary confusion.
    """
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    if isinstance(payload, str):
        return TEXT
    if isinstance(payload, bytes):
        if payload.startswith(BLOB_HEADER):
            return BLOB
        return UNKNOWN
    return UNKNOWN


def same_value(left, right):
    """Type-exact structural equality (bool never equals int, 1 never equals 1.0)."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys()
                and all(same_value(left[k], right[k]) for k in left))
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list) and len(left) == len(right)
                and all(same_value(a, b) for a, b in zip(left, right)))
    if isinstance(left, float) or isinstance(right, float):
        if not (isinstance(left, float) and isinstance(right, float)):
            return False
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    return type(left) is type(right) and left == right
