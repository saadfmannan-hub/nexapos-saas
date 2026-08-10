"""Celery-only backup, schedule-dispatch, and restore execution boundaries."""

from __future__ import annotations

import random
import uuid

from celery import shared_task
from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.exceptions import ImproperlyConfigured

from .engine.availability import (
    OPERATIONAL_PROVIDER_STACK_READY,
    RESTORE_ASYNC_EXECUTION_BOUNDARY_READY,
    RESTORE_MUTATION_ENGINE_READY,
    assert_real_execution_available,
    engine_setting_enabled,
    restore_mutation_setting_enabled,
    restore_runtime_configuration_ready,
)

BACKUP_QUEUE_NAME = "nexa.backups"
BACKUP_SCHEDULER_QUEUE_NAME = "nexa.backup_scheduling"
RESTORE_QUEUE_NAME = "nexa.restores"
VERIFICATION_QUEUE_NAME = "nexa.backup_verification"

BACKUP_EXECUTION_TASK_NAME = "apps.backups.tasks.execute_backup"
RESTORE_EXECUTION_TASK_NAME = "apps.backups.tasks.execute_restore"
SCHEDULE_DISPATCH_TASK_NAME = "apps.backups.tasks.dispatch_due_backup_schedules"


def _configured_route(task_name):
    routes = getattr(settings, "CELERY_TASK_ROUTES", {})
    if not isinstance(routes, dict):
        return None
    route = routes.get(task_name)
    if not isinstance(route, dict):
        return None
    return route


def assert_safe_async_execution_configuration():
    """Require a broker, non-eager delivery, and the exact isolated queue."""

    if not getattr(settings, "CELERY_BROKER_URL", ""):
        raise ImproperlyConfigured(
            "Backup execution requires a dedicated Celery broker."
        )
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        raise ImproperlyConfigured(
            "Backup execution cannot run with CELERY_TASK_ALWAYS_EAGER enabled."
        )
    if getattr(settings, "BACKUP_EXECUTION_QUEUE_NAME", "") != BACKUP_QUEUE_NAME:
        raise ImproperlyConfigured("The dedicated backup queue is not configured safely.")
    route = _configured_route(BACKUP_EXECUTION_TASK_NAME)
    if route is None or route.get("queue") != BACKUP_QUEUE_NAME:
        raise ImproperlyConfigured("The backup execution task route is not isolated.")
    return True


def assert_safe_restore_async_execution_configuration():
    """Fail closed unless restore has its exact non-eager worker boundary."""

    if not getattr(settings, "CELERY_BROKER_URL", ""):
        raise ImproperlyConfigured(
            "Restore execution requires a dedicated Celery broker."
        )
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        raise ImproperlyConfigured(
            "Restore execution cannot run with CELERY_TASK_ALWAYS_EAGER enabled."
        )
    if getattr(settings, "BACKUP_RESTORE_QUEUE_NAME", "") != RESTORE_QUEUE_NAME:
        raise ImproperlyConfigured("The dedicated restore queue is not configured safely.")
    route = _configured_route(RESTORE_EXECUTION_TASK_NAME)
    if route is None or route.get("queue") != RESTORE_QUEUE_NAME:
        raise ImproperlyConfigured("The restore execution task route is not isolated.")
    provider_hard_limit = getattr(
        settings,
        "BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS",
        None,
    )
    soft_limit = getattr(
        settings,
        "BACKUP_RESTORE_TASK_SOFT_TIME_LIMIT_SECONDS",
        None,
    )
    hard_limit = getattr(settings, "BACKUP_RESTORE_TASK_TIME_LIMIT_SECONDS", None)
    if (
        type(provider_hard_limit) is not int
        or type(soft_limit) is not int
        or type(hard_limit) is not int
        or not provider_hard_limit < soft_limit < hard_limit <= 90_000
    ):
        raise ImproperlyConfigured("The restore worker time limits are not safe.")
    return True


def assert_worker_execution_context(task_request, *, expected_queue):
    """Reject direct, eager, or incorrectly routed task invocation."""

    delivery_info = getattr(task_request, "delivery_info", None)
    if (
        task_request is None
        or getattr(task_request, "called_directly", True) is not False
        or getattr(task_request, "is_eager", True) is not False
        or not isinstance(delivery_info, dict)
        or delivery_info.get("routing_key") != expected_queue
    ):
        raise ImproperlyConfigured(
            "Backup and restore work may run only in its dedicated Celery worker queue."
        )
    return True


