"""Tenant-scoped logical export from an opaque Phase 2B SQLite snapshot."""

from __future__ import annotations

import math
import os
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from django.apps import apps as django_apps
from django.db import models
from django.utils import timezone

from .context import BackupExecutionContext
from .contracts import (
    ComponentExporter,
    ComponentExportReference,
    ComponentExportRequest,
    ComponentExportResult,
    SnapshotReference,
    SnapshotResult,
)
from .exceptions import (
    ComponentExportCleanupError,
    ComponentExportCreationError,
    ComponentExportLimitExceeded,
    ComponentExportNotFound,
    ComponentExportTimeout,
    ComponentExportValidationError,
    CrossTenantMediaReference,
    LogicalExportEngineError,
    LogicalReferenceResolutionError,
    SnapshotCleanupAfterExportError,
    SnapshotEngineError,
    SnapshotTimeout,
    TenantIsolationViolation,
    UnsafeWorkspacePath,
    UnsupportedLogicalExportField,
)
from .logical_export_policy import LogicalExportPolicy
from .logical_export_registry import (
    IdentityKind,
    LogicalExportRegistry,
    ScalarPolicy,
    get_logical_export_registry,
)
from .logical_serialization import (
    DETERMINISTIC_ORDERING_VERSION,
    LOGICAL_MEDIA_REFERENCE_SCHEMA,
    LOGICAL_RECORD_SCHEMA,
    CanonicalLogicalSerializer,
    canonical_uuid,
)
from .sqlite_snapshot import (
    SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER,
    SQLiteSnapshotProvider,
)
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    contained_path,
    path_is_link_like,
)

LOGICAL_EXPORT_PROVIDER_IDENTIFIER = "sqlite-tenant-logical-export-v1"
RECORDS_FILE_NAME = "records.ndjson"
MEDIA_INDEX_FILE_NAME = "media-index.ndjson"


class ComponentExportStream(StrEnum):
    RECORDS = "RECORDS"
    MEDIA_INDEX = "MEDIA_INDEX"


_STREAM_FILE_NAMES = {
    ComponentExportStream.RECORDS: RECORDS_FILE_NAME,
    ComponentExportStream.MEDIA_INDEX: MEDIA_INDEX_FILE_NAME,
}


def _quote_identifier(value) -> str:
    rendered = str(value)
    if not rendered or "\x00" in rendered:
        raise ComponentExportValidationError()
    return f'"{rendered.replace(chr(34), chr(34) * 2)}"'


