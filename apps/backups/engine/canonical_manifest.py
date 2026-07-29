"""Canonical Phase 2D-1 manifest construction and private publication.

This module deliberately does not build a package.  It consumes immutable,
already-reconciled logical-stream and media-capture evidence, constructs the
versioned manifest and its independent hash domains, and publishes only the
serialized ``manifest.json`` behind an opaque reference.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.backups.enums import (
    BackupScope,
    BackupTrigger,
    ProductOwner,
)

from .context import BackupExecutionContext
from .contracts import (
    CanonicalManifestResult,
    ComponentExportReference,
    ManifestReference,
    MediaCaptureReference,
    MediaCaptureResult,
    SnapshotReference,
    SnapshotResult,
)
from .exceptions import (
    CanonicalManifestCleanupError,
    CanonicalManifestCreationError,
    CanonicalManifestNotFound,
    CanonicalManifestValidationError,
    Phase2D1EngineError,
    UnsafeWorkspacePath,
)
from .logical_export import LOGICAL_EXPORT_PROVIDER_IDENTIFIER
from .logical_export_registry import (
    IdentityKind,
    get_logical_export_registry,
)
from .logical_serialization import (
    DETERMINISTIC_ORDERING_VERSION,
    LOGICAL_MEDIA_REFERENCE_SCHEMA,
    LOGICAL_RECORD_SCHEMA,
    encode_canonical_document,
    iter_canonical_document,
    validate_media_storage_name,
)
from .pipeline import ComponentPlanItem
from .sqlite_snapshot import SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    contained_path,
    path_is_link_like,
)

MANIFEST_SCHEMA_IDENTIFIER = "nexa.backup-manifest.v1"
MANIFEST_VERSION = "1.0.0"
CANONICAL_JSON_VERSION = "nexa.canonical-json.v1"
HASH_ALGORITHM = "sha256"
PACKAGE_FORMAT_IDENTIFIER = "nexa.zip-store.v1"
PAYLOAD_SET_SCHEMA = "nexa.backup-payload-set.v1"
COMPONENT_CONTENT_SCHEMA = "nexa.component-content-digest.v1"
CANONICAL_MANIFEST_PROVIDER_IDENTIFIER = "canonical-manifest-v1"
MEDIA_CAPTURE_POLICY_IDENTIFIER = "SNAPSHOT_CUTOFF_AND_STABLE_READ_V1"
MISSING_MEDIA_POLICY = "FAIL_BACKUP"
RESTORE_VERIFICATION_STATE = "NOT_VERIFIED"
CAPTURE_STATE = "CAPTURED_AND_HASHED"

MANIFEST_FILE_NAME = "manifest.json"
_MAXIMUM_MANIFEST_BYTES = 1024**3
_MAXIMUM_MEDIA_NAME_LENGTH = 4096
_MAXIMUM_SIGNED_COUNT = 2**63 - 1
_MAXIMUM_DURATION_MS = 86_400_000
_HASH_READ_BYTES = 1024**2
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


@dataclass(frozen=True, slots=True)
class PayloadDigest:
    """Exact future package path and digest evidence for one payload stream."""

    package_path: str
    byte_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ReconciledComponent:
    """One authoritative component after both logical streams were reconciled."""

    plan_item: ComponentPlanItem
    models: tuple[tuple[str, int], ...]
    records: PayloadDigest
    record_count: int
    media_index: PayloadDigest
    media_reference_count: int
    component_content_sha256: str


@dataclass(frozen=True, slots=True)
class MediaSource:
    """Immutable logical source binding retained for a captured media object."""

    component_ordinal: int
    component: str
    model: str
    identity_items: tuple[tuple[str, str], ...]
    identity_canonical_bytes: bytes
    field: str
    storage_name: str


@dataclass(frozen=True, slots=True)
class ManifestMediaItem:
    """One unique captured object and every logical source that references it."""

    ordinal: int
    package_path: str
    capture: MediaCaptureResult
    sources: tuple[MediaSource, ...]


@dataclass(frozen=True, slots=True)
class CanonicalManifestBuildRequest:
    """Complete immutable evidence accepted by the serialized manifest boundary."""

    context: BackupExecutionContext
    snapshot_result: SnapshotResult
    component_plan: tuple[ComponentPlanItem, ...]
    components: tuple[ReconciledComponent, ...]
    media: tuple[ManifestMediaItem, ...]


@dataclass(frozen=True, slots=True)
class _PublishedManifest:
    context: BackupExecutionContext
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int] | None
    byte_count: int
    sha256: str


class _OpaqueManifestReader:
    """Binary reader that does not expose a path or file descriptor."""

    __slots__ = ("__file",)

    def __init__(self, file_object):
        self.__file = file_object

    def read(self, size=-1):
        return self.__file.read(size)

    def readline(self, size=-1):
        return self.__file.readline(size)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.__file)

    def close(self):
        return self.__file.close()

    @property
    def closed(self):
        return self.__file.closed


def _is_aware(value) -> bool:
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _utc_timestamp(value) -> str:
    if not _is_aware(value):
        raise CanonicalManifestValidationError()
    try:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (OverflowError, ValueError):
        raise CanonicalManifestValidationError() from None


def _strict_string(value, *, allow_empty=False, maximum=4096) -> str:
    if type(value) is not str or (not allow_empty and not value) or len(value) > maximum:
        raise CanonicalManifestValidationError()
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise CanonicalManifestValidationError() from None
    return value


def _exact_count(value, *, positive=False, maximum=_MAXIMUM_SIGNED_COUNT) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= maximum:
        raise CanonicalManifestValidationError()
    return value


def _bounded_sum(values) -> int:
    total = 0
    for value in values:
        total += _exact_count(value)
        if total > _MAXIMUM_SIGNED_COUNT:
            raise CanonicalManifestValidationError()
    return total


def _validated_sha256(value) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise CanonicalManifestValidationError()
    return value


def _sha256_bytes(value: bytes) -> str:
    if type(value) is not bytes:
        raise CanonicalManifestValidationError()
    return hashlib.sha256(value).hexdigest()


def _identity_dict(source: MediaSource) -> dict[str, str]:
    if type(source.identity_items) is not tuple or not source.identity_items:
        raise CanonicalManifestValidationError()
    result = {}
    for item in source.identity_items:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
            or not item[0]
            or item[0] in result
        ):
            raise CanonicalManifestValidationError()
        _strict_string(item[0], maximum=128)
        _strict_string(item[1], maximum=4096)
        result[item[0]] = item[1]
    if source.identity_items != tuple(sorted(source.identity_items)):
        raise CanonicalManifestValidationError()
    return result


def _canonical_identity(source: MediaSource) -> bytes:
    try:
        encoded = encode_canonical_document(_identity_dict(source))
    except Phase2D1EngineError:
        raise
    except Exception:
        raise CanonicalManifestValidationError() from None
    if type(source.identity_canonical_bytes) is not bytes or (
        source.identity_canonical_bytes != encoded
    ):
        raise CanonicalManifestValidationError()
    return encoded


def component_content_descriptor(
    *,
    plan_item,
    models,
    records,
    record_count,
    media_index,
    media_reference_count,
):
    """Return the exact domain-separated component digest descriptor."""

    if type(plan_item) is not ComponentPlanItem:
        raise CanonicalManifestValidationError()
    _validate_models(models)
    _validate_payload_digest(records)
    _validate_payload_digest(media_index)
    _exact_count(record_count)
    _exact_count(media_reference_count)
    return {
        "schema": COMPONENT_CONTENT_SCHEMA,
        "component_key": plan_item.key,
        "component_version": plan_item.component_version,
        "record_schema": LOGICAL_RECORD_SCHEMA,
        "deterministic_ordering_version": DETERMINISTIC_ORDERING_VERSION,
        "records": {
            "record_count": record_count,
            "byte_count": records.byte_count,
            "sha256": records.sha256,
        },
        "media_index": {
            "reference_count": media_reference_count,
            "byte_count": media_index.byte_count,
            "sha256": media_index.sha256,
        },
        "models": [
            {
                "model": model,
                "record_count": count,
            }
            for model, count in models
        ],
    }


def calculate_component_content_sha256(
    *,
    plan_item,
    models,
    records,
    record_count,
    media_index,
    media_reference_count,
) -> str:
    """Hash one canonical component descriptor without a trailing LF."""

    try:
        descriptor = component_content_descriptor(
            plan_item=plan_item,
            models=models,
            records=records,
            record_count=record_count,
            media_index=media_index,
            media_reference_count=media_reference_count,
        )
        return _sha256_bytes(encode_canonical_document(descriptor))
    except CanonicalManifestValidationError:
        raise
    except Exception:
        raise CanonicalManifestValidationError() from None


def _validate_payload_digest(value):
    if type(value) is not PayloadDigest:
        raise CanonicalManifestValidationError()
    _strict_string(value.package_path, maximum=256)
    _exact_count(value.byte_count)
    _validated_sha256(value.sha256)
    return value


def _validate_models(value):
    if type(value) is not tuple:
        raise CanonicalManifestValidationError()
    labels = []
    for item in value:
        if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str:
            raise CanonicalManifestValidationError()
        _strict_string(item[0], maximum=256)
        _exact_count(item[1])
        labels.append(item[0])
    if len(labels) != len(set(labels)):
        raise CanonicalManifestValidationError()
    return value


def _component_records_path(ordinal) -> str:
    _exact_count(ordinal, positive=True, maximum=9999)
    return f"components/{ordinal:04d}/records.ndjson"


def _component_media_index_path(ordinal) -> str:
    _exact_count(ordinal, positive=True, maximum=9999)
    return f"components/{ordinal:04d}/media-index.ndjson"


def _media_path(ordinal) -> str:
    _exact_count(ordinal, positive=True, maximum=99_999_999)
    return f"media/{ordinal:08d}.bin"


def _package_collision_key(value) -> str:
    _strict_string(value, maximum=256)
    try:
        return unicodedata.normalize("NFKC", value).casefold()
    except (TypeError, ValueError):
        raise CanonicalManifestValidationError() from None


def _validate_package_paths(paths):
    exact = set()
    portable = set()
    for path in paths:
        _strict_string(path, maximum=256)
        try:
            encoded = path.encode("ascii")
        except UnicodeError:
            raise CanonicalManifestValidationError() from None
        if (
            not encoded
            or path.startswith(("/", "\\"))
            or "\\" in path
            or ":" in path
            or any(segment in {"", ".", ".."} for segment in path.split("/"))
            or path in exact
        ):
            raise CanonicalManifestValidationError()
        collision_key = _package_collision_key(path)
        if collision_key in portable:
            raise CanonicalManifestValidationError()
        exact.add(path)
        portable.add(collision_key)


def payload_set_descriptor(
    components: tuple[ReconciledComponent, ...],
    media: tuple[ManifestMediaItem, ...],
):
    """Return the exact ordered descriptor covering all future payloads."""

    if type(components) is not tuple or type(media) is not tuple:
        raise CanonicalManifestValidationError()
    payloads = []
    for component in components:
        if type(component) is not ReconciledComponent:
            raise CanonicalManifestValidationError()
        _validate_payload_digest(component.records)
        _validate_payload_digest(component.media_index)
        payloads.extend(
            (
                {
                    "kind": "COMPONENT_RECORDS",
                    "package_path": component.records.package_path,
                    "byte_count": component.records.byte_count,
                    "sha256": component.records.sha256,
                },
                {
                    "kind": "COMPONENT_MEDIA_INDEX",
                    "package_path": component.media_index.package_path,
                    "byte_count": component.media_index.byte_count,
                    "sha256": component.media_index.sha256,
                },
            )
        )
    for item in media:
        if type(item) is not ManifestMediaItem:
            raise CanonicalManifestValidationError()
        capture = item.capture
        if type(capture) is not MediaCaptureResult:
            raise CanonicalManifestValidationError()
        payloads.append(
            {
                "kind": "MEDIA",
                "package_path": item.package_path,
                "storage_name": capture.logical_storage_name,
                "byte_count": capture.byte_count,
                "sha256": capture.sha256,
            }
        )
    return {
        "schema": PAYLOAD_SET_SCHEMA,
        "payloads": payloads,
    }


def calculate_payload_set_sha256(
    components: tuple[ReconciledComponent, ...],
    media: tuple[ManifestMediaItem, ...],
) -> str:
    """Hash the expected payload set without a trailing LF."""

    try:
        return _sha256_bytes(
            encode_canonical_document(
                payload_set_descriptor(components, media),
            )
        )
    except CanonicalManifestValidationError:
        raise
    except Exception:
        raise CanonicalManifestValidationError() from None


def _validated_context(context):
    if (
        type(context) is not BackupExecutionContext
        or type(context.backup_public_id) is not uuid.UUID
        or type(context.business_public_id) is not uuid.UUID
        or type(context.business_id) is not int
        or context.business_id <= 0
        or type(context.workspace_reference) is not WorkspaceReference
        or type(context.workspace_reference.identifier) is not uuid.UUID
        or type(context.requested_scope) is not BackupScope
        or type(context.trigger_type) is not BackupTrigger
        or type(context.resolved_products) is not tuple
        or not context.resolved_products
        or any(type(item) is not ProductOwner for item in context.resolved_products)
        or len(context.resolved_products) != len(set(context.resolved_products))
    ):
        raise CanonicalManifestValidationError()
    products = frozenset(context.resolved_products)
    if (
        context.requested_scope == BackupScope.POS
        and products != {ProductOwner.POS}
        or context.requested_scope == BackupScope.WMS
        and products != {ProductOwner.WMS}
        or context.requested_scope == BackupScope.ALL_ENABLED
        and (not products or not products.issubset({ProductOwner.POS, ProductOwner.WMS}))
    ):
        raise CanonicalManifestValidationError()
    version_values = (
        context.application_version,
        context.backup_format_version,
        context.minimum_restore_version,
    )
    if any(
        type(value) is not str or _VERSION_TOKEN_PATTERN.fullmatch(value) is None
        for value in version_values
    ):
        raise CanonicalManifestValidationError()
    if (
        type(context.schema_migration_fingerprint) is not str
        or _SHA256_PATTERN.fullmatch(context.schema_migration_fingerprint) is None
    ):
        raise CanonicalManifestValidationError()
    return context


def _validated_snapshot(snapshot):
    if (
        type(snapshot) is not SnapshotResult
        or type(snapshot.reference) is not SnapshotReference
        or type(snapshot.reference.identifier) is not uuid.UUID
        or snapshot.consistent is not True
        or snapshot.provider_identifier != SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER
        or not _is_aware(snapshot.created_at)
        or not _is_aware(snapshot.consistency_cutoff_at)
    ):
        raise CanonicalManifestValidationError()
    cutoff = snapshot.consistency_cutoff_at
    try:
        if (
            cutoff.utcoffset() != timedelta(0)
            or cutoff > snapshot.created_at
        ):
            raise CanonicalManifestValidationError()
    except (OverflowError, TypeError, ValueError):
        raise CanonicalManifestValidationError() from None
    byte_count = _exact_count(snapshot.byte_count, positive=True)
    page_count = _exact_count(snapshot.page_count, positive=True)
    page_size = _exact_count(snapshot.page_size, positive=True, maximum=65_536)
    _exact_count(snapshot.schema_version)
    _exact_count(snapshot.duration_ms, maximum=3_600_000)
    if byte_count != page_count * page_size:
        raise CanonicalManifestValidationError()
    if (
        page_size
        not in {512, 1024, 2048, 4096, 8192, 16_384, 32_768, 65_536}
        or snapshot.journal_mode != "wal"
    ):
        raise CanonicalManifestValidationError()
    return snapshot


def _validated_plan(context, component_plan):
    if type(component_plan) is not tuple:
        raise CanonicalManifestValidationError()
    registry = get_logical_export_registry()
    try:
        registry.validate_complete()
        plan = registry.validate_component_plan(
            context=context,
            component_plan=component_plan,
            require_full=True,
        )
    except Exception:
        raise CanonicalManifestValidationError() from None
    if plan != component_plan or len(plan) > 9999:
        raise CanonicalManifestValidationError()
    return registry, plan


def _validated_components(registry, plan, components):
    if type(components) is not tuple or len(components) != len(plan) or not components:
        raise CanonicalManifestValidationError()
    references = set()
    validated = []
    for ordinal, (plan_item, component) in enumerate(
        zip(plan, components, strict=True),
        start=1,
    ):
        if (
            type(component) is not ReconciledComponent
            or component.plan_item != plan_item
            or type(component.plan_item) is not ComponentPlanItem
        ):
            raise CanonicalManifestValidationError()
        expected_models = tuple(spec.model_label for spec in registry.for_component(plan_item.key))
        models = _validate_models(component.models)
        if (
            tuple(label for label, _count in models) != expected_models
            or sum(count for _label, count in models) != component.record_count
        ):
            raise CanonicalManifestValidationError()
        _exact_count(component.record_count)
        _exact_count(component.media_reference_count)
        records = _validate_payload_digest(component.records)
        media_index = _validate_payload_digest(component.media_index)
        if (
            records.package_path != _component_records_path(ordinal)
            or media_index.package_path != _component_media_index_path(ordinal)
            or (component.record_count == 0) != (records.byte_count == 0)
            or (component.media_reference_count == 0) != (media_index.byte_count == 0)
        ):
            raise CanonicalManifestValidationError()
        expected_hash = calculate_component_content_sha256(
            plan_item=plan_item,
            models=models,
            records=records,
            record_count=component.record_count,
            media_index=media_index,
            media_reference_count=component.media_reference_count,
        )
        if component.component_content_sha256 != expected_hash:
            raise CanonicalManifestValidationError()
        # Component references are not serialized, but accepting duplicate
        # evidence would weaken the coordinator-to-manifest boundary.  The
        # coordinator places the UUID on the evidence object for this check.
        reference = getattr(component, "reference", None)
        if reference is not None:
            if (
                type(reference) is not ComponentExportReference
                or type(reference.identifier) is not uuid.UUID
                or reference.identifier in references
            ):
                raise CanonicalManifestValidationError()
            references.add(reference.identifier)
        validated.append(component)
    return tuple(validated)


def _canonical_uuid_string(value) -> str:
    if type(value) is not str:
        raise CanonicalManifestValidationError()
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise CanonicalManifestValidationError() from None
    rendered = str(parsed)
    if value != rendered:
        raise CanonicalManifestValidationError()
    return rendered


def _validate_source_identity(*, source, spec, context):
    identity = _identity_dict(source)
    if spec.identity_kind == IdentityKind.PUBLIC_UUID:
        if set(identity) != {"public_id"}:
            raise CanonicalManifestValidationError()
        public_id = _canonical_uuid_string(identity["public_id"])
        if spec.model_label == "tenants.Business" and public_id != str(context.business_public_id):
            raise CanonicalManifestValidationError()
    elif spec.identity_kind == IdentityKind.TENANT_SINGLETON:
        if set(identity) != {"singleton_model", "tenant_public_id"}:
            raise CanonicalManifestValidationError()
        if identity["singleton_model"] != spec.model_label or _canonical_uuid_string(
            identity["tenant_public_id"]
        ) != str(context.business_public_id):
            raise CanonicalManifestValidationError()
    else:
        raise CanonicalManifestValidationError()


def _media_storage_collision_key(value) -> str:
    try:
        segments = value.split("/")
        return "/".join(unicodedata.normalize("NFKC", segment).casefold() for segment in segments)
    except (AttributeError, TypeError, ValueError):
        raise CanonicalManifestValidationError() from None


def _validated_media(*, registry, plan, context, media):
    if type(media) is not tuple or len(media) > 99_999_999:
        raise CanonicalManifestValidationError()
    component_ordinals = {component.key: ordinal for ordinal, component in enumerate(plan, start=1)}
    capture_references = set()
    storage_names = set()
    storage_collision_keys = set()
    source_keys = set()
    references_by_component = {component.key: 0 for component in plan}
    validated_items = []
    try:
        from .media_capture import (
            LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER,
        )
    except ImportError:
        raise CanonicalManifestValidationError() from None

    for expected_ordinal, item in enumerate(media, start=1):
        if (
            type(item) is not ManifestMediaItem
            or item.ordinal != expected_ordinal
            or item.package_path != _media_path(expected_ordinal)
            or type(item.capture) is not MediaCaptureResult
            or type(item.capture.reference) is not MediaCaptureReference
            or type(item.capture.reference.identifier) is not uuid.UUID
            or item.capture.reference.identifier in capture_references
        ):
            raise CanonicalManifestValidationError()
        capture = item.capture
        name = validate_media_storage_name(
            capture.logical_storage_name,
            maximum_length=_MAXIMUM_MEDIA_NAME_LENGTH,
        )
        collision_key = _media_storage_collision_key(name)
        if name in storage_names or collision_key in storage_collision_keys:
            raise CanonicalManifestValidationError()
        capture_references.add(capture.reference.identifier)
        storage_names.add(name)
        storage_collision_keys.add(collision_key)
        _exact_count(capture.byte_count)
        _validated_sha256(capture.sha256)
        _exact_count(capture.source_reference_count, positive=True)
        if (
            not _is_aware(capture.captured_at)
            or type(capture.duration_ms) is not int
            or not 0 <= capture.duration_ms <= _MAXIMUM_DURATION_MS
            or capture.provider_identifier != LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER
        ):
            raise CanonicalManifestValidationError()
        if (
            type(item.sources) is not tuple
            or len(item.sources) != capture.source_reference_count
            or not item.sources
        ):
            raise CanonicalManifestValidationError()

        previous_sort_key = None
        for source in item.sources:
            if (
                type(source) is not MediaSource
                or source.storage_name != name
                or source.component not in component_ordinals
                or source.component_ordinal != component_ordinals[source.component]
            ):
                raise CanonicalManifestValidationError()
            _strict_string(source.model, maximum=256)
            _strict_string(source.field, maximum=128)
            spec = registry.maybe_get(source.model)
            if (
                spec is None
                or spec.component_key != source.component
                or source.field not in spec.media_fields
            ):
                raise CanonicalManifestValidationError()
            identity_bytes = _canonical_identity(source)
            _validate_source_identity(
                source=source,
                spec=spec,
                context=context,
            )
            sort_key = (
                source.component_ordinal,
                source.model,
                identity_bytes,
                source.field,
            )
            if previous_sort_key is not None and sort_key <= previous_sort_key:
                raise CanonicalManifestValidationError()
            previous_sort_key = sort_key
            source_key = (
                source.component,
                source.model,
                identity_bytes,
                source.field,
            )
            if source_key in source_keys:
                raise CanonicalManifestValidationError()
            source_keys.add(source_key)
            references_by_component[source.component] += 1
        validated_items.append(item)

    expected_names = tuple(sorted(storage_names))
    actual_names = tuple(item.capture.logical_storage_name for item in validated_items)
    if actual_names != expected_names:
        raise CanonicalManifestValidationError()
    return tuple(validated_items), references_by_component


def _validate_request(request):
    if type(request) is not CanonicalManifestBuildRequest:
        raise CanonicalManifestValidationError()
    context = _validated_context(request.context)
    snapshot = _validated_snapshot(request.snapshot_result)
    registry, plan = _validated_plan(context, request.component_plan)
    components = _validated_components(registry, plan, request.components)
    media, references_by_component = _validated_media(
        registry=registry,
        plan=plan,
        context=context,
        media=request.media,
    )
    for component in components:
        if references_by_component[component.plan_item.key] != component.media_reference_count:
            raise CanonicalManifestValidationError()
    paths = [MANIFEST_FILE_NAME]
    for component in components:
        paths.extend(
            (
                component.records.package_path,
                component.media_index.package_path,
            )
        )
    paths.extend(item.package_path for item in media)
    _validate_package_paths(paths)
    return context, snapshot, registry, plan, components, media


def _component_manifest_entry(*, ordinal, component):
    plan_item = component.plan_item
    return {
        "ordinal": ordinal,
        "key": plan_item.key,
        "product_owner": plan_item.product_owner.value,
        "component_version": plan_item.component_version,
        "restore_behavior": plan_item.restore_behavior.value,
        "required_component_keys": list(plan_item.required_component_keys),
        "export_order": plan_item.export_order,
        "import_order": plan_item.import_order,
        "record_schema": LOGICAL_RECORD_SCHEMA,
        "media_reference_schema": LOGICAL_MEDIA_REFERENCE_SCHEMA,
        "deterministic_ordering_version": DETERMINISTIC_ORDERING_VERSION,
        "models": [
            {
                "model": model,
                "record_count": count,
            }
            for model, count in component.models
        ],
        "records": {
            "package_path": component.records.package_path,
            "record_count": component.record_count,
            "byte_count": component.records.byte_count,
            "sha256": component.records.sha256,
        },
        "media_index": {
            "package_path": component.media_index.package_path,
            "reference_count": component.media_reference_count,
            "byte_count": component.media_index.byte_count,
            "sha256": component.media_index.sha256,
        },
        "component_content_schema": COMPONENT_CONTENT_SCHEMA,
        "component_content_sha256": component.component_content_sha256,
        "restore_verification_state": RESTORE_VERIFICATION_STATE,
    }


def _media_manifest_entry(item):
    return {
        "ordinal": item.ordinal,
        "storage_name": item.capture.logical_storage_name,
        "package_path": item.package_path,
        "byte_count": item.capture.byte_count,
        "sha256": item.capture.sha256,
        "source_reference_count": item.capture.source_reference_count,
        "sources": [
            {
                "component": source.component,
                "model": source.model,
                "identity": _identity_dict(source),
                "field": source.field,
            }
            for source in item.sources
        ],
        "capture_state": CAPTURE_STATE,
        "restore_verification_state": RESTORE_VERIFICATION_STATE,
    }


def build_manifest_document(request: CanonicalManifestBuildRequest):
    """Validate immutable evidence and return the exact manifest object."""

    context, snapshot, _registry, plan, components, media = _validate_request(request)
    payload_sha256 = calculate_payload_set_sha256(components, media)
    total_records = _bounded_sum(item.record_count for item in components)
    total_references = _bounded_sum(item.media_reference_count for item in components)
    component_records_bytes = _bounded_sum(item.records.byte_count for item in components)
    component_media_index_bytes = _bounded_sum(item.media_index.byte_count for item in components)
    media_bytes = _bounded_sum(item.capture.byte_count for item in media)
    planned_payload_bytes = _bounded_sum(
        (
            component_records_bytes,
            component_media_index_bytes,
            media_bytes,
        )
    )
    manifest = {
        "schema": MANIFEST_SCHEMA_IDENTIFIER,
        "manifest_version": MANIFEST_VERSION,
        "canonical_json_version": CANONICAL_JSON_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "package_format": PACKAGE_FORMAT_IDENTIFIER,
        "backup": {
            "backup_public_id": str(context.backup_public_id),
            "tenant_public_id": str(context.business_public_id),
            "scope": context.requested_scope.value,
            "trigger_type": context.trigger_type.value,
            "included_products": [product.value for product in context.resolved_products],
            "included_component_keys": [item.key for item in plan],
            "application_version": context.application_version,
            "backup_format_version": context.backup_format_version,
            "schema_migration_fingerprint": (context.schema_migration_fingerprint),
            "minimum_restore_version": context.minimum_restore_version,
            "created_timestamp": _utc_timestamp(snapshot.created_at),
        },
        "compatibility": {
            "minimum_restore_version": context.minimum_restore_version,
            "status": "NOT_CHECKED",
            "database_engine_neutral": True,
        },
        "source_consistency": {
            "database_snapshot_state": "CONSISTENT",
            "database_snapshot_created_at": _utc_timestamp(snapshot.created_at),
            "database_consistency_cutoff_at": _utc_timestamp(snapshot.consistency_cutoff_at),
            "logical_export_provider": LOGICAL_EXPORT_PROVIDER_IDENTIFIER,
            "logical_record_schema": LOGICAL_RECORD_SCHEMA,
            "logical_media_reference_schema": (LOGICAL_MEDIA_REFERENCE_SCHEMA),
            "logical_ordering_version": DETERMINISTIC_ORDERING_VERSION,
            "media_capture_policy": MEDIA_CAPTURE_POLICY_IDENTIFIER,
        },
        "components": [
            _component_manifest_entry(
                ordinal=ordinal,
                component=component,
            )
            for ordinal, component in enumerate(components, start=1)
        ],
        "media": [_media_manifest_entry(item) for item in media],
        "totals": {
            "component_count": len(components),
            "model_count": _bounded_sum(len(item.models) for item in components),
            "record_count": total_records,
            "media_reference_count": total_references,
            "unique_media_object_count": len(media),
            "component_records_bytes": component_records_bytes,
            "component_media_index_bytes": component_media_index_bytes,
            "media_bytes": media_bytes,
            "planned_payload_bytes": planned_payload_bytes,
        },
        "payload_set_schema": PAYLOAD_SET_SCHEMA,
        "payload_set_sha256": payload_sha256,
        "missing_media_policy": MISSING_MEDIA_POLICY,
        "missing_media_count": 0,
        "restore_verification_state": RESTORE_VERIFICATION_STATE,
    }
    return manifest


def _regular_file_state(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if path_is_link_like(path) or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
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


def _identity(current) -> tuple[int, int]:
    return current.st_dev, current.st_ino


def _apply_private_mode(path, mode, *, error_type):
    try:
        os.chmod(path, mode)
        current = os.stat(path, follow_symlinks=False)
        if os.name != "nt" and stat.S_IMODE(current.st_mode) != mode:
            raise error_type()
    except Phase2D1EngineError:
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
    except Phase2D1EngineError:
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
    parent_state = _directory_state(parent, error_type=error_type)
    child_state = _directory_state(child, error_type=error_type)
    if parent_state.st_dev != child_state.st_dev:
        raise error_type()


def _hash_descriptor(descriptor, *, byte_limit, error_type):
    digest = hashlib.sha256()
    byte_count = 0
    try:
        while True:
            chunk = os.read(descriptor, _HASH_READ_BYTES)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > byte_limit:
                raise error_type()
            digest.update(chunk)
    except Phase2D1EngineError:
        raise
    except OSError:
        raise error_type() from None
    return byte_count, digest.hexdigest()


class _AtomicManifestWriter:
    """Exclusive private file finalized by no-clobber hard-link publication."""

    def __init__(self, *, directory, directory_identity, failure_hook=None):
        self.directory = Path(directory)
        self.directory_identity = directory_identity
        self.final_path = contained_path(
            self.directory,
            self.directory / MANIFEST_FILE_NAME,
        )
        self.part_path = contained_path(
            self.directory,
            self.directory / f".{MANIFEST_FILE_NAME}.{uuid.uuid4().hex}.part",
        )
        self.failure_hook = failure_hook
        self.file_object = None
        self.file_identity = None
        self.byte_count = 0
        self.digest = hashlib.sha256()
        self.finalized = False
        self._reserve()

    def _run_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _assert_directory(self, *, error_type):
        current = _directory_state(self.directory, error_type=error_type)
        if _identity(current) != self.directory_identity:
            raise error_type()

    def _owned_state(self, path, *, error_type):
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            raise error_type() from None
        if (
            path_is_link_like(path)
            or not stat.S_ISREG(current.st_mode)
            or self.file_identity is None
            or _identity(current) != self.file_identity
        ):
            raise error_type()
        return current

    def _reserve(self):
        descriptor = None
        try:
            self._assert_directory(error_type=CanonicalManifestCreationError)
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(self.part_path, flags, 0o600)
            current = os.fstat(descriptor)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or current.st_dev != self.directory_identity[0]
            ):
                raise CanonicalManifestCreationError()
            self.file_identity = _identity(current)
            _apply_private_descriptor_mode(
                descriptor,
                self.part_path,
                0o600,
                error_type=CanonicalManifestCreationError,
            )
            current = os.fstat(descriptor)
            if _identity(current) != self.file_identity or current.st_nlink != 1:
                raise CanonicalManifestCreationError()
            self.file_object = os.fdopen(descriptor, "wb", closefd=True)
            descriptor = None
            self._run_hook("after_manifest_part_creation")
        except BaseException as exc:
            cleanup_incomplete = False
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    cleanup_incomplete = True
            try:
                self.cleanup_owned()
            except BaseException:
                cleanup_incomplete = True
            if isinstance(exc, Phase2D1EngineError):
                exc.cleanup_incomplete = bool(
                    cleanup_incomplete
                    or getattr(exc, "cleanup_incomplete", False)
                )
                raise
            if isinstance(
                exc,
                (KeyboardInterrupt, SystemExit, GeneratorExit),
            ):
                try:
                    exc.cleanup_incomplete = cleanup_incomplete
                except Exception:
                    pass
                raise
            raise CanonicalManifestCreationError(
                cleanup_incomplete=cleanup_incomplete
            ) from None

    def write(self, payload):
        if (
            type(payload) is not bytes
            or self.file_object is None
            or self.finalized
            or self.byte_count + len(payload) > _MAXIMUM_MANIFEST_BYTES
        ):
            raise CanonicalManifestCreationError()
        try:
            self._run_hook("during_manifest_write")
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = self.file_object.write(view[written:])
                if type(count) is not int or count <= 0 or count > len(view) - written:
                    raise CanonicalManifestCreationError()
                written += count
            self.byte_count += len(payload)
            self.digest.update(payload)
        except Phase2D1EngineError:
            raise
        except OSError:
            raise CanonicalManifestCreationError() from None

    def finalize(self):
        if self.file_object is None or self.file_identity is None or self.finalized:
            raise CanonicalManifestCreationError()
        try:
            self.file_object.flush()
            self._run_hook("after_manifest_flush")
            os.fsync(self.file_object.fileno())
            self._run_hook("after_manifest_fsync")
            current = os.fstat(self.file_object.fileno())
            if (
                _identity(current) != self.file_identity
                or current.st_nlink != 1
                or current.st_size != self.byte_count
            ):
                raise CanonicalManifestCreationError()
            self.file_object.close()
            self.file_object = None
            self._assert_directory(error_type=CanonicalManifestCreationError)
            part = self._owned_state(
                self.part_path,
                error_type=CanonicalManifestCreationError,
            )
            if part.st_nlink != 1 or part.st_size != self.byte_count:
                raise CanonicalManifestCreationError()
            self._run_hook("during_manifest_publication")
            os.link(
                self.part_path,
                self.final_path,
                follow_symlinks=False,
            )
            part = self._owned_state(
                self.part_path,
                error_type=CanonicalManifestCreationError,
            )
            final = self._owned_state(
                self.final_path,
                error_type=CanonicalManifestCreationError,
            )
            if part.st_nlink != 2 or final.st_nlink != 2:
                raise CanonicalManifestCreationError()
            os.unlink(self.part_path)
            final = self._owned_state(
                self.final_path,
                error_type=CanonicalManifestCreationError,
            )
            if final.st_nlink != 1 or final.st_size != self.byte_count:
                raise CanonicalManifestCreationError()
            self.finalized = True
            self._run_hook("after_manifest_publication")
        except Phase2D1EngineError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise CanonicalManifestCreationError() from None

    def cleanup_owned(self):
        close_failed = False
        if self.file_object is not None:
            file_object = self.file_object
            self.file_object = None
            try:
                file_object.close()
            except BaseException:
                close_failed = True
        if self.file_identity is None:
            if close_failed:
                raise CanonicalManifestCleanupError()
            return False
        try:
            self._assert_directory(error_type=CanonicalManifestCleanupError)
            existing = []
            for path in (self.part_path, self.final_path):
                if not os.path.lexists(path):
                    continue
                existing.append(
                    (
                        path,
                        self._owned_state(
                            path,
                            error_type=CanonicalManifestCleanupError,
                        ),
                    )
                )
            if not existing:
                if close_failed:
                    raise CanonicalManifestCleanupError()
                return False
            if any(current.st_nlink != len(existing) for _path, current in existing):
                raise CanonicalManifestCleanupError()
            remaining_links = len(existing)
            for path, _current in existing:
                current = self._owned_state(
                    path,
                    error_type=CanonicalManifestCleanupError,
                )
                if current.st_nlink != remaining_links:
                    raise CanonicalManifestCleanupError()
                os.unlink(path)
                remaining_links -= 1
            if close_failed:
                raise CanonicalManifestCleanupError()
            return True
        except Phase2D1EngineError:
            raise
        except OSError:
            raise CanonicalManifestCleanupError() from None


class CanonicalManifestProvider:
    """Build, open, and exactly clean a private canonical manifest."""

    def __init__(
        self,
        *,
        workspace_manager=None,
        reference_factory=None,
        failure_hook=None,
    ):
        self.workspace_manager = workspace_manager or BackupWorkspaceManager()
        if type(self.workspace_manager) is not BackupWorkspaceManager:
            raise CanonicalManifestValidationError()
        self.reference_factory = reference_factory or (lambda: ManifestReference(uuid.uuid4()))
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
                reference = ManifestReference(reference)
            if (
                type(reference) is not ManifestReference
                or type(reference.identifier) is not uuid.UUID
            ):
                raise TypeError
            return reference
        except (AttributeError, TypeError, ValueError):
            raise CanonicalManifestCreationError() from None

    def _state_key(self, context, reference):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(reference) is not ManifestReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise CanonicalManifestNotFound()
        return (
            context.workspace_reference.identifier,
            reference.identifier,
        )

    def _existing_workspace(self, context, *, error_type):
        try:
            _validated_context(context)
            root = self.workspace_manager.root
            root_state = _directory_state(root, error_type=error_type)
            workspace = self.workspace_manager.handle(context.workspace_reference)
            path = workspace.path
            path_state = _directory_state(path, error_type=error_type)
            if root_state.st_dev != path_state.st_dev:
                raise error_type()
            _assert_private_mode(root, 0o700, error_type=error_type)
            _assert_private_mode(path, 0o700, error_type=error_type)
            if _identity(_directory_state(root, error_type=error_type)) != _identity(
                root_state
            ) or _identity(_directory_state(path, error_type=error_type)) != _identity(path_state):
                raise error_type()
            return workspace
        except Phase2D1EngineError:
            raise
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            UnsafeWorkspacePath,
        ):
            raise error_type() from None

    def _manifest_parent(self, context, *, create, error_type):
        workspace = self._existing_workspace(context, error_type=error_type)
        try:
            path = workspace.system_area_path(WorkspaceArea.MANIFEST)
            if os.path.lexists(path) and path_is_link_like(path):
                raise error_type()
            if create:
                path.mkdir(mode=0o700, exist_ok=True)
            parent_state = _directory_state(path, error_type=error_type)
            _same_device(workspace.path, path, error_type=error_type)
            _apply_private_mode(path, 0o700, error_type=error_type)
            if _identity(_directory_state(path, error_type=error_type)) != _identity(parent_state):
                raise error_type()
            return workspace, path
        except Phase2D1EngineError:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWorkspacePath):
            raise error_type() from None

    def _manifest_directory(
        self,
        context,
        reference,
        *,
        require_exists,
        error_type,
    ):
        workspace, parent = self._manifest_parent(
            context,
            create=False,
            error_type=error_type,
        )
        try:
            path = workspace.system_area_path(
                WorkspaceArea.MANIFEST,
                generated_identifier=reference.identifier,
            )
            if os.path.lexists(path) and path_is_link_like(path):
                raise error_type()
            if require_exists:
                _directory_state(path, error_type=error_type)
                _same_device(parent, path, error_type=error_type)
            return path
        except Phase2D1EngineError:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWorkspacePath):
            raise error_type() from None

    def _create_directory(self, context, reference):
        _workspace, parent = self._manifest_parent(
            context,
            create=True,
            error_type=CanonicalManifestCreationError,
        )
        directory = self._manifest_directory(
            context,
            reference,
            require_exists=False,
            error_type=CanonicalManifestCreationError,
        )
        absent_before_creation = not os.path.lexists(directory)
        directory_identity = None
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
            current = _directory_state(
                directory,
                error_type=CanonicalManifestCreationError,
            )
            directory_identity = _identity(current)
            _same_device(
                parent,
                directory,
                error_type=CanonicalManifestCreationError,
            )
            _apply_private_mode(
                directory,
                0o700,
                error_type=CanonicalManifestCreationError,
            )
            if (
                _identity(
                    _directory_state(
                        directory,
                        error_type=CanonicalManifestCreationError,
                    )
                )
                != directory_identity
            ):
                raise CanonicalManifestCreationError()
            self._run_hook("after_manifest_directory_creation")
            return directory, directory_identity
        except BaseException as exc:
            cleanup_incomplete = False
            try:
                if os.path.lexists(directory):
                    if (
                        not absent_before_creation
                        or directory_identity is None
                    ):
                        raise CanonicalManifestCleanupError()
                    removed = self._remove_empty_directory(
                        directory,
                        expected_identity=directory_identity,
                        error_type=CanonicalManifestCleanupError,
                    )
                    if not removed or os.path.lexists(directory):
                        raise CanonicalManifestCleanupError()
            except BaseException:
                cleanup_incomplete = True
            if isinstance(exc, Phase2D1EngineError):
                exc.cleanup_incomplete = bool(
                    cleanup_incomplete
                    or getattr(exc, "cleanup_incomplete", False)
                )
                raise
            if isinstance(
                exc,
                (KeyboardInterrupt, SystemExit, GeneratorExit),
            ):
                try:
                    exc.cleanup_incomplete = cleanup_incomplete
                except Exception:
                    pass
                raise
            raise CanonicalManifestCreationError(
                cleanup_incomplete=cleanup_incomplete
            ) from None

    @staticmethod
    def _remove_empty_directory(
        directory,
        *,
        expected_identity,
        error_type,
    ):
        if directory is None or not os.path.lexists(directory):
            return False
        current = _directory_state(directory, error_type=error_type)
        if _identity(current) != expected_identity:
            raise error_type()
        with os.scandir(directory) as entries:
            if next(entries, None) is not None:
                return False
        current = _directory_state(directory, error_type=error_type)
        if _identity(current) != expected_identity:
            raise error_type()
        os.rmdir(directory)
        if os.path.lexists(directory):
            raise error_type()
        return True

    def _validate_published(
        self,
        *,
        context,
        reference,
        evidence,
        error_type,
    ):
        if (
            context != evidence.context
        ):
            raise error_type()
        directory = self._manifest_directory(
            context,
            reference,
            require_exists=True,
            error_type=error_type,
        )
        directory_state = _directory_state(directory, error_type=error_type)
        if _identity(directory_state) != evidence.directory_identity:
            raise error_type()
        _assert_private_mode(directory, 0o700, error_type=error_type)
        with os.scandir(directory) as entries:
            names = {entry.name for entry in entries}
        if names != {MANIFEST_FILE_NAME}:
            raise error_type()
        path = contained_path(
            directory,
            directory / MANIFEST_FILE_NAME,
        )
        descriptor = None
        try:
            path_state = _regular_file_state(path, error_type=error_type)
            if (
                evidence.file_identity is None
                or _identity(path_state) != evidence.file_identity
                or path_state.st_dev != evidence.directory_identity[0]
                or path_state.st_size != evidence.byte_count
            ):
                raise error_type()
            _assert_private_mode(path, 0o600, error_type=error_type)
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != evidence.file_identity
                or opened.st_nlink != 1
                or opened.st_dev != evidence.directory_identity[0]
                or opened.st_size != evidence.byte_count
            ):
                raise error_type()
            byte_count, digest = _hash_descriptor(
                descriptor,
                byte_limit=_MAXIMUM_MANIFEST_BYTES,
                error_type=error_type,
            )
            if byte_count != evidence.byte_count or digest != evidence.sha256:
                raise error_type()
            final_file = _regular_file_state(path, error_type=error_type)
            final_directory = _directory_state(
                directory,
                error_type=error_type,
            )
            if (
                _identity(final_file) != evidence.file_identity
                or final_file.st_size != evidence.byte_count
                or _identity(final_directory) != evidence.directory_identity
            ):
                raise error_type()
            return directory, path
        except Phase2D1EngineError:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWorkspacePath):
            raise error_type() from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, CanonicalManifestValidationError):
            return exc
        if isinstance(
            exc,
            (
                CanonicalManifestCreationError,
                CanonicalManifestCleanupError,
            ),
        ):
            return CanonicalManifestCreationError(
                cleanup_incomplete=getattr(
                    exc,
                    "cleanup_incomplete",
                    False,
                )
            )
        return CanonicalManifestCreationError()

    def build_manifest(
        self,
        request: CanonicalManifestBuildRequest,
    ) -> CanonicalManifestResult:
        directory = None
        directory_identity = None
        writer = None
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_abort = None
        cleanup_abort_traceback = None
        cleanup_incomplete = False

        try:
            manifest = build_manifest_document(request)
            payload_set_sha256 = manifest["payload_set_sha256"]
            reference = self._new_reference()
            key = self._state_key(request.context, reference)
            with self._state_lock:
                if key in self._published or key in self._cleaned:
                    raise CanonicalManifestCreationError()
            directory, directory_identity = self._create_directory(
                request.context,
                reference,
            )
            writer = _AtomicManifestWriter(
                directory=directory,
                directory_identity=directory_identity,
                failure_hook=self.failure_hook,
            )
            for chunk in iter_canonical_document(
                manifest,
                trailing_lf=True,
            ):
                writer.write(chunk)
            writer.finalize()
            evidence = _PublishedManifest(
                context=request.context,
                directory_identity=directory_identity,
                file_identity=writer.file_identity,
                byte_count=writer.byte_count,
                sha256=writer.digest.hexdigest(),
            )
            self._validate_published(
                context=request.context,
                reference=reference,
                evidence=evidence,
                error_type=CanonicalManifestCreationError,
            )
            self._run_hook("before_manifest_result_return")
            self._validate_published(
                context=request.context,
                reference=reference,
                evidence=evidence,
                error_type=CanonicalManifestCreationError,
            )
            totals = manifest["totals"]
            candidate_result = CanonicalManifestResult(
                reference=reference,
                byte_count=evidence.byte_count,
                sha256=evidence.sha256,
                component_count=totals["component_count"],
                unique_media_object_count=totals["unique_media_object_count"],
                total_record_count=totals["record_count"],
                total_media_bytes=totals["media_bytes"],
                payload_set_sha256=payload_set_sha256,
                schema_identifier=MANIFEST_SCHEMA_IDENTIFIER,
                created_at=request.snapshot_result.created_at,
                provider_identifier=CANONICAL_MANIFEST_PROVIDER_IDENTIFIER,
            )
            with self._state_lock:
                if key in self._published or key in self._cleaned:
                    raise CanonicalManifestCreationError()
                self._published[key] = evidence
            result = candidate_result
        except BaseException as exc:
            if isinstance(exc, Exception):
                safe_error = self._safe_error(exc)
            else:
                abort_error = exc
                abort_traceback = exc.__traceback__
        finally:
            if result is None:
                if writer is not None:
                    try:
                        writer.cleanup_owned()
                    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                        cleanup_incomplete = True
                        if cleanup_abort is None:
                            cleanup_abort = exc
                            cleanup_abort_traceback = exc.__traceback__
                    except BaseException:
                        cleanup_incomplete = True
                if directory is not None and directory_identity is not None:
                    try:
                        removed = self._remove_empty_directory(
                            directory,
                            expected_identity=directory_identity,
                            error_type=CanonicalManifestCleanupError,
                        )
                        if not removed and os.path.lexists(directory):
                            cleanup_incomplete = True
                    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                        cleanup_incomplete = True
                        if cleanup_abort is None:
                            cleanup_abort = exc
                            cleanup_abort_traceback = exc.__traceback__
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
        if cleanup_abort is not None:
            try:
                cleanup_abort.cleanup_incomplete = True
            except Exception:
                pass
            raise cleanup_abort.with_traceback(cleanup_abort_traceback)
        if safe_error is not None:
            safe_error.cleanup_incomplete = bool(
                cleanup_incomplete or getattr(safe_error, "cleanup_incomplete", False)
            )
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        if result is None:
            raise CanonicalManifestCreationError(cleanup_incomplete=cleanup_incomplete)
        return result

    @contextmanager
    def open_manifest(self, *, context, reference):
        try:
            key = self._state_key(context, reference)
            with self._state_lock:
                evidence = self._published.get(key)
            if evidence is None or evidence.file_identity is None:
                raise CanonicalManifestNotFound()
            directory, path = self._validate_published(
                context=context,
                reference=reference,
                evidence=evidence,
                error_type=CanonicalManifestNotFound,
            )
        except CanonicalManifestNotFound:
            raise
        except Exception:
            raise CanonicalManifestNotFound() from None

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
                or opened.st_dev != evidence.directory_identity[0]
                or opened.st_size != evidence.byte_count
            ):
                raise CanonicalManifestNotFound()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            reader = _OpaqueManifestReader(raw_file)
            yield reader
        except CanonicalManifestNotFound:
            raise
        except OSError:
            raise CanonicalManifestNotFound() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            final_error = None
            cleanup_abort = None
            cleanup_abort_traceback = None

            def record_close_failure(exc):
                nonlocal final_error
                nonlocal cleanup_abort
                nonlocal cleanup_abort_traceback
                if active_exception:
                    return
                if isinstance(
                    exc,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                ):
                    cleanup_abort = exc
                    cleanup_abort_traceback = exc.__traceback__
                else:
                    final_error = CanonicalManifestNotFound()

            if reader is not None:
                try:
                    reader.close()
                except BaseException as exc:
                    record_close_failure(exc)
            elif raw_file is not None:
                try:
                    raw_file.close()
                except BaseException as exc:
                    record_close_failure(exc)
            elif descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    record_close_failure(exc)
            try:
                self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    error_type=CanonicalManifestNotFound,
                )
                if (
                    _identity(
                        _directory_state(
                            directory,
                            error_type=CanonicalManifestNotFound,
                        )
                    )
                    != evidence.directory_identity
                ):
                    final_error = CanonicalManifestNotFound()
            except BaseException as exc:
                record_close_failure(exc)
            if cleanup_abort is not None and not active_exception:
                raise cleanup_abort.with_traceback(cleanup_abort_traceback)
            if final_error is not None and not active_exception:
                raise final_error from None

    def cleanup_manifest(self, *, context, reference) -> bool:
        try:
            key = self._state_key(context, reference)
        except CanonicalManifestNotFound:
            raise CanonicalManifestCleanupError() from None
        with self._state_lock:
            if key in self._cleaned:
                if self._cleaned[key] != context:
                    raise CanonicalManifestCleanupError()
                return True
            evidence = self._published.get(key)
        if evidence is None or context != evidence.context:
            raise CanonicalManifestCleanupError()

        try:
            directory = self._manifest_directory(
                context,
                reference,
                require_exists=True,
                error_type=CanonicalManifestCleanupError,
            )
            directory_state = _directory_state(
                directory,
                error_type=CanonicalManifestCleanupError,
            )
            if _identity(directory_state) != evidence.directory_identity:
                raise CanonicalManifestCleanupError()

            if evidence.file_identity is not None:
                original_evidence = evidence
                _validated_directory, path = self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    error_type=CanonicalManifestCleanupError,
                )
                current = _regular_file_state(
                    path,
                    error_type=CanonicalManifestCleanupError,
                )
                if (
                    _identity(current) != evidence.file_identity
                    or current.st_size != evidence.byte_count
                ):
                    raise CanonicalManifestCleanupError()
                self._run_hook("before_manifest_cleanup_unlink")
                unlink_abort = None
                unlink_traceback = None
                try:
                    os.unlink(path)
                except BaseException as exc:
                    if os.path.lexists(path):
                        raise
                    if not isinstance(exc, Exception):
                        unlink_abort = exc
                        unlink_traceback = exc.__traceback__
                if os.path.lexists(path):
                    raise CanonicalManifestCleanupError()
                evidence = replace(evidence, file_identity=None)
                with self._state_lock:
                    if self._published.get(key) != original_evidence:
                        raise CanonicalManifestCleanupError()
                    self._published[key] = evidence
                if unlink_abort is not None:
                    raise unlink_abort.with_traceback(unlink_traceback)

            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    raise CanonicalManifestCleanupError()
            if (
                _identity(
                    _directory_state(
                        directory,
                        error_type=CanonicalManifestCleanupError,
                    )
                )
                != evidence.directory_identity
            ):
                raise CanonicalManifestCleanupError()
            self._run_hook("before_manifest_cleanup_directory_removal")
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
                raise CanonicalManifestCleanupError()
            with self._state_lock:
                self._published.pop(key, None)
                self._cleaned[key] = context
            if directory_abort is not None:
                raise directory_abort.with_traceback(directory_abort_traceback)
            return True
        except CanonicalManifestCleanupError:
            raise
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            UnsafeWorkspacePath,
        ):
            raise CanonicalManifestCleanupError() from None


__all__ = [
    "CANONICAL_JSON_VERSION",
    "CANONICAL_MANIFEST_PROVIDER_IDENTIFIER",
    "CAPTURE_STATE",
    "COMPONENT_CONTENT_SCHEMA",
    "CanonicalManifestBuildRequest",
    "CanonicalManifestProvider",
    "HASH_ALGORITHM",
    "MANIFEST_FILE_NAME",
    "MANIFEST_SCHEMA_IDENTIFIER",
    "MANIFEST_VERSION",
    "MEDIA_CAPTURE_POLICY_IDENTIFIER",
    "MISSING_MEDIA_POLICY",
    "ManifestMediaItem",
    "MediaSource",
    "PACKAGE_FORMAT_IDENTIFIER",
    "PAYLOAD_SET_SCHEMA",
    "PayloadDigest",
    "RESTORE_VERIFICATION_STATE",
    "ReconciledComponent",
    "build_manifest_document",
    "calculate_component_content_sha256",
    "calculate_payload_set_sha256",
    "component_content_descriptor",
    "payload_set_descriptor",
]
