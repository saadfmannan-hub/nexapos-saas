"""Independent fail-closed package verification for Backup Engine Phase 2E.

The verifier accepts only immutable execution/build evidence and an explicitly
marked opaque package-access provider. Package and verification-evidence paths
are never accepted from, or returned to, callers.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import struct
import sys
import tempfile
import threading
import unicodedata
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from django.utils import timezone

from apps.backups.enums import BackupScope, BackupTrigger, ProductOwner, RestoreBehavior

from .canonical_manifest import (
    CANONICAL_JSON_VERSION,
    CAPTURE_STATE,
    COMPONENT_CONTENT_SCHEMA,
    HASH_ALGORITHM,
    MANIFEST_FILE_NAME,
    MANIFEST_SCHEMA_IDENTIFIER,
    MANIFEST_VERSION,
    MEDIA_CAPTURE_POLICY_IDENTIFIER,
    MISSING_MEDIA_POLICY,
    PACKAGE_FORMAT_IDENTIFIER,
    PAYLOAD_SET_SCHEMA,
    RESTORE_VERIFICATION_STATE,
)
from .context import BackupExecutionContext
from .contracts import (
    PackageBuildResult,
    PackageCompatibilityStatus,
    PackageReference,
    PackageVerificationRequest,
    PackageVerificationResult,
    RestoreReadinessResult,
    VerificationIssue,
    VerificationReference,
)
from .deterministic_package import (
    DETERMINISTIC_ZIP_TIMESTAMP,
    PACKAGE_ACCESS_PROVIDER_SCHEMA,
    DeterministicPackageProvider,
)
from .logical_export import LOGICAL_EXPORT_PROVIDER_IDENTIFIER
from .logical_export_registry import IdentityKind, get_logical_export_registry
from .logical_serialization import (
    DETERMINISTIC_ORDERING_VERSION,
    LOGICAL_MEDIA_REFERENCE_SCHEMA,
    LOGICAL_RECORD_SCHEMA,
    canonical_uuid,
    encode_canonical_document,
    validate_media_storage_name,
)
from .media_capture import media_storage_collision_key
from .package_exceptions import PackageNotFound, PackageValidationError
from .pipeline import ComponentPlanItem, resolve_component_plan
from .restore_workspace import RestoredPackageProvider
from .verification_exceptions import (
    Phase2EEngineError,
    VerificationCleanupError,
    VerificationEvidenceNotFound,
    VerificationProviderStateError,
    VerificationPublicationError,
)
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    contained_path,
    path_is_link_like,
)

INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER = "independent-package-verifier-v1"
VERIFICATION_SCHEMA_IDENTIFIER = "nexa.package-verification-evidence.v1"
VERIFICATION_VERSION = "1.0.0"
VERIFICATION_FILE_NAME = "verification.json"

_PACKAGE_CHUNK_BYTES = 1024**2
_EVIDENCE_CHUNK_BYTES = 64 * 1024
_SPOOL_MEMORY_BYTES = 8 * 1024**2
_MAXIMUM_PACKAGE_BYTES = 10 * 1024**4
_MAXIMUM_PACKAGE_ENTRIES = 200_000
_MAXIMUM_MANIFEST_BYTES = 64 * 1024**2
_MAXIMUM_RECORD_LINE_BYTES = 16 * 1024**2
_MAXIMUM_MEDIA_INDEX_LINE_BYTES = 1024**2
_MAXIMUM_MEDIA_NAME_LENGTH = 4096
_MAXIMUM_SIGNED_COUNT = 2**63 - 1
_MAXIMUM_ISSUES = 16
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_COMPONENT_RECORDS_PATTERN = re.compile(r"^components/([0-9]{4})/records\.ndjson$")
_COMPONENT_MEDIA_PATTERN = re.compile(r"^components/([0-9]{4})/media-index\.ndjson$")
_MEDIA_PATTERN = re.compile(r"^media/([0-9]{8})\.bin$")
_DOS_TIME = 0
_DOS_DATE = 33
_ZIP_VERSION = 45
_ZIP_UNIX_CREATOR = (3 << 8) | _ZIP_VERSION
_ZIP_EXTERNAL_ATTRIBUTES = (stat.S_IFREG | 0o600) << 16
_LOCAL_HEADER = struct.Struct("<4s5H3L2H")
_CENTRAL_HEADER = struct.Struct("<4s6H3L5H2L")
_END_HEADER = struct.Struct("<4s4H2LH")
_ZIP64_END_HEADER = struct.Struct("<4sQ2H2L4Q")
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP64_EXTRA_HEADER = struct.Struct("<HH")
_ZIP64_LOCAL_VALUES = struct.Struct("<QQ")

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "manifest_version",
        "canonical_json_version",
        "hash_algorithm",
        "package_format",
        "backup",
        "compatibility",
        "source_consistency",
        "components",
        "media",
        "totals",
        "payload_set_schema",
        "payload_set_sha256",
        "missing_media_policy",
        "missing_media_count",
        "restore_verification_state",
    }
)
_BACKUP_KEYS = frozenset(
    {
        "backup_public_id",
        "tenant_public_id",
        "scope",
        "trigger_type",
        "included_products",
        "included_component_keys",
        "application_version",
        "backup_format_version",
        "schema_migration_fingerprint",
        "minimum_restore_version",
        "created_timestamp",
    }
)
_COMPONENT_KEYS = frozenset(
    {
        "ordinal",
        "key",
        "product_owner",
        "component_version",
        "restore_behavior",
        "required_component_keys",
        "export_order",
        "import_order",
        "record_schema",
        "media_reference_schema",
        "deterministic_ordering_version",
        "models",
        "records",
        "media_index",
        "component_content_schema",
        "component_content_sha256",
        "restore_verification_state",
    }
)
_MEDIA_KEYS = frozenset(
    {
        "ordinal",
        "storage_name",
        "package_path",
        "byte_count",
        "sha256",
        "source_reference_count",
        "sources",
        "capture_state",
        "restore_verification_state",
    }
)
_MEDIA_INDEX_KEYS = frozenset(
    {
        "schema",
        "component",
        "model",
        "tenant_public_id",
        "identity",
        "field",
        "storage_name",
    }
)
_RECORD_KEYS = frozenset(
    {
        "schema",
        "component",
        "component_version",
        "model",
        "tenant_public_id",
        "identity",
        "fields",
    }
)
_TOTAL_KEYS = frozenset(
    {
        "component_count",
        "model_count",
        "record_count",
        "media_reference_count",
        "unique_media_object_count",
        "component_records_bytes",
        "component_media_index_bytes",
        "media_bytes",
        "planned_payload_bytes",
    }
)

_ISSUE_MESSAGES = {
    "package_evidence_rejected": "Package evidence is not owned by this verification boundary.",
    "package_integrity_failed": "Whole-package integrity verification failed.",
    "zip_structure_invalid": "The package archive structure is not valid.",
    "manifest_invalid": "The canonical package manifest is not valid.",
    "payload_invalid": "One or more package payloads failed independent verification.",
    "compatibility_incompatible": "The package is valid but incompatible with the restore policy.",
    "compatibility_not_proven": "The package is valid but restore compatibility is not proven.",
}


class _OrdinaryVerificationFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _CentralEntry:
    name: str
    crc32: int
    compressed_size: int
    uncompressed_size: int
    local_offset: int
    central_extra: bytes
    data_start: int = 0
    data_end: int = 0


@dataclass(frozen=True, slots=True)
class _SourceEvidence:
    component: str
    model: str
    identity_bytes: bytes
    field: str
    storage_name: str

    @property
    def logical_key(self):
        return (
            self.component,
            self.model,
            self.identity_bytes,
            self.field,
        )

    @property
    def sort_key(self):
        return (
            self.component,
            self.model,
            self.identity_bytes,
            self.field,
        )


@dataclass(frozen=True, slots=True)
class _ManifestAssessment:
    document: dict
    plan: tuple[ComponentPlanItem, ...]
    expected_names: tuple[str, ...]
    manifest_sha256: str
    payload_set_sha256: str
    compatibility_status: PackageCompatibilityStatus
    compatibility_issues: tuple[VerificationIssue, ...]


@dataclass(frozen=True, slots=True)
class _PublishedVerification:
    context: BackupExecutionContext
    package: PackageBuildResult
    result: PackageVerificationResult
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int] | None


class _OpaqueVerificationReader:
    __slots__ = ("__file",)

    def __init__(self, file_object):
        self.__file = file_object

    def read(self, size=-1):
        return self.__file.read(size)

    def readline(self, size=-1):
        return self.__file.readline(size)

    def close(self):
        return self.__file.close()

    @property
    def closed(self):
        return self.__file.closed


def _failure(code):
    if code not in _ISSUE_MESSAGES:
        raise VerificationProviderStateError()
    raise _OrdinaryVerificationFailure(code)


def _issue(code):
    try:
        message = _ISSUE_MESSAGES[code]
    except KeyError:
        raise VerificationProviderStateError() from None
    if len(code) > 64 or len(message) > 240:
        raise VerificationProviderStateError()
    return VerificationIssue(code=code, sanitized_message=message)


def _identity(current):
    return current.st_dev, current.st_ino


def _is_aware(value):
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _utc_timestamp(value):
    if not _is_aware(value):
        _failure("manifest_invalid")
    try:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (OverflowError, TypeError, ValueError):
        _failure("manifest_invalid")


def _parse_aware_timestamp(value):
    if type(value) is not str or not value.endswith("Z"):
        _failure("manifest_invalid")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except (OverflowError, TypeError, ValueError):
        _failure("manifest_invalid")
    if not _is_aware(parsed) or parsed.utcoffset() != UTC.utcoffset(parsed):
        _failure("manifest_invalid")
    if _utc_timestamp(parsed) != value:
        _failure("manifest_invalid")
    return parsed


def _exact_keys(value, keys, *, code="manifest_invalid"):
    if type(value) is not dict or frozenset(value) != frozenset(keys):
        _failure(code)
    return value


def _exact_list(value, *, code="manifest_invalid"):
    if type(value) is not list:
        _failure(code)
    return value


def _count(value, *, maximum=_MAXIMUM_SIGNED_COUNT, positive=False, code="manifest_invalid"):
    if type(value) is not int or value < (1 if positive else 0) or value > maximum:
        _failure(code)
    return value


def _sha256(value, *, code="manifest_invalid"):
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        _failure(code)
    return value


def _version_token(value, *, code="manifest_invalid"):
    if type(value) is not str or _VERSION_TOKEN_PATTERN.fullmatch(value) is None:
        _failure(code)
    return value


def _duplicate_rejecting_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_number(_value):
    raise ValueError


def _strict_json(raw, *, trailing_lf, code):
    try:
        if (
            type(raw) is not bytes
            or raw.startswith(b"\xef\xbb\xbf")
            or (trailing_lf and (not raw.endswith(b"\n") or raw.endswith(b"\r\n")))
        ):
            raise ValueError
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
        if encode_canonical_document(value, trailing_lf=trailing_lf) != raw:
            raise ValueError
    except (
        UnicodeError,
        json.JSONDecodeError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        _failure(code)
    return value


def _validated_context(context):
    if (
        type(context) is not BackupExecutionContext
        or type(context.backup_public_id) is not uuid.UUID
        or type(context.business_public_id) is not uuid.UUID
        or type(context.business_id) is not int
        or context.business_id <= 0
        or type(context.workspace_reference) is not WorkspaceReference
        or type(context.workspace_reference.identifier) is not uuid.UUID
        or type(context.operation_correlation_id) is not uuid.UUID
        or type(context.requested_scope) is not BackupScope
        or type(context.trigger_type) is not BackupTrigger
        or type(context.resolved_products) is not tuple
        or not context.resolved_products
        or any(type(item) is not ProductOwner for item in context.resolved_products)
        or len(context.resolved_products) != len(set(context.resolved_products))
    ):
        _failure("package_evidence_rejected")
    for value in (
        context.application_version,
        context.backup_format_version,
        context.minimum_restore_version,
    ):
        _version_token(value, code="package_evidence_rejected")
    _sha256(context.schema_migration_fingerprint, code="package_evidence_rejected")
    return context


def _validated_package_result(result, *, provider_identifier):
    if (
        type(result) is not PackageBuildResult
        or type(result.reference) is not PackageReference
        or type(result.reference.identifier) is not uuid.UUID
        or result.format_identifier != PACKAGE_FORMAT_IDENTIFIER
        or result.provider_identifier != provider_identifier
        or not _is_aware(result.created_at)
    ):
        _failure("package_evidence_rejected")
    _count(
        result.byte_count,
        maximum=_MAXIMUM_PACKAGE_BYTES,
        positive=True,
        code="package_evidence_rejected",
    )
    _count(
        result.entry_count,
        maximum=_MAXIMUM_PACKAGE_ENTRIES,
        positive=True,
        code="package_evidence_rejected",
    )
    _sha256(result.plaintext_sha256, code="package_evidence_rejected")
    _sha256(result.payload_set_sha256, code="package_evidence_rejected")
    return result


def _read_at(file_object, offset, size, *, code="zip_structure_invalid"):
    if (
        type(offset) is not int
        or type(size) is not int
        or offset < 0
        or size < 0
        or size > _MAXIMUM_MANIFEST_BYTES
    ):
        _failure(code)
    try:
        file_object.seek(offset, io.SEEK_SET)
        value = file_object.read(size)
    except (OSError, OverflowError, ValueError):
        _failure(code)
    if type(value) is not bytes or len(value) != size:
        _failure(code)
    return value


def _portable_path_key(value):
    try:
        return unicodedata.normalize("NFKC", value).casefold()
    except (TypeError, ValueError):
        _failure("zip_structure_invalid")


def _validated_entry_name(raw_name):
    try:
        name = raw_name.decode("ascii", errors="strict")
    except UnicodeError:
        _failure("zip_structure_invalid")
    if (
        not name
        or len(name) > 255
        or "\x00" in name
        or "\\" in name
        or ":" in name
        or name.startswith(("/", "//"))
        or name.endswith("/")
        or "//" in name
        or any(segment in {"", ".", ".."} for segment in name.split("/"))
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in name)
    ):
        _failure("zip_structure_invalid")
    return name


def _parse_zip64_central_extra(
    raw,
    *,
    compressed_size,
    uncompressed_size,
    local_offset,
    disk_number,
):
    marked = (
        uncompressed_size == 0xFFFFFFFF,
        compressed_size == 0xFFFFFFFF,
        local_offset == 0xFFFFFFFF,
        disk_number == 0xFFFF,
    )
    if not any(marked):
        if raw != b"":
            _failure("zip_structure_invalid")
        return compressed_size, uncompressed_size, local_offset, disk_number
    if len(raw) < _ZIP64_EXTRA_HEADER.size:
        _failure("zip_structure_invalid")
    identifier, payload_size = _ZIP64_EXTRA_HEADER.unpack_from(raw)
    payload = raw[_ZIP64_EXTRA_HEADER.size :]
    if identifier != 0x0001 or payload_size != len(payload):
        _failure("zip_structure_invalid")
    offset = 0

    def take_q():
        nonlocal offset
        if offset + 8 > len(payload):
            _failure("zip_structure_invalid")
        value = struct.unpack_from("<Q", payload, offset)[0]
        offset += 8
        return value

    def take_l():
        nonlocal offset
        if offset + 4 > len(payload):
            _failure("zip_structure_invalid")
        value = struct.unpack_from("<L", payload, offset)[0]
        offset += 4
        return value

    if marked[0]:
        uncompressed_size = take_q()
    if marked[1]:
        compressed_size = take_q()
    if marked[2]:
        local_offset = take_q()
    if marked[3]:
        disk_number = take_l()
    if offset != len(payload):
        _failure("zip_structure_invalid")
    return compressed_size, uncompressed_size, local_offset, disk_number


def _validate_raw_zip(file_object, package_size, expected_entry_count):
    if package_size < _END_HEADER.size:
        _failure("zip_structure_invalid")
    end_offset = package_size - _END_HEADER.size
    end = _END_HEADER.unpack(_read_at(file_object, end_offset, _END_HEADER.size))
    (
        signature,
        disk_number,
        central_disk,
        entries_on_disk,
        entry_count,
        central_size,
        central_offset,
        comment_size,
    ) = end
    if (
        signature != b"PK\x05\x06"
        or disk_number != 0
        or central_disk != 0
        or entries_on_disk != entry_count
        or comment_size != 0
    ):
        _failure("zip_structure_invalid")
    has_zip64_markers = (
        entry_count == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF
    )
    central_end = end_offset
    if has_zip64_markers:
        locator_offset = end_offset - _ZIP64_LOCATOR.size
        if locator_offset < _ZIP64_END_HEADER.size:
            _failure("zip_structure_invalid")
        (
            locator_signature,
            locator_disk,
            zip64_end_offset,
            total_disks,
        ) = _ZIP64_LOCATOR.unpack(_read_at(file_object, locator_offset, _ZIP64_LOCATOR.size))
        if (
            locator_signature != b"PK\x06\x07"
            or locator_disk != 0
            or total_disks != 1
            or zip64_end_offset + _ZIP64_END_HEADER.size != locator_offset
        ):
            _failure("zip_structure_invalid")
        (
            zip64_signature,
            zip64_record_size,
            made_by,
            extract_version,
            zip64_disk,
            zip64_central_disk,
            zip64_entries_on_disk,
            zip64_entry_count,
            zip64_central_size,
            zip64_central_offset,
        ) = _ZIP64_END_HEADER.unpack(
            _read_at(
                file_object,
                zip64_end_offset,
                _ZIP64_END_HEADER.size,
            )
        )
        if (
            zip64_signature != b"PK\x06\x06"
            or zip64_record_size != 44
            or made_by != _ZIP_UNIX_CREATOR
            or extract_version != _ZIP_VERSION
            or zip64_disk != 0
            or zip64_central_disk != 0
            or zip64_entries_on_disk != zip64_entry_count
            or entry_count != min(zip64_entry_count, 0xFFFF)
            or central_size != min(zip64_central_size, 0xFFFFFFFF)
            or central_offset != min(zip64_central_offset, 0xFFFFFFFF)
        ):
            _failure("zip_structure_invalid")
        entry_count = zip64_entry_count
        central_size = zip64_central_size
        central_offset = zip64_central_offset
        central_end = zip64_end_offset
    elif (
        end_offset >= _ZIP64_LOCATOR.size
        and _read_at(
            file_object,
            end_offset - _ZIP64_LOCATOR.size,
            4,
        )
        == b"PK\x06\x07"
    ):
        _failure("zip_structure_invalid")
    if (
        entry_count != expected_entry_count
        or not 0 < entry_count <= _MAXIMUM_PACKAGE_ENTRIES
        or central_offset + central_size != central_end
        or central_offset < _LOCAL_HEADER.size
    ):
        _failure("zip_structure_invalid")

    entries = []
    cursor = central_offset
    names = set()
    portable_names = set()
    for _index in range(entry_count):
        fixed = _CENTRAL_HEADER.unpack(_read_at(file_object, cursor, _CENTRAL_HEADER.size))
        (
            signature,
            made_by,
            extract_version,
            flags,
            compression,
            dos_time,
            dos_date,
            crc32,
            compressed_size,
            uncompressed_size,
            name_size,
            extra_size,
            entry_comment_size,
            entry_disk,
            internal_attributes,
            external_attributes,
            local_offset,
        ) = fixed
        if (
            signature != b"PK\x01\x02"
            or made_by != _ZIP_UNIX_CREATOR
            or extract_version != _ZIP_VERSION
            or flags != 0
            or compression != zipfile.ZIP_STORED
            or dos_time != _DOS_TIME
            or dos_date != _DOS_DATE
            or not 0 < name_size <= 255
            or extra_size > 64
            or entry_comment_size != 0
            or internal_attributes != 0
            or external_attributes != _ZIP_EXTERNAL_ATTRIBUTES
        ):
            _failure("zip_structure_invalid")
        variable_size = name_size + extra_size
        variable = _read_at(
            file_object,
            cursor + _CENTRAL_HEADER.size,
            variable_size,
        )
        raw_name = variable[:name_size]
        extra = variable[name_size:]
        name = _validated_entry_name(raw_name)
        if name in names or _portable_path_key(name) in portable_names:
            _failure("zip_structure_invalid")
        names.add(name)
        portable_names.add(_portable_path_key(name))
        (
            compressed_size,
            uncompressed_size,
            local_offset,
            entry_disk,
        ) = _parse_zip64_central_extra(
            extra,
            compressed_size=compressed_size,
            uncompressed_size=uncompressed_size,
            local_offset=local_offset,
            disk_number=entry_disk,
        )
        if (
            entry_disk != 0
            or compressed_size != uncompressed_size
            or compressed_size > _MAXIMUM_PACKAGE_BYTES
            or local_offset >= central_offset
        ):
            _failure("zip_structure_invalid")
        entries.append(
            _CentralEntry(
                name=name,
                crc32=crc32,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                local_offset=local_offset,
                central_extra=extra,
            )
        )
        cursor += _CENTRAL_HEADER.size + variable_size
    if cursor != central_end:
        _failure("zip_structure_invalid")

    validated_entries = []
    local_cursor = 0
    for entry in entries:
        if entry.local_offset != local_cursor:
            _failure("zip_structure_invalid")
        fixed = _LOCAL_HEADER.unpack(_read_at(file_object, entry.local_offset, _LOCAL_HEADER.size))
        (
            signature,
            extract_version,
            flags,
            compression,
            dos_time,
            dos_date,
            crc32,
            compressed_size,
            uncompressed_size,
            name_size,
            extra_size,
        ) = fixed
        if (
            signature != b"PK\x03\x04"
            or extract_version != _ZIP_VERSION
            or flags != 0
            or compression != zipfile.ZIP_STORED
            or dos_time != _DOS_TIME
            or dos_date != _DOS_DATE
            or crc32 != entry.crc32
            or compressed_size != 0xFFFFFFFF
            or uncompressed_size != 0xFFFFFFFF
            or name_size != len(entry.name.encode("ascii"))
            or extra_size != 20
        ):
            _failure("zip_structure_invalid")
        variable = _read_at(
            file_object,
            entry.local_offset + _LOCAL_HEADER.size,
            name_size + extra_size,
        )
        raw_name = variable[:name_size]
        local_extra = variable[name_size:]
        expected_extra = _ZIP64_EXTRA_HEADER.pack(
            0x0001, _ZIP64_LOCAL_VALUES.size
        ) + _ZIP64_LOCAL_VALUES.pack(
            entry.uncompressed_size,
            entry.compressed_size,
        )
        if _validated_entry_name(raw_name) != entry.name or local_extra != expected_extra:
            _failure("zip_structure_invalid")
        data_start = entry.local_offset + _LOCAL_HEADER.size + name_size + extra_size
        data_end = data_start + entry.compressed_size
        if data_end > central_offset:
            _failure("zip_structure_invalid")
        validated_entries.append(replace(entry, data_start=data_start, data_end=data_end))
        local_cursor = data_end
    if local_cursor != central_offset:
        _failure("zip_structure_invalid")
    return tuple(validated_entries)


def _validate_zipinfo(info, entry):
    if (
        type(info) is not zipfile.ZipInfo
        or info.filename != entry.name
        or info.orig_filename != entry.name
        or info.date_time != DETERMINISTIC_ZIP_TIMESTAMP
        or info.compress_type != zipfile.ZIP_STORED
        or info.file_size != entry.uncompressed_size
        or info.compress_size != entry.compressed_size
        or info.CRC != entry.crc32
        or info.comment != b""
        or info.extra != entry.central_extra
        or info.create_system != 3
        or info.create_version != _ZIP_VERSION
        or info.extract_version != _ZIP_VERSION
        or info.reserved != 0
        or info.flag_bits != 0
        or info.volume != 0
        or info.internal_attr != 0
        or info.external_attr != _ZIP_EXTERNAL_ATTRIBUTES
        or info.header_offset != entry.local_offset
        or info.is_dir()
    ):
        _failure("zip_structure_invalid")


def _read_entry_bytes(archive, info, *, maximum, code):
    digest = hashlib.sha256()
    count = 0
    chunks = []
    try:
        with archive.open(info, mode="r") as reader:
            while True:
                chunk = reader.read(_PACKAGE_CHUNK_BYTES)
                if type(chunk) is not bytes or len(chunk) > _PACKAGE_CHUNK_BYTES:
                    _failure(code)
                if not chunk:
                    break
                count += len(chunk)
                if count > maximum or count > info.file_size:
                    _failure(code)
                chunks.append(chunk)
                digest.update(chunk)
    except _OrdinaryVerificationFailure:
        raise
    except (
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        _failure(code)
    if count != info.file_size:
        _failure(code)
    return b"".join(chunks), count, digest.hexdigest()


def _validate_identity(identity, *, spec, context, code):
    if type(identity) is not dict:
        _failure(code)
    if spec.identity_kind == IdentityKind.PUBLIC_UUID:
        if frozenset(identity) != {"public_id"}:
            _failure(code)
        try:
            normalized = canonical_uuid(identity["public_id"])
        except Exception:
            _failure(code)
        if normalized != identity["public_id"]:
            _failure(code)
        if spec.model_label == "tenants.Business" and normalized != str(context.business_public_id):
            _failure(code)
    elif spec.identity_kind == IdentityKind.TENANT_SINGLETON:
        if identity != {
            "singleton_model": spec.model_label,
            "tenant_public_id": str(context.business_public_id),
        }:
            _failure(code)
    else:
        _failure(code)
    try:
        return encode_canonical_document(identity)
    except Exception:
        _failure(code)


def _stream_records(
    archive,
    info,
    *,
    component,
    manifest_component,
    registry,
    context,
):
    digest = hashlib.sha256()
    byte_count = 0
    line_count = 0
    model_counts = {item["model"]: 0 for item in manifest_component["models"]}
    try:
        with archive.open(info, mode="r") as reader:
            while True:
                line = reader.readline(_MAXIMUM_RECORD_LINE_BYTES + 1)
                if type(line) is not bytes:
                    _failure("payload_invalid")
                if not line:
                    break
                if (
                    len(line) > _MAXIMUM_RECORD_LINE_BYTES
                    or line == b"\n"
                    or not line.endswith(b"\n")
                    or line.endswith(b"\r\n")
                ):
                    _failure("payload_invalid")
                byte_count += len(line)
                if byte_count > info.file_size:
                    _failure("payload_invalid")
                digest.update(line)
                payload = _strict_json(
                    line,
                    trailing_lf=True,
                    code="payload_invalid",
                )
                _exact_keys(payload, _RECORD_KEYS, code="payload_invalid")
                model = payload.get("model")
                if (
                    payload.get("schema") != LOGICAL_RECORD_SCHEMA
                    or payload.get("component") != component.key
                    or payload.get("component_version") != manifest_component["component_version"]
                    or payload.get("tenant_public_id") != str(context.business_public_id)
                    or type(model) is not str
                    or model not in model_counts
                    or type(payload.get("fields")) is not dict
                ):
                    _failure("payload_invalid")
                spec = registry.maybe_get(model)
                if spec is None or spec.component_key != component.key:
                    _failure("payload_invalid")
                _validate_identity(
                    payload["identity"],
                    spec=spec,
                    context=context,
                    code="payload_invalid",
                )
                model_counts[model] += 1
                line_count += 1
    except _OrdinaryVerificationFailure:
        raise
    except (
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        _failure("payload_invalid")
    records = manifest_component["records"]
    if (
        byte_count != records["byte_count"]
        or line_count != records["record_count"]
        or digest.hexdigest() != records["sha256"]
        or tuple(model_counts.items())
        != tuple((item["model"], item["record_count"]) for item in manifest_component["models"])
    ):
        _failure("payload_invalid")
    return byte_count, line_count, digest.hexdigest()


def _stream_media_index(
    archive,
    info,
    *,
    component,
    manifest_component,
    registry,
    context,
):
    digest = hashlib.sha256()
    byte_count = 0
    sources = []
    seen = set()
    try:
        with archive.open(info, mode="r") as reader:
            while True:
                line = reader.readline(_MAXIMUM_MEDIA_INDEX_LINE_BYTES + 1)
                if type(line) is not bytes:
                    _failure("payload_invalid")
                if not line:
                    break
                if (
                    len(line) > _MAXIMUM_MEDIA_INDEX_LINE_BYTES
                    or line == b"\n"
                    or not line.endswith(b"\n")
                    or line.endswith(b"\r\n")
                ):
                    _failure("payload_invalid")
                byte_count += len(line)
                if byte_count > info.file_size:
                    _failure("payload_invalid")
                digest.update(line)
                payload = _strict_json(
                    line,
                    trailing_lf=True,
                    code="payload_invalid",
                )
                _exact_keys(payload, _MEDIA_INDEX_KEYS, code="payload_invalid")
                if (
                    payload.get("schema") != LOGICAL_MEDIA_REFERENCE_SCHEMA
                    or payload.get("component") != component.key
                    or payload.get("tenant_public_id") != str(context.business_public_id)
                    or type(payload.get("model")) is not str
                    or type(payload.get("field")) is not str
                    or type(payload.get("storage_name")) is not str
                ):
                    _failure("payload_invalid")
                spec = registry.maybe_get(payload["model"])
                if (
                    spec is None
                    or spec.component_key != component.key
                    or payload["field"] not in spec.media_fields
                ):
                    _failure("payload_invalid")
                identity_bytes = _validate_identity(
                    payload["identity"],
                    spec=spec,
                    context=context,
                    code="payload_invalid",
                )
                try:
                    storage_name = validate_media_storage_name(
                        payload["storage_name"],
                        maximum_length=_MAXIMUM_MEDIA_NAME_LENGTH,
                    )
                except Exception:
                    _failure("payload_invalid")
                source = _SourceEvidence(
                    component=component.key,
                    model=spec.model_label,
                    identity_bytes=identity_bytes,
                    field=payload["field"],
                    storage_name=storage_name,
                )
                if source.logical_key in seen:
                    _failure("payload_invalid")
                seen.add(source.logical_key)
                sources.append(source)
    except _OrdinaryVerificationFailure:
        raise
    except (
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        _failure("payload_invalid")
    media_index = manifest_component["media_index"]
    if (
        byte_count != media_index["byte_count"]
        or len(sources) != media_index["reference_count"]
        or digest.hexdigest() != media_index["sha256"]
    ):
        _failure("payload_invalid")
    return byte_count, tuple(sources), digest.hexdigest()


def _stream_media(archive, info, *, expected):
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with archive.open(info, mode="r") as reader:
            while True:
                chunk = reader.read(_PACKAGE_CHUNK_BYTES)
                if type(chunk) is not bytes or len(chunk) > _PACKAGE_CHUNK_BYTES:
                    _failure("payload_invalid")
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > expected["byte_count"]:
                    _failure("payload_invalid")
                digest.update(chunk)
    except _OrdinaryVerificationFailure:
        raise
    except (
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        _failure("payload_invalid")
    if byte_count != expected["byte_count"] or digest.hexdigest() != expected["sha256"]:
        _failure("payload_invalid")
    return byte_count, digest.hexdigest()


def _manifest_source(
    value,
    *,
    storage_name,
    component_ordinals,
    registry,
    context,
):
    _exact_keys(
        value,
        {"component", "model", "identity", "field"},
        code="manifest_invalid",
    )
    component = value.get("component")
    model = value.get("model")
    field = value.get("field")
    if (
        type(component) is not str
        or component not in component_ordinals
        or type(model) is not str
        or type(field) is not str
    ):
        _failure("manifest_invalid")
    spec = registry.maybe_get(model)
    if spec is None or spec.component_key != component or field not in spec.media_fields:
        _failure("manifest_invalid")
    identity_bytes = _validate_identity(
        value["identity"],
        spec=spec,
        context=context,
        code="manifest_invalid",
    )
    return _SourceEvidence(
        component=component,
        model=model,
        identity_bytes=identity_bytes,
        field=field,
        storage_name=storage_name,
    )


def _component_content_sha256(component):
    descriptor = {
        "schema": COMPONENT_CONTENT_SCHEMA,
        "component_key": component["key"],
        "component_version": component["component_version"],
        "record_schema": component["record_schema"],
        "deterministic_ordering_version": component["deterministic_ordering_version"],
        "records": {
            "record_count": component["records"]["record_count"],
            "byte_count": component["records"]["byte_count"],
            "sha256": component["records"]["sha256"],
        },
        "media_index": {
            "reference_count": component["media_index"]["reference_count"],
            "byte_count": component["media_index"]["byte_count"],
            "sha256": component["media_index"]["sha256"],
        },
        "models": component["models"],
    }
    try:
        return hashlib.sha256(encode_canonical_document(descriptor)).hexdigest()
    except Exception:
        _failure("manifest_invalid")


def _payload_set_sha256(document):
    payloads = []
    for component in document["components"]:
        for kind, key in (
            ("COMPONENT_RECORDS", "records"),
            ("COMPONENT_MEDIA_INDEX", "media_index"),
        ):
            item = component[key]
            payloads.append(
                {
                    "kind": kind,
                    "package_path": item["package_path"],
                    "byte_count": item["byte_count"],
                    "sha256": item["sha256"],
                }
            )
    for media in document["media"]:
        payloads.append(
            {
                "kind": "MEDIA",
                "package_path": media["package_path"],
                "storage_name": media["storage_name"],
                "byte_count": media["byte_count"],
                "sha256": media["sha256"],
            }
        )
    try:
        descriptor = {"schema": PAYLOAD_SET_SCHEMA, "payloads": payloads}
        return hashlib.sha256(encode_canonical_document(descriptor)).hexdigest()
    except Exception:
        _failure("manifest_invalid")


def _numeric_version(value):
    if type(value) is not str or re.fullmatch(r"\d+(?:\.\d+){0,3}", value) is None:
        return None
    try:
        parts = tuple(int(part) for part in value.split("."))
    except ValueError:
        return None
    return parts + (0,) * (4 - len(parts))


@dataclass(frozen=True, slots=True)
class PackageCompatibilityPolicy:
    """Small code-owned compatibility policy for the current restore contract."""

    identifier: str = "nexa.restore-compatibility-policy.v1"
    current_schema_fingerprint: str = ""
    current_application_version: str = ""
    current_backup_format_version: str = ""

    def assess(self, *, document, plan, context):
        issues = []
        incompatible = False
        not_proven = False
        for manifest_component, authoritative in zip(
            document["components"],
            plan,
            strict=True,
        ):
            metadata_well_typed = (
                type(manifest_component["product_owner"]) is str
                and type(manifest_component["component_version"]) is str
                and type(manifest_component["restore_behavior"]) is str
                and type(manifest_component["required_component_keys"]) is list
                and all(
                    type(value) is str
                    for value in manifest_component["required_component_keys"]
                )
                and type(manifest_component["export_order"]) is int
                and type(manifest_component["import_order"]) is int
            )
            recognized_restore = (
                metadata_well_typed
                and manifest_component["restore_behavior"]
                in {value.value for value in RestoreBehavior}
            )
            exact = (
                metadata_well_typed
                and manifest_component["product_owner"]
                == authoritative.product_owner.value
                and manifest_component["component_version"] == authoritative.component_version
                and manifest_component["restore_behavior"] == authoritative.restore_behavior.value
                and manifest_component["required_component_keys"]
                == list(authoritative.required_component_keys)
                and manifest_component["export_order"] == authoritative.export_order
                and manifest_component["import_order"] == authoritative.import_order
            )
            if not recognized_restore or not exact:
                incompatible = True

        backup_metadata = document["backup"]
        minimum = backup_metadata["minimum_restore_version"]
        application = self.current_application_version or context.application_version
        if (
            self.current_backup_format_version
            and backup_metadata["backup_format_version"]
            != self.current_backup_format_version
        ):
            incompatible = True
        if (
            self.current_schema_fingerprint
            and backup_metadata["schema_migration_fingerprint"]
            != self.current_schema_fingerprint
        ):
            not_proven = True
        if minimum != application:
            minimum_parts = _numeric_version(minimum)
            application_parts = _numeric_version(application)
            if minimum_parts is None or application_parts is None:
                not_proven = True
            elif minimum_parts > application_parts:
                incompatible = True

        if incompatible:
            issues.append(_issue("compatibility_incompatible"))
            status = PackageCompatibilityStatus.INCOMPATIBLE
        elif not_proven:
            issues.append(_issue("compatibility_not_proven"))
            status = PackageCompatibilityStatus.NOT_PROVEN
        else:
            status = PackageCompatibilityStatus.COMPATIBLE
        return status, tuple(issues[:_MAXIMUM_ISSUES])


def _validate_manifest(
    raw,
    *,
    context,
    package,
    actual_names,
    compatibility_policy,
):
    document = _strict_json(raw, trailing_lf=True, code="manifest_invalid")
    _exact_keys(document, _TOP_LEVEL_KEYS)
    if (
        document.get("schema") != MANIFEST_SCHEMA_IDENTIFIER
        or document.get("manifest_version") != MANIFEST_VERSION
        or document.get("canonical_json_version") != CANONICAL_JSON_VERSION
        or document.get("hash_algorithm") != HASH_ALGORITHM
        or document.get("package_format") != PACKAGE_FORMAT_IDENTIFIER
        or document.get("payload_set_schema") != PAYLOAD_SET_SCHEMA
        or document.get("missing_media_policy") != MISSING_MEDIA_POLICY
        or document.get("restore_verification_state") != RESTORE_VERIFICATION_STATE
    ):
        _failure("manifest_invalid")
    if _count(document.get("missing_media_count")) != 0:
        _failure("manifest_invalid")
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    payload_sha256 = _sha256(document.get("payload_set_sha256"))
    if payload_sha256 != package.payload_set_sha256:
        _failure("manifest_invalid")

    backup = _exact_keys(document["backup"], _BACKUP_KEYS)
    if (
        backup.get("backup_public_id") != str(context.backup_public_id)
        or backup.get("tenant_public_id") != str(context.business_public_id)
        or backup.get("scope") != context.requested_scope.value
        or backup.get("trigger_type") != context.trigger_type.value
        or backup.get("included_products") != [item.value for item in context.resolved_products]
        or backup.get("application_version") != context.application_version
        or backup.get("backup_format_version") != context.backup_format_version
        or backup.get("schema_migration_fingerprint") != context.schema_migration_fingerprint
        or backup.get("minimum_restore_version") != context.minimum_restore_version
        or backup.get("created_timestamp") != _utc_timestamp(package.created_at)
    ):
        _failure("manifest_invalid")
    _sha256(backup["schema_migration_fingerprint"])
    _version_token(backup["application_version"])
    _version_token(backup["backup_format_version"])
    _version_token(backup["minimum_restore_version"])
    _parse_aware_timestamp(backup["created_timestamp"])

    compatibility = _exact_keys(
        document["compatibility"],
        {"minimum_restore_version", "status", "database_engine_neutral"},
    )
    if (
        compatibility.get("minimum_restore_version") != context.minimum_restore_version
        or compatibility.get("status") != "NOT_CHECKED"
        or compatibility.get("database_engine_neutral") is not True
    ):
        _failure("manifest_invalid")

    source = _exact_keys(
        document["source_consistency"],
        {
            "database_snapshot_state",
            "database_snapshot_created_at",
            "database_consistency_cutoff_at",
            "logical_export_provider",
            "logical_record_schema",
            "logical_media_reference_schema",
            "logical_ordering_version",
            "media_capture_policy",
        },
    )
    if (
        source.get("database_snapshot_state") != "CONSISTENT"
        or source.get("database_snapshot_created_at") != backup["created_timestamp"]
        or source.get("logical_export_provider") != LOGICAL_EXPORT_PROVIDER_IDENTIFIER
        or source.get("logical_record_schema") != LOGICAL_RECORD_SCHEMA
        or source.get("logical_media_reference_schema") != LOGICAL_MEDIA_REFERENCE_SCHEMA
        or source.get("logical_ordering_version") != DETERMINISTIC_ORDERING_VERSION
        or source.get("media_capture_policy") != MEDIA_CAPTURE_POLICY_IDENTIFIER
    ):
        _failure("manifest_invalid")
    created = _parse_aware_timestamp(source["database_snapshot_created_at"])
    cutoff = _parse_aware_timestamp(source["database_consistency_cutoff_at"])
    if cutoff > created:
        _failure("manifest_invalid")

    try:
        plan = resolve_component_plan(
            scope=context.requested_scope,
            enabled_products=context.resolved_products,
        ).export_components
        registry = get_logical_export_registry()
        registry.validate_complete()
        if (
            registry.validate_component_plan(
                context=context,
                component_plan=plan,
                require_full=True,
            )
            != plan
        ):
            raise ValueError
    except Exception:
        raise VerificationProviderStateError() from None

    components = _exact_list(document["components"])
    if not components or len(components) != len(plan):
        _failure("manifest_invalid")
    if backup.get("included_component_keys") != [item.key for item in plan]:
        _failure("manifest_invalid")
    expected_names = [MANIFEST_FILE_NAME]
    total_records = 0
    total_references = 0
    total_record_bytes = 0
    total_index_bytes = 0
    model_total = 0
    for ordinal, (component, authoritative) in enumerate(
        zip(components, plan, strict=True),
        start=1,
    ):
        _exact_keys(component, _COMPONENT_KEYS)
        records = _exact_keys(
            component["records"],
            {"package_path", "record_count", "byte_count", "sha256"},
        )
        media_index = _exact_keys(
            component["media_index"],
            {"package_path", "reference_count", "byte_count", "sha256"},
        )
        models = _exact_list(component["models"])
        expected_models = tuple(
            spec.model_label for spec in registry.for_component(authoritative.key)
        )
        actual_models = []
        component_model_count = 0
        for model in models:
            _exact_keys(model, {"model", "record_count"})
            if type(model.get("model")) is not str:
                _failure("manifest_invalid")
            model_count = _count(model.get("record_count"))
            actual_models.append(model["model"])
            component_model_count += model_count
        if (
            tuple(actual_models) != expected_models
            or _count(
                component.get("ordinal"),
                maximum=9999,
                positive=True,
            )
            != ordinal
            or component.get("key") != authoritative.key
            or component.get("record_schema") != LOGICAL_RECORD_SCHEMA
            or component.get("media_reference_schema") != LOGICAL_MEDIA_REFERENCE_SCHEMA
            or component.get("deterministic_ordering_version") != DETERMINISTIC_ORDERING_VERSION
            or component.get("component_content_schema") != COMPONENT_CONTENT_SCHEMA
            or component.get("restore_verification_state") != RESTORE_VERIFICATION_STATE
        ):
            _failure("manifest_invalid")
        _version_token(component.get("component_version"))
        record_path = records.get("package_path")
        index_path = media_index.get("package_path")
        if (
            type(record_path) is not str
            or _COMPONENT_RECORDS_PATTERN.fullmatch(record_path) is None
            or int(_COMPONENT_RECORDS_PATTERN.fullmatch(record_path).group(1)) != ordinal
            or type(index_path) is not str
            or _COMPONENT_MEDIA_PATTERN.fullmatch(index_path) is None
            or int(_COMPONENT_MEDIA_PATTERN.fullmatch(index_path).group(1)) != ordinal
        ):
            _failure("manifest_invalid")
        record_count = _count(records.get("record_count"))
        record_bytes = _count(records.get("byte_count"))
        reference_count = _count(media_index.get("reference_count"))
        index_bytes = _count(media_index.get("byte_count"))
        _sha256(records.get("sha256"))
        _sha256(media_index.get("sha256"))
        _sha256(component.get("component_content_sha256"))
        if (
            component_model_count != record_count
            or (record_count == 0) != (record_bytes == 0)
            or (reference_count == 0) != (index_bytes == 0)
            or _component_content_sha256(component) != component["component_content_sha256"]
        ):
            _failure("manifest_invalid")
        expected_names.extend((record_path, index_path))
        total_records += record_count
        total_references += reference_count
        total_record_bytes += record_bytes
        total_index_bytes += index_bytes
        model_total += len(models)

    media = _exact_list(document["media"])
    if len(media) > 99_999_999:
        _failure("manifest_invalid")
    component_ordinals = {component.key: ordinal for ordinal, component in enumerate(plan, start=1)}
    previous_storage_name = None
    storage_names = set()
    collision_names = set()
    manifest_sources = []
    seen_source_keys = set()
    total_media_bytes = 0
    for ordinal, item in enumerate(media, start=1):
        _exact_keys(item, _MEDIA_KEYS)
        path = item.get("package_path")
        storage_name = item.get("storage_name")
        if (
            _count(
                item.get("ordinal"),
                maximum=99_999_999,
                positive=True,
            )
            != ordinal
            or type(path) is not str
            or _MEDIA_PATTERN.fullmatch(path) is None
            or int(_MEDIA_PATTERN.fullmatch(path).group(1)) != ordinal
            or type(storage_name) is not str
            or item.get("capture_state") != CAPTURE_STATE
            or item.get("restore_verification_state") != RESTORE_VERIFICATION_STATE
        ):
            _failure("manifest_invalid")
        try:
            storage_name = validate_media_storage_name(
                storage_name,
                maximum_length=_MAXIMUM_MEDIA_NAME_LENGTH,
            )
            collision_key = media_storage_collision_key(storage_name)
        except Exception:
            _failure("manifest_invalid")
        if (
            storage_name in storage_names
            or collision_key in collision_names
            or (previous_storage_name is not None and storage_name <= previous_storage_name)
        ):
            _failure("manifest_invalid")
        storage_names.add(storage_name)
        collision_names.add(collision_key)
        previous_storage_name = storage_name
        byte_count = _count(item.get("byte_count"))
        _sha256(item.get("sha256"))
        source_count = _count(item.get("source_reference_count"), positive=True)
        sources = _exact_list(item.get("sources"))
        if len(sources) != source_count:
            _failure("manifest_invalid")
        previous_source_sort = None
        for source_value in sources:
            source_item = _manifest_source(
                source_value,
                storage_name=storage_name,
                component_ordinals=component_ordinals,
                registry=registry,
                context=context,
            )
            source_sort = (
                component_ordinals[source_item.component],
                source_item.model,
                source_item.identity_bytes,
                source_item.field,
            )
            if (
                previous_source_sort is not None
                and source_sort <= previous_source_sort
                or source_item.logical_key in seen_source_keys
            ):
                _failure("manifest_invalid")
            previous_source_sort = source_sort
            seen_source_keys.add(source_item.logical_key)
            manifest_sources.append(source_item)
        total_media_bytes += byte_count
        expected_names.append(path)

    if tuple(expected_names) != actual_names:
        _failure("manifest_invalid")
    totals = _exact_keys(document["totals"], _TOTAL_KEYS)
    expected_totals = {
        "component_count": len(components),
        "model_count": model_total,
        "record_count": total_records,
        "media_reference_count": total_references,
        "unique_media_object_count": len(media),
        "component_records_bytes": total_record_bytes,
        "component_media_index_bytes": total_index_bytes,
        "media_bytes": total_media_bytes,
        "planned_payload_bytes": (total_record_bytes + total_index_bytes + total_media_bytes),
    }
    for value in totals.values():
        _count(value)
    if totals != expected_totals or len(manifest_sources) != total_references:
        _failure("manifest_invalid")
    calculated_payload_sha256 = _payload_set_sha256(document)
    if calculated_payload_sha256 != payload_sha256:
        _failure("manifest_invalid")

    compatibility_status, compatibility_issues = compatibility_policy.assess(
        document=document,
        plan=plan,
        context=context,
    )
    return (
        _ManifestAssessment(
            document=document,
            plan=plan,
            expected_names=tuple(expected_names),
            manifest_sha256=manifest_sha256,
            payload_set_sha256=calculated_payload_sha256,
            compatibility_status=compatibility_status,
            compatibility_issues=compatibility_issues,
        ),
        tuple(manifest_sources),
    )


def _verify_zip_payloads(
    file_object,
    *,
    entries,
    context,
    package,
    compatibility_policy,
):
    try:
        file_object.seek(0)
        with zipfile.ZipFile(file_object, mode="r", allowZip64=True) as archive:
            if archive.comment != b"":
                _failure("zip_structure_invalid")
            infos = archive.infolist()
            if len(infos) != len(entries):
                _failure("zip_structure_invalid")
            for info, entry in zip(infos, entries, strict=True):
                _validate_zipinfo(info, entry)
            actual_names = tuple(entry.name for entry in entries)
            if actual_names[0] != MANIFEST_FILE_NAME:
                _failure("manifest_invalid")
            manifest_raw, _manifest_bytes, _manifest_digest = _read_entry_bytes(
                archive,
                infos[0],
                maximum=_MAXIMUM_MANIFEST_BYTES,
                code="manifest_invalid",
            )
            assessment, manifest_sources = _validate_manifest(
                manifest_raw,
                context=context,
                package=package,
                actual_names=actual_names,
                compatibility_policy=compatibility_policy,
            )
            registry = get_logical_export_registry()
            media_index_sources = []
            component_count = len(assessment.document["components"])
            info_cursor = 1
            for component, manifest_component in zip(
                assessment.plan,
                assessment.document["components"],
                strict=True,
            ):
                _stream_records(
                    archive,
                    infos[info_cursor],
                    component=component,
                    manifest_component=manifest_component,
                    registry=registry,
                    context=context,
                )
                info_cursor += 1
                _bytes, sources, _digest = _stream_media_index(
                    archive,
                    infos[info_cursor],
                    component=component,
                    manifest_component=manifest_component,
                    registry=registry,
                    context=context,
                )
                media_index_sources.extend(sources)
                info_cursor += 1
            if info_cursor != 1 + (2 * component_count):
                _failure("payload_invalid")
            component_positions = {
                component_key: index
                for index, component_key in enumerate(
                    assessment.document["backup"]["included_component_keys"]
                )
            }
            if len(media_index_sources) != len(
                {source.logical_key for source in media_index_sources}
            ) or sorted(
                media_index_sources,
                key=lambda item: (
                    item.storage_name,
                    component_positions[item.component],
                    item.model,
                    item.identity_bytes,
                    item.field,
                ),
            ) != list(manifest_sources):
                _failure("payload_invalid")
            for info, manifest_media in zip(
                infos[info_cursor:],
                assessment.document["media"],
                strict=True,
            ):
                _stream_media(archive, info, expected=manifest_media)
            return assessment
    except _OrdinaryVerificationFailure:
        raise
    except (
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        _failure("zip_structure_invalid")


def _directory_state(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if path_is_link_like(path) or not stat.S_ISDIR(current.st_mode):
        raise error_type()
    return current


def _regular_file_state(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if path_is_link_like(path) or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise error_type()
    return current


def _apply_private_mode(path, mode, *, error_type):
    try:
        os.chmod(path, mode)
        current = os.stat(path, follow_symlinks=False)
        if os.name != "nt" and stat.S_IMODE(current.st_mode) != mode:
            raise error_type()
    except Phase2EEngineError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise error_type() from None


def _apply_private_descriptor_mode(descriptor, path, mode, *, error_type):
    try:
        before = os.fstat(descriptor)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            _apply_private_mode(path, mode, error_type=error_type)
        after = os.fstat(descriptor)
        if (
            _identity(before) != _identity(after)
            or not stat.S_ISREG(after.st_mode)
            or (os.name != "nt" and stat.S_IMODE(after.st_mode) != mode)
        ):
            raise error_type()
    except Phase2EEngineError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise error_type() from None


def _assert_private_mode(path, mode, *, error_type):
    if os.name == "nt":
        return
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if stat.S_IMODE(current.st_mode) != mode:
        raise error_type()


class IndependentPackageVerifier:
    """Reopen, independently verify, and publish opaque readiness evidence."""

    def __init__(
        self,
        *,
        package_provider,
        workspace_manager=None,
        compatibility_policy=None,
        reference_factory=None,
        clock=None,
        failure_hook=None,
    ):
        if (
            type(package_provider)
            not in {DeterministicPackageProvider, RestoredPackageProvider}
            or getattr(package_provider, "package_access_provider_schema", None)
            != PACKAGE_ACCESS_PROVIDER_SCHEMA
            or not callable(getattr(package_provider, "validate_package_evidence", None))
            or not callable(getattr(package_provider, "open_package", None))
            or type(
                getattr(package_provider, "package_result_provider_identifier", None)
            )
            is not str
            or not package_provider.package_result_provider_identifier
        ):
            raise VerificationProviderStateError()
        manager = workspace_manager or package_provider.workspace_manager
        if (
            type(manager) is not BackupWorkspaceManager
            or manager.root != package_provider.workspace_manager.root
        ):
            raise VerificationProviderStateError()
        policy = compatibility_policy or PackageCompatibilityPolicy()
        if type(policy) is not PackageCompatibilityPolicy:
            raise VerificationProviderStateError()
        self.package_provider = package_provider
        self.package_provider_identifier = (
            package_provider.package_result_provider_identifier
        )
        self.workspace_manager = manager
        self.compatibility_policy = policy
        self.reference_factory = reference_factory or (lambda: VerificationReference(uuid.uuid4()))
        self.clock = clock or timezone.now
        self.failure_hook = failure_hook
        self._published = {}
        self._cleaned = {}
        self._state_lock = threading.RLock()

    def _run_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _new_reference(self):
        try:
            reference = self.reference_factory()
            if type(reference) is uuid.UUID:
                reference = VerificationReference(reference)
            if (
                type(reference) is not VerificationReference
                or type(reference.identifier) is not uuid.UUID
            ):
                raise TypeError
            return reference
        except (AttributeError, TypeError, ValueError):
            raise VerificationPublicationError() from None

    def _state_key(self, context, reference, *, error_type):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(reference) is not VerificationReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise error_type()
        return context.workspace_reference.identifier, reference.identifier

    def _existing_workspace(self, context, *, error_type):
        try:
            root = self.workspace_manager.root
            root_state = _directory_state(root, error_type=error_type)
            workspace = self.workspace_manager.handle(context.workspace_reference)
            workspace_state = _directory_state(workspace.path, error_type=error_type)
            if root_state.st_dev != workspace_state.st_dev:
                raise error_type()
            _assert_private_mode(root, 0o700, error_type=error_type)
            _assert_private_mode(workspace.path, 0o700, error_type=error_type)
            return workspace
        except Phase2EEngineError:
            raise
        except Exception:
            raise error_type() from None

    def _verification_parent(self, context, *, create, error_type):
        workspace = self._existing_workspace(context, error_type=error_type)
        try:
            parent = workspace.system_area_path(WorkspaceArea.VERIFICATION)
            if os.path.lexists(parent) and path_is_link_like(parent):
                raise error_type()
            if create:
                parent.mkdir(mode=0o700, exist_ok=True)
            state = _directory_state(parent, error_type=error_type)
            if (
                state.st_dev
                != _directory_state(
                    workspace.path,
                    error_type=error_type,
                ).st_dev
            ):
                raise error_type()
            _apply_private_mode(parent, 0o700, error_type=error_type)
            if _identity(_directory_state(parent, error_type=error_type)) != _identity(state):
                raise error_type()
            return workspace, parent
        except Phase2EEngineError:
            raise
        except Exception:
            raise error_type() from None

    def _verification_directory(
        self,
        context,
        reference,
        *,
        require_exists,
        error_type,
    ):
        workspace, parent = self._verification_parent(
            context,
            create=False,
            error_type=error_type,
        )
        try:
            directory = workspace.system_area_path(
                WorkspaceArea.VERIFICATION,
                generated_identifier=reference.identifier,
            )
            if os.path.lexists(directory) and path_is_link_like(directory):
                raise error_type()
            if require_exists:
                state = _directory_state(directory, error_type=error_type)
                if (
                    state.st_dev
                    != _directory_state(
                        parent,
                        error_type=error_type,
                    ).st_dev
                ):
                    raise error_type()
            return directory
        except Phase2EEngineError:
            raise
        except Exception:
            raise error_type() from None

    @staticmethod
    def _remove_empty_directory(directory, *, expected_identity, error_type):
        if directory is None or not os.path.lexists(directory):
            return False
        state = _directory_state(directory, error_type=error_type)
        if _identity(state) != expected_identity:
            raise error_type()
        with os.scandir(directory) as entries:
            if next(entries, None) is not None:
                return False
        os.rmdir(directory)
        if os.path.lexists(directory):
            raise error_type()
        return True

    @staticmethod
    def _cleanup_owned_files(paths, *, expected_identity):
        owned = []
        for path in paths:
            if path is not None and path not in owned and os.path.lexists(path):
                owned.append(path)
        remaining_links = len(owned)
        for path in owned:
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError:
                raise VerificationCleanupError() from None
            if (
                path_is_link_like(path)
                or not stat.S_ISREG(current.st_mode)
                or _identity(current) != expected_identity
                or current.st_nlink != remaining_links
            ):
                raise VerificationCleanupError()
            os.unlink(path)
            if os.path.lexists(path):
                raise VerificationCleanupError()
            remaining_links -= 1
        return bool(owned)

    def _publish_evidence(
        self,
        *,
        context,
        package,
        verified_at,
        package_byte_count,
        plaintext_sha256,
        entry_count,
        manifest_sha256,
        payload_set_sha256,
        compatibility_status,
        issues,
    ):
        reference = self._new_reference()
        key = self._state_key(
            context,
            reference,
            error_type=VerificationPublicationError,
        )
        with self._state_lock:
            if key in self._published or key in self._cleaned:
                raise VerificationPublicationError()
        evidence_document = {
            "schema": VERIFICATION_SCHEMA_IDENTIFIER,
            "verification_version": VERIFICATION_VERSION,
            "verification_provider": INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER,
            "verified": True,
            "restore_ready": (compatibility_status == PackageCompatibilityStatus.COMPATIBLE),
            "verified_at": _utc_timestamp(verified_at),
            "package": {
                "format": package.format_identifier,
                "byte_count": package_byte_count,
                "plaintext_sha256": plaintext_sha256,
                "entry_count": entry_count,
            },
            "manifest_sha256": manifest_sha256,
            "payload_set_sha256": payload_set_sha256,
            "compatibility_status": compatibility_status.value,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.sanitized_message,
                }
                for issue in issues
            ],
        }
        try:
            raw = encode_canonical_document(evidence_document, trailing_lf=True)
        except Exception:
            raise VerificationProviderStateError() from None
        digest = hashlib.sha256(raw).hexdigest()

        directory = None
        directory_identity = None
        part_path = None
        final_path = None
        file_identity = None
        descriptor = None
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_incomplete = False
        directory_created = False
        try:
            _workspace, parent = self._verification_parent(
                context,
                create=True,
                error_type=VerificationPublicationError,
            )
            directory = self._verification_directory(
                context,
                reference,
                require_exists=False,
                error_type=VerificationPublicationError,
            )
            absent = not os.path.lexists(directory)
            if not absent:
                raise VerificationPublicationError()
            directory.mkdir(mode=0o700, exist_ok=False)
            directory_created = True
            directory_state = _directory_state(
                directory,
                error_type=VerificationPublicationError,
            )
            directory_identity = _identity(directory_state)
            if (
                directory_state.st_dev
                != _directory_state(
                    parent,
                    error_type=VerificationPublicationError,
                ).st_dev
            ):
                raise VerificationPublicationError()
            _apply_private_mode(
                directory,
                0o700,
                error_type=VerificationPublicationError,
            )
            self._run_hook("after_verification_directory_creation")
            part_path = contained_path(
                directory,
                directory / f".{VERIFICATION_FILE_NAME}.{uuid.uuid4().hex}.part",
            )
            final_path = contained_path(
                directory,
                directory / VERIFICATION_FILE_NAME,
            )
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(part_path, flags, 0o600)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != directory_state.st_dev
            ):
                raise VerificationPublicationError()
            file_identity = _identity(opened)
            _apply_private_descriptor_mode(
                descriptor,
                part_path,
                0o600,
                error_type=VerificationPublicationError,
            )
            written = 0
            while written < len(raw):
                amount = os.write(descriptor, raw[written : written + _EVIDENCE_CHUNK_BYTES])
                if type(amount) is not int or amount <= 0:
                    raise VerificationPublicationError()
                written += amount
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            current = _regular_file_state(
                part_path,
                error_type=VerificationPublicationError,
            )
            if _identity(current) != file_identity or current.st_size != len(raw):
                raise VerificationPublicationError()
            self._run_hook("before_verification_publication")
            os.link(part_path, final_path, follow_symlinks=False)
            for path in (part_path, final_path):
                linked = os.stat(path, follow_symlinks=False)
                if (
                    _identity(linked) != file_identity
                    or linked.st_nlink != 2
                    or not stat.S_ISREG(linked.st_mode)
                ):
                    raise VerificationPublicationError()
            self._run_hook("after_verification_publication_link")
            os.unlink(part_path)
            part_path = None
            final_state = _regular_file_state(
                final_path,
                error_type=VerificationPublicationError,
            )
            if _identity(final_state) != file_identity or final_state.st_size != len(raw):
                raise VerificationPublicationError()
            restore_ready = compatibility_status == PackageCompatibilityStatus.COMPATIBLE
            readiness = RestoreReadinessResult(
                restore_ready=restore_ready,
                compatibility_status=compatibility_status,
                issue_codes=tuple(issue.code for issue in issues),
            )
            candidate = PackageVerificationResult(
                reference=reference,
                verified=True,
                restore_ready=restore_ready,
                verified_at=verified_at,
                package_byte_count=package_byte_count,
                plaintext_sha256=plaintext_sha256,
                entry_count=entry_count,
                manifest_sha256=manifest_sha256,
                payload_set_sha256=payload_set_sha256,
                compatibility_status=compatibility_status,
                provider_identifier=INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER,
                verification_schema=VERIFICATION_SCHEMA_IDENTIFIER,
                issues=issues,
                restore_readiness=readiness,
                evidence_byte_count=len(raw),
                evidence_sha256=digest,
            )
            evidence = _PublishedVerification(
                context=context,
                package=package,
                result=candidate,
                directory_identity=directory_identity,
                file_identity=file_identity,
            )
            self._validate_published(
                context=context,
                reference=reference,
                evidence=evidence,
                error_type=VerificationPublicationError,
            )
            self._run_hook("before_verification_result_return")
            with self._state_lock:
                if key in self._published or key in self._cleaned:
                    raise VerificationPublicationError()
                self._published[key] = evidence
            result = candidate
        except BaseException as exc:
            if isinstance(exc, Exception):
                if isinstance(exc, Phase2EEngineError):
                    safe_error = exc
                else:
                    safe_error = VerificationPublicationError()
            else:
                abort_error = exc
                abort_traceback = exc.__traceback__
        finally:
            if result is None:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except BaseException:
                        cleanup_incomplete = True
                try:
                    if file_identity is not None:
                        self._cleanup_owned_files(
                            (part_path, final_path),
                            expected_identity=file_identity,
                        )
                    elif any(
                        path is not None and os.path.lexists(path)
                        for path in (part_path, final_path)
                    ):
                        raise VerificationCleanupError()
                except BaseException:
                    cleanup_incomplete = True
                if directory is not None and directory_identity is not None:
                    try:
                        removed = self._remove_empty_directory(
                            directory,
                            expected_identity=directory_identity,
                            error_type=VerificationCleanupError,
                        )
                        if not removed and os.path.lexists(directory):
                            cleanup_incomplete = True
                    except BaseException:
                        cleanup_incomplete = True
                elif directory_created and directory is not None and os.path.lexists(directory):
                    cleanup_incomplete = True
        if abort_error is not None:
            try:
                abort_error.cleanup_incomplete = bool(
                    cleanup_incomplete or getattr(abort_error, "cleanup_incomplete", False)
                )
            except Exception:
                pass
            raise abort_error.with_traceback(abort_traceback)
        if safe_error is not None:
            safe_error.cleanup_incomplete = bool(
                cleanup_incomplete or getattr(safe_error, "cleanup_incomplete", False)
            )
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        if result is None:
            raise VerificationPublicationError(cleanup_incomplete=cleanup_incomplete)
        return result

    @staticmethod
    def _failure_result(
        *,
        code,
        verified_at,
        package_byte_count=0,
        plaintext_sha256="",
        entry_count=0,
        manifest_sha256="",
        payload_set_sha256="",
    ):
        issue = _issue(code)
        readiness = RestoreReadinessResult(
            restore_ready=False,
            compatibility_status=PackageCompatibilityStatus.NOT_PROVEN,
            issue_codes=(issue.code,),
        )
        return PackageVerificationResult(
            reference=None,
            verified=False,
            restore_ready=False,
            verified_at=verified_at,
            package_byte_count=package_byte_count,
            plaintext_sha256=plaintext_sha256,
            entry_count=entry_count,
            manifest_sha256=manifest_sha256,
            payload_set_sha256=payload_set_sha256,
            compatibility_status=PackageCompatibilityStatus.NOT_PROVEN,
            provider_identifier=INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER,
            verification_schema=VERIFICATION_SCHEMA_IDENTIFIER,
            issues=(issue,),
            restore_readiness=readiness,
        )

    def verify(self, request):
        verified_at = None
        package_byte_count = 0
        plaintext_sha256 = ""
        entry_count = 0
        manifest_sha256 = ""
        payload_set_sha256 = ""
        try:
            verified_at = self.clock()
            if not _is_aware(verified_at):
                raise VerificationProviderStateError()
            if type(request) is not PackageVerificationRequest:
                _failure("package_evidence_rejected")
            context = _validated_context(request.context)
            package = _validated_package_result(
                request.package,
                provider_identifier=self.package_provider_identifier,
            )
            try:
                self.package_provider.validate_package_evidence(
                    context=context,
                    result=package,
                )
            except (PackageNotFound, PackageValidationError):
                _failure("package_evidence_rejected")
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except Exception:
                raise VerificationProviderStateError() from None

            digest = hashlib.sha256()
            with tempfile.SpooledTemporaryFile(
                max_size=_SPOOL_MEMORY_BYTES,
                mode="w+b",
            ) as package_copy:
                try:
                    with self.package_provider.open_package(
                        context=context,
                        reference=package.reference,
                    ) as reader:
                        while True:
                            chunk = reader.read(_PACKAGE_CHUNK_BYTES)
                            if type(chunk) is not bytes or len(chunk) > _PACKAGE_CHUNK_BYTES:
                                _failure("package_integrity_failed")
                            if not chunk:
                                break
                            package_byte_count += len(chunk)
                            if (
                                package_byte_count > package.byte_count
                                or package_byte_count > _MAXIMUM_PACKAGE_BYTES
                            ):
                                _failure("package_integrity_failed")
                            if package_copy.write(chunk) != len(chunk):
                                raise VerificationProviderStateError()
                            digest.update(chunk)
                except _OrdinaryVerificationFailure:
                    raise
                except (PackageNotFound, PackageValidationError):
                    _failure("package_integrity_failed")
                plaintext_sha256 = digest.hexdigest()
                if (
                    package_byte_count != package.byte_count
                    or plaintext_sha256 != package.plaintext_sha256
                ):
                    _failure("package_integrity_failed")
                package_copy.flush()
                entries = _validate_raw_zip(
                    package_copy,
                    package_byte_count,
                    package.entry_count,
                )
                entry_count = len(entries)
                assessment = _verify_zip_payloads(
                    package_copy,
                    entries=entries,
                    context=context,
                    package=package,
                    compatibility_policy=self.compatibility_policy,
                )
                manifest_sha256 = assessment.manifest_sha256
                payload_set_sha256 = assessment.payload_set_sha256

            try:
                self.package_provider.validate_package_evidence(
                    context=context,
                    result=package,
                )
            except (PackageNotFound, PackageValidationError):
                _failure("package_integrity_failed")
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except Exception:
                raise VerificationProviderStateError() from None
            return self._publish_evidence(
                context=context,
                package=package,
                verified_at=verified_at,
                package_byte_count=package_byte_count,
                plaintext_sha256=plaintext_sha256,
                entry_count=entry_count,
                manifest_sha256=manifest_sha256,
                payload_set_sha256=payload_set_sha256,
                compatibility_status=assessment.compatibility_status,
                issues=assessment.compatibility_issues,
            )
        except _OrdinaryVerificationFailure as exc:
            if verified_at is None or not _is_aware(verified_at):
                verified_at = timezone.now()
            return self._failure_result(
                code=exc.code,
                verified_at=verified_at,
                package_byte_count=package_byte_count,
                plaintext_sha256=plaintext_sha256,
                entry_count=entry_count,
                manifest_sha256=manifest_sha256,
                payload_set_sha256=payload_set_sha256,
            )

    def _hash_evidence_file(self, path, *, expected_identity, error_type):
        descriptor = None
        digest = hashlib.sha256()
        byte_count = 0
        try:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != expected_identity
                or opened.st_nlink != 1
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise error_type()
            while True:
                chunk = os.read(descriptor, _EVIDENCE_CHUNK_BYTES)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > _MAXIMUM_MANIFEST_BYTES:
                    raise error_type()
                digest.update(chunk)
        except Phase2EEngineError:
            raise
        except OSError:
            raise error_type() from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return byte_count, digest.hexdigest()

    def _validate_published(self, *, context, reference, evidence, error_type):
        if (
            context != evidence.context
            or reference != evidence.result.reference
            or evidence.file_identity is None
        ):
            raise error_type()
        directory = self._verification_directory(
            context,
            reference,
            require_exists=True,
            error_type=error_type,
        )
        directory_state = _directory_state(directory, error_type=error_type)
        if _identity(directory_state) != evidence.directory_identity:
            raise error_type()
        _assert_private_mode(directory, 0o700, error_type=error_type)
        with os.scandir(directory) as contents:
            if {entry.name for entry in contents} != {VERIFICATION_FILE_NAME}:
                raise error_type()
        path = contained_path(directory, directory / VERIFICATION_FILE_NAME)
        current = _regular_file_state(path, error_type=error_type)
        if (
            _identity(current) != evidence.file_identity
            or current.st_dev != evidence.directory_identity[0]
            or current.st_size != evidence.result.evidence_byte_count
        ):
            raise error_type()
        _assert_private_mode(path, 0o600, error_type=error_type)
        byte_count, digest = self._hash_evidence_file(
            path,
            expected_identity=evidence.file_identity,
            error_type=error_type,
        )
        if (
            byte_count != evidence.result.evidence_byte_count
            or digest != evidence.result.evidence_sha256
        ):
            raise error_type()
        return directory, path

    def validate_verification_evidence(self, *, context, package, result):
        if (
            type(package) is not PackageBuildResult
            or type(result) is not PackageVerificationResult
            or type(result.reference) is not VerificationReference
        ):
            raise VerificationProviderStateError()
        key = self._state_key(
            context,
            result.reference,
            error_type=VerificationProviderStateError,
        )
        with self._state_lock:
            evidence = self._published.get(key)
        if (
            evidence is None
            or evidence.context != context
            or evidence.package != package
            or evidence.result != result
        ):
            raise VerificationProviderStateError()
        self._validate_published(
            context=context,
            reference=result.reference,
            evidence=evidence,
            error_type=VerificationProviderStateError,
        )
        return True

    @contextmanager
    def open_verification_evidence(self, *, context, reference):
        key = self._state_key(
            context,
            reference,
            error_type=VerificationEvidenceNotFound,
        )
        with self._state_lock:
            evidence = self._published.get(key)
        if evidence is None or evidence.file_identity is None:
            raise VerificationEvidenceNotFound()
        directory, path = self._validate_published(
            context=context,
            reference=reference,
            evidence=evidence,
            error_type=VerificationEvidenceNotFound,
        )
        descriptor = None
        raw_file = None
        reader = None
        try:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != evidence.file_identity
                or opened.st_nlink != 1
                or opened.st_size != evidence.result.evidence_byte_count
            ):
                raise VerificationEvidenceNotFound()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            reader = _OpaqueVerificationReader(raw_file)
            yield reader
        except VerificationEvidenceNotFound:
            raise
        except OSError:
            raise VerificationEvidenceNotFound() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            close_error = None
            target = reader or raw_file
            if target is not None:
                try:
                    target.close()
                except BaseException as exc:
                    if not active_exception:
                        close_error = exc
            elif descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    if not active_exception:
                        close_error = exc
            try:
                if (
                    _identity(
                        _directory_state(
                            directory,
                            error_type=VerificationEvidenceNotFound,
                        )
                    )
                    != evidence.directory_identity
                ):
                    raise VerificationEvidenceNotFound()
                self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    error_type=VerificationEvidenceNotFound,
                )
            except BaseException as exc:
                if not active_exception:
                    close_error = exc
            if close_error is not None and not active_exception:
                if isinstance(close_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise close_error.with_traceback(close_error.__traceback__)
                raise VerificationEvidenceNotFound() from None

    def cleanup_verification_evidence(self, *, context, reference):
        key = self._state_key(
            context,
            reference,
            error_type=VerificationCleanupError,
        )
        with self._state_lock:
            if key in self._cleaned:
                if self._cleaned[key] != context:
                    raise VerificationCleanupError()
                return True
            evidence = self._published.get(key)
        if evidence is None or evidence.context != context:
            raise VerificationCleanupError()
        try:
            if evidence.file_identity is not None:
                directory, path = self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    error_type=VerificationCleanupError,
                )
                self._run_hook("before_verification_cleanup_unlink")
                unlink_abort = None
                unlink_abort_traceback = None
                try:
                    os.unlink(path)
                except BaseException as exc:
                    if os.path.lexists(path):
                        raise
                    if not isinstance(exc, Exception):
                        unlink_abort = exc
                        unlink_abort_traceback = exc.__traceback__
                if os.path.lexists(path):
                    raise VerificationCleanupError()
                updated = replace(evidence, file_identity=None)
                with self._state_lock:
                    if self._published.get(key) != evidence:
                        raise VerificationCleanupError()
                    self._published[key] = updated
                evidence = updated
                if unlink_abort is not None:
                    raise unlink_abort.with_traceback(unlink_abort_traceback)
            else:
                directory = self._verification_directory(
                    context,
                    reference,
                    require_exists=False,
                    error_type=VerificationCleanupError,
                )
                if not os.path.lexists(directory):
                    with self._state_lock:
                        if self._published.get(key) != evidence:
                            raise VerificationCleanupError()
                        self._published.pop(key, None)
                        self._cleaned[key] = context
                    return True
                if (
                    _identity(_directory_state(directory, error_type=VerificationCleanupError))
                    != evidence.directory_identity
                ):
                    raise VerificationCleanupError()
                path = contained_path(directory, directory / VERIFICATION_FILE_NAME)
                if os.path.lexists(path):
                    raise VerificationCleanupError()
            if (
                _identity(_directory_state(directory, error_type=VerificationCleanupError))
                != evidence.directory_identity
            ):
                raise VerificationCleanupError()
            with os.scandir(directory) as contents:
                if next(contents, None) is not None:
                    raise VerificationCleanupError()
            self._run_hook("before_verification_cleanup_directory_removal")
            directory_abort = None
            directory_abort_traceback = None
            try:
                os.rmdir(directory)
            except BaseException as exc:
                if os.path.lexists(directory):
                    raise
                if not isinstance(exc, Exception):
                    directory_abort = exc
                    directory_abort_traceback = exc.__traceback__
            if os.path.lexists(directory):
                raise VerificationCleanupError()
            with self._state_lock:
                if self._published.get(key) != evidence:
                    raise VerificationCleanupError()
                self._published.pop(key, None)
                self._cleaned[key] = context
            if directory_abort is not None:
                raise directory_abort.with_traceback(directory_abort_traceback)
            return True
        except VerificationCleanupError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise VerificationCleanupError() from None


__all__ = [
    "INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER",
    "IndependentPackageVerifier",
    "PackageCompatibilityPolicy",
    "VERIFICATION_FILE_NAME",
    "VERIFICATION_SCHEMA_IDENTIFIER",
    "VERIFICATION_VERSION",
]