@register(Tags.security)
def check_backup_task_and_schedule_configuration(app_configs, **kwargs):
    """Validate bounded task limits and the fixed Beat dispatcher definition."""

    errors = []
    cadence = getattr(settings, "BACKUP_SCHEDULE_DISPATCH_INTERVAL_SECONDS", None)
    soft_limit = getattr(settings, "BACKUP_EXECUTION_TASK_SOFT_TIME_LIMIT_SECONDS", None)
    hard_limit = getattr(settings, "BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS", None)
    if type(cadence) is not int or not 60 <= cadence <= 3_600:
        errors.append(
            Error(
                "The backup schedule dispatcher cadence is invalid.",
                hint="Use a bounded 60 to 3600 second dispatcher interval.",
                id="backups.E037",
            )
        )
    if (
        type(soft_limit) is not int
        or type(hard_limit) is not int
        or not 3_600 <= soft_limit < hard_limit <= 90_000
    ):
        errors.append(
            Error(
                "The backup worker time limits are invalid.",
                hint="Keep a bounded hard limit above the long-running soft limit.",
                id="backups.E038",
            )
        )
    beat = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
    entry = beat.get("dispatch-due-backup-schedules") if isinstance(beat, dict) else None
    expected_cadence = float(cadence) if type(cadence) is int else None
    if (
        not isinstance(entry, dict)
        or entry.get("task") != SCHEDULE_DISPATCH_TASK_NAME
        or entry.get("schedule") != expected_cadence
        or not isinstance(entry.get("options"), dict)
        or entry["options"].get("queue") != BACKUP_SCHEDULER_QUEUE_NAME
    ):
        errors.append(
            Error(
                "The fixed backup Beat dispatcher entry is not configured safely.",
                id="backups.E039",
            )
        )
    return errors


@register(Tags.security)
def check_backup_async_execution_configuration(app_configs, **kwargs):
    """Block engine enablement unless every asynchronous boundary is safe."""

    if not engine_setting_enabled():
        return []
    errors = []
    if not getattr(settings, "CELERY_BROKER_URL", ""):
        errors.append(
            Error(
                "The backup engine requires a dedicated Celery broker.",
                id="backups.E010",
            )
        )
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        errors.append(
            Error(
                "The backup engine cannot run in Celery eager mode.",
                hint="Use a dedicated worker and keep backup work out of Gunicorn.",
                id="backups.E011",
            )
        )
    if not OPERATIONAL_PROVIDER_STACK_READY:
        errors.append(
            Error(
                "The backup engine provider stack is not operational.",
                hint=(
                    "The worker and scheduler boundaries exist, but restart-persistent "
                    "historical durable attestation plus production KMS and storage "
                    "safeguards remain incomplete. Keep "
                    "BACKUP_EXECUTION_ENGINE_ENABLED false."
                ),
                id="backups.E012",
            )
        )
    if getattr(settings, "BACKUP_EXECUTION_QUEUE_NAME", "") != BACKUP_QUEUE_NAME:
        errors.append(
            Error(
                "The heavy backup task must use the nexa.backups queue.",
                id="backups.E034",
            )
        )
    execution_route = _configured_route(BACKUP_EXECUTION_TASK_NAME)
    if execution_route is None or execution_route.get("queue") != BACKUP_QUEUE_NAME:
        errors.append(
            Error(
                "The heavy backup task route is not isolated.",
                id="backups.E035",
            )
        )
    dispatch_route = _configured_route(SCHEDULE_DISPATCH_TASK_NAME)
    if (
        dispatch_route is None
        or dispatch_route.get("queue") != BACKUP_SCHEDULER_QUEUE_NAME
    ):
        errors.append(
            Error(
                "The backup dispatcher task route is not configured safely.",
                id="backups.E036",
            )
        )
    return errors


