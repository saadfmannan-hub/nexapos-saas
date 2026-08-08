"""Owner-facing orchestration boundaries for Backup & Restore Phase 3C."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.tenants.models import Business

from . import selectors, services
from .engine.availability import get_engine_capability
from .engine.context import ActorIdentitySnapshot
from .engine.restore_exceptions import RestoreEngineError
from .engine.restore_preflight import (
    RestorePreflightCleanupRequest,
    RestorePreflightCoordinator,
    RestorePreflightRequest,
    build_restore_preflight_provider_stack,
)
from .enums import BackupScope, BackupStatus, BackupTrigger

MANUAL_BACKUP_REQUESTED = "backup.manual_requested"
RESTORE_PREFLIGHT_REQUESTED = "restore.preflight_requested"


class OwnerBackupActionUnavailable(Exception):
    """Safe user-facing refusal for an unavailable owner action."""


@dataclass(frozen=True, slots=True)
class OwnerActionCapability:
    enabled: bool
    message: str


@dataclass(frozen=True, slots=True)
class OwnerPreflightOutcome:
    restore_public_id: str
    ready: bool
    compatibility: str
    component_count: int
    record_count: int
    media_count: int
    messages: tuple[str, ...]

    def as_session_value(self, *, business_public_id, backup_public_id):
        return {
            "business_public_id": str(business_public_id),
            "backup_public_id": str(backup_public_id),
            "restore_public_id": self.restore_public_id,
            "ready": self.ready,
            "compatibility": self.compatibility,
            "component_count": self.component_count,
            "record_count": self.record_count,
            "media_count": self.media_count,
            "messages": list(self.messages),
        }


def manual_backup_capability():
    capability = get_engine_capability()
    if capability.real_execution_available:
        return OwnerActionCapability(True, "Secure backup execution is available.")
    return OwnerActionCapability(
        False,
        "Manual backup is temporarily unavailable. Your existing recovery points remain safe.",
    )


def _enqueue_backup(*, backup_public_id, business_public_id):
    from .tasks import BACKUP_QUEUE_NAME, execute_backup

    execute_backup.apply_async(
        kwargs={
            "backup_public_id": str(backup_public_id),
            "business_public_id": str(business_public_id),
        },
        queue=BACKUP_QUEUE_NAME,
        retry=True,
        retry_policy={
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 1,
            "interval_max": 5,
        },
    )


def request_manual_backup(*, business, actor, scope, request=None):
    """Persist and enqueue one idempotent owner request; never execute inline."""

    capability = manual_backup_capability()
    if not capability.enabled:
        raise OwnerBackupActionUnavailable(capability.message)

    resolution = services.resolve_requested_scope(business, scope)
    with transaction.atomic():
        # Serialize rapid owner clicks on databases with row-lock support. The
        # existing tenant/idempotency constraints remain the durable fallback.
        locked_business = Business.objects.select_for_update().get(pk=business.pk)
        if selectors.active_backup_exists(locked_business):
            raise OwnerBackupActionUnavailable(
                "A backup is already in progress. Wait for it to finish before trying again."
            )
        request_bucket = timezone.now().replace(second=0, microsecond=0).isoformat()
        backup = services.create_backup_request(
            business=locked_business,
            scope=resolution.scope,
            actor=actor,
            trigger=BackupTrigger.MANUAL,
            idempotency_key=services.generate_idempotency_key(
                "owner-manual",
                business.public_id,
                getattr(actor, "public_id", ""),
                resolution.scope,
                request_bucket,
            ),
            request=request,
        )
        if backup.status != BackupStatus.QUEUED:
            raise OwnerBackupActionUnavailable(
                "A recent backup request has already been handled. Refresh the page before trying again."
            )
        services.create_backup_activity(
            business=locked_business,
            backup=backup,
            actor=actor,
            request=request,
            event_type=MANUAL_BACKUP_REQUESTED,
            sanitized_message="A manual backup was requested by the business owner.",
            structured_metadata={"scope": str(backup.scope)},
        )

    try:
        _enqueue_backup(
            backup_public_id=backup.public_id,
            business_public_id=business.public_id,
        )
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        if backup.status == BackupStatus.QUEUED:
            services.transition_backup(
                backup,
                BackupStatus.FAILED,
                failure_code="async_enqueue_unavailable",
                failure_summary="Backup could not be queued. No backup work was started.",
            )
        raise OwnerBackupActionUnavailable(
            "Manual backup could not be queued safely. Please try again later."
        ) from None
    return backup


def _not_ready_outcome(restore, message):
    return OwnerPreflightOutcome(
        restore_public_id=str(restore.public_id),
        ready=False,
        compatibility="Not verified",
        component_count=0,
        record_count=0,
        media_count=0,
        messages=(message,),
    )


def run_restore_preflight(*, business, backup, actor, reason, request=None):
    """Run Phase 3A only, then remove its transient plaintext workspace."""

    if not selectors.is_backup_restore_eligible(business, backup):
        raise OwnerBackupActionUnavailable(
            "This backup is not eligible for restore."
        )
    if selectors.active_backup_exists(business):
        raise OwnerBackupActionUnavailable(
            "A backup is currently in progress. Wait for it to finish before checking restore readiness."
        )

    restore = services.create_restore_request(
        business=business,
        source_backup=backup,
        requested_scope=BackupScope(backup.scope),
        actor=actor,
        reason=reason,
        idempotency_key=services.generate_idempotency_key("owner-preflight"),
        request=request,
    )
    services.create_backup_activity(
        business=business,
        backup=backup,
        restore=restore,
        actor=actor,
        request=request,
        event_type=RESTORE_PREFLIGHT_REQUESTED,
        sanitized_message="Restore readiness was requested by the business owner.",
        structured_metadata={"scope": str(restore.requested_scope)},
    )

    coordinator = None
    result = None
    try:
        coordinator = RestorePreflightCoordinator(
            provider_stack=build_restore_preflight_provider_stack(),
        )
        result = coordinator.run(
            RestorePreflightRequest(
                operation_public_id=restore.public_id,
                business_public_id=business.public_id,
                backup_public_id=backup.public_id,
                actor_identity=ActorIdentitySnapshot.from_actor(actor),
                idempotency_key=restore.idempotency_key,
            )
        )
        outcome = OwnerPreflightOutcome(
            restore_public_id=str(restore.public_id),
            ready=result.restore_ready is True,
            compatibility=(
                "Compatible" if result.restore_ready else "Not compatible"
            ),
            component_count=result.component_count,
            record_count=result.record_count,
            media_count=result.media_object_count,
            messages=(
                (
                    "Restore readiness checks passed."
                    if result.restore_ready
                    else "This backup did not pass every restore readiness check."
                ),
            ),
        )
    except (RestoreEngineError, ValidationError):
        return _not_ready_outcome(
            restore,
            "Restore readiness could not be verified safely. No business data was changed.",
        )
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        return _not_ready_outcome(
            restore,
            "Restore readiness could not be verified safely. No business data was changed.",
        )

    if result.preflight_reference is not None and coordinator is not None:
        try:
            coordinator.cleanup_restore_preflight(
                RestorePreflightCleanupRequest(
                    operation_public_id=restore.public_id,
                    business_public_id=business.public_id,
                    backup_public_id=backup.public_id,
                    preflight_reference=result.preflight_reference,
                )
            )
        except RestoreEngineError:
            return _not_ready_outcome(
                restore,
                "Restore readiness evidence could not be closed safely. No business data was changed.",
            )
    return outcome


def restore_mutation_capability():
    capability = get_engine_capability()
    if not capability.restore_mutation_setting_enabled:
        return OwnerActionCapability(
            False,
            "Restore is currently disabled by the system administrator.",
        )
    # Phase 3A evidence is process-local and Phase 3B has no Celery restore
    # handoff. Never imply that toggling the mutation flag alone is sufficient.
    return OwnerActionCapability(
        False,
        "Restore is waiting for its dedicated secure restore worker.",
    )