def _regular_file_identity(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if path_is_link_like(path) or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise error_type()
    return current.st_dev, current.st_ino


def _directory_identity(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if path_is_link_like(path) or not stat.S_ISDIR(current.st_mode):
        raise error_type()
    return current.st_dev, current.st_ino


def _same_device(parent, child, *, error_type):
    try:
        parent_device = os.stat(parent, follow_symlinks=False).st_dev
        child_device = os.stat(child, follow_symlinks=False).st_dev
    except OSError:
        raise error_type() from None
    if parent_device != child_device:
        raise error_type()


def _apply_private_mode(path, mode, *, error_type):
    try:
        os.chmod(path, mode)
        if os.name != "nt":
            actual = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
            if actual != mode:
                raise error_type()
    except LogicalExportEngineError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise error_type() from None


def _apply_private_mode_descriptor(descriptor, path, mode, *, error_type):
    try:
        before = os.fstat(descriptor)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            _apply_private_mode(path, mode, error_type=error_type)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or not stat.S_ISREG(
            after.st_mode
        ):
            raise error_type()
        if os.name != "nt" and stat.S_IMODE(after.st_mode) != mode:
            raise error_type()
    except LogicalExportEngineError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise error_type() from None


def _assert_private_mode(path, mode, *, error_type):
    if os.name == "nt":
        return
    try:
        actual = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        raise error_type() from None
    if actual != mode:
        raise error_type()


class _BoundedAtomicFile:
    """Exclusive private temporary file finalized under one fixed name."""

    def __init__(
        self,
        *,
        directory,
        final_name,
        byte_limit,
        directory_identity=None,
        stage_prefix=None,
        failure_hook=None,
    ):
        self.directory = Path(directory)
        self._directory_identity = directory_identity or _directory_identity(
            self.directory,
            error_type=ComponentExportCreationError,
        )
        self.final_path = contained_path(
            self.directory,
            self.directory / final_name,
        )
        self.temporary_path = contained_path(
            self.directory,
            self.directory / f".{final_name}.{uuid.uuid4().hex}.part",
        )
        self.byte_limit = int(byte_limit)
        self.byte_count = 0
        self._file = None
        self._identity = None
        self._finalized = False
        self._stage_prefix = stage_prefix
        self._failure_hook = failure_hook
        self._reserve()

    def _run_stage(self, suffix):
        hook = getattr(self, "_failure_hook", None)
        prefix = getattr(self, "_stage_prefix", None)
        if hook is not None and prefix:
            hook(f"{suffix}_{prefix}")

    def _assert_directory(self, *, error_type):
        if (
            _directory_identity(
                self.directory,
                error_type=error_type,
            )
            != self._directory_identity
        ):
            raise error_type()

    def _owned_state(self, path, *, error_type):
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            raise error_type() from None
        if (
            path_is_link_like(path)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != self._identity
        ):
            raise error_type()
        return current

    @property
    def owned_paths(self):
        if self._identity is None:
            return ()
        paths = []
        for path in (self.temporary_path, self.final_path):
            if os.path.lexists(path):
                paths.append(path)
        return tuple(paths)

    @property
    def finalized_identity(self):
        if not self._finalized or self._identity is None:
            raise ComponentExportValidationError()
        return self._identity

    def _reserve(self):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = None
        try:
            self._assert_directory(error_type=ComponentExportCreationError)
            descriptor = os.open(self.temporary_path, flags, 0o600)
            identity = os.fstat(descriptor)
            if (
                not stat.S_ISREG(identity.st_mode)
                or identity.st_nlink != 1
                or identity.st_dev != self._directory_identity[0]
            ):
                raise ComponentExportCreationError()
            self._identity = (identity.st_dev, identity.st_ino)
            _apply_private_mode_descriptor(
                descriptor,
                self.temporary_path,
                0o600,
                error_type=ComponentExportCreationError,
            )
            identity = os.fstat(descriptor)
            if (identity.st_dev, identity.st_ino) != self._identity or identity.st_nlink != 1:
                raise ComponentExportCreationError()
            self._file = os.fdopen(descriptor, "wb", closefd=True)
            descriptor = None
            self._run_stage("after_part_creation")
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
                descriptor = None
            try:
                self._rollback_failed_reservation()
            except BaseException:
                pass
            raise
        except Exception as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                descriptor = None
            error = (
                exc if isinstance(exc, LogicalExportEngineError) else ComponentExportCreationError()
            )
            error.cleanup_incomplete = self._rollback_failed_reservation()
            raise error.with_traceback(None) from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _rollback_failed_reservation(self):
        incomplete = False
        if self._file is not None:
            try:
                self._file.close()
            except BaseException:
                incomplete = True
            self._file = None
        try:
            self._assert_directory(error_type=ComponentExportCleanupError)
        except BaseException:
            return True
        existing = []
        for path in (self.temporary_path, self.final_path):
            try:
                if not os.path.lexists(path):
                    continue
                if self._identity is None:
                    incomplete = True
                    continue
                existing.append(
                    (
                        path,
                        self._owned_state(
                            path,
                            error_type=ComponentExportCleanupError,
                        ),
                    )
                )
            except BaseException:
                incomplete = True
        if existing and any(state.st_nlink != len(existing) for _path, state in existing):
            return True
        remaining_links = len(existing)
        for path, _state in existing:
            try:
                current = self._owned_state(
                    path,
                    error_type=ComponentExportCleanupError,
                )
                if current.st_nlink != remaining_links:
                    incomplete = True
                    continue
                os.unlink(path)
                remaining_links -= 1
            except BaseException:
                incomplete = True
        return incomplete

    def write(self, payload):
        if not isinstance(payload, bytes) or self._file is None:
            raise ComponentExportCreationError()
        if self.byte_count + len(payload) > self.byte_limit:
            raise ComponentExportLimitExceeded()
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = self._file.write(view[written:])
                if type(count) is not int or count <= 0 or count > len(view) - written:
                    raise ComponentExportCreationError()
                written += count
            self.byte_count += len(payload)
        except LogicalExportEngineError:
            raise
        except OSError:
            raise ComponentExportCreationError() from None

    def finalize(self):
        if self._file is None or self._identity is None or self._finalized:
            raise ComponentExportCreationError()
        try:
            self._file.flush()
            self._run_stage("after_flush")
            os.fsync(self._file.fileno())
            self._run_stage("after_fsync")
            current = os.fstat(self._file.fileno())
            if (current.st_dev, current.st_ino) != self._identity or current.st_nlink != 1:
                raise ComponentExportCreationError()
            self._file.close()
            self._file = None
            self._assert_directory(error_type=ComponentExportCreationError)
            temporary = self._owned_state(
                self.temporary_path,
                error_type=ComponentExportCreationError,
            )
            if temporary.st_nlink != 1:
                raise ComponentExportCreationError()
            self._run_stage("during_publication")
            os.link(
                self.temporary_path,
                self.final_path,
                follow_symlinks=False,
            )
            temporary = self._owned_state(
                self.temporary_path,
                error_type=ComponentExportCreationError,
            )
            final = self._owned_state(
                self.final_path,
                error_type=ComponentExportCreationError,
            )
            if temporary.st_nlink != 2 or final.st_nlink != 2:
                raise ComponentExportCreationError()
            self._run_stage("after_link")
            os.unlink(self.temporary_path)
            final = self._owned_state(
                self.final_path,
                error_type=ComponentExportCreationError,
            )
            if final.st_nlink != 1:
                raise ComponentExportCreationError()
            self._run_stage("after_temp_removal")
            self._finalized = True
        except LogicalExportEngineError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise ComponentExportCreationError() from None

    def close(self):
        if self._file is None:
            return
        file_object = self._file
        self._file = None
        file_object.close()

    def cleanup_owned(self):
        close_failed = False
        try:
            self.close()
        except BaseException:
            close_failed = True
        if self._identity is None:
            if close_failed:
                raise ComponentExportCleanupError()
            return False
        try:
            self._assert_directory(error_type=ComponentExportCleanupError)
            existing = []
            for path in (self.temporary_path, self.final_path):
                if not os.path.lexists(path):
                    continue
                existing.append(
                    (
                        path,
                        self._owned_state(
                            path,
                            error_type=ComponentExportCleanupError,
                        ),
                    )
                )
            if not existing:
                if close_failed:
                    raise ComponentExportCleanupError()
                return False
            if any(state.st_nlink != len(existing) for _path, state in existing):
                raise ComponentExportCleanupError()
            remaining_links = len(existing)
            for path, _state in existing:
                current = self._owned_state(
                    path,
                    error_type=ComponentExportCleanupError,
                )
                if current.st_nlink != remaining_links:
                    raise ComponentExportCleanupError()
                os.unlink(path)
                remaining_links -= 1
            if close_failed:
                raise ComponentExportCleanupError()
            return True
        except LogicalExportEngineError:
            raise
        except OSError:
            raise ComponentExportCleanupError() from None


class _OpaqueBinaryReader:
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
        line = self.__file.readline()
        if line:
            return line
        raise StopIteration

    def close(self):
        self.__file.close()

    @property
    def closed(self):
        return self.__file.closed


@dataclass(frozen=True, slots=True)
class _RowQuery:
    sql: str
    parameters: tuple
    value_fields: tuple[tuple[str, str, object], ...]
    oversize_sql: str
    oversize_parameters: tuple


@dataclass(frozen=True, slots=True)
class _PublishedComponentEvidence:
    context: BackupExecutionContext
    snapshot_identifier: uuid.UUID
    component_key: str
    result: ComponentExportResult
    directory_identity: tuple[int, int]
    expected_files: tuple[
        tuple[str, tuple[int, int] | None, int],
        ...,
    ]


class SQLiteLogicalComponentExporter(ComponentExporter):
    """Stream one registered component from the controlled SQLite reader."""

    def __init__(
        self,
        *,
        snapshot_provider=None,
        workspace_manager=None,
        registry=None,
        policy=None,
        serializer=None,
        monotonic=None,
        reference_factory=None,
        failure_hook=None,
    ):
        authoritative_registry = get_logical_export_registry()
        if snapshot_provider is not None and type(snapshot_provider) is not SQLiteSnapshotProvider:
            raise ComponentExportValidationError()
        if registry is not None and (
            type(registry) is not LogicalExportRegistry or registry is not authoritative_registry
        ):
            raise ComponentExportValidationError()
        if policy is not None and type(policy) is not LogicalExportPolicy:
            raise ComponentExportValidationError()
        if serializer is not None and type(serializer) is not CanonicalLogicalSerializer:
            raise ComponentExportValidationError()
        if workspace_manager is None and snapshot_provider is not None:
            workspace_manager = snapshot_provider.workspace_manager
        self.workspace_manager = workspace_manager or BackupWorkspaceManager()
        self.snapshot_provider = snapshot_provider or SQLiteSnapshotProvider(
            workspace_manager=self.workspace_manager
        )
        if (
            type(self.snapshot_provider) is not SQLiteSnapshotProvider
            or self.snapshot_provider.workspace_manager.root != self.workspace_manager.root
        ):
            raise ComponentExportValidationError()
        self.registry = authoritative_registry
        self.policy = policy
        self.serializer = serializer
        self.monotonic = monotonic or time.monotonic
        self.reference_factory = reference_factory or (
            lambda: ComponentExportReference(uuid.uuid4())
        )
        self.failure_hook = failure_hook
        self._published_results = {}
        self._exactly_cleaned_results = {}
        self._published_results_lock = threading.RLock()

    def export_component(self, request: ComponentExportRequest) -> ComponentExportResult:
        if type(self.snapshot_provider) is not SQLiteSnapshotProvider:
            raise ComponentExportValidationError()
        policy = (
            self.policy.validated()
            if self.policy is not None
            else LogicalExportPolicy.from_settings()
        )
        serializer = self.serializer or CanonicalLogicalSerializer(
            maximum_json_depth=policy.maximum_json_depth,
            maximum_media_name_length=policy.maximum_media_name_length,
        )
        context, component, snapshot, component_plan = self._validated_request(request)
        self.registry.validate_complete()
        component_plan = self.registry.validate_component_plan(
            context=context,
            component_plan=component_plan,
        )
        self.registry.validate_component_item(component)
        matching_components = tuple(item for item in component_plan if item.key == component.key)
        if len(matching_components) != 1 or matching_components[0] != component:
            raise ComponentExportValidationError()
        component = matching_components[0]
        specs = self.registry.for_component(component.key)
        reference = self._new_reference()
        started = self._monotonic_value()
        deadline = started + policy.component_timeout_seconds
        directory = None
        directory_identity = None
        records_writer = None
        media_writer = None
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_incomplete = False

        try:
            self._check_deadline(deadline)
            self._run_failure_hook("before_component_directory_creation")
            directory = self._create_component_directory(context, reference)
            directory_identity = _directory_identity(
                directory,
                error_type=ComponentExportCreationError,
            )
            records_writer = _BoundedAtomicFile(
                directory=directory,
                final_name=RECORDS_FILE_NAME,
                byte_limit=policy.maximum_records_bytes,
                directory_identity=directory_identity,
                stage_prefix="records",
                failure_hook=self.failure_hook,
            )
            media_writer = _BoundedAtomicFile(
                directory=directory,
                final_name=MEDIA_INDEX_FILE_NAME,
                byte_limit=policy.maximum_media_index_bytes,
                directory_identity=directory_identity,
                stage_prefix="media_index",
                failure_hook=self.failure_hook,
            )
            self._run_failure_hook("after_component_creation")
            model_counts = []
            row_count = 0
            media_count = 0
            seen_media = set()
            validated_media_names = set()
            with self.snapshot_provider.open_snapshot(
                context=context,
                reference=snapshot,
                _deadline=deadline,
            ) as reader:
                self._validate_tenant_identity(
                    reader=reader,
                    context=context,
                )
                for spec in specs:
                    count, discovered = self._export_model(
                        reader=reader,
                        context=context,
                        component=component,
                        spec=spec,
                        serializer=serializer,
                        policy=policy,
                        deadline=deadline,
                        records_writer=records_writer,
                        media_writer=media_writer,
                        seen_media=seen_media,
                        validated_media_names=validated_media_names,
                    )
                    row_count += count
                    media_count += discovered
                    model_counts.append((spec.model_label, count))
            self._run_failure_hook("before_component_finalize")
            self._check_deadline(deadline)
            records_writer.finalize()
            media_writer.finalize()
            self._run_failure_hook("after_component_finalize")
            expected_files = {
                RECORDS_FILE_NAME: (
                    records_writer.finalized_identity,
                    records_writer.byte_count,
                ),
                MEDIA_INDEX_FILE_NAME: (
                    media_writer.finalized_identity,
                    media_writer.byte_count,
                ),
            }
            self._validate_final_directory(
                directory,
                expected_identity=directory_identity,
                expected_files=expected_files,
            )
            self._check_deadline(deadline)
            candidate_result = ComponentExportResult(
                component_key=component.key,
                reference=reference,
                row_count=row_count,
                media_count=media_count,
                deterministic_ordering_version=DETERMINISTIC_ORDERING_VERSION,
                model_counts=tuple(model_counts),
                byte_count=records_writer.byte_count,
                media_index_byte_count=media_writer.byte_count,
                component_version=component.component_version,
                record_schema_version=LOGICAL_RECORD_SCHEMA,
                created_at=timezone.now(),
                duration_ms=self._duration_ms(started),
                provider_identifier=LOGICAL_EXPORT_PROVIDER_IDENTIFIER,
            )
            self._run_failure_hook("before_component_result_return")
            self._validate_final_directory(
                directory,
                expected_identity=directory_identity,
                expected_files=expected_files,
            )
            evidence_key = (
                context.workspace_reference.identifier,
                reference.identifier,
            )
            evidence = _PublishedComponentEvidence(
                context=context,
                snapshot_identifier=snapshot.identifier,
                component_key=component.key,
                result=candidate_result,
                directory_identity=directory_identity,
                expected_files=tuple(
                    (
                        name,
                        file_identity,
                        byte_count,
                    )
                    for name, (file_identity, byte_count) in sorted(
                        expected_files.items()
                    )
                ),
            )
            with self._published_results_lock:
                if evidence_key in self._published_results:
                    raise ComponentExportCreationError()
                self._published_results[evidence_key] = evidence
            result = candidate_result
        except BaseException as exc:
            abort_error = exc
            abort_traceback = exc.__traceback__
            if isinstance(exc, Exception):
                safe_error = self._safe_error(exc)
                abort_error = None
                abort_traceback = None
        finally:
            if result is None:
                for writer in (media_writer, records_writer):
                    if writer is None:
                        continue
                    try:
                        if writer.cleanup_owned() is not True:
                            cleanup_incomplete = True
                    except BaseException:
                        cleanup_incomplete = True
                if directory is not None:
                    try:
                        self._remove_directory_if_empty(
                            directory,
                            expected_identity=directory_identity,
                        )
                        if os.path.lexists(directory):
                            cleanup_incomplete = True
                    except BaseException:
                        cleanup_incomplete = True

        if abort_error is not None:
            raise abort_error.with_traceback(abort_traceback)
        if safe_error is not None:
            safe_error.cleanup_incomplete = bool(
                cleanup_incomplete or getattr(safe_error, "cleanup_incomplete", False)
            )
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        if result is None:
            raise ComponentExportCreationError(cleanup_incomplete=cleanup_incomplete)
        return result

    @staticmethod
    def _validate_tenant_identity(*, reader, context):
        try:
            business = django_apps.get_model("tenants.Business")
            table = _quote_identifier(business._meta.db_table)
            primary_key = _quote_identifier(business._meta.pk.column)
            public_id = _quote_identifier(business._meta.get_field("public_id").column)
            row = reader.first(
                f"SELECT {public_id} FROM {table} WHERE {primary_key} = ?",
                (context.business_id,),
            )
            if (
                row is None
                or len(row) != 1
                or canonical_uuid(row[0]) != str(context.business_public_id)
            ):
                raise TenantIsolationViolation()
        except TenantIsolationViolation:
            raise
        except (LookupError, LogicalExportEngineError, TypeError, ValueError):
            raise TenantIsolationViolation() from None

    def _validated_request(self, request):
        if type(request) is not ComponentExportRequest:
            raise ComponentExportValidationError()
        context = request.context
        if (
            type(context) is not BackupExecutionContext
            or context.workspace_reference is None
            or isinstance(context.business_id, bool)
            or not isinstance(context.business_id, int)
            or context.business_id <= 0
            or not isinstance(context.business_public_id, uuid.UUID)
            or not isinstance(context.backup_public_id, uuid.UUID)
            or type(request.snapshot) is not SnapshotReference
            or type(request.snapshot.identifier) is not uuid.UUID
            or type(request.component_plan) is not tuple
        ):
            raise ComponentExportValidationError()
        component_plan = request.component_plan
        if not component_plan:
            raise ComponentExportValidationError()
        return context, request.component, request.snapshot, component_plan

    def _new_reference(self):
        try:
            reference = self.reference_factory()
            if type(reference) is uuid.UUID:
                reference = ComponentExportReference(reference)
            if (
                type(reference) is not ComponentExportReference
                or type(
                    reference.identifier,
                )
                is not uuid.UUID
            ):
                raise TypeError
            return reference
        except (TypeError, ValueError, AttributeError):
            raise ComponentExportCreationError() from None

    def _existing_workspace(self, context, *, error_type):
        try:
            root = self.workspace_manager.root
            if not root.exists() or path_is_link_like(root) or not root.is_dir():
                raise error_type()
            root_identity = _directory_identity(root, error_type=error_type)
            workspace = self.workspace_manager.handle(context.workspace_reference)
            path = workspace.path
            if not path.exists() or path_is_link_like(path) or not path.is_dir():
                raise error_type()
            path_identity = _directory_identity(path, error_type=error_type)
            _same_device(root, path, error_type=error_type)
            if (
                _directory_identity(root, error_type=error_type) != root_identity
                or _directory_identity(path, error_type=error_type) != path_identity
            ):
                raise error_type()
            return workspace
        except LogicalExportEngineError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, UnsafeWorkspacePath):
            raise error_type() from None

    def _components_parent(self, context, *, create, error_type):
        workspace = self._existing_workspace(context, error_type=error_type)
        try:
            path = workspace.system_area_path(WorkspaceArea.COMPONENTS)
            if os.path.lexists(path) and path_is_link_like(path):
                raise error_type()
            if create:
                path.mkdir(mode=0o700, exist_ok=True)
            if not path.exists() or not path.is_dir():
                raise error_type()
            identity = _directory_identity(path, error_type=error_type)
            _same_device(workspace.path, path, error_type=error_type)
            _apply_private_mode(path, 0o700, error_type=error_type)
            if _directory_identity(path, error_type=error_type) != identity:
                raise error_type()
            return workspace, path
        except LogicalExportEngineError:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWorkspacePath):
            raise error_type() from None

    def _component_directory(
        self,
        context,
        reference,
        *,
        require_exists,
        error_type,
    ):
        if (
            type(reference) is not ComponentExportReference
            or type(
                reference.identifier,
            )
            is not uuid.UUID
        ):
            raise ComponentExportNotFound()
        workspace, parent = self._components_parent(
            context,
            create=False,
            error_type=error_type,
        )
        try:
            path = workspace.system_area_path(
                WorkspaceArea.COMPONENTS,
                generated_identifier=reference.identifier,
            )
            if os.path.lexists(path) and path_is_link_like(path):
                raise error_type()
            if require_exists:
                if not path.exists() or not path.is_dir():
                    raise ComponentExportNotFound()
                _same_device(parent, path, error_type=error_type)
            return path
        except ComponentExportNotFound:
            raise
        except LogicalExportEngineError:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWorkspacePath):
            raise error_type() from None

    def _create_component_directory(self, context, reference):
        workspace, parent = self._components_parent(
            context,
            create=True,
            error_type=ComponentExportCreationError,
        )
        del workspace
        directory = self._component_directory(
            context,
            reference,
            require_exists=False,
            error_type=ComponentExportCreationError,
        )
        created = False
        directory_identity = None
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
            created = True
            directory_identity = _directory_identity(
                directory,
                error_type=ComponentExportCreationError,
            )
            _same_device(parent, directory, error_type=ComponentExportCreationError)
            _apply_private_mode(
                directory,
                0o700,
                error_type=ComponentExportCreationError,
            )
            if (
                _directory_identity(
                    directory,
                    error_type=ComponentExportCreationError,
                )
                != directory_identity
            ):
                raise ComponentExportCreationError()
            return directory
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            self._rollback_created_directory(
                directory,
                created=created,
                expected_identity=directory_identity,
            )
            raise
        except Exception as exc:
            error = (
                exc if isinstance(exc, LogicalExportEngineError) else ComponentExportCreationError()
            )
            error.cleanup_incomplete = self._rollback_created_directory(
                directory,
                created=created,
                expected_identity=directory_identity,
            )
            raise error.with_traceback(None) from None

    @staticmethod
    def _rollback_created_directory(
        directory,
        *,
        created,
        expected_identity,
    ):
        if not created:
            return False
        try:
            if not os.path.lexists(directory):
                return False
            if (
                expected_identity is None
                or _directory_identity(
                    directory,
                    error_type=ComponentExportCleanupError,
                )
                != expected_identity
            ):
                return True
            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    return True
            os.rmdir(directory)
            return os.path.lexists(directory)
        except BaseException:
            return True

    @staticmethod
    def _query_for_spec(spec, context, *, maximum_row_input_bytes):
        model = django_apps.get_model(spec.model_label)
        table = _quote_identifier(model._meta.db_table)
        selected = []
        descriptors = []

        primary_key = model._meta.pk
        selected.append(_quote_identifier(primary_key.column))
        descriptors.append(("internal_pk", primary_key.name, primary_key))

        if spec.identity_field:
            identity_field = model._meta.get_field(spec.identity_field)
            selected.append(_quote_identifier(identity_field.column))
            descriptors.append(("identity", identity_field.name, identity_field))

        for field_name in spec.scalar_fields:
            field = model._meta.get_field(field_name)
            column = _quote_identifier(field.column)
            if isinstance(field, models.DecimalField):
                selected.append(f"CAST({column} AS TEXT)")
            else:
                selected.append(column)
            descriptors.append(("scalar", field_name, field))
        for json_spec in spec.json_fields:
            field = model._meta.get_field(json_spec.field_name)
            selected.append(_quote_identifier(field.column))
            descriptors.append(("json", json_spec.field_name, json_spec))
        for field_name in spec.media_fields:
            field = model._meta.get_field(field_name)
            selected.append(_quote_identifier(field.column))
            descriptors.append(("media", field_name, field))
        for relation_spec in spec.relation_fields:
            field = model._meta.get_field(relation_spec.field_name)
            selected.append(_quote_identifier(field.column))
            descriptors.append(("relation", relation_spec.field_name, relation_spec))

        if spec.model_label == "tenants.Business":
            public_field = model._meta.get_field("public_id")
            where = (
                f"{_quote_identifier(primary_key.column)} = ? AND "
                f"{_quote_identifier(public_field.column)} = ?"
            )
            parameters = (
                context.business_id,
                context.business_public_id.hex,
            )
        else:
            ownership = model._meta.get_field(spec.ownership_field)
            where = f"{_quote_identifier(ownership.column)} = ?"
            parameters = (context.business_id,)
        if spec.identity_field:
            order_field = model._meta.get_field(spec.identity_field)
        else:
            order_field = primary_key
        size_expression = " + ".join(
            f"COALESCE(length(CAST(({expression}) AS BLOB)), 0)" for expression in selected
        )
        limit = int(maximum_row_input_bytes)
        order_expression = _quote_identifier(order_field.column)
        oversize_sql = (
            f"SELECT 1 FROM {table} " f"WHERE {where} AND ({size_expression}) > ? LIMIT 1"
        )
        sql = (
            f"SELECT {', '.join(selected)} FROM {table} "
            f"WHERE {where} ORDER BY {order_expression} ASC"
        )
        oversize_parameters = (
            *parameters,
            limit,
        )
        return model, _RowQuery(
            sql,
            parameters,
            tuple(descriptors),
            oversize_sql,
            oversize_parameters,
        )

    def _export_model(
        self,
        *,
        reader,
        context,
        component,
        spec,
        serializer,
        policy,
        deadline,
        records_writer,
        media_writer,
        seen_media,
        validated_media_names,
    ):
        model, query = self._query_for_spec(
            spec,
            context,
            maximum_row_input_bytes=min(
                policy.maximum_row_input_bytes,
                policy.maximum_records_bytes,
            ),
        )
        count = 0
        media_count = 0
        oversized = reader.first(
            query.oversize_sql,
            query.oversize_parameters,
        )
        if oversized is not None:
            if oversized != (1,):
                raise ComponentExportValidationError()
            raise ComponentExportLimitExceeded()
        for row in reader.iter_query(
            query.sql,
            query.parameters,
            batch_size=policy.fetch_batch_size,
        ):
            self._run_failure_hook("during_record_iteration")
            self._check_deadline(deadline)
            if len(row) != len(query.value_fields):
                raise ComponentExportValidationError()
            values = {
                name: raw
                for (_kind, name, _policy), raw in zip(
                    query.value_fields,
                    row,
                    strict=True,
                )
            }
            internal_pk = values[model._meta.pk.name]
            identity = self._identity_for_row(
                spec=spec,
                values=values,
                context=context,
            )
            fields = {}
            self._run_failure_hook("during_serialization")
            for field_name in spec.scalar_fields:
                field = model._meta.get_field(field_name)
                fields[field_name] = serializer.scalar(
                    field,
                    values[field_name],
                )
            for field_name, scalar_policy in spec.scalar_policies:
                if scalar_policy == ScalarPolicy.CANONICAL:
                    continue
                if (
                    scalar_policy == ScalarPolicy.OPAQUE_BUSINESS_REFERENCE
                    and field_name == "reference_id"
                    and "stock_movement_business_reference_matches" in spec.validators
                ):
                    continue
                if (
                    scalar_policy == ScalarPolicy.VALIDATED_UUID_SNAPSHOT
                    and "wms_salary_assignment_snapshot_matches" in spec.validators
                ):
                    continue
                raise ComponentExportValidationError()
            json_by_name = {item.field_name: item for item in spec.json_fields}
            for field_name, json_spec in json_by_name.items():
                fields[field_name] = serializer.json(
                    json_spec,
                    values[field_name],
                )
            relation_raw_values = {}
            for relation_spec in spec.relation_fields:
                raw_value = values[relation_spec.field_name]
                relation_raw_values[relation_spec.field_name] = raw_value
                fields[relation_spec.field_name] = self._resolve_relation(
                    reader=reader,
                    context=context,
                    relation_spec=relation_spec,
                    raw_identifier=raw_value,
                )
            for m2m_spec in spec.many_to_many_fields:
                fields[m2m_spec.field_name] = self._resolve_many_to_many(
                    reader=reader,
                    context=context,
                    model=model,
                    internal_pk=internal_pk,
                    m2m_spec=m2m_spec,
                    batch_size=policy.fetch_batch_size,
                    maximum_references=max(
                        1,
                        min(
                            100_000,
                            policy.maximum_records_bytes // 64,
                        ),
                    ),
                )
            for field_name in spec.media_fields:
                raw_name = values[field_name]
                if raw_name is None:
                    fields[field_name] = None
                    continue
                if raw_name == "":
                    fields[field_name] = ""
                    continue
                safe_name = serializer.media_name(raw_name)
                if safe_name not in validated_media_names:
                    self._validate_media_name_tenant_exclusive(
                        reader=reader,
                        context=context,
                        storage_name=safe_name,
                    )
                    validated_media_names.add(safe_name)
                fields[field_name] = safe_name
                media_key = (
                    component.key,
                    spec.model_label,
                    tuple(sorted(identity.items())),
                    field_name,
                    safe_name,
                )
                if media_key not in seen_media:
                    seen_media.add(media_key)
                    media_payload = {
                        "schema": LOGICAL_MEDIA_REFERENCE_SCHEMA,
                        "component": component.key,
                        "model": spec.model_label,
                        "tenant_public_id": str(context.business_public_id),
                        "identity": identity,
                        "field": field_name,
                        "storage_name": safe_name,
                    }
                    self._write_encoded_line(
                        writer=media_writer,
                        serializer=serializer,
                        payload=media_payload,
                        deadline=deadline,
                    )
                    media_count += 1

            self._run_validators(
                validators=spec.validators,
                reader=reader,
                context=context,
                fields=fields,
                relation_raw_values=relation_raw_values,
            )
            record = {
                "schema": LOGICAL_RECORD_SCHEMA,
                "component": component.key,
                "component_version": component.component_version,
                "model": spec.model_label,
                "tenant_public_id": str(context.business_public_id),
                "identity": identity,
                "fields": fields,
            }
            self._write_encoded_line(
                writer=records_writer,
                serializer=serializer,
                payload=record,
                deadline=deadline,
            )
            count += 1
            self._check_deadline(deadline)

        if spec.model_label == "tenants.Business" and count != 1:
            raise TenantIsolationViolation()
        if spec.identity_kind == IdentityKind.TENANT_SINGLETON and count != 1:
            raise TenantIsolationViolation()
        return count, media_count

    def _validate_media_name_tenant_exclusive(
        self,
        *,
        reader,
        context,
        storage_name,
    ):
        try:
            for registered_spec in self.registry.specs:
                if not registered_spec.media_fields:
                    continue
                model = django_apps.get_model(registered_spec.model_label)
                table = _quote_identifier(model._meta.db_table)
                if registered_spec.model_label == "tenants.Business":
                    ownership_column = _quote_identifier(model._meta.pk.column)
                else:
                    ownership = model._meta.get_field(
                        registered_spec.ownership_field
                    )
                    ownership_column = _quote_identifier(ownership.column)
                for field_name in registered_spec.media_fields:
                    media_column = _quote_identifier(
                        model._meta.get_field(field_name).column
                    )
                    row = reader.first(
                        f"SELECT 1 FROM {table} "
                        f"WHERE CAST({media_column} AS BLOB) = CAST(? AS BLOB) "
                        f"AND {ownership_column} IS NOT ? LIMIT 1",
                        (storage_name, context.business_id),
                    )
                    if row is not None:
                        raise CrossTenantMediaReference()
        except CrossTenantMediaReference:
            raise
        except (
            AttributeError,
            LookupError,
            LogicalExportEngineError,
            TypeError,
            ValueError,
        ):
            raise CrossTenantMediaReference() from None

    @staticmethod
    def _identity_for_row(*, spec, values, context):
        if spec.identity_kind == IdentityKind.PUBLIC_UUID:
            public_id = canonical_uuid(values[spec.identity_field])
            if spec.model_label == "tenants.Business" and public_id != str(
                context.business_public_id
            ):
                raise TenantIsolationViolation()
            return {"public_id": public_id}
        if spec.identity_kind == IdentityKind.TENANT_SINGLETON:
            return {
                "singleton_model": spec.model_label,
                "tenant_public_id": str(context.business_public_id),
            }
        raise ComponentExportValidationError()

    def _resolve_relation(
        self,
        *,
        reader,
        context,
        relation_spec,
        raw_identifier,
    ):
        if raw_identifier is None:
            if relation_spec.nullable:
                return None
            raise LogicalReferenceResolutionError()
        try:
            target = django_apps.get_model(relation_spec.target_model_label)
            table = _quote_identifier(target._meta.db_table)
            primary_column = _quote_identifier(target._meta.pk.column)
            public_field = target._meta.get_field("public_id")
            public_column = _quote_identifier(public_field.column)
        except (LookupError, ValueError):
            raise LogicalReferenceResolutionError() from None

        if relation_spec.global_reference:
            row = reader.first(
                f"SELECT {public_column} FROM {table} " f"WHERE {primary_column} = ?",
                (raw_identifier,),
            )
            if row is None or len(row) != 1:
                raise LogicalReferenceResolutionError()
            public_id = canonical_uuid(row[0])
        elif relation_spec.target_model_label == "tenants.Business":
            row = reader.first(
                f"SELECT {public_column} FROM {table} "
                f"WHERE {primary_column} = ? AND {primary_column} = ?",
                (raw_identifier, context.business_id),
            )
            if row is None or len(row) != 1:
                raise TenantIsolationViolation()
            public_id = canonical_uuid(row[0])
            if public_id != str(context.business_public_id):
                raise TenantIsolationViolation()
        else:
            target_spec = self.registry.maybe_get(relation_spec.target_model_label)
            if target_spec is None or target_spec.ownership_field is None:
                raise LogicalReferenceResolutionError()
            ownership = target._meta.get_field(target_spec.ownership_field)
            ownership_column = _quote_identifier(ownership.column)
            row = reader.first(
                f"SELECT {public_column}, {ownership_column} FROM {table} "
                f"WHERE {primary_column} = ?",
                (raw_identifier,),
            )
            if row is None or len(row) != 2 or row[1] != context.business_id:
                raise TenantIsolationViolation()
            public_id = canonical_uuid(row[0])
        return {
            "model": relation_spec.target_model_label,
            "public_id": public_id,
        }

    def _resolve_many_to_many(
        self,
        *,
        reader,
        context,
        model,
        internal_pk,
        m2m_spec,
        batch_size,
        maximum_references,
    ):
        try:
            field = model._meta.get_field(m2m_spec.field_name)
            through = field.remote_field.through
            source_fk = through._meta.get_field(field.m2m_field_name())
            target_fk = through._meta.get_field(field.m2m_reverse_field_name())
            target = field.related_model
            target_spec = self.registry.get(target._meta.label)
            ownership = target._meta.get_field(target_spec.ownership_field)
            public_field = target._meta.get_field("public_id")
            through_table = _quote_identifier(through._meta.db_table)
            target_table = _quote_identifier(target._meta.db_table)
            through_source = _quote_identifier(source_fk.column)
            through_target = _quote_identifier(target_fk.column)
            target_pk = _quote_identifier(target._meta.pk.column)
            target_public = _quote_identifier(public_field.column)
            target_owner = _quote_identifier(ownership.column)
        except (AttributeError, LookupError, TypeError, ValueError):
            raise LogicalReferenceResolutionError() from None
        sql = (
            f"SELECT target.{target_public}, target.{target_owner} "
            f"FROM {through_table} AS link "
            f"LEFT JOIN {target_table} AS target "
            f"ON link.{through_target} = target.{target_pk} "
            f"WHERE link.{through_source} = ? "
            f"ORDER BY target.{target_public} ASC"
        )
        references = []
        seen = set()
        for row in reader.iter_query(
            sql,
            (internal_pk,),
            batch_size=batch_size,
        ):
            if len(row) != 2 or row[0] is None or row[1] != context.business_id:
                raise TenantIsolationViolation()
            public_id = canonical_uuid(row[0])
            if public_id in seen:
                continue
            if len(references) >= maximum_references:
                raise ComponentExportLimitExceeded()
            seen.add(public_id)
            references.append(
                {
                    "model": m2m_spec.target_model_label,
                    "public_id": public_id,
                }
            )
        return references

    def _write_encoded_line(self, *, writer, serializer, payload, deadline):
        for chunk in serializer.iter_encoded_line(payload):
            self._check_deadline(deadline)
            writer.write(chunk)

    def _run_validators(
        self,
        *,
        validators,
        reader,
        context,
        fields,
        relation_raw_values,
    ):
        for validator in validators:
            if validator == "stock_movement_business_reference_matches":
                self._validate_stock_movement_reference(
                    reader=reader,
                    context=context,
                    fields=fields,
                )
                continue
            if validator != "wms_salary_assignment_snapshot_matches":
                raise ComponentExportValidationError()
            raw_production_line = relation_raw_values.get("production_line")
            if raw_production_line is None:
                raise TenantIsolationViolation()
            line = django_apps.get_model("wms_production.WmsProductionEntryLine")
            assignment = django_apps.get_model("wms_workforce.WmsEmployeeCategoryAssignment")
            line_table = _quote_identifier(line._meta.db_table)
            assignment_table = _quote_identifier(assignment._meta.db_table)
            line_pk = _quote_identifier(line._meta.pk.column)
            line_assignment = _quote_identifier(line._meta.get_field("assignment").column)
            assignment_pk = _quote_identifier(assignment._meta.pk.column)
            assignment_public = _quote_identifier(assignment._meta.get_field("public_id").column)
            assignment_business = _quote_identifier(assignment._meta.get_field("business").column)
            row = reader.first(
                f"SELECT target.{assignment_public}, "
                f"target.{assignment_business} "
                f"FROM {line_table} AS source "
                f"LEFT JOIN {assignment_table} AS target "
                f"ON source.{line_assignment} = target.{assignment_pk} "
                f"WHERE source.{line_pk} = ?",
                (raw_production_line,),
            )
            if (
                row is None
                or len(row) != 2
                or row[1] != context.business_id
                or canonical_uuid(row[0]) != fields["assignment_public_id_snapshot"]
            ):
                raise TenantIsolationViolation()

    @staticmethod
    def _validate_stock_movement_reference(*, reader, context, fields):
        reference_type = fields.get("reference_type")
        reference_id = fields.get("reference_id")
        if not isinstance(reference_type, str) or not isinstance(reference_id, str):
            raise LogicalReferenceResolutionError()
        if reference_type in {"", "Opening", "Import"}:
            if reference_id:
                raise LogicalReferenceResolutionError()
            return
        if not reference_id:
            raise LogicalReferenceResolutionError()
        mappings = {
            "Transfer": ("inventory.StockTransfer", "transfer_number"),
            "TransferCancel": ("inventory.StockTransfer", "transfer_number"),
            "Adjustment": ("inventory.StockAdjustment", "adjustment_number"),
            "StockCount": ("inventory.StockCount", "count_number"),
            "Sale": ("sales.Sale", "invoice_number"),
            "Void": ("sales.Sale", "invoice_number"),
            "SaleReturn": ("sales.SaleReturn", "return_number"),
            "Purchase": ("purchases.Purchase", "purchase_number"),
            "PurchaseReturn": ("purchases.PurchaseReturn", "return_number"),
        }
        try:
            model_label, reference_field_name = mappings[reference_type]
            target = django_apps.get_model(model_label)
            table = _quote_identifier(target._meta.db_table)
            ownership = _quote_identifier(target._meta.get_field("business").column)
            reference_column = _quote_identifier(
                target._meta.get_field(reference_field_name).column
            )
        except (KeyError, LookupError, ValueError):
            raise LogicalReferenceResolutionError() from None
        row = reader.first(
            f"SELECT COUNT(*) FROM {table} " f"WHERE {ownership} = ? AND {reference_column} = ?",
            (context.business_id, reference_id),
        )
        if row is None or len(row) != 1 or row[0] != 1:
            raise LogicalReferenceResolutionError()

    @contextmanager
    def open_component_export(
        self,
        *,
        context,
        reference,
        stream=ComponentExportStream.RECORDS,
    ):
        if type(context) is not BackupExecutionContext:
            raise ComponentExportNotFound()
        try:
            selected_stream = ComponentExportStream(stream)
        except (TypeError, ValueError):
            raise ComponentExportNotFound() from None
        directory = self._component_directory(
            context,
            reference,
            require_exists=True,
            error_type=ComponentExportNotFound,
        )
        directory_identity = _directory_identity(
            directory,
            error_type=ComponentExportNotFound,
        )
        self._validate_final_directory(
            directory,
            expected_identity=directory_identity,
        )
        path = contained_path(
            directory,
            directory / _STREAM_FILE_NAMES[selected_stream],
        )
        identity = _regular_file_identity(
            path,
            error_type=ComponentExportNotFound,
        )
        descriptor = None
        raw_file = None
        opaque_reader = None
        try:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                (opened.st_dev, opened.st_ino) != identity
                or opened.st_nlink != 1
                or opened.st_dev != directory_identity[0]
            ):
                raise ComponentExportNotFound()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            opaque_reader = _OpaqueBinaryReader(raw_file)
            yield opaque_reader
        except ComponentExportNotFound:
            raise
        except OSError:
            raise ComponentExportNotFound() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            final_error = None
            cleanup_abort = None
            cleanup_abort_traceback = None

            def record_cleanup_failure(exc):
                nonlocal final_error
                nonlocal cleanup_abort
                nonlocal cleanup_abort_traceback
                if active_exception:
                    return
                if isinstance(
                    exc,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                ):
                    if cleanup_abort is None:
                        cleanup_abort = exc
                        cleanup_abort_traceback = exc.__traceback__
                    return
                final_error = ComponentExportNotFound()

            if opaque_reader is not None:
                try:
                    opaque_reader.close()
                except BaseException as exc:
                    record_cleanup_failure(exc)
            elif raw_file is not None:
                try:
                    raw_file.close()
                except BaseException as exc:
                    record_cleanup_failure(exc)
            elif descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    record_cleanup_failure(exc)
            try:
                if (
                    _regular_file_identity(
                        path,
                        error_type=ComponentExportNotFound,
                    )
                    != identity
                    or _directory_identity(
                        directory,
                        error_type=ComponentExportNotFound,
                    )
                    != directory_identity
                ):
                    final_error = ComponentExportNotFound()
            except BaseException as exc:
                record_cleanup_failure(exc)
            if cleanup_abort is not None and not active_exception:
                raise cleanup_abort.with_traceback(cleanup_abort_traceback)
            if final_error is not None and not active_exception:
                raise final_error from None

    def validate_component_export_evidence(
        self,
        *,
        context,
        snapshot_result,
        component,
        result,
    ) -> bool:
        """Bind an opaque result to this provider, workspace, snapshot, and plan."""

        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(snapshot_result) is not SnapshotResult
            or type(snapshot_result.reference) is not SnapshotReference
            or type(result) is not ComponentExportResult
            or type(result.reference) is not ComponentExportReference
            or type(result.reference.identifier) is not uuid.UUID
        ):
            raise ComponentExportValidationError()
        self.registry.validate_component_item(component)
        evidence = self._validated_component_reference_evidence(
            context=context,
            reference=result.reference,
            error_type=ComponentExportValidationError,
        )
        if (
            evidence.snapshot_identifier
            != snapshot_result.reference.identifier
            or evidence.component_key != component.key
            or evidence.result != result
        ):
            raise ComponentExportValidationError()
        return True

    def validate_component_export_reference_evidence(
        self,
        *,
        context,
        reference,
    ) -> bool:
        """Confirm that a reference is an exact output owned by this context."""

        self._validated_component_reference_evidence(
            context=context,
            reference=reference,
            error_type=ComponentExportValidationError,
        )
        return True

    def owns_component_export_reference_evidence(
        self,
        *,
        context,
        reference,
    ) -> bool:
        """Recognize provider ownership without claiming live-file validity."""

        self._component_reference_evidence_state(
            context=context,
            reference=reference,
            error_type=ComponentExportValidationError,
        )
        return True

    def _validated_component_reference_evidence(
        self,
        *,
        context,
        reference,
        error_type,
    ):
        evidence = self._component_reference_evidence_state(
            context=context,
            reference=reference,
            error_type=error_type,
        )
        directory = self._component_directory(
            context,
            reference,
            require_exists=True,
            error_type=error_type,
        )
        try:
            self._validate_final_directory(
                directory,
                expected_identity=evidence.directory_identity,
                expected_files={
                    name: (file_identity, byte_count)
                    for name, file_identity, byte_count in evidence.expected_files
                },
            )
        except ComponentExportValidationError:
            raise error_type() from None
        return evidence

    def _component_reference_evidence_state(
        self,
        *,
        context,
        reference,
        error_type,
    ):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(reference) is not ComponentExportReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise error_type()
        key = (
            context.workspace_reference.identifier,
            reference.identifier,
        )
        with self._published_results_lock:
            evidence = self._published_results.get(key)
        if (
            type(evidence) is not _PublishedComponentEvidence
            or evidence.context != context
            or evidence.result.reference != reference
        ):
            raise error_type()
        return evidence

    def cleanup_component_export_evidence(
        self,
        *,
        context,
        reference,
    ) -> bool:
        """Exactly clean a component publication bound to provider evidence."""

        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(reference) is not ComponentExportReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise ComponentExportCleanupError()
        key = (
            context.workspace_reference.identifier,
            reference.identifier,
        )
        with self._published_results_lock:
            if key in self._exactly_cleaned_results:
                if self._exactly_cleaned_results[key] != context:
                    raise ComponentExportCleanupError()
                return True
            evidence = self._published_results.get(key)
        if (
            type(evidence) is not _PublishedComponentEvidence
            or evidence.context != context
            or evidence.result.reference != reference
        ):
            raise ComponentExportCleanupError()

        try:
            workspace = self._existing_workspace(
                context,
                error_type=ComponentExportCleanupError,
            )
            parent = workspace.system_area_path(WorkspaceArea.COMPONENTS)
            directory = self._component_directory(
                context,
                reference,
                require_exists=True,
                error_type=ComponentExportCleanupError,
            )
            if (
                _directory_identity(
                    directory,
                    error_type=ComponentExportCleanupError,
                )
                != evidence.directory_identity
            ):
                raise ComponentExportCleanupError()
            _same_device(
                parent,
                directory,
                error_type=ComponentExportCleanupError,
            )

            remaining_names = {
                name
                for name, file_identity, _byte_count in evidence.expected_files
                if file_identity is not None
            }
            with os.scandir(directory) as entries:
                if {entry.name for entry in entries} != remaining_names:
                    raise ComponentExportCleanupError()

            for file_name, file_identity, byte_count in evidence.expected_files:
                if file_identity is None:
                    continue
                path = contained_path(directory, directory / file_name)
                current_identity = _regular_file_identity(
                    path,
                    error_type=ComponentExportCleanupError,
                )
                try:
                    current = os.stat(path, follow_symlinks=False)
                except OSError:
                    raise ComponentExportCleanupError() from None
                if (
                    current_identity != file_identity
                    or current.st_dev != evidence.directory_identity[0]
                    or current.st_size != byte_count
                ):
                    raise ComponentExportCleanupError()

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
                    raise ComponentExportCleanupError()

                updated_files = tuple(
                    (
                        expected_name,
                        None
                        if expected_name == file_name
                        else expected_identity,
                        expected_byte_count,
                    )
                    for (
                        expected_name,
                        expected_identity,
                        expected_byte_count,
                    ) in evidence.expected_files
                )
                updated_evidence = replace(
                    evidence,
                    expected_files=updated_files,
                )
                with self._published_results_lock:
                    if self._published_results.get(key) != evidence:
                        raise ComponentExportCleanupError()
                    self._published_results[key] = updated_evidence
                evidence = updated_evidence
                if unlink_abort is not None:
                    raise unlink_abort.with_traceback(unlink_abort_traceback)

            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    raise ComponentExportCleanupError()
            if (
                _directory_identity(
                    directory,
                    error_type=ComponentExportCleanupError,
                )
                != evidence.directory_identity
            ):
                raise ComponentExportCleanupError()

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
                raise ComponentExportCleanupError()
            with self._published_results_lock:
                if self._published_results.get(key) != evidence:
                    raise ComponentExportCleanupError()
                self._published_results.pop(key, None)
                self._exactly_cleaned_results[key] = context
            if directory_abort is not None:
                raise directory_abort.with_traceback(
                    directory_abort_traceback
                )
            return True
        except ComponentExportCleanupError:
            raise
        except LogicalExportEngineError:
            raise ComponentExportCleanupError() from None
        except Exception:
            raise ComponentExportCleanupError() from None

    def cleanup_component_export(
        self,
        *,
        context,
        reference,
        require_exact_evidence=False,
    ) -> bool:
        if type(require_exact_evidence) is not bool:
            raise ComponentExportCleanupError()
        if require_exact_evidence:
            return self.cleanup_component_export_evidence(
                context=context,
                reference=reference,
            )
        if (
            type(context) is not BackupExecutionContext
            or type(reference) is not ComponentExportReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise ComponentExportCleanupError()
        try:
            workspace = self._existing_workspace(
                context,
                error_type=ComponentExportCleanupError,
            )
            raw_parent = workspace.system_area_path(WorkspaceArea.COMPONENTS)
            if not os.path.lexists(raw_parent):
                return False
            directory = self._component_directory(
                context,
                reference,
                require_exists=False,
                error_type=ComponentExportCleanupError,
            )
            if not os.path.lexists(directory):
                return False
            directory_identity = _directory_identity(
                directory,
                error_type=ComponentExportCleanupError,
            )
            _same_device(
                raw_parent,
                directory,
                error_type=ComponentExportCleanupError,
            )
            with os.scandir(directory) as entries:
                names = {entry.name for entry in entries}
            expected_names = {RECORDS_FILE_NAME, MEDIA_INDEX_FILE_NAME}
            present_names = names.intersection(expected_names)
            existing = []
            for file_name in present_names:
                path = contained_path(directory, directory / file_name)
                identity = _regular_file_identity(
                    path,
                    error_type=ComponentExportCleanupError,
                )
                try:
                    current = os.stat(path, follow_symlinks=False)
                except OSError:
                    raise ComponentExportCleanupError() from None
                if current.st_dev != directory_identity[0]:
                    raise ComponentExportCleanupError()
                existing.append((path, identity))
            for path, identity in existing:
                if (
                    _regular_file_identity(
                        path,
                        error_type=ComponentExportCleanupError,
                    )
                    != identity
                ):
                    raise ComponentExportCleanupError()
                os.unlink(path)
                if os.path.lexists(path):
                    raise ComponentExportCleanupError()
            with os.scandir(directory) as entries:
                is_empty = next(entries, None) is None
            removed_directory = False
            if is_empty:
                if (
                    _directory_identity(
                        directory,
                        error_type=ComponentExportCleanupError,
                    )
                    != directory_identity
                ):
                    raise ComponentExportCleanupError()
                os.rmdir(directory)
                if os.path.lexists(directory):
                    raise ComponentExportCleanupError()
                removed_directory = True
            cleaned = bool(existing) or removed_directory
            if cleaned:
                key = (
                    context.workspace_reference.identifier,
                    reference.identifier,
                )
                with self._published_results_lock:
                    self._published_results.pop(key, None)
            return cleaned
        except ComponentExportNotFound:
            return False
        except LogicalExportEngineError as exc:
            if isinstance(exc, ComponentExportCleanupError):
                raise
            raise ComponentExportCleanupError() from None
        except (OSError, RuntimeError, ValueError, UnsafeWorkspacePath):
            raise ComponentExportCleanupError() from None

    @staticmethod
    def _validate_final_directory(
        directory,
        *,
        expected_identity=None,
        expected_files=None,
    ):
        try:
            directory_identity = _directory_identity(
                directory,
                error_type=ComponentExportValidationError,
            )
            if expected_identity is not None and directory_identity != expected_identity:
                raise ComponentExportValidationError()
            _assert_private_mode(
                directory,
                0o700,
                error_type=ComponentExportValidationError,
            )
            with os.scandir(directory) as entries:
                names = {entry.name for entry in entries}
            if names != {RECORDS_FILE_NAME, MEDIA_INDEX_FILE_NAME}:
                raise ComponentExportValidationError()
            if expected_files is not None and set(expected_files) != names:
                raise ComponentExportValidationError()
            for name in names:
                path = Path(directory) / name
                identity = _regular_file_identity(
                    path,
                    error_type=ComponentExportValidationError,
                )
                current = os.stat(path, follow_symlinks=False)
                if current.st_dev != directory_identity[0]:
                    raise ComponentExportValidationError()
                if expected_files is not None:
                    expected_file_identity, expected_size = expected_files[name]
                    if identity != expected_file_identity or current.st_size != expected_size:
                        raise ComponentExportValidationError()
                _assert_private_mode(
                    path,
                    0o600,
                    error_type=ComponentExportValidationError,
                )
            if (
                _directory_identity(
                    directory,
                    error_type=ComponentExportValidationError,
                )
                != directory_identity
            ):
                raise ComponentExportValidationError()
        except LogicalExportEngineError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise ComponentExportValidationError() from None

    @staticmethod
    def _remove_directory_if_empty(directory, *, expected_identity=None):
        try:
            if not os.path.lexists(directory):
                return False
            identity = _directory_identity(
                directory,
                error_type=ComponentExportCleanupError,
            )
            if expected_identity is not None and identity != expected_identity:
                raise ComponentExportCleanupError()
            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    return False
            if (
                _directory_identity(
                    directory,
                    error_type=ComponentExportCleanupError,
                )
                != identity
            ):
                raise ComponentExportCleanupError()
            os.rmdir(directory)
            return True
        except LogicalExportEngineError:
            raise
        except OSError:
            raise ComponentExportCleanupError() from None

    def _run_failure_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _monotonic_value(self):
        try:
            value = float(self.monotonic())
        except (TypeError, ValueError, OverflowError):
            raise ComponentExportTimeout() from None
        if not math.isfinite(value):
            raise ComponentExportTimeout()
        return value

    def _check_deadline(self, deadline):
        if self._monotonic_value() > deadline:
            raise ComponentExportTimeout()

    def _duration_ms(self, started):
        try:
            return min(
                int(max(0.0, self._monotonic_value() - started) * 1000),
                3_600_000,
            )
        except LogicalExportEngineError:
            return 0

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, LogicalExportEngineError):
            return exc
        if isinstance(exc, SnapshotTimeout):
            return ComponentExportTimeout()
        if isinstance(exc, SnapshotEngineError):
            return ComponentExportValidationError()
        if isinstance(exc, UnsupportedLogicalExportField):
            return exc
        if isinstance(exc, (OSError, RuntimeError, TypeError, ValueError)):
            return ComponentExportCreationError()
        return ComponentExportValidationError()