@register(Tags.security)
def check_restore_async_execution_configuration(app_configs, **kwargs):
    """Reject mutation enablement without the complete restore boundary."""

    if not restore_mutation_setting_enabled():
        return []
    errors = []
    if not RESTORE_MUTATION_ENGINE_READY or not RESTORE_ASYNC_EXECUTION_BOUNDARY_READY:
        errors.append(
            Error(
                "The guarded restore mutation boundary is not code-ready.",
                id="backups.E040",
            )
        )
    if not getattr(settings, "CELERY_BROKER_URL", ""):
        errors.append(
            Error(
                "Restore mutation requires a dedicated Celery broker.",
                id="backups.E041",
            )
        )
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        errors.append(
            Error(
                "Restore mutation cannot run in Celery eager mode.",
                id="backups.E042",
            )
        )
    if getattr(settings, "BACKUP_RESTORE_QUEUE_NAME", "") != RESTORE_QUEUE_NAME:
        errors.append(
            Error(
                "The restore task must use the nexa.restores queue.",
                id="backups.E043",
            )
        )
    route = _configured_route(RESTORE_EXECUTION_TASK_NAME)
    if route is None or route.get("queue") != RESTORE_QUEUE_NAME:
        errors.append(
            Error(
                "The restore execution task route is not isolated.",
                id="backups.E044",
            )
        )
    provider_hard_limit = getattr(
        settings,
        "BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS",
        None,
    )
    soft_limit = getattr(
        settings,
        "BACKUP_RESTORE_TASK_SOFT_TIME_LIMIT_SECONDS",
        None,
    )
    hard_limit = getattr(settings, "BACKUP_RESTORE_TASK_TIME_LIMIT_SECONDS", None)
    if (
        type(provider_hard_limit) is not int
        or type(soft_limit) is not int
        or type(hard_limit) is not int
        or not provider_hard_limit < soft_limit < hard_limit <= 90_000
    ):
        errors.append(
            Error(
                "The restore worker time limits are not safely bounded.",
                id="backups.E045",
            )
        )
    async_error_ids = {
        "backups.E041",
        "backups.E042",
        "backups.E043",
        "backups.E044",
        "backups.E045",
    }
    async_safe = not any(error.id in async_error_ids for error in errors)
    if async_safe and not restore_runtime_configuration_ready():
        errors.append(
            Error(
                "The restore runtime provider composition is not valid.",
                id="backups.E046",
            )
        )
    return errors


def _public_uuid(value):
    if type(value) is not str:
        raise ImproperlyConfigured("Backup task identifiers must be public UUID strings.")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ImproperlyConfigured(
            "Backup task identifiers must be public UUID strings."
        ) from None
    if str(parsed) != value:
        raise ImproperlyConfigured("Backup task identifiers must use canonical UUID form.")
    return parsed


def _resolve_backup(*, backup_public_id, business_public_id):
    from apps.backups.engine.exceptions import BackupTenantMismatch
    from apps.backups.models import BackupRecord
    from apps.tenants.models import Business

    business = Business.objects.filter(public_id=business_public_id).first()
    if business is None:
        raise BackupTenantMismatch()
    backup = (
        BackupRecord.objects.for_business(business)
        .filter(public_id=backup_public_id)
        .first()
    )
    if backup is None:
        raise BackupTenantMismatch()
    return business, backup


def _mark_execution_blocked(*, business, backup, error_code, message):
    from apps.backups.engine.events import EXECUTION_BLOCKED
    from apps.backups.enums import ActivitySeverity, BackupStatus
    from apps.backups.services import create_backup_activity, transition_backup

    if backup.status == BackupStatus.QUEUED:
        backup = transition_backup(
            backup,
            BackupStatus.FAILED,
            failure_code=error_code,
            failure_summary="Backup execution was blocked; no artifact was created.",
        )
    create_backup_activity(
        business=business,
        backup=backup,
        event_type=EXECUTION_BLOCKED,
        severity=ActivitySeverity.WARNING,
        sanitized_message=message,
        structured_metadata={"error_code": error_code},
    )
    return backup


