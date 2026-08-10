"""Guarded Platform Admin action boundaries through Backup & Restore Phase 3E."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.tenants.models import Business

from . import selectors, services
from .engine.availability import (
    get_engine_capability,
    restore_execution_available,
    restore_preflight_configuration_ready,
)
from .engine.context import ActorIdentitySnapshot
from .engine.events import RESTORE_QUEUED
from .engine.restore_exceptions import RestoreEngineError
from .engine.restore_preflight import (
    RestorePreflightCleanupRequest,
    RestorePreflightCoordinator,
    RestorePreflightRequest,
    build_restore_preflight_provider_stack,
)
from .enums import BackupScope, BackupStatus, BackupTrigger, RestoreStatus
from .models import BackupActivity, RestoreOperation
from .platform_permissions import (
    PlatformBackupCapability,
    has_platform_backup_capability,
)

PLATFORM_MANUAL_BACKUP_REQUESTED = "platform.manual_backup_requested"
PLATFORM_RESTORE_PREFLIGHT_REQUESTED = "platform.restore_preflight_requested"
PLATFORM_RESTORE_REQUESTED = "platform.restore_requested"


class PlatformBackupActionUnavailable(Exception):
    """Safe refusal for a Platform Admin action that cannot run."""


@dataclass(frozen=True, slots=True)
class PlatformActionCapability:
    enabled: bool
    message: str


@dataclass(frozen=True, slots=True)
class PlatformPreflightOutcome:
    restore_public_id: str
    ready: bool
    compatibility: str
    component_count: int
    record_count: int
    media_count: int
    messages: tuple[str, ...]

    def as_session_value(self, *, business_public_id, backup_public_id, actor_public_id):
        return {
            "business_public_id": str(business_public_id),
            "backup_public_id": str(backup_public_id),
            "restore_public_id": self.restore_public_id,
            "actor_public_id": str(actor_public_id),
            "ready": self.ready,
            "compatibility": self.compatibility,
            "component_count": self.component_count,
            "record_count": self.record_count,
            "media_count": self.media_count,
            "messages": list(self.messages),
        }


def _require_capability(actor, capability):
    if not has_platform_backup_capability(actor, capability):
        raise PermissionDenied("The platform backup capability is not assigned.")


def manual_backup_capability():
    capability = get_engine_capability()
    if capability.real_execution_available:
        return PlatformActionCapability(True, "Secure backup execution is available.")
    return PlatformActionCapability(
        False,
        "Backup execution is unavailable. No request will be queued.",
    )


def restore_mutation_capability():
    capability = get_engine_capability()
    if not capability.restore_mutation_setting_enabled:
        return PlatformActionCapability(False, "Restore execution is not yet enabled.")
    if restore_execution_available():
        return PlatformActionCapability(True, "Secure restore execution is available.")
    return PlatformActionCapability(
        False,
        "Restore execution is unavailable until its dedicated secure worker is configured.",
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


def platform_request_manual_backup(*, business, actor, scope, request=None):
    """Persist and enqueue one exact-tenant request; never run backup work inline."""

    _require_capability(actor, PlatformBackupCapability.MANAGE_BACKUPS)
    capability = manual_backup_capability()
    if not capability.enabled:
        raise PlatformBackupActionUnavailable(capability.message)

    resolution = services.resolve_requested_scope(business, scope)
    with transaction.atomic():
        locked_business = Business.objects.select_for_update().get(pk=business.pk)
        if selectors.active_backup_exists(locked_business):
            raise PlatformBackupActionUnavailable(
                "A backup is already in progress for this business."
            )
        request_bucket = timezone.now().replace(second=0, microsecond=0).isoformat()
        backup = services.create_backup_request(
            business=locked_business,
            scope=resolution.scope,
            actor=actor,
            trigger=BackupTrigger.MANUAL,
            idempotency_key=services.generate_idempotency_key(
                "platform-manual",
                business.public_id,
                getattr(actor, "public_id", ""),
                resolution.scope,
                request_bucket,
            ),
            request=request,
        )
        if backup.status != BackupStatus.QUEUED:
            raise PlatformBackupActionUnavailable(
                "A recent backup request has already been handled. Refresh before retrying."
            )
        services.create_backup_activity(
            business=locked_business,
            backup=backup,
            actor=actor,
            request=request,
            event_type=PLATFORM_MANUAL_BACKUP_REQUESTED,
            sanitized_message="A manual backup was requested by Platform Administration.",
            structured_metadata={"scope": str(backup.scope), "actor_type": "platform_admin"},
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
        raise PlatformBackupActionUnavailable(
            "Manual backup could not be queued safely. Try again later."
        ) from None
    return backup


def _not_ready_outcome(restore, message):
    return PlatformPreflightOutcome(
        restore_public_id=str(restore.public_id),
        ready=False,
        compatibility="Not verified",
        component_count=0,
        record_count=0,
        media_count=0,
        messages=(message,),
    )


def platform_run_restore_preflight(*, business, backup, actor, reason, request=None):
    """Run Phase 3A against an explicitly bound tenant and clean plaintext evidence."""

    _require_capability(actor, PlatformBackupCapability.APPROVE_RESTORE)
    if backup.business_id != business.pk:
        raise ValidationError("The backup belongs to another business.")
    if not selectors.is_backup_restore_eligible(business, backup):
        raise PlatformBackupActionUnavailable("This backup is not eligible for restore.")
    if selectors.active_backup_exists(business):
        raise PlatformBackupActionUnavailable(
            "A backup is in progress for this business. Wait before running preflight."
        )
    if not restore_preflight_configuration_ready():
        raise PlatformBackupActionUnavailable(
            "Restore readiness is unavailable because secure backup providers are not configured. "
            "No business data was changed."
        )

    restore = services.create_restore_request(
        business=business,
        source_backup=backup,
        requested_scope=BackupScope(backup.scope),
        actor=actor,
        reason=reason,
        idempotency_key=services.generate_idempotency_key("platform-preflight"),
        request=request,
    )
    services.create_backup_activity(
        business=business,
        backup=backup,
        restore=restore,
        actor=actor,
        request=request,
        event_type=PLATFORM_RESTORE_PREFLIGHT_REQUESTED,
        reason=reason,
        sanitized_message="Restore readiness was requested by Platform Administration.",
        structured_metadata={"scope": str(restore.requested_scope), "mutation": False},
    )

    coordinator = None
    result = None
    try:
        coordinator = RestorePreflightCoordinator(
            provider_stack=build_restore_preflight_provider_stack()
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
        outcome = PlatformPreflightOutcome(
            restore_public_id=str(restore.public_id),
            ready=result.restore_ready is True,
            compatibility="Compatible" if result.restore_ready else "Not compatible",
            component_count=result.component_count,
            record_count=result.record_count,
            media_count=result.media_object_count,
            messages=(
                "Restore readiness checks passed."
                if result.restore_ready
                else "This backup did not pass every restore readiness check.",
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


def platform_request_restore(*, business, backup, restore, actor, request=None):
    """Audit and enqueue a confirmed request without running restore inline."""

    _require_capability(actor, PlatformBackupCapability.APPROVE_RESTORE)
    capability = restore_mutation_capability()
    if not capability.enabled:
        raise PlatformBackupActionUnavailable(capability.message)
    if backup.business_id != business.pk or restore.business_id != business.pk:
        raise ValidationError("The restore selection belongs to another business.")
    if restore.source_backup_id != backup.pk or restore.requested_by_id != actor.pk:
        raise ValidationError("The restore preflight does not match this request.")

    should_enqueue = False
    with transaction.atomic():
        current = (
            RestoreOperation.objects.select_for_update()
            .select_related("source_backup", "requested_by")
            .get(pk=restore.pk, business=business)
        )
        already_queued = BackupActivity.objects.filter(
            restore=current,
            event_type=RESTORE_QUEUED,
        ).exists()
        if already_queued and current.failure_code == "pre_mutation_async_enqueue_unavailable":
            raise PlatformBackupActionUnavailable(
                "This restore request was not delivered to the worker. Run restore preflight again before retrying."
            )
        if not already_queued:
            if current.status != RestoreStatus.QUEUED:
                raise PlatformBackupActionUnavailable(
                    "This restore request cannot be queued from its current state."
                )
            services.create_backup_activity(
                business=business,
                backup=backup,
                restore=current,
                actor=actor,
                request=request,
                event_type=PLATFORM_RESTORE_REQUESTED,
                reason=current.reason,
                sanitized_message=(
                    "A guarded restore request was confirmed by Platform Administration."
                ),
                structured_metadata={
                    "scope": str(current.requested_scope),
                    "queued": True,
                },
            )
            services.create_backup_activity(
                business=business,
                backup=backup,
                restore=current,
                actor=actor,
                request=request,
                event_type=RESTORE_QUEUED,
                reason=current.reason,
                sanitized_message="The restore request was queued for the dedicated worker.",
                structured_metadata={
                    "scope": str(current.requested_scope),
                    "queued": True,
                    "actor_type": "platform_admin",
                },
            )
            should_enqueue = True

    if should_enqueue:
        try:
            _enqueue_restore(
                restore_public_id=current.public_id,
                business_public_id=business.public_id,
            )
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            current.refresh_from_db()
            if current.status == RestoreStatus.QUEUED:
                services.transition_restore(
                    current,
                    RestoreStatus.FAILED,
                    failure_code="pre_mutation_async_enqueue_unavailable",
                    failure_summary=(
                        "Restore could not be queued. No restore work was started."
                    ),
                )
            raise PlatformBackupActionUnavailable(
                "Restore could not be queued safely. Try again later."
            ) from None
    current.refresh_from_db()
    return current


def _enqueue_restore(*, restore_public_id, business_public_id):
    from .tasks import RESTORE_QUEUE_NAME, execute_restore

    execute_restore.apply_async(
        kwargs={
            "restore_public_id": str(restore_public_id),
            "business_public_id": str(business_public_id),
        },
        queue=RESTORE_QUEUE_NAME,
        retry=True,
        retry_policy={
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 1,
            "interval_max": 5,
        },
    )
