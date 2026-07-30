"""Deterministic plaintext package construction for Backup Engine Phase 2D-2.

The provider consumes only opaque Phase 2D-1 references.  It never accepts a
filesystem path from a caller, never reopens the deleted SQLite snapshot, and
never serializes the whole-package hash into ``manifest.json``.  Package bytes
remain plaintext and private; encryption and durable storage are later phases.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from apps.backups.enums import BackupScope, BackupTrigger, ProductOwner

from .canonical_manifest import (
    CANONICAL_JSON_VERSION,
    CANONICAL_MANIFEST_PROVIDER_IDENTIFIER,
    HASH_ALGORITHM,
    MANIFEST_FILE_NAME,
    MANIFEST_SCHEMA_IDENTIFIER,
    MANIFEST_VERSION,
    PACKAGE_FORMAT_IDENTIFIER,
    PAYLOAD_SET_SCHEMA,
    CanonicalManifestProvider,
)
from .context import BackupExecutionContext
from .contracts import (
    CanonicalManifestResult,
    ComponentExportReference,
    ComponentExportResult,
    MediaCaptureReference,
    ManifestReference,
    MediaCaptureResult,
    PackageBuildRequest,
    PackageBuildResult,
    PackageReference,
    Phase2D1Result,
)
from .exceptions import (
    CanonicalManifestNotFound,
    ComponentExportNotFound,
    MediaObjectNotFound,
    UnsafeWorkspacePath,
)
from .package_exceptions import (
    PackageCleanupError,
    PackageContentMismatch,
    PackageCreationError,
    PackageNotFound,
    PackageValidationError,
    Phase2D2EngineError,
)
from .logical_export import (
    LOGICAL_EXPORT_PROVIDER_IDENTIFIER,
    ComponentExportStream,
    SQLiteLogicalComponentExporter,
)
from .logical_serialization import (
    DETERMINISTIC_ORDERING_VERSION,
    LOGICAL_MEDIA_REFERENCE_SCHEMA,
    LOGICAL_RECORD_SCHEMA,
    encode_canonical_document,
)
from .media_capture import (
    LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER,
    LocalFilesystemMediaCaptureProvider,
)
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    contained_path,
    path_is_link_like,
)

DETERMINISTIC_PACKAGE_PROVIDER_IDENTIFIER = "deterministic-zip-store-v1"
PACKAGE_FILE_NAME = "package.zip"
PLAINTEXT_PACKAGE_HASH_ALGORITHM = "sha256"
DETERMINISTIC_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

_PACKAGE_CHUNK_BYTES = 1024**2
_MAXIMUM_PACKAGE_BYTES = 10 * 1024**4
_MAXIMUM_PACKAGE_ENTRIES = 200_000
_MAXIMUM_MANIFEST_BYTES = 1024**3
_PACKAGE_TIMEOUT_SECONDS = 1800.0
_MINIMUM_FREE_BYTES = 1024**3
_CAPACITY_HEADROOM_MULTIPLIER = 1.25
_MAXIMUM_SIGNED_COUNT = 2**63 - 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMPONENT_RECORDS_PATTERN = re.compile(r"^components/([0-9]{4})/records\.ndjson$")
_COMPONENT_MEDIA_PATTERN = re.compile(r"^components/([0-9]{4})/media-index\.ndjson$")
_MEDIA_PATTERN = re.compile(r"^media/([0-9]{8})\.bin$")


@dataclass(frozen=True, slots=True)
class _PackageEntry:
    path: str
    byte_count: int
    sha256: str
    source_kind: str
    source_index: int


@dataclass(frozen=True, slots=True)
class _PublishedPackage:
    context: BackupExecutionContext
    result: PackageBuildResult
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int] | None


class _OpaquePackageReader:
    __slots__ = ("__file",)

    def __init__(self, file_object):
        self.__file = file_object

    def read(self, size=-1):
        return self.__file.read(size)

    def readline(self, size=-1):
        return self.__file.readline(size)

    def seek(self, offset, whence=io.SEEK_SET):
        return self.__file.seek(offset, whence)

    def tell(self):
        return self.__file.tell()

    def close(self):
        return self.__file.close()

    @property
    def closed(self):
        return self.__file.closed


class _BoundedBytesReader:
    __slots__ = ("_stream",)

    def __init__(self, value):
        self._stream = io.BytesIO(value)

    def read(self, size=-1):
        return self._stream.read(size)

    def close(self):
        return self._stream.close()


@contextmanager
def _bytes_reader(value):
    reader = _BoundedBytesReader(value)
    try:
        yield reader
    finally:
        reader.close()


def _identity(current) -> tuple[int, int]:
    return current.st_dev, current.st_ino


def _is_aware(value) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _utc_timestamp(value) -> str:
    if not _is_aware(value):
        raise PackageValidationError()
    try:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
    except (OverflowError, TypeError, ValueError):
        raise PackageValidationError() from None


def _strict_nonnegative(value, *, maximum=_MAXIMUM_SIGNED_COUNT) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise PackageValidationError()
    return value


def _validated_sha256(value) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise PackageValidationError()
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


def _strict_json_document(raw: bytes):
    try:
        if type(raw) is not bytes or raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PackageValidationError() from None
    if type(value) is not dict:
        raise PackageValidationError()
    try:
        if encode_canonical_document(value, trailing_lf=True) != raw:
            raise PackageValidationError()
    except PackageValidationError:
        raise
    except Exception:
        raise PackageValidationError() from None
    return value


def _regular_file_state(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if (
        path_is_link_like(path)
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
    ):
        raise error_type()
    return current


def _owned_regular_file_state(
    path,
    *,
    expected_identity,
    expected_link_count,
    error_type,
):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if (
        path_is_link_like(path)
        or not stat.S_ISREG(current.st_mode)
        or expected_identity is None
        or _identity(current) != expected_identity
        or type(expected_link_count) is not int
        or expected_link_count <= 0
        or current.st_nlink != expected_link_count
    ):
        raise error_type()
    return current


def _directory_state(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if path_is_link_like(path) or not stat.S_ISDIR(current.st_mode):
        raise error_type()
    return current


def _apply_private_mode(path, mode, *, error_type):
    try:
        os.chmod(path, mode)
        current = os.stat(path, follow_symlinks=False)
        if os.name != "nt" and stat.S_IMODE(current.st_mode) != mode:
            raise error_type()
    except Phase2D2EngineError:
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
    except Phase2D2EngineError:
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


def _same_device(parent, child, *, error_type):
    if (
        _directory_state(parent, error_type=error_type).st_dev
        != _directory_state(child, error_type=error_type).st_dev
    ):
        raise error_type()


def _validate_context(context):
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
        raise PackageValidationError()
    return context


def _validate_phase2d1_result(result):
    if (
        type(result) is not Phase2D1Result
        or type(result.component_exports) is not tuple
        or not result.component_exports
        or type(result.media_captures) is not tuple
        or type(result.manifest) is not CanonicalManifestResult
    ):
        raise PackageValidationError()
    manifest = result.manifest
    if (
        type(manifest.reference) is not ManifestReference
        or type(manifest.reference.identifier) is not uuid.UUID
        or not 0 < _strict_nonnegative(manifest.byte_count) <= _MAXIMUM_MANIFEST_BYTES
        or manifest.schema_identifier != MANIFEST_SCHEMA_IDENTIFIER
        or not _is_aware(manifest.created_at)
        or manifest.provider_identifier != CANONICAL_MANIFEST_PROVIDER_IDENTIFIER
        or manifest.component_count != len(result.component_exports)
        or manifest.unique_media_object_count != len(result.media_captures)
    ):
        raise PackageValidationError()
    _strict_nonnegative(manifest.total_record_count)
    _strict_nonnegative(manifest.total_media_bytes)
    _validated_sha256(manifest.sha256)
    _validated_sha256(manifest.payload_set_sha256)

    component_references = set()
    for item in result.component_exports:
        if (
            type(item) is not ComponentExportResult
            or type(item.reference) is not ComponentExportReference
            or type(item.reference.identifier) is not uuid.UUID
            or item.reference.identifier in component_references
            or item.provider_identifier != LOGICAL_EXPORT_PROVIDER_IDENTIFIER
            or not _is_aware(item.created_at)
        ):
            raise PackageValidationError()
        for value in (
            item.row_count,
            item.media_count,
            item.byte_count,
            item.media_index_byte_count,
            item.duration_ms,
        ):
            _strict_nonnegative(value)
        component_references.add(item.reference.identifier)

    media_references = set()
    storage_names = set()
    for item in result.media_captures:
        if (
            type(item) is not MediaCaptureResult
            or type(item.reference) is not MediaCaptureReference
            or type(item.reference.identifier) is not uuid.UUID
            or item.reference.identifier in media_references
            or type(item.logical_storage_name) is not str
            or not item.logical_storage_name
            or item.logical_storage_name in storage_names
            or item.provider_identifier
            != LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER
            or not _is_aware(item.captured_at)
        ):
            raise PackageValidationError()
        _strict_nonnegative(item.byte_count)
        _strict_nonnegative(item.source_reference_count)
        _strict_nonnegative(item.duration_ms)
        _validated_sha256(item.sha256)
        media_references.add(item.reference.identifier)
        storage_names.add(item.logical_storage_name)
    return result


def _read_exact_stream(reader, *, maximum, expected_count, expected_sha256):
    digest = hashlib.sha256()
    count = 0
    while True:
        chunk = reader.read(_PACKAGE_CHUNK_BYTES)
        if type(chunk) is not bytes or len(chunk) > _PACKAGE_CHUNK_BYTES:
            raise PackageContentMismatch()
        if not chunk:
            break
        count += len(chunk)
        if count > maximum or count > expected_count:
            raise PackageContentMismatch()
        digest.update(chunk)
    if count != expected_count or digest.hexdigest() != expected_sha256:
        raise PackageContentMismatch()
    return count, digest.hexdigest()


def _validated_package_path(value, *, expected_pattern=None, expected_ordinal=None):
    if type(value) is not str or not value or len(value) > 255:
        raise PackageValidationError()
    try:
        encoded = value.encode("ascii", errors="strict")
    except UnicodeError:
        raise PackageValidationError() from None
    if (
        value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or "//" in value
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
        or encoded.decode("ascii") != value
    ):
        raise PackageValidationError()
    if expected_pattern is not None:
        matched = expected_pattern.fullmatch(value)
        if matched is None or int(matched.group(1)) != expected_ordinal:
            raise PackageValidationError()
    return value


def _expect_exact_keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise PackageValidationError()
    return value


def _manifest_entries(*, context, phase2d1_result, document, manifest_bytes):
    expected_top_level = {
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
    _expect_exact_keys(document, expected_top_level)
    manifest_result = phase2d1_result.manifest
    if (
        document["schema"] != MANIFEST_SCHEMA_IDENTIFIER
        or document["manifest_version"] != MANIFEST_VERSION
        or document["canonical_json_version"] != CANONICAL_JSON_VERSION
        or document["hash_algorithm"] != HASH_ALGORITHM
        or document["package_format"] != PACKAGE_FORMAT_IDENTIFIER
        or document["payload_set_schema"] != PAYLOAD_SET_SCHEMA
        or document["payload_set_sha256"] != manifest_result.payload_set_sha256
        or document["missing_media_policy"] != "FAIL_BACKUP"
        or document["missing_media_count"] != 0
        or document["restore_verification_state"] != "NOT_VERIFIED"
    ):
        raise PackageValidationError()

    backup = _expect_exact_keys(
        document["backup"],
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
        },
    )
    if (
        backup["backup_public_id"] != str(context.backup_public_id)
        or backup["tenant_public_id"] != str(context.business_public_id)
        or backup["scope"] != context.requested_scope.value
        or backup["trigger_type"] != context.trigger_type.value
        or backup["included_products"]
        != [item.value for item in context.resolved_products]
        or backup["application_version"] != context.application_version
        or backup["backup_format_version"] != context.backup_format_version
        or backup["schema_migration_fingerprint"]
        != context.schema_migration_fingerprint
        or backup["minimum_restore_version"] != context.minimum_restore_version
        or backup["created_timestamp"] != _utc_timestamp(manifest_result.created_at)
    ):
        raise PackageValidationError()

    components = document["components"]
    media = document["media"]
    totals = document["totals"]
    if type(totals) is dict:
        _expect_exact_keys(
            totals,
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
            },
        )
    if (
        type(components) is not list
        or type(media) is not list
        or type(totals) is not dict
        or len(components) != len(phase2d1_result.component_exports)
        or len(media) != len(phase2d1_result.media_captures)
        or totals.get("component_count") != len(components)
        or totals.get("unique_media_object_count") != len(media)
        or totals.get("record_count") != manifest_result.total_record_count
        or totals.get("media_bytes") != manifest_result.total_media_bytes
    ):
        raise PackageValidationError()

    entries = [
        _PackageEntry(
            path=MANIFEST_FILE_NAME,
            byte_count=len(manifest_bytes),
            sha256=manifest_result.sha256,
            source_kind="manifest",
            source_index=0,
        )
    ]
    planned_payload_bytes = 0
    component_keys = []
    for ordinal, (manifest_component, export_result) in enumerate(
        zip(components, phase2d1_result.component_exports, strict=True),
        start=1,
    ):
        manifest_component = _expect_exact_keys(
            manifest_component,
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
            },
        )
        records = _expect_exact_keys(
            manifest_component["records"],
            {"package_path", "record_count", "byte_count", "sha256"},
        )
        media_index = _expect_exact_keys(
            manifest_component["media_index"],
            {"package_path", "reference_count", "byte_count", "sha256"},
        )
        if (
            manifest_component.get("ordinal") != ordinal
            or manifest_component.get("key") != export_result.component_key
            or manifest_component.get("component_version")
            != export_result.component_version
            or manifest_component.get("record_schema") != LOGICAL_RECORD_SCHEMA
            or manifest_component.get("media_reference_schema")
            != LOGICAL_MEDIA_REFERENCE_SCHEMA
            or manifest_component.get("deterministic_ordering_version")
            != DETERMINISTIC_ORDERING_VERSION
            or export_result.provider_identifier
            != LOGICAL_EXPORT_PROVIDER_IDENTIFIER
        ):
            raise PackageValidationError()
        component_keys.append(export_result.component_key)
        record_path = _validated_package_path(
            records.get("package_path"),
            expected_pattern=_COMPONENT_RECORDS_PATTERN,
            expected_ordinal=ordinal,
        )
        media_index_path = _validated_package_path(
            media_index.get("package_path"),
            expected_pattern=_COMPONENT_MEDIA_PATTERN,
            expected_ordinal=ordinal,
        )
        record_count = _strict_nonnegative(records.get("record_count"))
        record_bytes = _strict_nonnegative(records.get("byte_count"))
        media_count = _strict_nonnegative(media_index.get("reference_count"))
        media_bytes = _strict_nonnegative(media_index.get("byte_count"))
        record_sha = _validated_sha256(records.get("sha256"))
        media_sha = _validated_sha256(media_index.get("sha256"))
        if (
            record_count != export_result.row_count
            or record_bytes != export_result.byte_count
            or media_count != export_result.media_count
            or media_bytes != export_result.media_index_byte_count
        ):
            raise PackageValidationError()
        entries.extend(
            (
                _PackageEntry(
                    path=record_path,
                    byte_count=record_bytes,
                    sha256=record_sha,
                    source_kind="component-records",
                    source_index=ordinal - 1,
                ),
                _PackageEntry(
                    path=media_index_path,
                    byte_count=media_bytes,
                    sha256=media_sha,
                    source_kind="component-media-index",
                    source_index=ordinal - 1,
                ),
            )
        )
        planned_payload_bytes += record_bytes + media_bytes

    if backup.get("included_component_keys") != component_keys:
        raise PackageValidationError()

    previous_storage_name = None
    for ordinal, (manifest_media, capture_result) in enumerate(
        zip(media, phase2d1_result.media_captures, strict=True),
        start=1,
    ):
        manifest_media = _expect_exact_keys(
            manifest_media,
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
            },
        )
        package_path = _validated_package_path(
            manifest_media.get("package_path"),
            expected_pattern=_MEDIA_PATTERN,
            expected_ordinal=ordinal,
        )
        byte_count = _strict_nonnegative(manifest_media.get("byte_count"))
        digest = _validated_sha256(manifest_media.get("sha256"))
        storage_name = manifest_media.get("storage_name")
        if (
            manifest_media.get("ordinal") != ordinal
            or storage_name != capture_result.logical_storage_name
            or byte_count != capture_result.byte_count
            or digest != capture_result.sha256
            or manifest_media.get("source_reference_count")
            != capture_result.source_reference_count
            or manifest_media.get("capture_state") != "CAPTURED_AND_HASHED"
            or capture_result.provider_identifier
            != LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER
            or (previous_storage_name is not None and storage_name <= previous_storage_name)
        ):
            raise PackageValidationError()
        previous_storage_name = storage_name
        entries.append(
            _PackageEntry(
                path=package_path,
                byte_count=byte_count,
                sha256=digest,
                source_kind="media",
                source_index=ordinal - 1,
            )
        )
        planned_payload_bytes += byte_count

    if (
        len(entries) > _MAXIMUM_PACKAGE_ENTRIES
        or totals.get("planned_payload_bytes") != planned_payload_bytes
        or totals.get("component_records_bytes")
        != sum(item.byte_count for item in entries if item.source_kind == "component-records")
        or totals.get("component_media_index_bytes")
        != sum(item.byte_count for item in entries if item.source_kind == "component-media-index")
        or totals.get("media_bytes")
        != sum(item.byte_count for item in entries if item.source_kind == "media")
    ):
        raise PackageValidationError()

    paths = tuple(item.path for item in entries)
    if len(paths) != len(set(paths)):
        raise PackageValidationError()
    normalized = tuple(path.casefold() for path in paths)
    if len(normalized) != len(set(normalized)):
        raise PackageValidationError()
    return tuple(entries), planned_payload_bytes


def _zip_info(path):
    info = zipfile.ZipInfo(path, date_time=DETERMINISTIC_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.internal_attr = 0
    info.comment = b""
    info.extra = b""
    return info


class DeterministicPackageProvider:
    """Build, open, validate, and exactly clean one private plaintext package."""

    def __init__(
        self,
        *,
        component_exporter,
        media_capture_provider,
        manifest_provider,
        workspace_manager=None,
        reference_factory=None,
        monotonic=None,
        disk_usage_provider=None,
        failure_hook=None,
    ):
        if (
            type(component_exporter) is not SQLiteLogicalComponentExporter
            or type(media_capture_provider) is not LocalFilesystemMediaCaptureProvider
            or type(manifest_provider) is not CanonicalManifestProvider
            or component_exporter.workspace_manager.root
            != media_capture_provider.workspace_manager.root
            or component_exporter.workspace_manager.root
            != manifest_provider.workspace_manager.root
        ):
            raise PackageValidationError()
        manager = workspace_manager or manifest_provider.workspace_manager
        if (
            type(manager) is not BackupWorkspaceManager
            or manager.root != manifest_provider.workspace_manager.root
        ):
            raise PackageValidationError()
        self.component_exporter = component_exporter
        self.media_capture_provider = media_capture_provider
        self.manifest_provider = manifest_provider
        self.workspace_manager = manager
        self.reference_factory = reference_factory or (lambda: PackageReference(uuid.uuid4()))
        self.monotonic = monotonic or time.monotonic
        self.disk_usage_provider = disk_usage_provider or shutil.disk_usage
        self.failure_hook = failure_hook
        self._published = {}
        self._cleaned = {}
        self._state_lock = threading.RLock()

    def _run_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _check_deadline(self, deadline, *, error_type=PackageCreationError):
        try:
            if self.monotonic() > deadline:
                raise error_type()
        except Phase2D2EngineError:
            raise
        except Exception:
            raise error_type() from None

    def _new_reference(self):
        try:
            reference = self.reference_factory()
            if type(reference) is uuid.UUID:
                reference = PackageReference(reference)
            if (
                type(reference) is not PackageReference
                or type(reference.identifier) is not uuid.UUID
            ):
                raise TypeError
            return reference
        except (AttributeError, TypeError, ValueError):
            raise PackageCreationError() from None

    def _state_key(self, context, reference, *, error_type):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(reference) is not PackageReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise error_type()
        return context.workspace_reference.identifier, reference.identifier

    def _existing_workspace(self, context, *, error_type):
        try:
            _validate_context(context)
            root = self.workspace_manager.root
            root_state = _directory_state(root, error_type=error_type)
            workspace = self.workspace_manager.handle(context.workspace_reference)
            path_state = _directory_state(workspace.path, error_type=error_type)
            if root_state.st_dev != path_state.st_dev:
                raise error_type()
            _assert_private_mode(root, 0o700, error_type=error_type)
            _assert_private_mode(workspace.path, 0o700, error_type=error_type)
            return workspace
        except Phase2D2EngineError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, UnsafeWorkspacePath):
            raise error_type() from None

    def _package_parent(self, context, *, create, error_type):
        workspace = self._existing_workspace(context, error_type=error_type)
        try:
            parent = workspace.system_area_path(WorkspaceArea.PACKAGE)
            if os.path.lexists(parent) and path_is_link_like(parent):
                raise error_type()
            if create:
                parent.mkdir(mode=0o700, exist_ok=True)
            state = _directory_state(parent, error_type=error_type)
            _same_device(workspace.path, parent, error_type=error_type)
            _apply_private_mode(parent, 0o700, error_type=error_type)
            if _identity(_directory_state(parent, error_type=error_type)) != _identity(state):
                raise error_type()
            return workspace, parent
        except Phase2D2EngineError:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWorkspacePath):
            raise error_type() from None

    def _package_directory(self, context, reference, *, require_exists, error_type):
        workspace, parent = self._package_parent(
            context,
            create=False,
            error_type=error_type,
        )
        try:
            directory = workspace.system_area_path(
                WorkspaceArea.PACKAGE,
                generated_identifier=reference.identifier,
            )
            if os.path.lexists(directory) and path_is_link_like(directory):
                raise error_type()
            if require_exists:
                _directory_state(directory, error_type=error_type)
                _same_device(parent, directory, error_type=error_type)
            return directory
        except Phase2D2EngineError:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWorkspacePath):
            raise error_type() from None

    def _create_directory(self, context, reference):
        _workspace, parent = self._package_parent(
            context,
            create=True,
            error_type=PackageCreationError,
        )
        directory = self._package_directory(
            context,
            reference,
            require_exists=False,
            error_type=PackageCreationError,
        )
        absent_before_creation = not os.path.lexists(directory)
        directory_identity = None
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
            state = _directory_state(directory, error_type=PackageCreationError)
            directory_identity = _identity(state)
            _same_device(parent, directory, error_type=PackageCreationError)
            _apply_private_mode(directory, 0o700, error_type=PackageCreationError)
            if (
                _identity(_directory_state(directory, error_type=PackageCreationError))
                != directory_identity
            ):
                raise PackageCreationError()
            self._run_hook("after_package_directory_creation")
            return directory, directory_identity
        except BaseException as exc:
            cleanup_incomplete = False
            try:
                if os.path.lexists(directory):
                    if not absent_before_creation or directory_identity is None:
                        raise PackageCleanupError()
                    removed = self._remove_empty_directory(
                        directory,
                        expected_identity=directory_identity,
                        error_type=PackageCleanupError,
                    )
                    if not removed or os.path.lexists(directory):
                        raise PackageCleanupError()
            except BaseException:
                cleanup_incomplete = True
            if isinstance(exc, Phase2D2EngineError):
                exc.cleanup_incomplete = bool(
                    cleanup_incomplete
                    or getattr(exc, "cleanup_incomplete", False)
                )
                raise
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                try:
                    exc.cleanup_incomplete = cleanup_incomplete
                except Exception:
                    pass
                raise
            raise PackageCreationError(
                cleanup_incomplete=cleanup_incomplete
            ) from None

    @staticmethod
    def _remove_empty_directory(directory, *, expected_identity, error_type):
        if directory is None or not os.path.lexists(directory):
            return False
        current = _directory_state(directory, error_type=error_type)
        if _identity(current) != expected_identity:
            raise error_type()
        with os.scandir(directory) as entries:
            if next(entries, None) is not None:
                return False
        os.rmdir(directory)
        if os.path.lexists(directory):
            raise error_type()
        return True

    @staticmethod
    def _cleanup_owned_publication(*, paths, expected_identity):
        unique_paths = []
        for path in paths:
            if path is not None and path not in unique_paths and os.path.lexists(path):
                unique_paths.append(path)
        if not unique_paths:
            return False
        remaining_links = len(unique_paths)
        for path in unique_paths:
            _owned_regular_file_state(
                path,
                expected_identity=expected_identity,
                expected_link_count=remaining_links,
                error_type=PackageCleanupError,
            )
            os.unlink(path)
            if os.path.lexists(path):
                raise PackageCleanupError()
            remaining_links -= 1
        return True

    def _read_manifest(self, *, context, result):
        manifest = result.manifest
        digest = hashlib.sha256()
        byte_count = 0
        chunks = []
        try:
            with self.manifest_provider.open_manifest(
                context=context,
                reference=manifest.reference,
            ) as reader:
                while True:
                    chunk = reader.read(_PACKAGE_CHUNK_BYTES)
                    if type(chunk) is not bytes or len(chunk) > _PACKAGE_CHUNK_BYTES:
                        raise PackageContentMismatch()
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > manifest.byte_count or byte_count > _MAXIMUM_MANIFEST_BYTES:
                        raise PackageContentMismatch()
                    chunks.append(chunk)
                    digest.update(chunk)
        except PackageContentMismatch:
            raise
        except CanonicalManifestNotFound:
            raise PackageContentMismatch() from None
        except Exception:
            raise PackageContentMismatch() from None
        raw = b"".join(chunks)
        if byte_count != manifest.byte_count or digest.hexdigest() != manifest.sha256:
            raise PackageContentMismatch()
        return raw, _strict_json_document(raw)

    def _source_reader(self, *, context, phase2d1_result, entry):
        if entry.source_kind == "manifest":
            raise PackageValidationError()
        if entry.source_kind in {"component-records", "component-media-index"}:
            component = phase2d1_result.component_exports[entry.source_index]
            stream = (
                ComponentExportStream.RECORDS
                if entry.source_kind == "component-records"
                else ComponentExportStream.MEDIA_INDEX
            )
            return self.component_exporter.open_component_export(
                context=context,
                reference=component.reference,
                stream=stream,
            )
        if entry.source_kind == "media":
            media = phase2d1_result.media_captures[entry.source_index]
            return self.media_capture_provider.open_media_capture(
                context=context,
                reference=media.reference,
            )
        raise PackageValidationError()

    def _prevalidate_sources(self, *, context, result):
        try:
            for component in result.component_exports:
                self.component_exporter.validate_component_export_reference_evidence(
                    context=context,
                    reference=component.reference,
                )
        except Exception:
            raise PackageValidationError() from None

    def _capacity_check(self, *, parent, entries, planned_payload_bytes):
        overhead_allowance = 1024**2 + len(entries) * 2048
        upper_bound = (
            planned_payload_bytes
            + entries[0].byte_count
            + overhead_allowance
        )
        if upper_bound > _MAXIMUM_PACKAGE_BYTES:
            raise PackageValidationError()
        try:
            free = self.disk_usage_provider(parent).free
        except Exception:
            raise PackageCreationError() from None
        required = max(
            _MINIMUM_FREE_BYTES,
            int(upper_bound * _CAPACITY_HEADROOM_MULTIPLIER),
        )
        if type(free) is not int or free < required:
            raise PackageCreationError()

    def _write_package(
        self,
        *,
        file_object,
        context,
        phase2d1_result,
        manifest_bytes,
        entries,
        deadline,
    ):
        try:
            with zipfile.ZipFile(
                file_object,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
                strict_timestamps=True,
            ) as archive:
                archive.comment = b""
                for entry in entries:
                    self._check_deadline(deadline)
                    source_context = (
                        _bytes_reader(manifest_bytes)
                        if entry.source_kind == "manifest"
                        else self._source_reader(
                            context=context,
                            phase2d1_result=phase2d1_result,
                            entry=entry,
                        )
                    )
                    digest = hashlib.sha256()
                    byte_count = 0
                    with source_context as reader:
                        with archive.open(
                            _zip_info(entry.path),
                            mode="w",
                            force_zip64=True,
                        ) as destination:
                            while True:
                                self._check_deadline(deadline)
                                chunk = reader.read(_PACKAGE_CHUNK_BYTES)
                                if (
                                    type(chunk) is not bytes
                                    or len(chunk) > _PACKAGE_CHUNK_BYTES
                                ):
                                    raise PackageContentMismatch()
                                if not chunk:
                                    break
                                byte_count += len(chunk)
                                if byte_count > entry.byte_count:
                                    raise PackageContentMismatch()
                                digest.update(chunk)
                                if destination.write(chunk) != len(chunk):
                                    raise PackageCreationError()
                    if (
                        byte_count != entry.byte_count
                        or digest.hexdigest() != entry.sha256
                    ):
                        raise PackageContentMismatch()
                    self._run_hook("after_package_entry")
        except (ComponentExportNotFound, MediaObjectNotFound):
            raise PackageContentMismatch() from None
        except Phase2D2EngineError:
            raise
        except Exception:
            raise PackageCreationError() from None

    def _verify_archive(self, *, path, entries, deadline, error_type):
        try:
            with zipfile.ZipFile(path, mode="r", allowZip64=True) as archive:
                if archive.comment != b"":
                    raise error_type()
                infos = archive.infolist()
                if [info.filename for info in infos] != [entry.path for entry in entries]:
                    raise error_type()
                if len({info.filename for info in infos}) != len(infos):
                    raise error_type()
                for info, entry in zip(infos, entries, strict=True):
                    self._check_deadline(deadline, error_type=error_type)
                    permissions = (info.external_attr >> 16) & 0o777
                    if (
                        info.filename != entry.path
                        or info.date_time != DETERMINISTIC_ZIP_TIMESTAMP
                        or info.compress_type != zipfile.ZIP_STORED
                        or info.file_size != entry.byte_count
                        or info.compress_size != entry.byte_count
                        or info.comment != b""
                        or info.create_system != 3
                        or info.internal_attr != 0
                        or info.is_dir()
                        or info.flag_bits != 0
                        or permissions != 0o600
                    ):
                        raise error_type()
                    digest = hashlib.sha256()
                    byte_count = 0
                    with archive.open(info, mode="r") as reader:
                        while True:
                            self._check_deadline(deadline, error_type=error_type)
                            chunk = reader.read(_PACKAGE_CHUNK_BYTES)
                            if type(chunk) is not bytes or len(chunk) > _PACKAGE_CHUNK_BYTES:
                                raise error_type()
                            if not chunk:
                                break
                            byte_count += len(chunk)
                            if byte_count > entry.byte_count:
                                raise error_type()
                            digest.update(chunk)
                    if byte_count != entry.byte_count or digest.hexdigest() != entry.sha256:
                        raise error_type()
        except Phase2D2EngineError:
            raise
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise error_type() from None

    def _hash_file(self, path, *, expected_identity, deadline, error_type):
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
                self._check_deadline(deadline, error_type=error_type)
                chunk = os.read(descriptor, _PACKAGE_CHUNK_BYTES)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > _MAXIMUM_PACKAGE_BYTES:
                    raise error_type()
                digest.update(chunk)
        except Phase2D2EngineError:
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
        if context != evidence.context:
            raise error_type()
        directory = self._package_directory(
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
            if {entry.name for entry in contents} != {PACKAGE_FILE_NAME}:
                raise error_type()
        path = contained_path(directory, directory / PACKAGE_FILE_NAME)
        current = _regular_file_state(path, error_type=error_type)
        if (
            evidence.file_identity is None
            or _identity(current) != evidence.file_identity
            or current.st_dev != evidence.directory_identity[0]
            or current.st_size != evidence.result.byte_count
        ):
            raise error_type()
        _assert_private_mode(path, 0o600, error_type=error_type)
        deadline = self.monotonic() + _PACKAGE_TIMEOUT_SECONDS
        byte_count, digest = self._hash_file(
            path,
            expected_identity=evidence.file_identity,
            deadline=deadline,
            error_type=error_type,
        )
        if (
            byte_count != evidence.result.byte_count
            or digest != evidence.result.plaintext_sha256
        ):
            raise error_type()
        return directory, path

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, (PackageValidationError, PackageContentMismatch)):
            return exc
        if isinstance(exc, Phase2D2EngineError):
            return PackageCreationError(
                cleanup_incomplete=getattr(exc, "cleanup_incomplete", False)
            )
        return PackageCreationError()

    def build_package(self, request: PackageBuildRequest) -> PackageBuildResult:
        directory = None
        directory_identity = None
        part_path = None
        final_path = None
        file_identity = None
        file_object = None
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_incomplete = False
        deadline = self.monotonic() + _PACKAGE_TIMEOUT_SECONDS

        try:
            if type(request) is not PackageBuildRequest:
                raise PackageValidationError()
            context = _validate_context(request.context)
            phase2d1_result = _validate_phase2d1_result(request.phase2d1_result)
            self._prevalidate_sources(context=context, result=phase2d1_result)
            manifest_bytes, document = self._read_manifest(
                context=context,
                result=phase2d1_result,
            )
            entries, planned_payload_bytes = _manifest_entries(
                context=context,
                phase2d1_result=phase2d1_result,
                document=document,
                manifest_bytes=manifest_bytes,
            )
            reference = self._new_reference()
            key = self._state_key(context, reference, error_type=PackageCreationError)
            with self._state_lock:
                if key in self._published or key in self._cleaned:
                    raise PackageCreationError()
            directory, directory_identity = self._create_directory(context, reference)
            _workspace, parent = self._package_parent(
                context,
                create=False,
                error_type=PackageCreationError,
            )
            self._capacity_check(
                parent=parent,
                entries=entries,
                planned_payload_bytes=planned_payload_bytes,
            )
            part_path = contained_path(
                directory,
                directory / f".{PACKAGE_FILE_NAME}.{uuid.uuid4().hex}.part",
            )
            final_path = contained_path(directory, directory / PACKAGE_FILE_NAME)
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(part_path, flags, 0o600)
            current = os.fstat(descriptor)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or current.st_dev != directory_identity[0]
            ):
                os.close(descriptor)
                raise PackageCreationError()
            file_identity = _identity(current)
            _apply_private_descriptor_mode(
                descriptor,
                part_path,
                0o600,
                error_type=PackageCreationError,
            )
            file_object = os.fdopen(descriptor, "w+b", closefd=True)
            self._write_package(
                file_object=file_object,
                context=context,
                phase2d1_result=phase2d1_result,
                manifest_bytes=manifest_bytes,
                entries=entries,
                deadline=deadline,
            )
            file_object.flush()
            os.fsync(file_object.fileno())
            file_object.close()
            file_object = None
            current = _regular_file_state(part_path, error_type=PackageCreationError)
            if _identity(current) != file_identity or current.st_size <= 0:
                raise PackageCreationError()
            self._verify_archive(
                path=part_path,
                entries=entries,
                deadline=deadline,
                error_type=PackageCreationError,
            )
            byte_count, plaintext_sha256 = self._hash_file(
                part_path,
                expected_identity=file_identity,
                deadline=deadline,
                error_type=PackageCreationError,
            )
            if byte_count <= 0 or byte_count > _MAXIMUM_PACKAGE_BYTES:
                raise PackageCreationError()
            self._run_hook("before_package_publication")
            os.link(part_path, final_path, follow_symlinks=False)
            linked_part = _owned_regular_file_state(
                part_path,
                expected_identity=file_identity,
                expected_link_count=2,
                error_type=PackageCreationError,
            )
            linked_final = _owned_regular_file_state(
                final_path,
                expected_identity=file_identity,
                expected_link_count=2,
                error_type=PackageCreationError,
            )
            if (
                _identity(linked_part) != file_identity
                or _identity(linked_final) != file_identity
                or linked_part.st_nlink != 2
                or linked_final.st_nlink != 2
            ):
                raise PackageCreationError()
            self._run_hook("after_package_publication_link")
            os.unlink(part_path)
            part_path = None
            self._run_hook("after_package_part_unlink")
            final = _regular_file_state(final_path, error_type=PackageCreationError)
            if _identity(final) != file_identity or final.st_nlink != 1:
                raise PackageCreationError()
            candidate = PackageBuildResult(
                reference=reference,
                byte_count=byte_count,
                plaintext_sha256=plaintext_sha256,
                entry_count=len(entries),
                payload_set_sha256=phase2d1_result.manifest.payload_set_sha256,
                format_identifier=PACKAGE_FORMAT_IDENTIFIER,
                created_at=phase2d1_result.manifest.created_at,
                provider_identifier=DETERMINISTIC_PACKAGE_PROVIDER_IDENTIFIER,
            )
            evidence = _PublishedPackage(
                context=context,
                result=candidate,
                directory_identity=directory_identity,
                file_identity=file_identity,
            )
            self._validate_published(
                context=context,
                reference=reference,
                evidence=evidence,
                error_type=PackageCreationError,
            )
            self._run_hook("before_package_result_return")
            with self._state_lock:
                if key in self._published or key in self._cleaned:
                    raise PackageCreationError()
                self._published[key] = evidence
            result = candidate
        except BaseException as exc:
            if isinstance(exc, Exception):
                safe_error = self._safe_error(exc)
            else:
                abort_error = exc
                abort_traceback = exc.__traceback__
        finally:
            if result is None:
                if file_object is not None:
                    try:
                        file_object.close()
                    except BaseException:
                        cleanup_incomplete = True
                try:
                    if file_identity is not None:
                        self._cleanup_owned_publication(
                            paths=(part_path, final_path),
                            expected_identity=file_identity,
                        )
                    elif any(
                        path is not None and os.path.lexists(path)
                        for path in (part_path, final_path)
                    ):
                        raise PackageCleanupError()
                except BaseException:
                    cleanup_incomplete = True
                if directory is not None and directory_identity is not None:
                    try:
                        removed = self._remove_empty_directory(
                            directory,
                            expected_identity=directory_identity,
                            error_type=PackageCleanupError,
                        )
                        if not removed and os.path.lexists(directory):
                            cleanup_incomplete = True
                    except BaseException:
                        cleanup_incomplete = True

        if abort_error is not None:
            try:
                abort_error.cleanup_incomplete = bool(
                    cleanup_incomplete
                    or getattr(abort_error, "cleanup_incomplete", False)
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
            raise PackageCreationError(cleanup_incomplete=cleanup_incomplete)
        return result

    def validate_package_evidence(self, *, context, result) -> bool:
        if (
            type(result) is not PackageBuildResult
            or type(result.reference) is not PackageReference
        ):
            raise PackageValidationError()
        key = self._state_key(context, result.reference, error_type=PackageValidationError)
        with self._state_lock:
            evidence = self._published.get(key)
        if evidence is None or evidence.context != context or evidence.result != result:
            raise PackageValidationError()
        self._validate_published(
            context=context,
            reference=result.reference,
            evidence=evidence,
            error_type=PackageValidationError,
        )
        return True

    @contextmanager
    def open_package(self, *, context, reference):
        key = self._state_key(context, reference, error_type=PackageNotFound)
        with self._state_lock:
            evidence = self._published.get(key)
        if evidence is None or evidence.file_identity is None:
            raise PackageNotFound()
        directory, path = self._validate_published(
            context=context,
            reference=reference,
            evidence=evidence,
            error_type=PackageNotFound,
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
                or opened.st_size != evidence.result.byte_count
            ):
                raise PackageNotFound()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            reader = _OpaquePackageReader(raw_file)
            yield reader
        except PackageNotFound:
            raise
        except OSError:
            raise PackageNotFound() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            close_error = None
            if reader is not None:
                try:
                    reader.close()
                except BaseException as exc:
                    if not active_exception:
                        close_error = exc
            elif raw_file is not None:
                try:
                    raw_file.close()
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
                    _identity(_directory_state(directory, error_type=PackageNotFound))
                    != evidence.directory_identity
                ):
                    raise PackageNotFound()
                self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    error_type=PackageNotFound,
                )
            except BaseException as exc:
                if not active_exception:
                    close_error = exc
            if close_error is not None and not active_exception:
                if isinstance(close_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise close_error.with_traceback(close_error.__traceback__)
                raise PackageNotFound() from None

    def cleanup_package(self, *, context, reference) -> bool:
        key = self._state_key(context, reference, error_type=PackageCleanupError)
        with self._state_lock:
            if key in self._cleaned:
                if self._cleaned[key] != context:
                    raise PackageCleanupError()
                return True
            evidence = self._published.get(key)
        if evidence is None or evidence.context != context:
            raise PackageCleanupError()
        try:
            if evidence.file_identity is not None:
                directory, path = self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    error_type=PackageCleanupError,
                )
                current = _regular_file_state(path, error_type=PackageCleanupError)
                if _identity(current) != evidence.file_identity:
                    raise PackageCleanupError()
                os.unlink(path)
                if os.path.lexists(path):
                    raise PackageCleanupError()
                updated = replace(evidence, file_identity=None)
                with self._state_lock:
                    if self._published.get(key) != evidence:
                        raise PackageCleanupError()
                    self._published[key] = updated
                evidence = updated
            else:
                directory = self._package_directory(
                    context,
                    reference,
                    require_exists=False,
                    error_type=PackageCleanupError,
                )
                if not os.path.lexists(directory):
                    with self._state_lock:
                        if self._published.get(key) != evidence:
                            raise PackageCleanupError()
                        self._published.pop(key, None)
                        self._cleaned[key] = context
                    return True
                current_directory = _directory_state(
                    directory,
                    error_type=PackageCleanupError,
                )
                if _identity(current_directory) != evidence.directory_identity:
                    raise PackageCleanupError()
                package_path = contained_path(
                    directory,
                    directory / PACKAGE_FILE_NAME,
                )
                if os.path.lexists(package_path):
                    raise PackageCleanupError()

            current_directory = _directory_state(
                directory,
                error_type=PackageCleanupError,
            )
            if _identity(current_directory) != evidence.directory_identity:
                raise PackageCleanupError()
            with os.scandir(directory) as contents:
                if next(contents, None) is not None:
                    raise PackageCleanupError()
            os.rmdir(directory)
            if os.path.lexists(directory):
                raise PackageCleanupError()
            with self._state_lock:
                if self._published.get(key) != evidence:
                    raise PackageCleanupError()
                self._published.pop(key, None)
                self._cleaned[key] = context
            return True
        except PackageCleanupError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, UnsafeWorkspacePath):
            raise PackageCleanupError() from None



__all__ = [
    "DETERMINISTIC_PACKAGE_PROVIDER_IDENTIFIER",
    "DETERMINISTIC_ZIP_TIMESTAMP",
    "DeterministicPackageProvider",
    "PACKAGE_FILE_NAME",
    "PLAINTEXT_PACKAGE_HASH_ALGORITHM",
]
