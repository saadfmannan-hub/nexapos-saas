"""Structured, secret-free production readiness and optional provider attestation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.utils import timezone

from apps.audit import services as audit_services

from .engine import availability, events


class ReadinessState(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    WARNING = "WARNING"


class ReadinessCategory(StrEnum):
    KEY_MANAGEMENT = "KEY_MANAGEMENT"
    DURABLE_STORAGE = "DURABLE_STORAGE"
    BROKER = "BROKER"
    BACKUP_WORKER = "BACKUP_WORKER"
    RESTORE_WORKER = "RESTORE_WORKER"
    SCHEDULER = "SCHEDULER"
    RECONCILIATION = "RECONCILIATION"
    RETENTION = "RETENTION"
    DATABASE_POLICY = "DATABASE_POLICY"


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    category: ReadinessCategory
    state: ReadinessState
    summary: str
    provider: str = ""

    def as_dict(self):
        return {
            "category": self.category.value,
            "state": self.state.value,
            "summary": self.summary,
            "provider": self.provider,
        }


@dataclass(frozen=True, slots=True)
class OperationalReadinessResult:
    checked_at: object
    provider_attestation_performed: bool
    checks: tuple[ReadinessCheck, ...]

    @property
    def code_ready(self):
        return all(check.state != ReadinessState.NOT_READY for check in self.checks)

    def as_dict(self):
        return {
            "checked_at": self.checked_at.isoformat(),
            "provider_attestation_performed": self.provider_attestation_performed,
            "code_ready": self.code_ready,
            "checks": [check.as_dict() for check in self.checks],
        }


def _route_ready(task_name, queue_name):
    routes = getattr(settings, "CELERY_TASK_ROUTES", {})
    route = routes.get(task_name) if isinstance(routes, dict) else None
    return isinstance(route, dict) and route.get("queue") == queue_name


def _beat_ready(entry_name, task_name, cadence_setting):
    beat = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
    entry = beat.get(entry_name) if isinstance(beat, dict) else None
    cadence = getattr(settings, cadence_setting, None)
    return bool(
        type(cadence) is int
        and isinstance(entry, dict)
        and entry.get("task") == task_name
        and entry.get("schedule") == float(cadence)
        and isinstance(entry.get("options"), dict)
        and entry["options"].get("queue") == "nexa.backup_scheduling"
    )


def _key_management_check(*, attest, key_provider=None):
    try:
        from .engine.key_management import (
            AWS_KMS_PROVIDER_IDENTIFIER,
            build_key_provider_registry_from_settings,
            validate_key_provider_settings,
        )

        if validate_key_provider_settings() != "aws_kms":
            return ReadinessCheck(
                ReadinessCategory.KEY_MANAGEMENT,
                ReadinessState.NOT_READY,
                "Production activation rejects the local key provider.",
            )
        if not attest:
            return ReadinessCheck(
                ReadinessCategory.KEY_MANAGEMENT,
                ReadinessState.WARNING,
                "Production KMS is configured but was not contacted by this check.",
                AWS_KMS_PROVIDER_IDENTIFIER,
            )
        provider = key_provider
        if provider is None:
            provider = build_key_provider_registry_from_settings().active_provider
        health = provider.health_check()
        state = (
            ReadinessState.READY
            if health.reachable and health.enabled
            else ReadinessState.NOT_READY
        )
        return ReadinessCheck(
            ReadinessCategory.KEY_MANAGEMENT,
            state,
            (
                "KMS DescribeKey attestation passed. Encrypt/decrypt permission still requires a controlled activation rehearsal."
                if state == ReadinessState.READY
                else "KMS attestation did not prove an enabled encryption key."
            ),
            str(health.provider_identifier),
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        return ReadinessCheck(
            ReadinessCategory.KEY_MANAGEMENT,
            ReadinessState.NOT_READY,
            "KMS configuration or non-destructive attestation is unavailable.",
        )


def _durable_storage_check(*, attest, storage_provider=None, runtime_stack_factory=None):
    try:
        from .engine.s3_storage import S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER
        from .engine.storage_registry import validate_storage_provider_settings

        if validate_storage_provider_settings() != "s3":
            return ReadinessCheck(
                ReadinessCategory.DURABLE_STORAGE,
                ReadinessState.NOT_READY,
                "Production activation rejects the local durable-storage provider.",
            )
        if not attest:
            return ReadinessCheck(
                ReadinessCategory.DURABLE_STORAGE,
                ReadinessState.WARNING,
                "Private S3-compatible storage is configured but was not contacted by this check.",
                S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
            )
        provider = storage_provider
        if provider is None:
            if runtime_stack_factory is None:
                from .engine.runtime import build_runtime_provider_stack

                runtime_stack_factory = build_runtime_provider_stack
            provider = runtime_stack_factory().durable_storage_provider
        reachable = provider.health_attestation()
        return ReadinessCheck(
            ReadinessCategory.DURABLE_STORAGE,
            ReadinessState.READY if reachable else ReadinessState.NOT_READY,
            (
                "HeadBucket attestation passed without upload or deletion. Object permissions still require a controlled activation rehearsal."
                if reachable
                else "The configured private bucket could not be attested."
            ),
            S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        return ReadinessCheck(
            ReadinessCategory.DURABLE_STORAGE,
            ReadinessState.NOT_READY,
            "S3-compatible storage configuration or non-destructive attestation is unavailable.",
        )


def _configuration_checks():
    broker = bool(
        getattr(settings, "CELERY_BROKER_URL", "")
        and getattr(settings, "CELERY_TASK_ALWAYS_EAGER", True) is False
    )
    backup_route = _route_ready("apps.backups.tasks.execute_backup", "nexa.backups")
    restore_route = _route_ready("apps.backups.tasks.execute_restore", "nexa.restores")
    schedule_route = _route_ready(
        "apps.backups.tasks.dispatch_due_backup_schedules", "nexa.backup_scheduling"
    )
    reconciliation_route = _route_ready(
        "apps.backups.tasks.reconcile_backup_control_plane", "nexa.backup_scheduling"
    )
    scheduler = schedule_route and _beat_ready(
        "dispatch-due-backup-schedules",
        "apps.backups.tasks.dispatch_due_backup_schedules",
        "BACKUP_SCHEDULE_DISPATCH_INTERVAL_SECONDS",
    )
    reconciliation = reconciliation_route and _beat_ready(
        "reconcile-backup-control-plane",
        "apps.backups.tasks.reconcile_backup_control_plane",
        "BACKUP_RECONCILIATION_INTERVAL_SECONDS",
    )
    threshold_values = (
        getattr(settings, "BACKUP_QUEUED_AGE_WARNING_SECONDS", None),
        getattr(settings, "BACKUP_RESTORE_QUEUED_AGE_WARNING_SECONDS", None),
        getattr(settings, "BACKUP_STALE_OPERATION_SECONDS", None),
    )
    database_policy = all(
        type(value) is int and 300 <= value <= 604_800 for value in threshold_values
    ) and type(getattr(settings, "BACKUP_FAILED_COUNT_WARNING", None)) is int
    return (
        ReadinessCheck(
            ReadinessCategory.BROKER,
            ReadinessState.READY if broker else ReadinessState.NOT_READY,
            "Broker is configured for non-eager delivery."
            if broker
            else "A non-eager broker is not configured.",
        ),
        ReadinessCheck(
            ReadinessCategory.BACKUP_WORKER,
            ReadinessState.WARNING if broker and backup_route else ReadinessState.NOT_READY,
            "Backup queue routing is ready; live worker presence is not attested."
            if broker and backup_route
            else "The isolated backup worker route is not ready.",
        ),
        ReadinessCheck(
            ReadinessCategory.RESTORE_WORKER,
            ReadinessState.WARNING if broker and restore_route else ReadinessState.NOT_READY,
            "Restore queue routing is ready; live worker presence is not attested."
            if broker and restore_route
            else "The isolated restore worker route is not ready.",
        ),
        ReadinessCheck(
            ReadinessCategory.SCHEDULER,
            ReadinessState.WARNING if scheduler else ReadinessState.NOT_READY,
            "Beat configuration is ready; exactly one live owner must be assigned."
            if scheduler
            else "The scheduler route or Beat entry is not ready.",
        ),
        ReadinessCheck(
            ReadinessCategory.RECONCILIATION,
            ReadinessState.READY
            if reconciliation and availability.RECONCILIATION_READY
            else ReadinessState.NOT_READY,
            "Dispatch reconciliation is code-ready on the control queue."
            if reconciliation and availability.RECONCILIATION_READY
            else "Dispatch reconciliation is not configured safely.",
        ),
        ReadinessCheck(
            ReadinessCategory.RETENTION,
            ReadinessState.READY
            if availability.RETENTION_ENGINE_READY
            else ReadinessState.NOT_READY,
            "Retention runs only after durable verified success and fails independently.",
        ),
        ReadinessCheck(
            ReadinessCategory.DATABASE_POLICY,
            ReadinessState.READY if database_policy else ReadinessState.NOT_READY,
            "DB-derived monitoring and bounded alert thresholds are configured."
            if database_policy
            else "Operational thresholds are invalid.",
        ),
    )


def assess_operational_readiness(
    *,
    attest_providers=False,
    key_provider=None,
    storage_provider=None,
    runtime_stack_factory=None,
    actor=None,
    request=None,
):
    """Return a sanitized snapshot; provider calls occur only when explicitly requested."""

    checks = (
        _key_management_check(attest=attest_providers, key_provider=key_provider),
        _durable_storage_check(
            attest=attest_providers,
            storage_provider=storage_provider,
            runtime_stack_factory=runtime_stack_factory,
        ),
        *_configuration_checks(),
    )
    result = OperationalReadinessResult(
        checked_at=timezone.now(),
        provider_attestation_performed=bool(attest_providers),
        checks=checks,
    )
    if actor is not None or request is not None:
        unavailable = any(check.state == ReadinessState.NOT_READY for check in checks[:2])
        audit_services.log(
            events.BACKUP_PROVIDER_UNAVAILABLE
            if attest_providers and unavailable
            else events.BACKUP_READINESS_CHECKED,
            user=actor,
            request=request,
            module="backups",
            description="A sanitized backup production-readiness check was recorded.",
            new_values=result.as_dict(),
        )
    return result


__all__ = [
    "OperationalReadinessResult",
    "ReadinessCategory",
    "ReadinessCheck",
    "ReadinessState",
    "assess_operational_readiness",
]