def _validated_component_result(*, result, component, registry):
    if (
        not isinstance(result, ComponentExportResult)
        or result.component_key != component.key
        or result.component_version != component.component_version
        or result.deterministic_ordering_version != DETERMINISTIC_ORDERING_VERSION
        or result.record_schema_version != LOGICAL_RECORD_SCHEMA
        or result.provider_identifier != LOGICAL_EXPORT_PROVIDER_IDENTIFIER
        or not isinstance(result.reference, ComponentExportReference)
        or not isinstance(result.reference.identifier, uuid.UUID)
        or not isinstance(result.created_at, datetime)
        or not timezone.is_aware(result.created_at)
    ):
        raise ComponentExportValidationError()
    numeric_values = (
        result.row_count,
        result.media_count,
        result.byte_count,
        result.media_index_byte_count,
        result.duration_ms,
    )
    if any(type(value) is not int or value < 0 for value in numeric_values):
        raise ComponentExportValidationError()
    if result.duration_ms > 3_600_000 or not isinstance(
        result.model_counts,
        tuple,
    ):
        raise ComponentExportValidationError()
    expected_labels = tuple(spec.model_label for spec in registry.for_component(component.key))
    model_counts = []
    for item in result.model_counts:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or type(item[1]) is not int
            or item[1] < 0
        ):
            raise ComponentExportValidationError()
        model_counts.append(item)
    if (
        tuple(label for label, _count in model_counts) != expected_labels
        or sum(count for _label, count in model_counts) != result.row_count
    ):
        raise ComponentExportValidationError()
    return result