def _retry_countdown(retries):
    base = min(900, 60 * (2 ** min(max(int(retries), 0), 4)))
    return base + random.SystemRandom().randint(0, min(30, base // 2))


def _safe_execution_result(result):
    return {
        "backup_public_id": str(result.backup_public_id),
        "business_public_id": str(result.business_public_id),
        "status": str(result.final_status),
        "retention_outcome": str(result.retention_outcome),
        "retention_warning_code": result.retention_warning_code,
        "provider_stack_version": result.provider_stack_version,
    }


@shared_task(
    bind=True,
    name=BACKUP_EXECUTION_TASK_NAME,
    queue=BACKUP_QUEUE_NAME,
    max_retries=3,
    acks_late=False,
    reject_on_worker_lost=False,
    soft_time_limit=getattr(settings, "BACKUP_EXECUTION_TASK_SOFT_TIME_LIMIT_SECONDS", 21_600),
    time_limit=getattr(settings, "BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS", 21_900),
)
def execute_backup(self, backup_public_id, business_public_id):
    """Run one durable request only in the isolated long-running worker."""

    from apps.backups.engine.exceptions import BackupEngineDisabled
    from apps.backups.engine.runtime import request_backup_execution
    from apps.backups.engine.runtime_exceptions import RuntimeEngineError
    from apps.backups.enums import BackupStatus
    from apps.backups.scheduling import record_scheduled_backup_outcome

    backup_uuid = _public_uuid(backup_public_id)
    business_uuid = _public_uuid(business_public_id)
    business, backup = _resolve_backup(
        backup_public_id=backup_uuid,
        business_public_id=business_uuid,
    )
    try:
        assert_worker_execution_context(self.request, expected_queue=BACKUP_QUEUE_NAME)
        assert_real_execution_available()
        assert_safe_async_execution_configuration()
    except BackupEngineDisabled as exc:
        backup = _mark_execution_blocked(
            business=business,
            backup=backup,
            error_code=exc.engine_code,
            message=exc.sanitized_message,
        )
        record_scheduled_backup_outcome(backup)
        raise
    except ImproperlyConfigured:
        backup = _mark_execution_blocked(
            business=business,
            backup=backup,
            error_code="async_configuration_unsafe",
            message="Backup execution was blocked by its asynchronous safety guard.",
        )
        record_scheduled_backup_outcome(backup)
        raise

    try:
        result = request_backup_execution(
            backup_public_id=backup_uuid,
            business_public_id=business_uuid,
            worker_task_identifier=str(self.request.id or "")[:255],
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except RuntimeEngineError as exc:
        backup.refresh_from_db()
        retries = int(getattr(self.request, "retries", 0) or 0)
        if exc.retryable and backup.status == BackupStatus.QUEUED and retries < self.max_retries:
            raise self.retry(
                exc=exc,
                args=(),
                kwargs={
                    "backup_public_id": str(backup_uuid),
                    "business_public_id": str(business_uuid),
                },
                countdown=_retry_countdown(retries),
                max_retries=self.max_retries,
            ) from exc
        if exc.retryable and backup.status == BackupStatus.QUEUED:
            backup = _mark_execution_blocked(
                business=business,
                backup=backup,
                error_code="task_retry_exhausted",
                message="Backup execution exhausted its bounded worker retries.",
            )
        record_scheduled_backup_outcome(backup)
        raise

    backup.refresh_from_db()
    record_scheduled_backup_outcome(backup)
    return _safe_execution_result(result)


def _resolve_restore(*, restore_public_id, business_public_id):
    from apps.backups.engine.restore_exceptions import RestoreTenantMismatch
    from apps.backups.models import RestoreOperation
    from apps.tenants.models import Business

    business = Business.objects.filter(public_id=business_public_id).first()
    if business is None:
        raise RestoreTenantMismatch()
    restore = (
        RestoreOperation.objects.for_business(business)
        .select_related("source_backup", "safety_backup", "requested_by")
        .filter(public_id=restore_public_id)
        .first()
    )
    if (
        restore is None
        or restore.business_id != business.pk
        or restore.source_backup.business_id != business.pk
        or restore.source_backup.tenant_public_id_snapshot != business_public_id
    ):
        raise RestoreTenantMismatch()
    return business, restore


def _restore_activity_exists(*, restore, event_type, error_code=""):
    from apps.backups.models import BackupActivity

    activities = BackupActivity.objects.filter(restore=restore, event_type=event_type)
    if error_code:
        activities = activities.filter(structured_metadata__error_code=error_code)
    return activities.exists()


def _mark_restore_pre_mutation_failure(
    *,
    business,
    restore,
    error_code,
    message,
):
    from apps.backups import services
    from apps.backups.engine.events import RESTORE_FAILED
    from apps.backups.enums import ActivitySeverity, RestoreStatus
    from apps.backups.models import RestoreOperation

    current = RestoreOperation.objects.get(pk=restore.pk)
    pre_mutation_statuses = {
        RestoreStatus.QUEUED,
        RestoreStatus.AUTHORIZING,
        RestoreStatus.LOCKING,
        RestoreStatus.SAFETY_BACKUP,
        RestoreStatus.VALIDATING,
    }
    safe_code = f"pre_mutation_{error_code}"[:80]
    transitioned = current.status in pre_mutation_statuses
    if transitioned:
        current = services.transition_restore(
            current,
            RestoreStatus.FAILED,
            failure_code=safe_code,
            failure_summary=message,
        )
    if transitioned and not _restore_activity_exists(
        restore=current,
        event_type=RESTORE_FAILED,
        error_code=safe_code,
    ):
        services.create_backup_activity(
            business=business,
            backup=current.source_backup,
            restore=current,
            actor=current.requested_by,
            event_type=RESTORE_FAILED,
            severity=ActivitySeverity.ERROR,
            sanitized_message=message,
            structured_metadata={
                "error_code": safe_code,
                "mutation_started": False,
            },
        )
    return current


def _mark_restore_retry_exhausted(*, business, restore):
    from django.utils import timezone

    from apps.backups import services
    from apps.backups.engine.events import RESTORE_FAILED
    from apps.backups.enums import ActivitySeverity, RestoreStatus
    from apps.backups.models import RestoreOperation

    code = "pre_mutation_task_retry_exhausted"
    message = "Restore execution exhausted its bounded pre-mutation retries."
    RestoreOperation.objects.filter(
        pk=restore.pk,
        status=RestoreStatus.FAILED,
        rollback_attempted=False,
    ).update(
        failure_code=code,
        sanitized_failure_summary=message,
        completed_at=timezone.now(),
        updated_at=timezone.now(),
    )
    current = RestoreOperation.objects.get(pk=restore.pk)
    if not _restore_activity_exists(
        restore=current,
        event_type=RESTORE_FAILED,
        error_code=code,
    ):
        services.create_backup_activity(
            business=business,
            backup=current.source_backup,
            restore=current,
            actor=current.requested_by,
            event_type=RESTORE_FAILED,
            severity=ActivitySeverity.ERROR,
            sanitized_message=message,
            structured_metadata={"error_code": code, "mutation_started": False},
        )
    return current


def _mark_unexpected_restore_recovery(*, business, restore):
    from apps.backups import services
    from apps.backups.engine.events import RESTORE_RECOVERY_REQUIRED
    from apps.backups.engine.restore_exceptions import RestoreRecoveryRequired
    from apps.backups.enums import ActivitySeverity, RestoreStatus
    from apps.backups.models import RestoreOperation

    current = RestoreOperation.objects.get(pk=restore.pk)
    if current.status in {
        RestoreStatus.RESTORING,
        RestoreStatus.VERIFYING,
    }:
        current = services.transition_restore(
            current,
            RestoreStatus.INDETERMINATE,
            failure_code="restore_recovery_required",
            failure_summary="Restore requires administrator recovery.",
        )
    if current.status == RestoreStatus.INDETERMINATE and not _restore_activity_exists(
        restore=current,
        event_type=RESTORE_RECOVERY_REQUIRED,
        error_code="restore_recovery_required",
    ):
        services.create_backup_activity(
            business=business,
            backup=current.source_backup,
            restore=current,
            actor=current.requested_by,
            event_type=RESTORE_RECOVERY_REQUIRED,
            severity=ActivitySeverity.CRITICAL,
            sanitized_message="Restore requires administrator recovery.",
            structured_metadata={
                "error_code": "restore_recovery_required",
                "mutation_started": True,
            },
        )
    return RestoreRecoveryRequired(issue_code="restore_recovery_required")


def _safe_restore_task_result(restore):
    return {
        "restore_public_id": str(restore.public_id),
        "business_public_id": str(restore.business.public_id),
        "status": str(restore.status),
    }


@shared_task(
    bind=True,
    name=RESTORE_EXECUTION_TASK_NAME,
    queue=RESTORE_QUEUE_NAME,
    max_retries=3,
    acks_late=False,
    reject_on_worker_lost=False,
    soft_time_limit=getattr(
        settings,
        "BACKUP_RESTORE_TASK_SOFT_TIME_LIMIT_SECONDS",
        43_200,
    ),
    time_limit=getattr(settings, "BACKUP_RESTORE_TASK_TIME_LIMIT_SECONDS", 43_500),
)
def execute_restore(self, restore_public_id, business_public_id):
    """Execute one claimed restore only inside the dedicated Celery worker."""

    from apps.backups import services
    from apps.backups.engine.context import ActorIdentitySnapshot
    from apps.backups.engine.events import RESTORE_WORKER_STARTED
    from apps.backups.engine.restore_exceptions import (
        RestoreCompatibilityError,
        RestoreEngineError,
        RestoreLockUnavailable,
        RestoreMutationError,
        RestoreRecoveryRequired,
    )
    from apps.backups.engine.restore_mutation import (
        RestoreExecutionCoordinator,
        RestoreExecutionRequest,
        build_restore_runtime_stack,
    )
    from apps.backups.engine.restore_preflight import (
        RestorePreflightRequest,
    )
    from apps.backups.enums import RestoreStatus
    from apps.backups.restore_execution import (
        RestoreClaimState,
        claim_restore_operation,
    )

    restore_uuid = _public_uuid(restore_public_id)
    business_uuid = _public_uuid(business_public_id)
    business, restore = _resolve_restore(
        restore_public_id=restore_uuid,
        business_public_id=business_uuid,
    )
    assert_worker_execution_context(self.request, expected_queue=RESTORE_QUEUE_NAME)
    if restore.status == RestoreStatus.SUCCEEDED:
        return _safe_restore_task_result(restore)
    if restore.status == RestoreStatus.INDETERMINATE:
        raise RestoreRecoveryRequired(issue_code="restore_recovery_required")
    if restore.status in {
        RestoreStatus.AUTHORIZING,
        RestoreStatus.LOCKING,
        RestoreStatus.SAFETY_BACKUP,
        RestoreStatus.VALIDATING,
        RestoreStatus.RESTORING,
        RestoreStatus.VERIFYING,
        RestoreStatus.ROLLING_BACK,
    }:
        raise RestoreMutationError(issue_code="restore_replay_blocked")
    try:
        if not restore_mutation_setting_enabled() or not RESTORE_MUTATION_ENGINE_READY:
            raise RestoreMutationError(issue_code="restore_mutation_disabled")
        assert_safe_restore_async_execution_configuration()
        runtime_stack = build_restore_runtime_stack().validated()
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except RestoreEngineError as exc:
        _mark_restore_pre_mutation_failure(
            business=business,
            restore=restore,
            error_code=exc.issue_code,
            message=exc.sanitized_message,
        )
        raise
    except ImproperlyConfigured:
        safe_error = RestoreMutationError(
            "Restore execution was blocked by its asynchronous safety guard.",
            issue_code="async_configuration_unsafe",
        )
        _mark_restore_pre_mutation_failure(
            business=business,
            restore=restore,
            error_code=safe_error.issue_code,
            message=safe_error.sanitized_message,
        )
        raise safe_error from None
    except Exception:
        safe_error = RestoreMutationError(
            "Restore execution was blocked because its runtime is unavailable.",
            issue_code="restore_runtime_unavailable",
        )
        _mark_restore_pre_mutation_failure(
            business=business,
            restore=restore,
            error_code=safe_error.issue_code,
            message=safe_error.sanitized_message,
        )
        raise safe_error from None

    try:
        claim = claim_restore_operation(
            restore_public_id=restore_uuid,
            business_public_id=business_uuid,
        )
    except ValueError:
        raise RestoreMutationError(issue_code="restore_request_mismatch") from None
    restore = claim.restore
    if claim.state == RestoreClaimState.ALREADY_SUCCEEDED:
        return _safe_restore_task_result(restore)
    if claim.state == RestoreClaimState.RECOVERY_REQUIRED:
        raise RestoreRecoveryRequired(issue_code="restore_recovery_required")
    if claim.state == RestoreClaimState.ACTIVE_OR_AMBIGUOUS:
        raise RestoreMutationError(issue_code="restore_replay_blocked")
    if claim.state != RestoreClaimState.CLAIMED:
        raise RestoreMutationError(issue_code="restore_retry_blocked")

    worker_identifier = str(self.request.id or "")[:255]
    services.create_backup_activity(
        business=business,
        backup=restore.source_backup,
        restore=restore,
        actor=restore.requested_by,
        event_type=RESTORE_WORKER_STARTED,
        sanitized_message="The dedicated restore worker claimed the restore request.",
        structured_metadata={"worker_claimed": True},
    )
    actor_identity = ActorIdentitySnapshot.from_actor(restore.requested_by)
    try:
        approved_preflight = runtime_stack.preflight_coordinator.run(
            RestorePreflightRequest(
                operation_public_id=restore.public_id,
                business_public_id=business.public_id,
                backup_public_id=restore.source_backup.public_id,
                actor_identity=actor_identity,
                idempotency_key=restore.idempotency_key,
                worker_task_identifier=worker_identifier,
            )
        )
        if approved_preflight.restore_ready is not True:
            raise RestoreCompatibilityError(issue_code="restore_preflight_invalid")
        result = RestoreExecutionCoordinator(runtime_stack=runtime_stack).execute(
            RestoreExecutionRequest(
                business_public_id=business.public_id,
                selected_backup_public_id=restore.source_backup.public_id,
                actor_identity=actor_identity,
                idempotency_key=restore.idempotency_key,
                approved_preflight_result=approved_preflight,
                restore_request_public_id=restore.public_id,
                worker_task_identifier=worker_identifier,
            )
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except RestoreLockUnavailable as exc:
        restore = _mark_restore_pre_mutation_failure(
            business=business,
            restore=restore,
            error_code=exc.issue_code,
            message=exc.sanitized_message,
        )
        retries = int(getattr(self.request, "retries", 0) or 0)
        if retries < self.max_retries:
            raise self.retry(
                exc=exc,
                args=(),
                kwargs={
                    "restore_public_id": str(restore_uuid),
                    "business_public_id": str(business_uuid),
                },
                countdown=_retry_countdown(retries),
                max_retries=self.max_retries,
            ) from exc
        _mark_restore_retry_exhausted(business=business, restore=restore)
        raise
    except RestoreEngineError as exc:
        restore.refresh_from_db()
        if restore.status in {
            "AUTHORIZING",
            "LOCKING",
            "SAFETY_BACKUP",
            "VALIDATING",
        }:
            _mark_restore_pre_mutation_failure(
                business=business,
                restore=restore,
                error_code=exc.issue_code,
                message=exc.sanitized_message,
            )
        raise
    except Exception:
        restore.refresh_from_db()
        if restore.status in {"RESTORING", "VERIFYING"}:
            raise _mark_unexpected_restore_recovery(
                business=business,
                restore=restore,
            ) from None
        safe_error = RestoreMutationError(issue_code="restore_worker_failed")
        _mark_restore_pre_mutation_failure(
            business=business,
            restore=restore,
            error_code=safe_error.issue_code,
            message=safe_error.sanitized_message,
        )
        raise safe_error from None

    restore.refresh_from_db()
    if str(result.restore_operation_public_id) != str(restore.public_id):
        raise RestoreMutationError(issue_code="restore_result_mismatch")
    return _safe_restore_task_result(restore)


def _enqueue_scheduled_backup(*, backup_public_id, business_public_id):
    execute_backup.apply_async(
        kwargs={
            "backup_public_id": backup_public_id,
            "business_public_id": business_public_id,
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


@shared_task(
    bind=True,
    name=SCHEDULE_DISPATCH_TASK_NAME,
    queue=BACKUP_SCHEDULER_QUEUE_NAME,
    acks_late=False,
    reject_on_worker_lost=False,
    soft_time_limit=240,
    time_limit=270,
)
def dispatch_due_backup_schedules(self):
    """Find due schedules and enqueue durable identifiers, never backup work."""

    if not engine_setting_enabled():
        return {"state": "DISABLED", "examined_count": 0, "dispatched_count": 0}
    assert_worker_execution_context(
        self.request,
        expected_queue=BACKUP_SCHEDULER_QUEUE_NAME,
    )
    assert_real_execution_available()
    assert_safe_async_execution_configuration()
    from apps.backups.scheduling import dispatch_due_schedules

    return dispatch_due_schedules(enqueue=_enqueue_scheduled_backup).as_dict()


__all__ = [
    "BACKUP_EXECUTION_TASK_NAME",
    "BACKUP_QUEUE_NAME",
    "BACKUP_SCHEDULER_QUEUE_NAME",
    "RESTORE_EXECUTION_TASK_NAME",
    "RESTORE_QUEUE_NAME",
    "SCHEDULE_DISPATCH_TASK_NAME",
    "assert_safe_async_execution_configuration",
    "assert_safe_restore_async_execution_configuration",
    "assert_worker_execution_context",
    "check_restore_async_execution_configuration",
    "dispatch_due_backup_schedules",
    "execute_backup",
    "execute_restore",
]
