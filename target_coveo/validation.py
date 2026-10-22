"""Validation helpers for the Coveo Push API document contract.

The target never decodes JSON strings, never fills in defaults, and never rewrites
permissions. Records that do not satisfy the contract are rejected before any HTTP
call so that access can never be widened by the target.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import re
from typing import NoReturn
from urllib.parse import urlsplit

from hotglue_etl_exceptions import InvalidPayloadError

EXTERNAL_ID_KEY = "externalId"
DOCUMENT_ID_KEY = "documentId"
ACTION_KEY = "_coveoAction"
DELETE_ACTION = "delete"
CONTROL_KEYS = frozenset({EXTERNAL_ID_KEY, DOCUMENT_ID_KEY})

UNCOMPRESSED = "UNCOMPRESSED"
SUPPORTED_COMPRESSION_TYPES = frozenset({UNCOMPRESSED})
IDENTITY_TYPES = frozenset({"User", "Group", "VirtualGroup", "Unknown"})
# Coveo Push IdentityBody uses uppercase entity types.
SECURITY_IDENTITY_TYPES = frozenset({"USER", "GROUP", "VIRTUAL_GROUP", "UNKNOWN"})
_SECURITY_IDENTITY_BODY_KEYS = frozenset({"identity", "members", "mappings", "wellKnowns"})
_SECURITY_IDENTITY_REF_KEYS = frozenset({"name", "type", "provider"})

# Coveo limits `data` and `compressedBinaryData` to 5 MB per single-item push.
MAX_INLINE_BYTES = 5 * 1024 * 1024

# Keys with a special meaning in the Coveo DocumentBody model (or this target).
# Coveo metadata keys are case insensitive, so any non-canonical spelling is rejected.
RESERVED_KEYS = frozenset(
    {
        EXTERNAL_ID_KEY,
        DOCUMENT_ID_KEY,
        ACTION_KEY,
        "data",
        "compressedBinaryData",
        "compressedBinaryDataFileId",
        "compressionType",
        "fileExtension",
        "parentId",
        "permissions",
    }
)
_RESERVED_BY_LOWER = {key.lower(): key for key in RESERVED_KEYS}

_FILE_EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]{1,16}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x20\x7f]")
_PERMISSION_SET_KEYS = frozenset({"allowAnonymous", "allowedPermissions", "deniedPermissions"})
_PERMISSION_LEVEL_KEYS = frozenset({"name", "permissionSets"})
_IDENTITY_KEYS = frozenset({"identity", "identityType", "securityProvider"})
_PRIMITIVES = (str, bool, int, float, dt.date, dt.datetime)


def _fail(message: str) -> NoReturn:
    raise InvalidPayloadError(message)


def _is_non_empty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def validate_external_id(record: dict) -> str:
    """Return the record externalId, requiring a non-empty string."""
    external_id = record.get(EXTERNAL_ID_KEY)
    if not _is_non_empty_str(external_id):
        _fail("externalId is required and must be a non-empty string")
    return external_id


def validate_document_id(record: dict) -> str:
    """Return the record documentId, requiring a stable absolute HTTPS URL."""
    document_id = record.get(DOCUMENT_ID_KEY)
    if not _is_non_empty_str(document_id):
        _fail("documentId is required and must be a non-empty string")
    if _CONTROL_CHARS_RE.search(document_id):
        _fail("documentId must not contain whitespace or control characters")
    parts = urlsplit(document_id)
    if parts.scheme != "https" or not parts.netloc or not document_id.startswith("https://"):
        _fail("documentId must be an absolute https:// URL")
    return document_id


def validate_record_keys(record: dict) -> None:
    """Reject non-canonical spellings of reserved keys and case-insensitive duplicates."""
    seen: dict[str, str] = {}
    for key in record:
        if not _is_non_empty_str(key):
            _fail("record keys must be non-empty strings")
        lowered = key.lower()
        canonical = _RESERVED_BY_LOWER.get(lowered)
        if canonical is not None and key != canonical:
            _fail(f"field '{key}' must be spelled '{canonical}'")
        if lowered in seen:
            _fail(
                f"fields '{seen[lowered]}' and '{key}' collide; Coveo metadata keys are "
                "case insensitive"
            )
        seen[lowered] = key


def _validate_metadata_value(key: str, value: object) -> None:
    if value is None or isinstance(value, _PRIMITIVES):
        return
    if isinstance(value, list) and all(
        item is not None and isinstance(item, _PRIMITIVES) for item in value
    ):
        return
    _fail(
        f"metadata field '{key}' must be a primitive or a list of primitives; "
        "nested objects must be flattened by the ETL"
    )


def _validate_identity(value: object, path: str) -> None:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object")
    unknown = set(value) - _IDENTITY_KEYS
    if unknown:
        _fail(f"{path} has unsupported keys: {sorted(unknown)}")
    if not _is_non_empty_str(value.get("identity")):
        _fail(f"{path}.identity is required and must be a non-empty string")
    if value.get("identityType") not in IDENTITY_TYPES:
        _fail(f"{path}.identityType must be one of {sorted(IDENTITY_TYPES)}")
    if "securityProvider" in value and not _is_non_empty_str(value["securityProvider"]):
        _fail(f"{path}.securityProvider must be a non-empty string when provided")


def _validate_identity_list(value: object, path: str) -> int:
    if not isinstance(value, list):
        _fail(f"{path} must be an array")
    for index, identity in enumerate(value):
        _validate_identity(identity, f"{path}[{index}]")
    return len(value)


def _validate_permission_set(value: object, path: str) -> None:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object")
    unknown = set(value) - _PERMISSION_SET_KEYS
    if unknown:
        _fail(f"{path} has unsupported keys: {sorted(unknown)}")
    allow_anonymous = value.get("allowAnonymous", False)
    if not isinstance(allow_anonymous, bool):
        _fail(f"{path}.allowAnonymous must be a boolean")
    allowed = _validate_identity_list(
        value.get("allowedPermissions", []), f"{path}.allowedPermissions"
    )
    _validate_identity_list(value.get("deniedPermissions", []), f"{path}.deniedPermissions")
    if not allow_anonymous and allowed == 0:
        _fail(
            f"{path} must explicitly set allowAnonymous true or list at least one "
            "allowedPermissions identity"
        )


def validate_permissions(permissions: object) -> None:
    """Validate a Coveo simplified or complete permission model without modifying it."""
    if isinstance(permissions, str):
        _fail("permissions must be an array, not a string; the ETL must provide decoded JSON")
    if not isinstance(permissions, list) or not permissions:
        _fail("permissions is required and must be a non-empty array")
    is_level = [isinstance(item, dict) and "permissionSets" in item for item in permissions]
    if any(is_level) and not all(is_level):
        _fail("permissions must not mix permission levels and permission sets")
    for index, item in enumerate(permissions):
        if is_level[index]:
            _validate_permission_level(item, f"permissions[{index}]")
        else:
            _validate_permission_set(item, f"permissions[{index}]")


def _validate_permission_level(level: dict, path: str) -> None:
    unknown = set(level) - _PERMISSION_LEVEL_KEYS
    if unknown:
        _fail(f"{path} has unsupported keys: {sorted(unknown)}")
    if "name" in level and not _is_non_empty_str(level["name"]):
        _fail(f"{path}.name must be a non-empty string when provided")
    sets = level["permissionSets"]
    if not isinstance(sets, list) or not sets:
        _fail(f"{path}.permissionSets must be a non-empty array")
    for set_index, permission_set in enumerate(sets):
        _validate_permission_set(permission_set, f"{path}.permissionSets[{set_index}]")


def _validate_content(record: dict) -> None:
    if "compressedBinaryDataFileId" in record:
        _fail("compressedBinaryDataFileId (file containers) is not supported")
    has_data = "data" in record
    has_binary = "compressedBinaryData" in record
    if has_data == has_binary:
        _fail("exactly one of data or compressedBinaryData is required")

    if has_data:
        _validate_data(record)
    else:
        _validate_binary(record)


def _validate_data(record: dict) -> None:
    data = record["data"]
    if not _is_non_empty_str(data):
        _fail("data must be a non-empty string")
    if len(data.encode("utf-8")) > MAX_INLINE_BYTES:
        _fail("data exceeds the 5 MB single-item Push API limit")
    if "compressionType" in record:
        _fail("compressionType is only allowed with compressedBinaryData")


def _validate_binary(record: dict) -> None:
    binary = record["compressedBinaryData"]
    if not _is_non_empty_str(binary):
        _fail("compressedBinaryData must be a non-empty base64 string")
    try:
        decoded = base64.b64decode(binary, validate=True)
    except (binascii.Error, ValueError):
        _fail("compressedBinaryData must be valid base64")
    if not decoded:
        _fail("compressedBinaryData must not decode to empty content")
    if len(decoded) > MAX_INLINE_BYTES:
        _fail("compressedBinaryData exceeds the 5 MB single-item Push API limit")
    if record.get("compressionType") not in SUPPORTED_COMPRESSION_TYPES:
        _fail("compressionType must be UNCOMPRESSED when compressedBinaryData is provided")


def validate_upsert_record(record: dict) -> None:
    """Validate a document add/update record (raises InvalidPayloadError)."""
    validate_record_keys(record)
    validate_external_id(record)
    validate_document_id(record)
    _validate_content(record)

    file_extension = record.get("fileExtension")
    if not isinstance(file_extension, str) or not _FILE_EXTENSION_RE.match(file_extension):
        _fail("fileExtension is required and must look like '.html' or '.pdf'")
    if "parentId" in record:
        validate_document_id({DOCUMENT_ID_KEY: record["parentId"]})

    validate_permissions(record.get("permissions"))

    for key, value in record.items():
        if key in RESERVED_KEYS:
            continue
        _validate_metadata_value(key, value)


def validate_delete_record(record: dict) -> None:
    """Validate an explicit single-document delete record."""
    validate_record_keys(record)
    unknown = set(record) - {EXTERNAL_ID_KEY, DOCUMENT_ID_KEY, ACTION_KEY}
    if unknown:
        _fail(f"delete records only accept externalId, documentId and {ACTION_KEY}")
    validate_external_id(record)
    validate_document_id(record)


def is_delete_record(record: dict) -> bool:
    """Return True for an explicit delete action; reject any other action value."""
    if ACTION_KEY not in record:
        return False
    if record[ACTION_KEY] != DELETE_ACTION:
        _fail(f"{ACTION_KEY} only supports the value '{DELETE_ACTION}'")
    return True


def _validate_security_identity_ref(value: object, path: str, *, allow_provider: bool) -> None:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object")
    allowed = _SECURITY_IDENTITY_REF_KEYS if allow_provider else {"name", "type"}
    unknown = set(value) - allowed
    if unknown:
        _fail(f"{path} has unsupported keys: {sorted(unknown)}")
    if not _is_non_empty_str(value.get("name")):
        _fail(f"{path}.name is required and must be a non-empty string")
    identity_type = value.get("type")
    if not isinstance(identity_type, str) or identity_type.upper() not in SECURITY_IDENTITY_TYPES:
        _fail(f"{path}.type must be one of {sorted(SECURITY_IDENTITY_TYPES)}")
    if value["type"] != value["type"].upper():
        _fail(f"{path}.type must be uppercase ({value['type'].upper()})")
    if allow_provider and "provider" in value and not _is_non_empty_str(value["provider"]):
        _fail(f"{path}.provider must be a non-empty string when provided")


def validate_security_identity_record(record: dict) -> None:
    """Validate a Push API security identity upsert without modifying it."""
    validate_external_id(record)
    unknown = set(record) - ({EXTERNAL_ID_KEY} | _SECURITY_IDENTITY_BODY_KEYS)
    if unknown:
        _fail(f"SecurityIdentities record has unsupported keys: {sorted(unknown)}")
    if "identity" not in record:
        _fail("identity is required on SecurityIdentities records")
    _validate_security_identity_ref(record["identity"], "identity", allow_provider=False)
    for key in ("members", "mappings", "wellKnowns"):
        if key not in record:
            continue
        value = record[key]
        if not isinstance(value, list):
            _fail(f"{key} must be an array")
        for index, item in enumerate(value):
            _validate_security_identity_ref(
                item, f"{key}[{index}]", allow_provider=(key == "mappings")
            )


def normalize_push_api_url(url: str | None, default: str) -> str:
    """Validate and normalize the regional Push API base URL."""
    if url is None or url == "":
        return default
    if not isinstance(url, str):
        msg = "push_api_url must be a string"
        raise TypeError(msg)
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(".cloud.coveo.com"):
        msg = "push_api_url must be an https://*.cloud.coveo.com Push API URL"
        raise ValueError(msg)
    if parts.query or parts.fragment or parts.username or parts.password:
        msg = "push_api_url must not contain credentials, a query string or a fragment"
        raise ValueError(msg)
    path = parts.path.rstrip("/") or "/push/v1"
    return f"https://{parts.netloc.lower()}{path}"
