"""Phase 3B guarded restore mutation and mandatory safety-backup coordination."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.backups import services
from apps.backups.enums import (
    ActivitySeverity,
    BackupScope,
    BackupStatus,
    BackupTrigger,
    IntegrityStatus,
    OperationKind,
    RestoreStatus,
)
from apps.backups.models import BackupActivity, RestoreOperation
from apps.tenants.models import Business

from .availability import restore_mutation_setting_enabled
from .context import ActorIdentitySnapshot
from .events import (
    RESTORE_COMPLETED,
    RESTORE_COMPONENT_COMPLETED,
    RESTORE_FAILED,
    RESTORE_MEDIA_COMPLETED,
    RESTORE_MUTATION_STARTED,
    RESTORE_POST_VERIFICATION_COMPLETED,
    RESTORE_PREFLIGHT_VALIDATED,
    RESTORE_RECOVERY_REQUIRED,
    RESTORE_SAFETY_BACKUP_COMPLETED,
    RESTORE_SAFETY_BACKUP_STARTED,
    RESTORE_STARTED,
)
from .logical_restore import LogicalRestoreEngine
from .media_restore import LocalFilesystemMediaRestoreProvider
from .restore_exceptions import (
    Phase3BCoordinationError,
    RestoreEngineError,
    RestoreLockLost,
    RestoreLockUnavailable,
    RestoreMutationError,
    RestoreRecoveryRequired,
    RestoreRollbackError,
    RestoreSafetyBackupError,
    RestoreSelectionError,
)
from .restore_preflight import (
    RestorePreflightCoordinator,
    RestorePreflightProviderStack,
    RestorePreflightResult,
    build_restore_preflight_provider_stack,
)
from .restore_verification import (
    IndependentRestoreStateVerifier,
    PostRestoreVerificationState,
)
from .runtime import (
    BackupExecutionCoordinator,
    BackupExecutionRequest,
    InheritedTenantOperationLease,
    RuntimeProviderStack,
    build_runtime_provider_stack,
)

RESTORE_RUNTIME_STACK_VERSION = "nexa.restore-runtime.v1"


class RestoreExecutionState(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED_BEFORE_MUTATION = "FAILED_BEFORE_MUTATION"
    FAILED_ROLLED_BACK = "FAILED_ROLLED_BACK"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass(frozen=True, slots=True)
class RestoreExecutionRequest:
    business_public_id: uuid.UUID
    selected_backup_public_id: uuid.UUID
    actor_identity: ActorIdentitySnapshot
    idempotency_key: str
    approved_preflight_result: RestorePreflightResult
    restore_request_public_id: uuid.UUID | None = None
    worker_task_identifier: str = ""


@dataclass(frozen=True, slots=True)
class RestoreExecutionResult:
    restore_operation_public_id: uuid.UUID
    business_public_id: uuid.UUID
    source_backup_public_id: uuid.UUID
    safety_backup_public_id: uuid.UUID
    final_state: RestoreExecutionState
    started_at: datetime
    completed_at: datetime
    component_count: int
    restored_record_count: int
    restored_media_count: int
    post_restore_verification_state: PostRestoreVerificationState
    sanitized_issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestoreRuntimeStack:
    backup_runtime_stack: RuntimeProviderStack
    preflight_provider_stack: RestorePreflightProviderStack
    preflight_coordinator: RestorePreflightCoordinator
    backup_coordinator: BackupExecutionCoordinator
    logical_restore_engine: LogicalRestoreEngine
    media_restore_provider: LocalFilesystemMediaRestoreProvider
    post_restore_verifier: IndependentRestoreStateVerifier

    def validated(self):
        if (
            type(self.backup_runtime_stack) is not RuntimeProviderStack
            or type(self.preflight_provider_stack) is not RestorePreflightProviderStack
            or type(self.preflight_coordinator) is not RestorePreflightCoordinator
            or type(self.backup_coordinator) is not BackupExecutionCoordinator
            or type(self.logical_restore_engine) is not LogicalRestoreEngine
            or type(self.media_restore_provider)
            is not LocalFilesystemMediaRestoreProvider
            or type(self.post_restore_verifier) is not IndependentRestoreStateVerifier
            or self.preflight_coordinator.provider_stack
            is not self.preflight_provider_stack
            or self.preflight_provider_stack.workspace_manager
            is not self.backup_runtime_stack.workspace_manager
            or self.preflight_provider_stack.durable_storage_provider
            is not self.backup_runtime_stack.durable_storage_provider
            or self.backup_coordinator.provider_stack is not self.backup_runtime_stack
            or self.post_restore_verifier.logical_engine
            is not self.logical_restore_engine
            or self.post_restore_verifier.media_provider
            is not self.media_restore_provider
        ):
            raise Phase3BCoordinationError(issue_code="restore_stack_invalid")
        return self


def build_restore_runtime_stack():
    """Build the single trusted restore composition without enabling mutation."""

    try:
        backup_stack = build_runtime_provider_stack()
        preflight_stack = build_restore_preflight_provider_stack(
            runtime_stack=backup_stack,
        )
        preflight_coordinator = RestorePreflightCoordinator(
            provider_stack=preflight_stack,
        )
        logical_engine = LogicalRestoreEngine()
        media_provider = LocalFilesystemMediaRestoreProvider()
        return RestoreRuntimeStack(
            backup_runtime_stack=backup_stack,
            preflight_provider_stack=preflight_stack,
            preflight_coordinator=preflight_coordinator,
            backup_coordinator=BackupExecutionCoordinator(provider_stack=backup_stack),
            logical_restore_engine=logical_engine,
            media_restore_provider=media_provider,
            post_restore_verifier=IndependentRestoreStateVerifier(
                logical_engine=logical_engine,
                media_provider=media_provider,
            ),
        ).validated()
    except RestoreEngineError:
        raise
    except Exception:
        raise Phase3BCoordinationError(issue_code="restore_stack_invalid") from None


class RestoreExecutionCoordinator:
    """Execute one persisted restore under one uninterrupted tenant lease."""

    def __init__(
        self,
        *,
        runtime_stack,
        clock=None,
        monotonic=None,
        lock_lease_seconds=None,
        failure_hook=None,
    ):
        if type(runtime_stack) is not RestoreRuntimeStack:
            raise Phase3BCoordinationError(issue_code="restore_stack_invalid")
        self.runtime_stack = runtime_stack.validated()
        self.clock = clock or timezone.now
        self.monotonic = monotonic or time.monotonic
        self.failure_hook = failure_hook
        selected_lease = (
            getattr(settings, "BACKUP_EXECUTION_LOCK_LEASE_SECONDS", 21_600)
            if lock_lease_seconds is None
            else lock_lease_seconds
        )
        if type(selected_lease) is not int or not 300 <= selected_lease <= 86_400:
            raise Phase3BCoordinationError(issue_code="restore_lock_policy_invalid")
        self.lock_lease_seconds = selected_lease

    def _hook(self, stage, operation_public_id):
        if self.failure_hook is not None:
            self.failure_hook(stage, operation_public_id)

    @staticmethod
    def _validate_request(request):
        if (
            type(request) is not RestoreExecutionRequest
            or type(request.business_public_id) is not uuid.UUID
            or type(request.selected_backup_public_id) is not uuid.UUID
            or type(request.actor_identity) is not ActorIdentitySnapshot
            or type(request.idempotency_key) is not str
            or not request.idempotency_key
            or len(request.idempotency_key) > 128
            or type(request.approved_preflight_result) is not RestorePreflightResult
            or request.restore_request_public_id is not None
            and type(request.restore_request_public_id) is not uuid.UUID
            or type(request.worker_task_identifier) is not str
            or len(request.worker_task_identifier) > 255
        ):
            raise RestoreSelectionError(issue_code="restore_request_invalid")

    @staticmethod
    def _resolve_request(request):
        business = Business.objects.filter(public_id=request.business_public_id).first()
        if business is None:
            raise RestoreSelectionError(issue_code="restore_selection_unavailable")
        restores = RestoreOperation.objects.for_business(business).select_related(
            "source_backup",
            "safety_backup",
            "requested_by",
        )
        if request.restore_request_public_id is not None:
            restore = restores.filter(public_id=request.restore_request_public_id).first()
        else:
            restore = restores.filter(idempotency_key=request.idempotency_key).first()
        if restore is None:
            raise RestoreSelectionError(issue_code="restore_request_unavailable")
        source = restore.source_backup
        actor = restore.requested_by
        if (
            restore.business_id != business.pk
            or restore.idempotency_key != request.idempotency_key
            or source.business_id != business.pk
            or source.public_id != request.selected_backup_public_id
            or source.tenant_public_id_snapshot != business.public_id
            or source.status != BackupStatus.SUCCEEDED
            or source.integrity_status != IntegrityStatus.VERIFIED
            or source.deleted_at is not None
            or ActorIdentitySnapshot.from_actor(actor) != request.actor_identity
            or request.approved_preflight_result.operation_reference != restore.public_id
            or request.approved_preflight_result.business_public_id != business.public_id
            or request.approved_preflight_result.backup_public_id != source.public_id
        ):
            raise RestoreSelectionError(issue_code="restore_request_mismatch")
        return business, restore, source, actor

    @staticmethod
    def _activity(
        *,
        business,
        restore,
        actor,
        event_type,
        message,
        metadata=None,
        severity=ActivitySeverity.INFO,
        backup=None,
    ):
        return services.create_backup_activity(
            business=business,
            event_type=event_type,
            backup=backup,
            restore=restore,
            actor=actor,
            severity=severity,
            sanitized_message=str(message)[:500],
            structured_metadata=dict(metadata or {}),
        )

    def _heartbeat(self, lock):
        if not services.heartbeat_tenant_operation_lock(
            lock,
            lock_token=lock.lock_token,
            lease_seconds=self.lock_lease_seconds,
        ):
            raise RestoreLockLost(issue_code="restore_lock_lost")

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, RestoreEngineError):
            return exc
        return RestoreMutationError(issue_code="restore_mutation_failed")

    def _existing_success_result(self, *, business, restore, source):
        if restore.status != RestoreStatus.SUCCEEDED or restore.safety_backup is None:
            return None
        activity = (
            BackupActivity.objects.for_business(business)
            .filter(restore=restore, event_type=RESTORE_COMPLETED)
            .order_by("-created_at")
            .first()
        )
        metadata = activity.structured_metadata if activity is not None else None
        if type(metadata) is not dict:
            raise Phase3BCoordinationError(issue_code="restore_result_evidence_missing")
        try:
            component_count = int(metadata["component_count"])
            record_count = int(metadata["restored_record_count"])
            media_count = int(metadata["restored_media_count"])
        except (KeyError, TypeError, ValueError):
            raise Phase3BCoordinationError(issue_code="restore_result_evidence_invalid") from None
        return RestoreExecutionResult(
            restore_operation_public_id=restore.public_id,
            business_public_id=business.public_id,
            source_backup_public_id=source.public_id,
            safety_backup_public_id=restore.safety_backup.public_id,
            final_state=RestoreExecutionState.SUCCESS,
            started_at=restore.started_at,
            completed_at=restore.completed_at,
            component_count=component_count,
            restored_record_count=record_count,
            restored_media_count=media_count,
            post_restore_verification_state=PostRestoreVerificationState.VERIFIED,
            sanitized_issues=(),
        )

    def _prepare_restore_state(self, *, business, restore, source):
        existing = self._existing_success_result(
            business=business,
            restore=restore,
            source=source,
        )
        if existing is not None:
            return restore, existing
        if restore.status == RestoreStatus.INDETERMINATE:
            raise RestoreRecoveryRequired(issue_code="restore_recovery_required")
        if restore.status in {RestoreStatus.ROLLED_BACK, RestoreStatus.ROLLING_BACK}:
            raise RestoreMutationError(issue_code="restore_retry_blocked")
        if restore.status == RestoreStatus.FAILED:
            try:
                restore = services.restart_failed_restore_before_mutation(restore)
            except Exception:
                raise RestoreMutationError(issue_code="restore_retry_blocked") from None
        elif restore.status == RestoreStatus.QUEUED:
            restore = services.transition_restore(restore, RestoreStatus.AUTHORIZING)
        elif restore.status != RestoreStatus.AUTHORIZING:
            raise RestoreMutationError(issue_code="restore_operation_active")
        return restore, None

    def _create_safety_backup(self, *, business, restore, actor, lock, request):
        current = RestoreOperation.objects.select_related("safety_backup").get(pk=restore.pk)
        safety = current.safety_backup
        if (
            safety is not None
            and safety.business_id == business.pk
            and safety.parent_restore_operation_id == current.pk
            and safety.trigger == BackupTrigger.PRE_RESTORE_SAFETY
            and safety.protected
            and not safety.retention_eligible
            and safety.status == BackupStatus.SUCCEEDED
            and safety.integrity_status == IntegrityStatus.VERIFIED
            and safety.deleted_at is None
        ):
            return safety
        safety = services.create_backup_request(
            business=business,
            scope=BackupScope.ALL_ENABLED,
            actor=actor,
            trigger=BackupTrigger.PRE_RESTORE_SAFETY,
            idempotency_key=(
                f"pre-restore-safety:{restore.public_id}:{uuid.uuid4().hex}"
            )[:128],
            parent_restore_operation=restore,
            system_actor=True,
        )
        inherited = InheritedTenantOperationLease(
            business_public_id=business.public_id,
            restore_operation_public_id=restore.public_id,
            lock_token=lock.lock_token,
        )
        try:
            result = self.runtime_stack.backup_coordinator.execute(
                BackupExecutionRequest.from_record(
                    safety,
                    worker_task_identifier=request.worker_task_identifier,
                ),
                inherited_lease=inherited,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            raise RestoreSafetyBackupError(
                issue_code="safety_backup_execution_failed"
            ) from None
        safety.refresh_from_db()
        if (
            result.final_status != BackupStatus.SUCCEEDED
            or safety.status != BackupStatus.SUCCEEDED
            or safety.integrity_status != IntegrityStatus.VERIFIED
            or not safety.protected
            or safety.retention_eligible
            or safety.parent_restore_operation_id != restore.pk
            or not safety.storage_backend_identifier
            or not safety.opaque_object_key
            or not safety.whole_artifact_hash
            or safety.backup_size_bytes <= 0
            or str(result.stored_object.reference_identifier)
            != safety.opaque_object_key
            or result.stored_object.bucket_identifier
            != safety.storage_bucket_identifier
            or result.stored_object.version_identifier
            != safety.storage_object_version_identifier
            or result.stored_object.sha256 != safety.whole_artifact_hash
            or result.stored_object.byte_count != safety.backup_size_bytes
        ):
            raise RestoreSafetyBackupError(issue_code="safety_backup_not_durable")
        services.set_restore_safety_backup(restore, safety)
        return safety

    @staticmethod
    def _mark_pre_mutation_failure(restore, error):
        current = RestoreOperation.objects.get(pk=restore.pk)
        if current.status in {
            RestoreStatus.AUTHORIZING,
            RestoreStatus.LOCKING,
            RestoreStatus.SAFETY_BACKUP,
            RestoreStatus.VALIDATING,
        }:
            return services.transition_restore(
                current,
                RestoreStatus.FAILED,
                failure_code=f"pre_mutation_{error.issue_code}"[:80],
                failure_summary=error.sanitized_message,
            )
        return current

    @staticmethod
    def _mark_rolled_back(restore, error):
        current = RestoreOperation.objects.get(pk=restore.pk)
        if current.status in {RestoreStatus.RESTORING, RestoreStatus.VERIFYING, RestoreStatus.FAILED}:
            current = services.transition_restore(current, RestoreStatus.ROLLING_BACK)
        if current.status == RestoreStatus.ROLLING_BACK:
            current = services.transition_restore(
                current,
                RestoreStatus.ROLLED_BACK,
                rollback_result="Database rollback and provider-owned media rollback were verified.",
            )
        return current

    @staticmethod
    def _mark_recovery_required(restore, error):
        current = RestoreOperation.objects.get(pk=restore.pk)
        if current.status in {RestoreStatus.RESTORING, RestoreStatus.VERIFYING, RestoreStatus.FAILED}:
            return services.transition_restore(
                current,
                RestoreStatus.INDETERMINATE,
                failure_code="restore_recovery_required",
                failure_summary=error.sanitized_message,
            )
        return current

    def execute(self, request):
        self._validate_request(request)
        if not restore_mutation_setting_enabled():
            raise RestoreMutationError(issue_code="restore_mutation_disabled")
        business, restore, source, actor = self._resolve_request(request)
        restore, existing = self._prepare_restore_state(
            business=business,
            restore=restore,
            source=source,
        )
        if existing is not None:
            return existing

        lock = None
        consumption = None
        staged_media = None
        logical_result = None
        verification_result = None
        safety = None
        mutation_started = False
        mutation_committed = False
        safe_error = None
        abort_error = None
        abort_traceback = None
        rollback_proven = False
        try:
            restore = services.transition_restore(restore, RestoreStatus.LOCKING)
            try:
                lock = services.acquire_tenant_operation_lock(
                    business=business,
                    operation_kind=OperationKind.RESTORE,
                    operation_public_id=restore.public_id,
                    worker_task_identifier=request.worker_task_identifier,
                    lease_seconds=self.lock_lease_seconds,
                )
            except services.TenantOperationLocked:
                raise RestoreLockUnavailable(issue_code="restore_lock_unavailable") from None
            self._activity(
                business=business,
                restore=restore,
                actor=actor,
                event_type=RESTORE_STARTED,
                message="Guarded tenant restore execution started.",
                metadata={"source_backup_public_id": str(source.public_id)},
                backup=source,
            )
            consumption = self.runtime_stack.preflight_coordinator.revalidate_for_execution(
                operation_public_id=restore.public_id,
                business_public_id=business.public_id,
                backup_public_id=source.public_id,
                actor_identity=request.actor_identity,
                approved_result=request.approved_preflight_result,
            )
            self._heartbeat(lock)
            self._activity(
                business=business,
                restore=restore,
                actor=actor,
                event_type=RESTORE_PREFLIGHT_VALIDATED,
                message="Restore preflight evidence was revalidated.",
                metadata={"component_count": consumption.result.component_count},
                backup=source,
            )
            restore = services.transition_restore(restore, RestoreStatus.SAFETY_BACKUP)
            self._activity(
                business=business,
                restore=restore,
                actor=actor,
                event_type=RESTORE_SAFETY_BACKUP_STARTED,
                message="Mandatory pre-restore safety backup started.",
                backup=source,
            )
            self._hook("before_safety_backup", restore.public_id)
            safety = self._create_safety_backup(
                business=business,
                restore=restore,
                actor=actor,
                lock=lock,
                request=request,
            )
            self._hook("after_safety_backup", restore.public_id)
            self._heartbeat(lock)
            self._activity(
                business=business,
                restore=restore,
                actor=actor,
                event_type=RESTORE_SAFETY_BACKUP_COMPLETED,
                message="Mandatory safety backup reached durable verified success.",
                metadata={"safety_backup_public_id": str(safety.public_id)},
                backup=safety,
            )
            restore = services.transition_restore(restore, RestoreStatus.VALIDATING)
            consumption = self.runtime_stack.preflight_coordinator.revalidate_for_execution(
                operation_public_id=restore.public_id,
                business_public_id=business.public_id,
                backup_public_id=source.public_id,
                actor_identity=request.actor_identity,
                approved_result=request.approved_preflight_result,
            )
            prepared = self.runtime_stack.logical_restore_engine.prepare(
                consumption=consumption,
                package_provider=self.runtime_stack.preflight_provider_stack.restored_package_provider,
            )
            self.runtime_stack.logical_restore_engine.validate_non_mutating_dependencies(
                business=business,
                prepared=prepared,
            )
            staged_media = self.runtime_stack.media_restore_provider.stage(
                consumption=consumption,
                prepared=prepared,
                package_provider=self.runtime_stack.preflight_provider_stack.restored_package_provider,
            )
            self._heartbeat(lock)
            restore = services.transition_restore(restore, RestoreStatus.RESTORING)
            mutation_started = True
            self._activity(
                business=business,
                restore=restore,
                actor=actor,
                event_type=RESTORE_MUTATION_STARTED,
                message="Tenant-scoped logical restore mutation started.",
                metadata={"component_count": len(prepared.component_keys)},
                backup=source,
            )
            self._hook("before_database_mutation", restore.public_id)
            with transaction.atomic():
                original_hook = self.runtime_stack.logical_restore_engine.component_completed_hook

                def component_completed(component_key):
                    if original_hook is not None:
                        original_hook(component_key)
                    self._activity(
                        business=business,
                        restore=restore,
                        actor=actor,
                        event_type=RESTORE_COMPONENT_COMPLETED,
                        message="A registered restore component completed.",
                        metadata={"component": component_key},
                        backup=source,
                    )

                self.runtime_stack.logical_restore_engine.component_completed_hook = component_completed
                try:
                    logical_result = self.runtime_stack.logical_restore_engine.mutate(
                        business=business,
                        prepared=prepared,
                    )
                finally:
                    self.runtime_stack.logical_restore_engine.component_completed_hook = original_hook
                self._hook("after_database_mutation", restore.public_id)
                publication = self.runtime_stack.media_restore_provider.publish(staged_media)
                self._activity(
                    business=business,
                    restore=restore,
                    actor=actor,
                    event_type=RESTORE_MEDIA_COMPLETED,
                    message="Restored media publication completed.",
                    metadata={
                        "object_count": publication.object_count,
                        "created_count": publication.created_count,
                        "reused_count": publication.reused_count,
                    },
                    backup=source,
                )
                restore = services.transition_restore(restore, RestoreStatus.VERIFYING)
                verification_result = self.runtime_stack.post_restore_verifier.verify(
                    business=business,
                    prepared=prepared,
                    staged_media=staged_media,
                )
                self._hook("after_post_verification", restore.public_id)
            mutation_committed = True
            self._activity(
                business=business,
                restore=restore,
                actor=actor,
                event_type=RESTORE_POST_VERIFICATION_COMPLETED,
                message="Independent restored-state verification completed.",
                metadata={
                    "state": verification_result.state.value,
                    "record_count": verification_result.record_count,
                    "media_count": verification_result.media_count,
                },
                backup=source,
            )
            self.runtime_stack.media_restore_provider.cleanup(staged_media)
            staged_media = None
            self.runtime_stack.preflight_coordinator.cleanup_consumed_preflight(consumption)
            consumption = None
            restore = services.transition_restore(restore, RestoreStatus.SUCCEEDED)
            self._activity(
                business=business,
                restore=restore,
                actor=actor,
                event_type=RESTORE_COMPLETED,
                message="Tenant restore completed with verified state.",
                metadata={
                    "component_count": logical_result.component_count,
                    "restored_record_count": logical_result.restored_record_count,
                    "restored_media_count": verification_result.media_count,
                    "safety_backup_public_id": str(safety.public_id),
                },
                backup=source,
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                abort_error = exc
                abort_traceback = exc.__traceback__
                safe_error = RestoreMutationError(issue_code="restore_abort_signal")
            else:
                safe_error = self._safe_error(exc)

        if safe_error is not None:
            if mutation_started and not mutation_committed:
                try:
                    if staged_media is not None:
                        self.runtime_stack.media_restore_provider.rollback(staged_media)
                    rollback_proven = True
                except BaseException as rollback_exc:
                    if isinstance(rollback_exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                        abort_error = abort_error or rollback_exc
                        abort_traceback = abort_traceback or rollback_exc.__traceback__
                    safe_error = RestoreRollbackError(issue_code="restore_rollback_unproven")
                    rollback_proven = False
            try:
                if not mutation_started:
                    restore = self._mark_pre_mutation_failure(restore, safe_error)
                    final_event = RESTORE_FAILED
                elif not mutation_committed and rollback_proven:
                    restore = self._mark_rolled_back(restore, safe_error)
                    final_event = RESTORE_FAILED
                else:
                    restore = self._mark_recovery_required(restore, safe_error)
                    safe_error = RestoreRecoveryRequired(issue_code="restore_recovery_required")
                    final_event = RESTORE_RECOVERY_REQUIRED
                self._activity(
                    business=business,
                    restore=restore,
                    actor=actor,
                    event_type=final_event,
                    severity=(
                        ActivitySeverity.CRITICAL
                        if final_event == RESTORE_RECOVERY_REQUIRED
                        else ActivitySeverity.ERROR
                    ),
                    message=safe_error.sanitized_message,
                    metadata={
                        "error_code": safe_error.issue_code,
                        "mutation_started": mutation_started,
                        "rollback_proven": rollback_proven,
                        "safety_backup_available": safety is not None,
                    },
                    backup=source,
                )
            except Exception:
                if mutation_started:
                    safe_error = RestoreRecoveryRequired(issue_code="restore_recovery_required")

        cleanup_error = None
        if staged_media is not None:
            try:
                self.runtime_stack.media_restore_provider.cleanup(staged_media)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    abort_error = abort_error or exc
                    abort_traceback = abort_traceback or exc.__traceback__
                else:
                    cleanup_error = RestoreMutationError(issue_code="restore_media_cleanup_failed")
        if consumption is not None:
            try:
                self.runtime_stack.preflight_coordinator.cleanup_consumed_preflight(consumption)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    abort_error = abort_error or exc
                    abort_traceback = abort_traceback or exc.__traceback__
                elif cleanup_error is None:
                    cleanup_error = RestoreMutationError(issue_code="restore_preflight_cleanup_failed")
        if cleanup_error is not None and safe_error is None:
            safe_error = cleanup_error
            restore = self._mark_recovery_required(restore, safe_error)

        if lock is not None:
            try:
                if not services.release_tenant_operation_lock(
                    lock,
                    lock_token=lock.lock_token,
                ):
                    raise RestoreLockLost(issue_code="restore_lock_release_failed")
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    abort_error = abort_error or exc
                    abort_traceback = abort_traceback or exc.__traceback__
                elif safe_error is None:
                    safe_error = RestoreLockLost(issue_code="restore_lock_release_failed")

        if abort_error is not None:
            raise abort_error.with_traceback(abort_traceback)
        if safe_error is not None:
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        if (
            restore.status != RestoreStatus.SUCCEEDED
            or safety is None
            or logical_result is None
            or verification_result is None
        ):
            raise Phase3BCoordinationError(issue_code="restore_finalization_invalid")
        return RestoreExecutionResult(
            restore_operation_public_id=restore.public_id,
            business_public_id=business.public_id,
            source_backup_public_id=source.public_id,
            safety_backup_public_id=safety.public_id,
            final_state=RestoreExecutionState.SUCCESS,
            started_at=restore.started_at,
            completed_at=restore.completed_at,
            component_count=logical_result.component_count,
            restored_record_count=logical_result.restored_record_count,
            restored_media_count=verification_result.media_count,
            post_restore_verification_state=verification_result.state,
            sanitized_issues=(),
        )


def execute_restore(request, *, runtime_stack=None):
    """Explicit internal entrypoint; setting remains disabled by default."""

    stack = runtime_stack or build_restore_runtime_stack()
    return RestoreExecutionCoordinator(runtime_stack=stack).execute(request)


__all__ = [
    "RESTORE_RUNTIME_STACK_VERSION",
    "RestoreExecutionCoordinator",
    "RestoreExecutionRequest",
    "RestoreExecutionResult",
    "RestoreExecutionState",
    "RestoreRuntimeStack",
    "build_restore_runtime_stack",
    "execute_restore",
]
