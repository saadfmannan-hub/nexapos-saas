"""Secure Phase 2D-1 reconciliation, media capture, and manifest coordination."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

from django.utils import timezone

from apps.backups.enums import BackupScope, BackupTrigger, ProductOwner

from .canonical_manifest import (
    CANONICAL_MANIFEST_PROVIDER_IDENTIFIER,
    MANIFEST_SCHEMA_IDENTIFIER,
    CanonicalManifestBuildRequest,
    CanonicalManifestProvider,
    ManifestMediaItem,
    MediaSource,
    PayloadDigest,
    ReconciledComponent,
    calculate_payload_set_sha256,
)
from .context import BackupExecutionContext
from .contracts import (
    CanonicalManifestResult,
    ComponentExportReference,
    ComponentExportResult,
    ManifestReference,
    MediaCaptureReference,
    MediaCaptureResult,
    Phase2D1Request,
    Phase2D1Result,
    SnapshotReference,
    SnapshotResult,
)
from .exceptions import (
    ComponentContentMismatch,
    MediaIndexValidationError,
    MediaStorageNameCollision,
    Phase2D1CoordinationError,
    Phase2D1EngineError,
)
from .logical_export import (
    LOGICAL_EXPORT_PROVIDER_IDENTIFIER,
    ComponentExportStream,
    SQLiteLogicalComponentExporter,
)
from .logical_export_policy import LogicalExportPolicy
from .logical_export_registry import (
    IdentityKind,
    get_logical_export_registry,
)
from .logical_serialization import (
    DETERMINISTIC_ORDERING_VERSION,
    LOGICAL_MEDIA_REFERENCE_SCHEMA,
    LOGICAL_RECORD_SCHEMA,
    canonical_uuid,
    encode_canonical_document,
    validate_media_storage_name,
)
from .media_capture import (
    LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER,
    LocalFilesystemMediaCaptureProvider,
    media_storage_collision_key,
)
from .media_capture_policy import MediaCapturePolicy
from .sqlite_snapshot import SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER
from .workspace import WorkspaceReference

COMPONENT_CONTENT_SCHEMA = "nexa.component-content-digest.v1"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
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
_MAXIMUM_COMPONENT_DURATION_MS = 3_600_000


def _is_aware_datetime(value):
    return isinstance(value, datetime) and timezone.is_aware(value)


def _strict_nonnegative_integer(value, *, maximum):
    return type(value) is int and 0 <= value <= maximum


def _duplicate_rejecting_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_number(_value):
    raise ValueError


def _strict_json_object(raw):
    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise MediaIndexValidationError() from None
    if type(value) is not dict:
        raise MediaIndexValidationError()
    return value


def _component_content_descriptor(
    *,
    component,
    models,
    records,
    record_count,
    media_index,
    media_reference_count,
):
    return {
        "schema": COMPONENT_CONTENT_SCHEMA,
        "component_key": component.key,
        "component_version": component.component_version,
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
                "model": model_label,
                "record_count": count,
            }
            for model_label, count in models
        ],
    }


def _component_content_sha256(**values):
    try:
        encoded = encode_canonical_document(
            _component_content_descriptor(**values),
            trailing_lf=False,
        )
    except Exception:
        raise ComponentContentMismatch() from None
    return hashlib.sha256(encoded).hexdigest()


class Phase2D1Coordinator:
    """Coordinate only immutable Phase 2D-1 outputs; never reopen a snapshot."""

    def __init__(
        self,
        *,
        component_exporter,
        media_capture_provider,
        manifest_provider,
        failure_hook=None,
    ):
        authoritative_registry = get_logical_export_registry()
        if (
            type(component_exporter) is not SQLiteLogicalComponentExporter
            or component_exporter.registry is not authoritative_registry
            or type(media_capture_provider) is not LocalFilesystemMediaCaptureProvider
            or type(manifest_provider) is not CanonicalManifestProvider
            or media_capture_provider.snapshot_provider
            is not component_exporter.snapshot_provider
            or component_exporter.workspace_manager.root
            != media_capture_provider.workspace_manager.root
            or component_exporter.workspace_manager.root
            != manifest_provider.workspace_manager.root
        ):
            raise Phase2D1CoordinationError()
        self.component_exporter = component_exporter
        self.media_capture_provider = media_capture_provider
        self.manifest_provider = manifest_provider
        self.registry = authoritative_registry
        self.failure_hook = failure_hook

    def build(self, request: Phase2D1Request) -> Phase2D1Result:
        component_references = []
        media_results = ()
        manifest_result = None
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_abort = None
        cleanup_abort_traceback = None
        cleanup_incomplete = False

        try:
            if (
                type(request) is Phase2D1Request
                and type(request.component_exports) is tuple
            ):
                enrolled_identifiers = set()
                for component_result in request.component_exports:
                    if (
                        type(component_result) is not ComponentExportResult
                        or type(component_result.reference)
                        is not ComponentExportReference
                        or type(component_result.reference.identifier)
                        is not uuid.UUID
                    ):
                        continue
                    try:
                        self.component_exporter.owns_component_export_reference_evidence(
                            context=request.context,
                            reference=component_result.reference,
                        )
                    except Exception:
                        continue
                    identifier = component_result.reference.identifier
                    if identifier not in enrolled_identifiers:
                        enrolled_identifiers.add(identifier)
                        component_references.append(
                            component_result.reference
                        )
            if (
                type(request) is Phase2D1Request
                and type(request.component_plan) is tuple
                and type(request.component_exports) is tuple
                and len(request.component_plan)
                == len(request.component_exports)
            ):
                seen_component_references = set()
                for component, component_result in zip(
                    request.component_plan,
                    request.component_exports,
                    strict=True,
                ):
                    self.component_exporter.validate_component_export_evidence(
                        context=request.context,
                        snapshot_result=request.snapshot_result,
                        component=component,
                        result=component_result,
                    )
                    identifier = component_result.reference.identifier
                    if identifier in seen_component_references:
                        raise ComponentContentMismatch()
                    seen_component_references.add(identifier)
                self.component_exporter.snapshot_provider.validate_snapshot_evidence(
                    context=request.context,
                    snapshot_result=request.snapshot_result,
                )
            (
                context,
                snapshot_result,
                component_plan,
                component_exports,
                media_policy,
                logical_policy,
            ) = self._validate_request(request)
            if len(component_references) != len(component_exports):
                raise ComponentContentMismatch()

            reconciled_components = []
            media_by_name = {}
            seen_sources = set()
            for ordinal, (component, component_result) in enumerate(
                zip(component_plan, component_exports, strict=True),
                start=1,
            ):
                reconciled_components.append(
                    self._reconcile_component(
                        context=context,
                        component=component,
                        result=component_result,
                        ordinal=ordinal,
                        logical_policy=logical_policy,
                        media_policy=media_policy,
                        media_by_name=media_by_name,
                        seen_sources=seen_sources,
                    )
                )
                self._run_failure_hook("after_component_reconciliation")

            ordered_names = tuple(sorted(media_by_name))
            if len(ordered_names) > media_policy.maximum_objects:
                raise Phase2D1CoordinationError()
            self._validate_storage_name_collisions(ordered_names)
            media_inputs = tuple(
                (storage_name, len(media_by_name[storage_name]))
                for storage_name in ordered_names
            )
            media_results = self.media_capture_provider.capture_media(
                context=context,
                snapshot_result=snapshot_result,
                media_sources=media_inputs,
            )
            self._validate_media_results(
                results=media_results,
                ordered_names=ordered_names,
                media_by_name=media_by_name,
                policy=media_policy,
            )
            self._run_failure_hook("after_media_capture")

            manifest_media = tuple(
                ManifestMediaItem(
                    ordinal=ordinal,
                    package_path=f"media/{ordinal:08d}.bin",
                    capture=capture,
                    sources=tuple(
                        sorted(
                            media_by_name[capture.logical_storage_name],
                            key=lambda item: (
                                item.component_ordinal,
                                item.model,
                                item.identity_canonical_bytes,
                                item.field,
                            ),
                        )
                    ),
                )
                for ordinal, capture in enumerate(media_results, start=1)
            )
            manifest_result = self.manifest_provider.build_manifest(
                CanonicalManifestBuildRequest(
                    context=context,
                    snapshot_result=snapshot_result,
                    component_plan=component_plan,
                    components=tuple(reconciled_components),
                    media=manifest_media,
                )
            )
            self._validate_manifest_result(
                context=context,
                snapshot_result=snapshot_result,
                component_plan=component_plan,
                components=tuple(reconciled_components),
                media=manifest_media,
                result=manifest_result,
                chunk_bytes=media_policy.chunk_bytes,
            )
            self._run_failure_hook("before_coordinator_result_return")
            result = Phase2D1Result(
                component_exports=component_exports,
                media_captures=media_results,
                manifest=manifest_result,
            )
        except BaseException as exc:
            if isinstance(exc, Exception):
                safe_error = self._safe_error(exc)
            else:
                abort_error = exc
                abort_traceback = exc.__traceback__
        finally:
            if result is None and type(request) is Phase2D1Request:
                context = request.context
                if manifest_result is not None:
                    try:
                        if (
                            self.manifest_provider.cleanup_manifest(
                                context=context,
                                reference=manifest_result.reference,
                            )
                            is not True
                        ):
                            cleanup_incomplete = True
                    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                        cleanup_incomplete = True
                        if cleanup_abort is None:
                            cleanup_abort = exc
                            cleanup_abort_traceback = exc.__traceback__
                    except BaseException:
                        cleanup_incomplete = True
                for captured in reversed(media_results):
                    try:
                        if (
                            self.media_capture_provider.cleanup_media_capture(
                                context=context,
                                reference=captured.reference,
                            )
                            is not True
                        ):
                            cleanup_incomplete = True
                    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                        cleanup_incomplete = True
                        if cleanup_abort is None:
                            cleanup_abort = exc
                            cleanup_abort_traceback = exc.__traceback__
                    except BaseException:
                        cleanup_incomplete = True
                for reference in reversed(component_references):
                    try:
                        if (
                            self.component_exporter.cleanup_component_export(
                                context=context,
                                reference=reference,
                                require_exact_evidence=True,
                            )
                            is not True
                        ):
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
                cleanup_incomplete
                or getattr(safe_error, "cleanup_incomplete", False)
            )
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        if result is None:
            raise Phase2D1CoordinationError(
                cleanup_incomplete=cleanup_incomplete,
            )
        return result

    def _validate_request(self, request):
        if type(request) is not Phase2D1Request:
            raise Phase2D1CoordinationError()
        context = request.context
        snapshot = request.snapshot_result
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(context.workspace_reference.identifier) is not uuid.UUID
            or type(context.backup_public_id) is not uuid.UUID
            or type(context.business_public_id) is not uuid.UUID
            or type(context.business_id) is not int
            or context.business_id <= 0
            or type(context.operation_correlation_id) is not uuid.UUID
            or type(context.requested_scope) is not BackupScope
            or type(context.trigger_type) is not BackupTrigger
            or type(context.resolved_products) is not tuple
            or not context.resolved_products
            or any(
                type(product) is not ProductOwner
                for product in context.resolved_products
            )
            or len(context.resolved_products)
            != len(set(context.resolved_products))
            or any(
                type(value) is not str or not value or len(value) > maximum
                for value, maximum in (
                    (context.application_version, 128),
                    (context.backup_format_version, 64),
                    (context.schema_migration_fingerprint, 256),
                    (context.minimum_restore_version, 128),
                    (context.idempotency_key, 512),
                )
            )
            or type(snapshot) is not SnapshotResult
            or type(snapshot.reference) is not SnapshotReference
            or type(snapshot.reference.identifier) is not uuid.UUID
            or snapshot.consistent is not True
            or snapshot.provider_identifier != SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER
            or not _is_aware_datetime(snapshot.created_at)
            or not _is_aware_datetime(snapshot.consistency_cutoff_at)
            or snapshot.consistency_cutoff_at.utcoffset()
            != UTC.utcoffset(None)
            or snapshot.consistency_cutoff_at > snapshot.created_at
            or snapshot.journal_mode != "wal"
        ):
            raise Phase2D1CoordinationError()
        snapshot_numeric = (
            (snapshot.byte_count, 10 * 1024**4),
            (snapshot.page_count, 2**63 - 1),
            (snapshot.page_size, 1024**2),
            (snapshot.schema_version, 2**31 - 1),
            (snapshot.duration_ms, _MAXIMUM_COMPONENT_DURATION_MS),
        )
        if any(
            not _strict_nonnegative_integer(value, maximum=maximum)
            for value, maximum in snapshot_numeric
        ) or (
            snapshot.byte_count <= 0
            or snapshot.page_count <= 0
            or snapshot.page_size
            not in {512, 1024, 2048, 4096, 8192, 16_384, 32_768, 65_536}
            or snapshot.byte_count != snapshot.page_count * snapshot.page_size
        ):
            raise Phase2D1CoordinationError()

        self.registry.validate_complete()
        plan = self.registry.validate_component_plan(
            context=context,
            component_plan=request.component_plan,
            require_full=True,
        )
        if request.component_plan != plan or type(request.component_exports) is not tuple:
            raise Phase2D1CoordinationError()
        if len(request.component_exports) != len(plan):
            raise Phase2D1CoordinationError()

        media_policy = (
            self.media_capture_provider.policy.validated()
            if self.media_capture_provider.policy is not None
            else MediaCapturePolicy.from_settings()
        )
        logical_policy = (
            self.component_exporter.policy.validated()
            if self.component_exporter.policy is not None
            else LogicalExportPolicy.from_settings()
        )
        return (
            context,
            snapshot,
            plan,
            request.component_exports,
            media_policy,
            logical_policy,
        )

    def _validate_component_result(
        self,
        *,
        component,
        result,
        logical_policy,
    ):
        if (
            type(result) is not ComponentExportResult
            or result.component_key != component.key
            or type(result.reference) is not ComponentExportReference
            or type(result.reference.identifier) is not uuid.UUID
            or result.component_version != component.component_version
            or result.provider_identifier != LOGICAL_EXPORT_PROVIDER_IDENTIFIER
            or result.record_schema_version != LOGICAL_RECORD_SCHEMA
            or result.deterministic_ordering_version
            != DETERMINISTIC_ORDERING_VERSION
            or not _is_aware_datetime(result.created_at)
            or type(result.model_counts) is not tuple
        ):
            raise ComponentContentMismatch()
        numeric = (
            (result.row_count, logical_policy.maximum_records_bytes),
            (result.media_count, logical_policy.maximum_media_index_bytes),
            (result.byte_count, logical_policy.maximum_records_bytes),
            (
                result.media_index_byte_count,
                logical_policy.maximum_media_index_bytes,
            ),
            (result.duration_ms, _MAXIMUM_COMPONENT_DURATION_MS),
        )
        if any(
            not _strict_nonnegative_integer(value, maximum=maximum)
            for value, maximum in numeric
        ):
            raise ComponentContentMismatch()
        expected_labels = tuple(
            spec.model_label for spec in self.registry.for_component(component.key)
        )
        if len(result.model_counts) != len(expected_labels):
            raise ComponentContentMismatch()
        normalized = []
        for expected_label, item in zip(
            expected_labels,
            result.model_counts,
            strict=True,
        ):
            if (
                type(item) is not tuple
                or len(item) != 2
                or item[0] != expected_label
                or not _strict_nonnegative_integer(
                    item[1],
                    maximum=logical_policy.maximum_records_bytes,
                )
            ):
                raise ComponentContentMismatch()
            normalized.append(item)
        if sum(count for _label, count in normalized) != result.row_count:
            raise ComponentContentMismatch()
        return tuple(normalized)

    def _reconcile_component(
        self,
        *,
        context,
        component,
        result,
        ordinal,
        logical_policy,
        media_policy,
        media_by_name,
        seen_sources,
    ):
        models = self._validate_component_result(
            component=component,
            result=result,
            logical_policy=logical_policy,
        )
        records = self._hash_records_stream(
            context=context,
            result=result,
            ordinal=ordinal,
            chunk_bytes=media_policy.chunk_bytes,
        )
        media_index, sources = self._read_media_index(
            context=context,
            component=component,
            result=result,
            ordinal=ordinal,
            logical_policy=logical_policy,
            media_policy=media_policy,
        )
        for source in sources:
            source_key = (
                source.component,
                source.model,
                source.identity_canonical_bytes,
                source.field,
            )
            if source_key in seen_sources:
                raise MediaIndexValidationError()
            seen_sources.add(source_key)
            media_by_name.setdefault(source.storage_name, []).append(source)
        content_sha256 = _component_content_sha256(
            component=component,
            models=models,
            records=records,
            record_count=result.row_count,
            media_index=media_index,
            media_reference_count=result.media_count,
        )
        return ReconciledComponent(
            plan_item=component,
            models=models,
            records=records,
            record_count=result.row_count,
            media_index=media_index,
            media_reference_count=result.media_count,
            component_content_sha256=content_sha256,
        )

    def _hash_records_stream(self, *, context, result, ordinal, chunk_bytes):
        digest = hashlib.sha256()
        byte_count = 0
        line_count = 0
        last_byte = None
        try:
            with self.component_exporter.open_component_export(
                context=context,
                reference=result.reference,
                stream=ComponentExportStream.RECORDS,
            ) as reader:
                while True:
                    chunk = reader.read(chunk_bytes)
                    if type(chunk) is not bytes or len(chunk) > chunk_bytes:
                        raise ComponentContentMismatch()
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > result.byte_count:
                        raise ComponentContentMismatch()
                    line_count += chunk.count(b"\n")
                    last_byte = chunk[-1]
                    digest.update(chunk)
        except ComponentContentMismatch:
            raise
        except Exception:
            raise ComponentContentMismatch() from None
        if (
            byte_count != result.byte_count
            or line_count != result.row_count
            or (result.row_count == 0 and byte_count != 0)
            or (result.row_count > 0 and last_byte != 0x0A)
        ):
            raise ComponentContentMismatch()
        return PayloadDigest(
            package_path=f"components/{ordinal:04d}/records.ndjson",
            byte_count=byte_count,
            sha256=digest.hexdigest(),
        )

    def _read_media_index(
        self,
        *,
        context,
        component,
        result,
        ordinal,
        logical_policy,
        media_policy,
    ):
        digest = hashlib.sha256()
        byte_count = 0
        sources = []
        try:
            with self.component_exporter.open_component_export(
                context=context,
                reference=result.reference,
                stream=ComponentExportStream.MEDIA_INDEX,
            ) as reader:
                while True:
                    line = reader.readline(
                        media_policy.media_index_maximum_line_bytes + 1
                    )
                    if type(line) is not bytes:
                        raise MediaIndexValidationError()
                    if not line:
                        break
                    if (
                        len(line) > media_policy.media_index_maximum_line_bytes
                        or line == b"\n"
                        or not line.endswith(b"\n")
                        or line.endswith(b"\r\n")
                    ):
                        raise MediaIndexValidationError()
                    byte_count += len(line)
                    if byte_count > result.media_index_byte_count:
                        raise ComponentContentMismatch()
                    digest.update(line)
                    sources.append(
                        self._parse_media_line(
                            raw_line=line,
                            context=context,
                            component=component,
                            component_ordinal=ordinal,
                            maximum_name_length=logical_policy.maximum_media_name_length,
                        )
                    )
        except (ComponentContentMismatch, MediaIndexValidationError):
            raise
        except Exception:
            raise MediaIndexValidationError() from None
        if (
            byte_count != result.media_index_byte_count
            or len(sources) != result.media_count
        ):
            raise ComponentContentMismatch()
        return (
            PayloadDigest(
                package_path=f"components/{ordinal:04d}/media-index.ndjson",
                byte_count=byte_count,
                sha256=digest.hexdigest(),
            ),
            tuple(sources),
        )

    def _parse_media_line(
        self,
        *,
        raw_line,
        context,
        component,
        component_ordinal,
        maximum_name_length,
    ):
        payload = _strict_json_object(raw_line[:-1])
        if (
            frozenset(payload) != _MEDIA_INDEX_KEYS
            or len(payload) != len(_MEDIA_INDEX_KEYS)
            or payload.get("schema") != LOGICAL_MEDIA_REFERENCE_SCHEMA
            or payload.get("component") != component.key
            or payload.get("tenant_public_id") != str(context.business_public_id)
            or type(payload.get("model")) is not str
            or type(payload.get("field")) is not str
            or type(payload.get("storage_name")) is not str
        ):
            raise MediaIndexValidationError()
        try:
            canonical_line = encode_canonical_document(
                payload,
                trailing_lf=True,
            )
        except Exception:
            raise MediaIndexValidationError() from None
        if canonical_line != raw_line:
            raise MediaIndexValidationError()
        try:
            spec = self.registry.get(payload["model"])
            if (
                spec.component_key != component.key
                or payload["field"] not in spec.media_fields
            ):
                raise MediaIndexValidationError()
            identity = payload["identity"]
            if type(identity) is not dict:
                raise MediaIndexValidationError()
            if spec.identity_kind == IdentityKind.PUBLIC_UUID:
                if frozenset(identity) != {"public_id"} or len(identity) != 1:
                    raise MediaIndexValidationError()
                normalized_public_id = canonical_uuid(identity["public_id"])
                if normalized_public_id != identity["public_id"]:
                    raise MediaIndexValidationError()
                if (
                    spec.model_label == "tenants.Business"
                    and normalized_public_id != str(context.business_public_id)
                ):
                    raise MediaIndexValidationError()
            elif spec.identity_kind == IdentityKind.TENANT_SINGLETON:
                expected_identity = {
                    "singleton_model": spec.model_label,
                    "tenant_public_id": str(context.business_public_id),
                }
                if identity != expected_identity:
                    raise MediaIndexValidationError()
            else:
                raise MediaIndexValidationError()
            storage_name = validate_media_storage_name(
                payload["storage_name"],
                maximum_length=maximum_name_length,
            )
            identity_bytes = encode_canonical_document(
                identity,
                trailing_lf=False,
            )
        except MediaIndexValidationError:
            raise
        except Exception:
            raise MediaIndexValidationError() from None
        return MediaSource(
            component_ordinal=component_ordinal,
            component=component.key,
            model=spec.model_label,
            identity_items=tuple(sorted(identity.items())),
            identity_canonical_bytes=identity_bytes,
            field=payload["field"],
            storage_name=storage_name,
        )

    @staticmethod
    def _validate_storage_name_collisions(ordered_names):
        seen = {}
        for storage_name in ordered_names:
            try:
                collision_key = media_storage_collision_key(storage_name)
            except Exception:
                raise MediaStorageNameCollision() from None
            if collision_key in seen and seen[collision_key] != storage_name:
                raise MediaStorageNameCollision()
            seen[collision_key] = storage_name

    @staticmethod
    def _validate_media_results(*, results, ordered_names, media_by_name, policy):
        if type(results) is not tuple or len(results) != len(ordered_names):
            raise Phase2D1CoordinationError()
        seen_references = set()
        total_bytes = 0
        maximum_duration_ms = min(
            86_400_000,
            int(policy.timeout_seconds * 1000) + 1000,
        )
        for storage_name, result in zip(ordered_names, results, strict=True):
            if (
                type(result) is not MediaCaptureResult
                or type(result.reference) is not MediaCaptureReference
                or type(result.reference.identifier) is not uuid.UUID
                or result.reference.identifier in seen_references
                or result.logical_storage_name != storage_name
                or type(result.source_reference_count) is not int
                or result.source_reference_count != len(media_by_name[storage_name])
                or not _strict_nonnegative_integer(
                    result.byte_count,
                    maximum=policy.maximum_file_bytes,
                )
                or not _SHA256_HEX.fullmatch(result.sha256)
                or not _is_aware_datetime(result.captured_at)
                or not _strict_nonnegative_integer(
                    result.duration_ms,
                    maximum=maximum_duration_ms,
                )
                or result.provider_identifier
                != LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER
            ):
                raise Phase2D1CoordinationError()
            seen_references.add(result.reference.identifier)
            total_bytes += result.byte_count
            if total_bytes > policy.maximum_total_bytes:
                raise Phase2D1CoordinationError()

    def _run_failure_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _validate_manifest_result(
        self,
        *,
        context,
        snapshot_result,
        component_plan,
        components,
        media,
        result,
        chunk_bytes,
    ):
        expected_payload_sha256 = calculate_payload_set_sha256(
            components,
            media,
        )
        if (
            type(result) is not CanonicalManifestResult
            or type(result.reference) is not ManifestReference
            or type(result.reference.identifier) is not uuid.UUID
            or not _strict_nonnegative_integer(
                result.byte_count,
                maximum=1024**3,
            )
            or result.byte_count <= 0
            or type(result.sha256) is not str
            or not _SHA256_HEX.fullmatch(result.sha256)
            or not _strict_nonnegative_integer(
                result.component_count,
                maximum=9999,
            )
            or result.component_count != len(component_plan)
            or not _strict_nonnegative_integer(
                result.unique_media_object_count,
                maximum=99_999_999,
            )
            or result.unique_media_object_count != len(media)
            or not _strict_nonnegative_integer(
                result.total_record_count,
                maximum=2**63 - 1,
            )
            or result.total_record_count
            != sum(component.record_count for component in components)
            or not _strict_nonnegative_integer(
                result.total_media_bytes,
                maximum=10 * 1024**4,
            )
            or result.total_media_bytes
            != sum(item.capture.byte_count for item in media)
            or result.payload_set_sha256 != expected_payload_sha256
            or result.schema_identifier != MANIFEST_SCHEMA_IDENTIFIER
            or result.created_at != snapshot_result.created_at
            or result.provider_identifier
            != CANONICAL_MANIFEST_PROVIDER_IDENTIFIER
        ):
            raise Phase2D1CoordinationError()
        digest = hashlib.sha256()
        byte_count = 0
        last_byte = None
        try:
            with self.manifest_provider.open_manifest(
                context=context,
                reference=result.reference,
            ) as reader:
                while True:
                    chunk = reader.read(chunk_bytes)
                    if type(chunk) is not bytes or len(chunk) > chunk_bytes:
                        raise Phase2D1CoordinationError()
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > result.byte_count:
                        raise Phase2D1CoordinationError()
                    digest.update(chunk)
                    last_byte = chunk[-1]
        except Phase2D1CoordinationError:
            raise
        except Exception:
            raise Phase2D1CoordinationError() from None
        if (
            byte_count != result.byte_count
            or digest.hexdigest() != result.sha256
            or last_byte != 0x0A
        ):
            raise Phase2D1CoordinationError()

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, Phase2D1EngineError):
            return exc
        return Phase2D1CoordinationError()


__all__ = [
    "COMPONENT_CONTENT_SCHEMA",
    "Phase2D1Coordinator",
]