def export_snapshot_components(
    *,
    context,
    snapshot_result,
    component_plan,
    snapshot_provider,
    component_exporter,
):
    """Export one resolved plan and always remove its full SQLite snapshot."""

    if (
        type(context) is not BackupExecutionContext
        or context.workspace_reference is None
        or type(snapshot_result) is not SnapshotResult
        or type(snapshot_result.reference) is not SnapshotReference
        or type(snapshot_result.reference.identifier) is not uuid.UUID
        or type(snapshot_provider) is not SQLiteSnapshotProvider
    ):
        raise ComponentExportValidationError()
    results = []
    primary_error = None
    abort_error = None
    abort_traceback = None
    cleanup_abort = None
    cleanup_abort_traceback = None
    cleanup_incomplete = False
    owned_references = []
    seen_references = set()

    try:
        if (
            snapshot_result.consistent is not True
            or snapshot_result.provider_identifier != SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER
            or not isinstance(component_exporter, ComponentExporter)
            or getattr(component_exporter, "snapshot_provider", None) is not snapshot_provider
        ):
            raise ComponentExportValidationError()
        registry = getattr(component_exporter, "registry", None)
        if registry is None:
            raise ComponentExportValidationError()
        plan = registry.validate_component_plan(
            context=context,
            component_plan=component_plan,
            require_full=True,
        )
        for component in plan:
            produced = component_exporter.export_component(
                ComponentExportRequest(
                    context=context,
                    component=component,
                    snapshot=snapshot_result.reference,
                    component_plan=plan,
                )
            )
            if (
                isinstance(produced, ComponentExportResult)
                and isinstance(produced.reference, ComponentExportReference)
                and isinstance(produced.reference.identifier, uuid.UUID)
            ):
                if produced.reference.identifier in seen_references:
                    raise ComponentExportValidationError()
                seen_references.add(produced.reference.identifier)
                owned_references.append(produced.reference)
            else:
                cleanup_incomplete = True
            results.append(
                _validated_component_result(
                    result=produced,
                    component=component,
                    registry=registry,
                )
            )
        if type(component_exporter) is SQLiteLogicalComponentExporter:
            component_exporter._run_failure_hook("before_batch_result_return")
    except BaseException as exc:
        if isinstance(exc, Exception):
            primary_error = SQLiteLogicalComponentExporter._safe_error(exc)
        else:
            abort_error = exc
            abort_traceback = exc.__traceback__
    finally:
        if primary_error is not None or abort_error is not None:
            for reference in reversed(owned_references):
                try:
                    if (
                        component_exporter.cleanup_component_export(
                            context=context,
                            reference=reference,
                        )
                        is not True
                    ):
                        cleanup_incomplete = True
                except BaseException:
                    cleanup_incomplete = True
        snapshot_cleaned = False
        try:
            snapshot_cleaned = (
                snapshot_provider.cleanup_snapshot(
                    context=context,
                    reference=snapshot_result.reference,
                )
                is True
            )
        except BaseException as exc:
            cleanup_incomplete = True
            if (
                primary_error is None
                and abort_error is None
                and isinstance(
                    exc,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                )
            ):
                cleanup_abort = exc
                cleanup_abort_traceback = exc.__traceback__
        if not snapshot_cleaned:
            cleanup_incomplete = True

    if abort_error is not None:
        raise abort_error.with_traceback(abort_traceback)
    if primary_error is not None:
        primary_error.cleanup_incomplete = bool(
            cleanup_incomplete or getattr(primary_error, "cleanup_incomplete", False)
        )
        primary_error.__cause__ = None
        primary_error.__context__ = None
        raise primary_error.with_traceback(None) from None
    if cleanup_abort is not None:
        for reference in reversed(owned_references):
            try:
                component_exporter.cleanup_component_export(
                    context=context,
                    reference=reference,
                )
            except BaseException:
                pass
        raise cleanup_abort.with_traceback(cleanup_abort_traceback)
    if cleanup_incomplete:
        for reference in reversed(owned_references):
            try:
                component_exporter.cleanup_component_export(
                    context=context,
                    reference=reference,
                )
            except BaseException:
                pass
        raise SnapshotCleanupAfterExportError(cleanup_incomplete=True)
    return tuple(results)


__all__ = [
    "ComponentExportStream",
    "LOGICAL_EXPORT_PROVIDER_IDENTIFIER",
    "MEDIA_INDEX_FILE_NAME",
    "RECORDS_FILE_NAME",
    "SQLiteLogicalComponentExporter",
    "export_snapshot_components",
]
