"""Phase 2I internal end-to-end backup execution coordination."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.backups import services
from apps.backups.enums import (
    ActivitySeverity,
    BackupScope,
    BackupStatus,
    BackupTrigger,
    CompatibilityStatus,
    IntegrityStatus,
    OperationKind,
)
from apps.backups.models import BackupRecord, TenantOperationLock
from apps.backups.registry import COMPONENT_REGISTRY
from apps.tenants.models import Business

from .canonical_manifest import CanonicalManifestProvider
from .context import ActorIdentitySnapshot
from .contracts import (
    EncryptedArtifactRequest,
    PackageVerificationRequest,
    Phase2D1Request,
    Phase2D2Request,
    SnapshotRequest,
    StoredBackupObjectRequest,
    StoredBackupObjectResult,
)
from .deterministic_package import DeterministicPackageProvider
from .durable_storage import LocalPrivateDurableStorageProvider
from .encrypted_artifact import EncryptedArtifactProvider
from .events import (
    BACKUP_COMPLETED,
    BACKUP_DURABLE_STORED,
    BACKUP_ENCRYPTED,
    BACKUP_EXECUTION_STARTED,
    BACKUP_EXPORT_COMPLETED,
    BACKUP_FAILED,
    BACKUP_MANIFEST_COMPLETED,
    BACKUP_PACKAGE_COMPLETED,
    BACKUP_SNAPSHOT_COMPLETED,
    BACKUP_VERIFIED,
    BACKUP_WORKSPACE_CLEANUP_DEFERRED,
    RETENTION_COMPLETED,
    RETENTION_FAILED,
    RETENTION_HISTORICAL_EVIDENCE_DEFERRED,
    RETENTION_PARTIAL,
)
from .exceptions import BackupEngineError, BackupTenantMismatch
from .key_management import (
    KeyEncryptionProvider,
    build_key_provider_registry_from_settings,
    key_metadata_identifier,
)
from .logical_export import (
    SQLiteLogicalComponentExporter,
    export_snapshot_components,
)
from .media_capture import LocalFilesystemMediaCaptureProvider
from .metadata import BackupMetadataBuilder
from .package_verification import IndependentPackageVerifier
from .phase2d1 import Phase2D1Coordinator
from .phase2d2 import Phase2D2Coordinator
from .pipeline import resolve_component_plan
from .retention import (
    BackupRetentionClass,
    RetentionCandidate,
    RetentionEngine,
    RetentionExecutionState,
)
from .runtime_exceptions import (
    Phase2ICoordinationError,
    RuntimeAlreadyCompleted,
    RuntimeEngineError,
    RuntimeExecutionError,
    RuntimeLockLost,
    RuntimeLockUnavailable,
    RuntimePersistenceError,
    RuntimeProviderStackError,
    RuntimeRequestError,
    RuntimeStateError,
    RuntimeVerificationError,
)
from .sqlite_snapshot import SQLiteSnapshotProvider
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    path_is_link_like,
)

RUNTIME_PROVIDER_STACK_VERSION = "nexa.backup-runtime.v1"


class RuntimeRetentionOutcome(StrEnum):
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED_SAFE = "FAILED_SAFE"
    HISTORICAL_EVIDENCE_DEFERRED = "HISTORICAL_EVIDENCE_DEFERRED"


@dataclass(frozen=True, slots=True)
class BackupExecutionRequest:
    backup_public_id: uuid.UUID
    business_public_id: uuid.UUID
    requested_scope: BackupScope
    trigger: BackupTrigger
    actor_identity: ActorIdentitySnapshot
    idempotency_key: str
    worker_task_identifier: str = ""

    @classmethod
    def from_record(cls, record, *, worker_task_identifier=""):
        if type(record) is not BackupRecord:
            raise RuntimeRequestError()
        try:
            return cls(
                backup_public_id=record.public_id,
                business_public_id=record.tenant_public_id_snapshot,
                requested_scope=BackupScope(record.scope),
                trigger=BackupTrigger(record.trigger),
                actor_identity=ActorIdentitySnapshot.from_actor(
                    record.created_by,
                    system_actor=record.system_actor,
                ),
                idempotency_key=record.idempotency_key,
                worker_task_identifier=str(worker_task_identifier or "")[:255],
            )
        except (AttributeError, TypeError, ValueError):
            raise RuntimeRequestError() from None


@dataclass(frozen=True, slots=True)
class InheritedTenantOperationLease:
    """Opaque proof that a restore coordinator already owns the tenant lease."""

    business_public_id: uuid.UUID
    restore_operation_public_id: uuid.UUID
    lock_token: uuid.UUID


@dataclass(frozen=True, slots=True)
class FinalStoredObjectEvidence:
    reference_identifier: uuid.UUID
    backend_identifier: str
    byte_count: int
    sha256: str
    provider_identifier: str


@dataclass(frozen=True, slots=True)
class BackupExecutionResult:
    backup_public_id: uuid.UUID
    business_public_id: uuid.UUID
    final_status: BackupStatus
    stored_object: FinalStoredObjectEvidence
    total_duration_ms: int
    completed_at: datetime
    retention_outcome: RuntimeRetentionOutcome
    retention_warning_code: str
    provider_stack_version: str


@dataclass(frozen=True, slots=True)
class RuntimeProviderStack:
    workspace_manager: BackupWorkspaceManager
    snapshot_provider: SQLiteSnapshotProvider
    component_exporter: SQLiteLogicalComponentExporter
    media_capture_provider: LocalFilesystemMediaCaptureProvider
    manifest_provider: CanonicalManifestProvider
    phase2d1_coordinator: Phase2D1Coordinator
    package_provider: DeterministicPackageProvider
    phase2d2_coordinator: Phase2D2Coordinator
    verification_provider: IndependentPackageVerifier
    kek_provider: KeyEncryptionProvider
    encrypted_artifact_provider: EncryptedArtifactProvider
    durable_storage_provider: LocalPrivateDurableStorageProvider
    retention_engine: RetentionEngine

    def validated(self):
        if (
            type(self.workspace_manager) is not BackupWorkspaceManager
            or type(self.snapshot_provider) is not SQLiteSnapshotProvider
            or type(self.component_exporter) is not SQLiteLogicalComponentExporter
            or type(self.media_capture_provider)
            is not LocalFilesystemMediaCaptureProvider
            or type(self.manifest_provider) is not CanonicalManifestProvider
            or type(self.phase2d1_coordinator) is not Phase2D1Coordinator
            or type(self.package_provider) is not DeterministicPackageProvider
            or type(self.phase2d2_coordinator) is not Phase2D2Coordinator
            or type(self.verification_provider) is not IndependentPackageVerifier
            or not isinstance(self.kek_provider, KeyEncryptionProvider)
            or type(self.encrypted_artifact_provider) is not EncryptedArtifactProvider
            or type(self.durable_storage_provider)
            is not LocalPrivateDurableStorageProvider
            or type(self.retention_engine) is not RetentionEngine
            or self.snapshot_provider.workspace_manager is not self.workspace_manager
            or self.component_exporter.snapshot_provider is not self.snapshot_provider
            or self.component_exporter.workspace_manager is not self.workspace_manager
            or self.media_capture_provider.snapshot_provider
            is not self.snapshot_provider
            or self.media_capture_provider.workspace_manager
            is not self.workspace_manager
            or self.manifest_provider.workspace_manager is not self.workspace_manager
            or self.package_provider.workspace_manager is not self.workspace_manager
            or self.verification_provider.package_provider is not self.package_provider
            or self.encrypted_artifact_provider.package_provider
            is not self.package_provider
            or self.encrypted_artifact_provider.verification_provider
            is not self.verification_provider
            or self.encrypted_artifact_provider.kek_provider is not self.kek_provider
            or self.durable_storage_provider.encrypted_artifact_provider
            is not self.encrypted_artifact_provider
            or self.retention_engine.durable_provider
            is not self.durable_storage_provider
        ):
            raise RuntimeProviderStackError()
        return self


def build_runtime_provider_stack() -> RuntimeProviderStack:
    """Build the one trusted composition root without creating artifacts."""

    try:
        workspace_manager = BackupWorkspaceManager()
        snapshot_provider = SQLiteSnapshotProvider(
            workspace_manager=workspace_manager,
        )
        component_exporter = SQLiteLogicalComponentExporter(
            snapshot_provider=snapshot_provider,
            workspace_manager=workspace_manager,
        )
        media_capture_provider = LocalFilesystemMediaCaptureProvider(
            snapshot_provider=snapshot_provider,
            workspace_manager=workspace_manager,
        )
        manifest_provider = CanonicalManifestProvider(
            workspace_manager=workspace_manager,
        )
        phase2d1_coordinator = Phase2D1Coordinator(
            component_exporter=component_exporter,
            media_capture_provider=media_capture_provider,
            manifest_provider=manifest_provider,
        )
        package_provider = DeterministicPackageProvider(
            component_exporter=component_exporter,
            media_capture_provider=media_capture_provider,
            manifest_provider=manifest_provider,
            workspace_manager=workspace_manager,
        )
        phase2d2_coordinator = Phase2D2Coordinator(
            component_exporter=component_exporter,
            media_capture_provider=media_capture_provider,
            manifest_provider=manifest_provider,
            package_provider=package_provider,
        )
        verification_provider = IndependentPackageVerifier(
            package_provider=package_provider,
            workspace_manager=workspace_manager,
        )
        key_provider_registry = build_key_provider_registry_from_settings()
        kek_provider = key_provider_registry.active_provider
        encrypted_artifact_provider = EncryptedArtifactProvider(
            package_provider=package_provider,
            verification_provider=verification_provider,
            kek_provider=kek_provider,
            key_provider_registry=key_provider_registry,
            workspace_manager=workspace_manager,
        )
        durable_storage_provider = LocalPrivateDurableStorageProvider(
            encrypted_artifact_provider=encrypted_artifact_provider,
        )
        retention_engine = RetentionEngine(
            durable_provider=durable_storage_provider,
        )
        return RuntimeProviderStack(
            workspace_manager=workspace_manager,
            snapshot_provider=snapshot_provider,
            component_exporter=component_exporter,
            media_capture_provider=media_capture_provider,
            manifest_provider=manifest_provider,
            phase2d1_coordinator=phase2d1_coordinator,
            package_provider=package_provider,
            phase2d2_coordinator=phase2d2_coordinator,
            verification_provider=verification_provider,
            kek_provider=kek_provider,
            encrypted_artifact_provider=encrypted_artifact_provider,
            durable_storage_provider=durable_storage_provider,
            retention_engine=retention_engine,
        ).validated()
    except RuntimeEngineError:
        raise
    except Exception:
        raise RuntimeProviderStackError() from None


class BackupExecutionCoordinator:
    """Execute one QUEUED durable record under the tenant operation lease."""

    _TRANSITIONAL_STATUSES = frozenset(
        {
            BackupStatus.QUEUED,
            BackupStatus.PREPARING,
            BackupStatus.SNAPSHOTTING,
            BackupStatus.PACKAGING,
            BackupStatus.UPLOADING,
            BackupStatus.VERIFYING,
        }
    )

    def __init__(
        self,
        *,
        provider_stack,
        using="default",
        clock=None,
        monotonic=None,
        failure_hook=None,
        lock_lease_seconds=None,
    ):
        if type(provider_stack) is not RuntimeProviderStack:
            raise RuntimeProviderStackError()
        self.provider_stack = provider_stack.validated()
        self.using = str(using)
        self.clock = clock or timezone.now
        self.monotonic = monotonic or time.monotonic
        self.failure_hook = failure_hook
        selected_lease = (
            getattr(settings, "BACKUP_EXECUTION_LOCK_LEASE_SECONDS", 21_600)
            if lock_lease_seconds is None
            else lock_lease_seconds
        )
        if type(selected_lease) is not int or not 300 <= selected_lease <= 86_400:
            raise RuntimeProviderStackError()
        self.lock_lease_seconds = selected_lease

    def _run_hook(self, stage, backup_public_id):
        if self.failure_hook is not None:
            self.failure_hook(stage, backup_public_id)

    def _elapsed_ms(self, started):
        try:
            value = int(max(0.0, (float(self.monotonic()) - started) * 1000))
        except Exception:
            raise Phase2ICoordinationError() from None
        return min(value, 2**63 - 1)

    @staticmethod
    def _validate_request(request):
        if (
            type(request) is not BackupExecutionRequest
            or type(request.backup_public_id) is not uuid.UUID
            or type(request.business_public_id) is not uuid.UUID
            or type(request.requested_scope) is not BackupScope
            or type(request.trigger) is not BackupTrigger
            or type(request.actor_identity) is not ActorIdentitySnapshot
            or type(request.idempotency_key) is not str
            or not request.idempotency_key
            or len(request.idempotency_key) > 128
            or type(request.worker_task_identifier) is not str
            or len(request.worker_task_identifier) > 255
        ):
            raise RuntimeRequestError()

    def _resolve_request(self, request):
        self._validate_request(request)
        business = Business.objects.filter(
            public_id=request.business_public_id,
        ).first()
        if business is None:
            raise BackupTenantMismatch()
        backup = (
            BackupRecord.objects.for_business(business)
            .select_related("business", "created_by")
            .filter(public_id=request.backup_public_id)
            .first()
        )
        if (
            backup is None
            or backup.business_id != business.pk
            or backup.tenant_public_id_snapshot != business.public_id
            or backup.scope != request.requested_scope
            or backup.trigger != request.trigger
            or backup.idempotency_key != request.idempotency_key
            or ActorIdentitySnapshot.from_actor(
                backup.created_by,
                system_actor=backup.system_actor,
            )
            != request.actor_identity
        ):
            raise BackupTenantMismatch()
        if backup.status == BackupStatus.SUCCEEDED:
            raise RuntimeAlreadyCompleted()
        if backup.status != BackupStatus.QUEUED:
            raise RuntimeStateError()
        return business, backup, backup.created_by

    @staticmethod
    def _activity(
        *,
        business,
        backup,
        actor,
        event_type,
        message,
        metadata=None,
        severity=ActivitySeverity.INFO,
    ):
        return services.create_backup_activity(
            business=business,
            backup=backup,
            actor=actor,
            event_type=event_type,
            severity=severity,
            sanitized_message=message,
            structured_metadata=metadata or {},
        )

    def _heartbeat(self, lock):
        if not services.heartbeat_tenant_operation_lock(
            lock,
            lock_token=lock.lock_token,
            lease_seconds=self.lock_lease_seconds,
        ):
            raise RuntimeLockLost()

    @staticmethod
    def _resolve_inherited_lock(*, business, lease):
        if (
            type(lease) is not InheritedTenantOperationLease
            or type(lease.business_public_id) is not uuid.UUID
            or type(lease.restore_operation_public_id) is not uuid.UUID
            or type(lease.lock_token) is not uuid.UUID
            or lease.business_public_id != business.public_id
        ):
            raise RuntimeLockUnavailable()
        current = (
            TenantOperationLock.objects.for_business(business)
            .filter(
                operation_kind=OperationKind.RESTORE,
                operation_public_id=lease.restore_operation_public_id,
                lock_token=lease.lock_token,
                active=True,
                lease_expires_at__gt=timezone.now(),
            )
            .first()
        )
        if current is None:
            raise RuntimeLockUnavailable()
        return current

    @staticmethod
    def _retention_class(backup):
        if backup.pinned:
            return BackupRetentionClass.PINNED
        if (
            backup.trigger == BackupTrigger.SCHEDULED
            and backup.scope == BackupScope.ALL_ENABLED
        ):
            return BackupRetentionClass.DAILY_FULL
        return BackupRetentionClass.MANUAL

    def _persist_durable_metadata(
        self,
        *,
        backup,
        stored,
        phase2d1_result,
        verification,
    ):
        if type(stored) is not StoredBackupObjectResult:
            raise RuntimePersistenceError()
        object_key = str(stored.reference.identifier)
        existing_key = backup.opaque_object_key
        if existing_key and existing_key != object_key:
            raise RuntimePersistenceError(durable_object_preserved=True)
        manifest = phase2d1_result.manifest
        key_identifier = key_metadata_identifier(
            stored.kek_provider_identifier,
            stored.kek_key_identifier,
            stored.kek_version,
        )
        try:
            with transaction.atomic():
                changed = (
                    BackupRecord.objects.filter(
                        pk=backup.pk,
                        status__in=self._TRANSITIONAL_STATUSES,
                    )
                    .filter(Q(opaque_object_key="") | Q(opaque_object_key=object_key))
                    .update(
                        storage_backend_identifier=stored.backend_identifier[:80],
                        opaque_object_key=object_key,
                        encryption_key_identifier=key_identifier,
                        whole_artifact_hash=stored.sha256,
                        backup_size_bytes=stored.byte_count,
                        total_row_count=manifest.total_record_count,
                        component_count=manifest.component_count,
                        media_count=manifest.unique_media_object_count,
                        compatibility_status=(
                            CompatibilityStatus.COMPATIBLE
                            if verification.restore_ready
                            else CompatibilityStatus.INCOMPATIBLE
                        ),
                        restore_compatibility_reason="",
                        failure_code="",
                        sanitized_failure_summary="",
                        updated_at=self.clock(),
                    )
                )
        except (IntegrityError, TypeError, ValueError):
            raise RuntimePersistenceError(durable_object_preserved=True) from None
        if changed != 1:
            raise RuntimePersistenceError(durable_object_preserved=True)
        backup.refresh_from_db()
        return backup

    def _mark_failed(self, *, backup, code, summary):
        current = BackupRecord.objects.get(pk=backup.pk)
        if current.status not in self._TRANSITIONAL_STATUSES:
            return current
        if current.integrity_status in {
            IntegrityStatus.NOT_CHECKED,
            IntegrityStatus.VERIFYING,
        }:
            try:
                current = services.set_backup_integrity(
                    current,
                    IntegrityStatus.FAILED,
                )
            except Exception:
                current.refresh_from_db()
        return services.transition_backup(
            current,
            BackupStatus.FAILED,
            failure_code=code,
            failure_summary=summary,
        )

    @staticmethod
    def _failure_for_stage(stage, exc):
        mapping = {
            "preparing": ("execution_state_invalid", "Backup preparation failed safely."),
            "snapshot": ("snapshot_failure", "The database snapshot could not be completed."),
            "export": ("logical_export_failure", "Logical backup export could not be completed."),
            "manifest": ("media_manifest_failure", "Media and manifest capture could not be completed."),
            "package": ("package_failure", "The deterministic package could not be completed."),
            "verification": ("package_verification_failure", "Independent package verification failed."),
            "encryption": ("encryption_failure", "Authenticated backup encryption failed."),
            "durable": ("durable_storage_failure", "Durable encrypted storage could not be completed."),
            "finalization": ("durable_finalization_failure", "Durable backup metadata could not be finalized."),
        }
        code, summary = mapping.get(
            stage,
            ("runtime_execution_failure", "Backup execution failed safely."),
        )
        if isinstance(exc, RuntimeEngineError):
            code = exc.engine_code
        elif isinstance(exc, BackupEngineError) and exc.engine_code:
            code = str(exc.engine_code)[:80]
        return code, summary

    @staticmethod
    def _safe_error(exc, *, durable_preserved=False):
        if isinstance(exc, RuntimeEngineError):
            exc.durable_object_preserved = bool(
                durable_preserved or exc.durable_object_preserved
            )
            return exc
        if isinstance(exc, BackupEngineError):
            return RuntimeExecutionError(
                durable_object_preserved=durable_preserved,
            )
        return Phase2ICoordinationError(
            durable_object_preserved=durable_preserved,
        )

    def _cleanup_transients(
        self,
        *,
        context,
        workspace,
        snapshot,
        component_exports,
        phase2d1_result,
        package,
        verification,
        encrypted,
        stored,
        durable_store_attempted,
    ):
        stack = self.provider_stack
        cleanup_incomplete = False
        def attempt(action):
            nonlocal cleanup_incomplete
            try:
                action()
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                cleanup_incomplete = True
                raise
            except BaseException:
                cleanup_incomplete = True

        if context is not None and verification is not None and verification.reference:
            attempt(
                lambda: stack.verification_provider.cleanup_verification_evidence(
                    context=context,
                    reference=verification.reference,
                )
            )
        if (
            context is not None
            and encrypted is not None
            and stored is None
            and not durable_store_attempted
        ):
            attempt(
                lambda: stack.encrypted_artifact_provider.cleanup_encrypted_artifact(
                    context=context,
                    reference=encrypted.reference,
                )
            )
        if context is not None and package is not None and encrypted is None:
            attempt(
                lambda: stack.package_provider.cleanup_package(
                    context=context,
                    reference=package.reference,
                )
            )
        if context is not None and phase2d1_result is not None and package is None:
            attempt(
                lambda: stack.manifest_provider.cleanup_manifest(
                    context=context,
                    reference=phase2d1_result.manifest.reference,
                )
            )
            for capture in reversed(phase2d1_result.media_captures):
                attempt(
                    lambda capture=capture: stack.media_capture_provider.cleanup_media_capture(
                        context=context,
                        reference=capture.reference,
                    )
                )
            for component in reversed(phase2d1_result.component_exports):
                attempt(
                    lambda component=component: stack.component_exporter.cleanup_component_export(
                        context=context,
                        reference=component.reference,
                        require_exact_evidence=True,
                    )
                )
        elif context is not None and component_exports:
            for component in reversed(component_exports):
                attempt(
                    lambda component=component: stack.component_exporter.cleanup_component_export(
                        context=context,
                        reference=component.reference,
                        require_exact_evidence=True,
                    )
                )
        if context is not None and snapshot is not None:
            attempt(
                lambda: stack.snapshot_provider.cleanup_snapshot(
                    context=context,
                    reference=snapshot.reference,
                )
            )
        if workspace is not None:
            try:
                for area in WorkspaceArea:
                    area_path = workspace.system_area_path(area)
                    if not os.path.lexists(area_path):
                        continue
                    if path_is_link_like(area_path) or not area_path.is_dir():
                        cleanup_incomplete = True
                        continue
                    with os.scandir(area_path) as area_contents:
                        area_empty = next(area_contents, None) is None
                    if area_empty:
                        os.rmdir(area_path)
                    else:
                        cleanup_incomplete = True
                with os.scandir(workspace.path) as contents:
                    empty = next(contents, None) is None
                if empty:
                    stack.workspace_manager.cleanup(workspace.reference)
                else:
                    cleanup_incomplete = True
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except BaseException:
                cleanup_incomplete = True
        return cleanup_incomplete

    def _execute_retention(self, *, business, backup, actor, context, stored):
        stack = self.provider_stack
        candidate = RetentionCandidate(
            context=context,
            stored_object=stored,
            retention_class=self._retention_class(backup),
            package_verified=True,
            encrypted_artifact_valid=True,
            durable_verified=True,
            pinned=backup.pinned,
            protected=backup.protected,
        )
        prior_records_exist = (
            BackupRecord.objects.for_business(business)
            .filter(
                status=BackupStatus.SUCCEEDED,
                integrity_status=IntegrityStatus.VERIFIED,
                retention_eligible=True,
            )
            .exclude(pk=backup.pk)
            .exists()
        )
        try:
            plan = stack.retention_engine.build_retention_plan(
                tenant_public_id=business.public_id,
                candidates=(candidate,),
            )
            execution = stack.retention_engine.execute_retention_plan(
                plan=plan,
                current_candidates=(candidate,),
            )
        except Exception:
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=RETENTION_FAILED,
                severity=ActivitySeverity.WARNING,
                message="Retention lifecycle maintenance failed safely.",
                metadata={"error_code": "retention_failed_safe"},
            )
            return RuntimeRetentionOutcome.FAILED_SAFE, "retention_failed_safe"
        if prior_records_exist:
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=RETENTION_HISTORICAL_EVIDENCE_DEFERRED,
                severity=ActivitySeverity.WARNING,
                message=(
                    "Historical retention was deferred because exact restart-persistent "
                    "provider evidence is unavailable."
                ),
                metadata={"new_backup_protected": True},
            )
            return (
                RuntimeRetentionOutcome.HISTORICAL_EVIDENCE_DEFERRED,
                "historical_provider_evidence_unavailable",
            )
        state_mapping = {
            RetentionExecutionState.NO_ACTION_REQUIRED: (
                RuntimeRetentionOutcome.NO_ACTION_REQUIRED,
                RETENTION_COMPLETED,
                ActivitySeverity.INFO,
            ),
            RetentionExecutionState.COMPLETED: (
                RuntimeRetentionOutcome.COMPLETED,
                RETENTION_COMPLETED,
                ActivitySeverity.INFO,
            ),
            RetentionExecutionState.PARTIALLY_COMPLETED: (
                RuntimeRetentionOutcome.PARTIALLY_COMPLETED,
                RETENTION_PARTIAL,
                ActivitySeverity.WARNING,
            ),
            RetentionExecutionState.FAILED_SAFE: (
                RuntimeRetentionOutcome.FAILED_SAFE,
                RETENTION_FAILED,
                ActivitySeverity.WARNING,
            ),
        }
        outcome, event_type, severity = state_mapping[execution.execution_state]
        self._activity(
            business=business,
            backup=backup,
            actor=actor,
            event_type=event_type,
            severity=severity,
            message="Retention lifecycle maintenance completed with safe evidence.",
            metadata={
                "state": execution.execution_state.value,
                "deleted_count": len(execution.deleted_backup_public_ids),
                "failed_deletion_count": execution.failed_deletion_count,
            },
        )
        warning = "" if severity == ActivitySeverity.INFO else "retention_incomplete"
        return outcome, warning

    def execute(
        self,
        request: BackupExecutionRequest,
        *,
        inherited_lease: InheritedTenantOperationLease | None = None,
    ) -> BackupExecutionResult:
        try:
            started = float(self.monotonic())
        except Exception:
            raise Phase2ICoordinationError() from None
        business, backup, actor = self._resolve_request(request)
        lock = None
        workspace = None
        context = None
        snapshot = None
        component_exports = ()
        phase2d1_result = None
        package = None
        verification = None
        encrypted = None
        stored = None
        durable_metadata_persisted = False
        durable_store_attempted = False
        cleanup_incomplete = False
        stage = "preparing"
        owns_lock = inherited_lease is None

        try:
            if inherited_lease is None:
                try:
                    lock = services.acquire_tenant_operation_lock(
                        business=business,
                        operation_kind=OperationKind.BACKUP,
                        operation_public_id=backup.public_id,
                        worker_task_identifier=request.worker_task_identifier,
                        lease_seconds=self.lock_lease_seconds,
                    )
                except services.TenantOperationLocked:
                    self._activity(
                        business=business,
                        backup=backup,
                        actor=actor,
                        event_type=BACKUP_FAILED,
                        severity=ActivitySeverity.WARNING,
                        message="Backup execution could not acquire the tenant lock.",
                        metadata={"error_code": "lock_unavailable"},
                    )
                    raise RuntimeLockUnavailable() from None
            else:
                lock = self._resolve_inherited_lock(
                    business=business,
                    lease=inherited_lease,
                )

            business, backup, actor = self._resolve_request(request)
            resolution = services.resolve_requested_scope(business, backup.scope)
            if tuple(backup.included_products or ()) != tuple(
                resolution.included_products
            ):
                raise RuntimeRequestError()
            metadata_builder = BackupMetadataBuilder(
                registry=COMPONENT_REGISTRY,
                using=self.using,
            )
            context = metadata_builder.build_context(
                business=business,
                backup_record=backup,
                actor=actor,
                scope_resolution=resolution,
            )
            component_plan = resolve_component_plan(
                scope=resolution.scope,
                enabled_products=resolution.included_products,
                registry=COMPONENT_REGISTRY,
            )
            workspace = self.provider_stack.workspace_manager.create(
                WorkspaceReference(context.operation_correlation_id)
            )
            context = context.with_workspace(workspace.reference)
            backup = services.transition_backup(backup, BackupStatus.PREPARING)
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=BACKUP_EXECUTION_STARTED,
                message="Operational backup execution started.",
                metadata={
                    "scope": request.requested_scope.value,
                    "trigger": request.trigger.value,
                    "system_actor": backup.system_actor,
                },
            )
            self._run_hook("after_execution_started", backup.public_id)
            self._heartbeat(lock)

            stage = "snapshot"
            backup = services.transition_backup(backup, BackupStatus.SNAPSHOTTING)
            snapshot = self.provider_stack.snapshot_provider.create_snapshot(
                SnapshotRequest(context=context)
            )
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=BACKUP_SNAPSHOT_COMPLETED,
                message="A consistent SQLite snapshot was completed.",
                metadata={
                    "byte_count": snapshot.byte_count,
                    "duration_ms": snapshot.duration_ms,
                    "provider": snapshot.provider_identifier,
                },
            )
            self._run_hook("after_snapshot", backup.public_id)
            self._heartbeat(lock)

            stage = "export"
            component_exports = export_snapshot_components(
                context=context,
                snapshot_result=snapshot,
                component_plan=component_plan.export_components,
                snapshot_provider=self.provider_stack.snapshot_provider,
                component_exporter=self.provider_stack.component_exporter,
            )
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=BACKUP_EXPORT_COMPLETED,
                message="Tenant logical component export completed.",
                metadata={
                    "component_count": len(component_exports),
                    "row_count": sum(item.row_count for item in component_exports),
                },
            )
            self._run_hook("after_export", backup.public_id)
            self._heartbeat(lock)

            stage = "manifest"
            phase2d1_result = self.provider_stack.phase2d1_coordinator.build(
                Phase2D1Request(
                    context=context,
                    snapshot_result=snapshot,
                    component_plan=component_plan.export_components,
                    component_exports=component_exports,
                )
            )
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=BACKUP_MANIFEST_COMPLETED,
                message="Media capture and canonical manifest completed.",
                metadata={
                    "component_count": phase2d1_result.manifest.component_count,
                    "media_count": phase2d1_result.manifest.unique_media_object_count,
                    "row_count": phase2d1_result.manifest.total_record_count,
                },
            )
            self._run_hook("after_manifest", backup.public_id)
            self._heartbeat(lock)

            stage = "package"
            backup = services.transition_backup(backup, BackupStatus.PACKAGING)
            phase2d2_result = self.provider_stack.phase2d2_coordinator.build(
                Phase2D2Request(
                    context=context,
                    phase2d1_result=phase2d1_result,
                )
            )
            package = phase2d2_result.package
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=BACKUP_PACKAGE_COMPLETED,
                message="The deterministic plaintext package completed.",
                metadata={
                    "byte_count": package.byte_count,
                    "entry_count": package.entry_count,
                    "provider": package.provider_identifier,
                },
            )
            self._run_hook("after_package", backup.public_id)
            self._heartbeat(lock)

            stage = "verification"
            verification = self.provider_stack.verification_provider.verify(
                PackageVerificationRequest(
                    context=context,
                    package=package,
                )
            )
            if verification.verified is not True or verification.restore_ready is not True:
                raise RuntimeVerificationError()
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=BACKUP_VERIFIED,
                message="Independent package verification completed.",
                metadata={
                    "verified": True,
                    "restore_ready": True,
                    "entry_count": verification.entry_count,
                    "provider": verification.provider_identifier,
                },
            )
            self._run_hook("after_verification", backup.public_id)
            self._heartbeat(lock)

            stage = "encryption"
            encryption_request = EncryptedArtifactRequest(
                context=context,
                package=package,
                verification=verification,
            )
            encrypted = (
                self.provider_stack.encrypted_artifact_provider.encrypt_verified_package(
                    encryption_request
                )
            )
            if encrypted.plaintext_cleanup_incomplete:
                encrypted = self.provider_stack.encrypted_artifact_provider.retry_plaintext_package_cleanup(
                    encryption_request,
                    encrypted,
                )
            self.provider_stack.encrypted_artifact_provider.validate_owned_encrypted_artifact(
                context=context,
                result=encrypted,
            )
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=BACKUP_ENCRYPTED,
                message="Authenticated backup encryption completed.",
                metadata={
                    "byte_count": encrypted.encrypted_byte_count,
                    "format": encrypted.format_identifier,
                    "provider": encrypted.provider_identifier,
                },
            )
            self._run_hook("after_encryption", backup.public_id)
            self._heartbeat(lock)

            stage = "durable"
            backup = services.transition_backup(backup, BackupStatus.UPLOADING)
            storage_request = StoredBackupObjectRequest(
                context=context,
                encrypted_artifact=encrypted,
            )
            durable_store_attempted = True
            stored = self.provider_stack.durable_storage_provider.store_encrypted_artifact(
                storage_request
            )
            if stored.encrypted_staging_cleanup_incomplete:
                stored = self.provider_stack.durable_storage_provider.retry_encrypted_staging_cleanup(
                    storage_request,
                    stored,
                )
            self._run_hook("after_durable_publication", backup.public_id)
            self.provider_stack.durable_storage_provider.validate_stored_object(
                context=context,
                result=stored,
            )
            self._run_hook("after_durable_validation", backup.public_id)
            backup = self._persist_durable_metadata(
                backup=backup,
                stored=stored,
                phase2d1_result=phase2d1_result,
                verification=verification,
            )
            durable_metadata_persisted = True
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=BACKUP_DURABLE_STORED,
                message="The encrypted backup was durably stored and revalidated.",
                metadata={
                    "byte_count": stored.byte_count,
                    "backend": stored.backend_identifier,
                    "provider": stored.provider_identifier,
                },
            )
            self._heartbeat(lock)

            stage = "finalization"
            backup = services.transition_backup(backup, BackupStatus.VERIFYING)
            backup = services.set_backup_integrity(
                backup,
                IntegrityStatus.VERIFYING,
            )
            backup = services.set_backup_integrity(
                backup,
                IntegrityStatus.VERIFIED,
            )
            self._run_hook("before_success_finalization", backup.public_id)
            backup = services.transition_backup(backup, BackupStatus.SUCCEEDED)
            duration_ms = self._elapsed_ms(started)
            BackupRecord.objects.filter(pk=backup.pk).update(
                duration=timedelta(milliseconds=duration_ms),
                updated_at=self.clock(),
            )
            backup.refresh_from_db()
            self._activity(
                business=business,
                backup=backup,
                actor=actor,
                event_type=BACKUP_COMPLETED,
                message="Backup execution completed at the durable success boundary.",
                metadata={
                    "status": BackupStatus.SUCCEEDED,
                    "duration_ms": duration_ms,
                    "byte_count": stored.byte_count,
                },
            )
            self._run_hook("after_success_finalization", backup.public_id)
            self._heartbeat(lock)

            retention_outcome, retention_warning = self._execute_retention(
                business=business,
                backup=backup,
                actor=actor,
                context=context,
                stored=stored,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            if stored is not None and not durable_metadata_persisted:
                try:
                    backup = self._persist_durable_metadata(
                        backup=backup,
                        stored=stored,
                        phase2d1_result=phase2d1_result,
                        verification=verification,
                    )
                except Exception:
                    pass
            raise
        except Exception as exc:
            durable_preserved = stored is not None
            if stored is not None and not durable_metadata_persisted:
                try:
                    backup = self._persist_durable_metadata(
                        backup=backup,
                        stored=stored,
                        phase2d1_result=phase2d1_result,
                        verification=verification,
                    )
                    durable_metadata_persisted = True
                except Exception:
                    pass
            safe_error = self._safe_error(
                exc,
                durable_preserved=durable_preserved,
            )
            code, summary = self._failure_for_stage(stage, exc)
            if lock is not None:
                try:
                    backup = self._mark_failed(
                        backup=backup,
                        code=code,
                        summary=summary,
                    )
                    self._activity(
                        business=business,
                        backup=backup,
                        actor=actor,
                        event_type=BACKUP_FAILED,
                        severity=ActivitySeverity.ERROR,
                        message=summary,
                        metadata={
                            "stage": stage,
                            "error_code": code,
                            "durable_object_preserved": durable_preserved,
                        },
                    )
                except Exception:
                    pass
            raise safe_error from None
        finally:
            try:
                cleanup_incomplete = self._cleanup_transients(
                    context=context,
                    workspace=workspace,
                    snapshot=snapshot,
                    component_exports=component_exports,
                    phase2d1_result=phase2d1_result,
                    package=package,
                    verification=verification,
                    encrypted=encrypted,
                    stored=stored,
                    durable_store_attempted=durable_store_attempted,
                )
            finally:
                if cleanup_incomplete and lock is not None:
                    try:
                        self._activity(
                            business=business,
                            backup=backup,
                            actor=actor,
                            event_type=BACKUP_WORKSPACE_CLEANUP_DEFERRED,
                            severity=ActivitySeverity.WARNING,
                            message=(
                                "Exact transient cleanup requires a later safe retry."
                            ),
                            metadata={
                                "durable_object_preserved": stored is not None,
                            },
                        )
                    except Exception:
                        pass
                if lock is not None and owns_lock:
                    services.release_tenant_operation_lock(
                        lock,
                        lock_token=lock.lock_token,
                    )
        return BackupExecutionResult(
            backup_public_id=backup.public_id,
            business_public_id=business.public_id,
            final_status=BackupStatus.SUCCEEDED,
            stored_object=FinalStoredObjectEvidence(
                reference_identifier=stored.reference.identifier,
                backend_identifier=stored.backend_identifier,
                byte_count=stored.byte_count,
                sha256=stored.sha256,
                provider_identifier=stored.provider_identifier,
            ),
            total_duration_ms=duration_ms,
            completed_at=backup.completed_at,
            retention_outcome=retention_outcome,
            retention_warning_code=retention_warning,
            provider_stack_version=RUNTIME_PROVIDER_STACK_VERSION,
        )


def request_backup_execution(
    *,
    backup_public_id,
    business_public_id,
    worker_task_identifier="",
):
    """Guarded internal service boundary for a future dedicated worker."""

    from apps.backups.tasks import assert_safe_async_execution_configuration

    from .availability import assert_real_execution_available

    assert_real_execution_available()
    assert_safe_async_execution_configuration()
    business = Business.objects.filter(public_id=business_public_id).first()
    if business is None:
        raise BackupTenantMismatch()
    backup = (
        BackupRecord.objects.for_business(business)
        .select_related("created_by")
        .filter(public_id=backup_public_id)
        .first()
    )
    if backup is None:
        raise BackupTenantMismatch()
    execution_request = BackupExecutionRequest.from_record(
        backup,
        worker_task_identifier=worker_task_identifier,
    )
    coordinator = BackupExecutionCoordinator(
        provider_stack=build_runtime_provider_stack(),
    )
    return coordinator.execute(execution_request)


__all__ = [
    "BackupExecutionCoordinator",
    "BackupExecutionRequest",
    "BackupExecutionResult",
    "FinalStoredObjectEvidence",
    "InheritedTenantOperationLease",
    "RUNTIME_PROVIDER_STACK_VERSION",
    "RuntimeProviderStack",
    "RuntimeRetentionOutcome",
    "build_runtime_provider_stack",
    "request_backup_execution",
]
