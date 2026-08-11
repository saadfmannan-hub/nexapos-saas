"""Fail-closed production activation gate without backup or restore side effects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings

from .engine import availability
from .engine.checks import check_backup_staging_root
from .engine.retention_policy import DAILY_FULL_KEEP_COUNT, RetentionPolicy
from .operational_readiness import (
    OperationalReadinessResult,
    ReadinessCategory,
    ReadinessState,
    assess_operational_readiness,
)


class ActivationMarker(StrEnum):
    """Independent milestones; a result may contain more than one marker."""

    CODE_READY = "CODE_READY"
    INFRASTRUCTURE_NOT_CONFIGURED = "INFRASTRUCTURE_NOT_CONFIGURED"
    READY_FOR_BACKUP_UAT = "READY_FOR_BACKUP_UAT"
    READY_FOR_RESTORE_UAT = "READY_FOR_RESTORE_UAT"


@dataclass(frozen=True, slots=True)
class ActivationCheck:
    identifier: str
    ready: bool
    summary: str

    def as_dict(self):
        return {
            "identifier": self.identifier,
            "ready": self.ready,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class ProductionActivationReadiness:
    """Secret-free activation evidence separated from feature enablement."""

    operational_readiness: OperationalReadinessResult
    markers: tuple[ActivationMarker, ...]
    checks: tuple[ActivationCheck, ...]
    code_ready: bool
    infrastructure_configured: bool
    infrastructure_ready: bool
    ready_for_backup_uat: bool
    ready_for_restore_uat: bool
    backup_execution_enabled: bool
    restore_mutation_enabled: bool
    operational_provider_stack_ready: bool
    backup_uat_blockers: tuple[str, ...]
    restore_uat_blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def checked_at(self):
        return self.operational_readiness.checked_at

    def as_dict(self):
        return {
            "checked_at": self.checked_at.isoformat(),
            "markers": [marker.value for marker in self.markers],
            "code_ready": self.code_ready,
            "infrastructure_configured": self.infrastructure_configured,
            "infrastructure_ready": self.infrastructure_ready,
            "ready_for_backup_uat": self.ready_for_backup_uat,
            "ready_for_restore_uat": self.ready_for_restore_uat,
            "backup_execution_enabled": self.backup_execution_enabled,
            "restore_mutation_enabled": self.restore_mutation_enabled,
            "operational_provider_stack_ready": self.operational_provider_stack_ready,
            "provider_attestation_performed": (
                self.operational_readiness.provider_attestation_performed
            ),
            "checks": [check.as_dict() for check in self.checks],
            "backup_uat_blockers": list(self.backup_uat_blockers),
            "restore_uat_blockers": list(self.restore_uat_blockers),
            "warnings": list(self.warnings),
        }


_CODE_CAPABILITY_NAMES = (
    "SQLITE_SNAPSHOT_PROVIDER_READY",
    "TENANT_LOGICAL_EXPORT_PROVIDER_READY",
    "MEDIA_CAPTURE_PROVIDER_READY",
    "CANONICAL_MANIFEST_PROVIDER_READY",
    "DETERMINISTIC_PACKAGE_PROVIDER_READY",
    "INDEPENDENT_PACKAGE_VERIFIER_READY",
    "ENCRYPTED_ARTIFACT_PROVIDER_READY",
    "PRODUCTION_KEY_PROVIDER_READY",
    "PRODUCTION_DURABLE_STORAGE_PROVIDER_READY",
    "RETENTION_ENGINE_READY",
    "RUNTIME_ORCHESTRATOR_READY",
    "ASYNC_EXECUTION_BOUNDARY_READY",
    "SCHEDULE_DISPATCHER_READY",
    "RUNTIME_COMPOSITION_READY",
    "RESTORE_PREFLIGHT_ENGINE_READY",
    "RESTORE_MUTATION_ENGINE_READY",
    "RESTORE_ASYNC_EXECUTION_BOUNDARY_READY",
    "RECONCILIATION_READY",
)


def _route_ready(task_name, queue_name):
    routes = getattr(settings, "CELERY_TASK_ROUTES", {})
    route = routes.get(task_name) if isinstance(routes, dict) else None
    return isinstance(route, dict) and route.get("queue") == queue_name


def _beat_has_single_owner(task_name, queue_name):
    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
    if not isinstance(schedule, dict):
        return False
    entries = [entry for entry in schedule.values() if entry.get("task") == task_name]
    return bool(
        len(entries) == 1
        and isinstance(entries[0].get("options"), dict)
        and entries[0]["options"].get("queue") == queue_name
    )


def _broker_ready():
    value = getattr(settings, "CELERY_BROKER_URL", "")
    if type(value) is not str or not value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"redis", "rediss"}
        and parsed.hostname
        and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}
        and getattr(settings, "CELERY_TASK_ALWAYS_EAGER", True) is False
    )


def _is_relative_to(candidate, root):
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _staging_ready():
    if check_backup_staging_root(None):
        return False
    try:
        staging = Path(settings.BACKUP_STAGING_ROOT).expanduser().resolve(strict=False)
        application_root = Path(settings.BASE_DIR).resolve(strict=False)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return False
    return staging != application_root and not _is_relative_to(staging, application_root)


def _database_ready():
    configuration = getattr(settings, "DATABASES", {}).get("default", {})
    return bool(
        isinstance(configuration, dict)
        and configuration.get("ENGINE") == "django.db.backends.sqlite3"
        and getattr(settings, "BACKUP_SQLITE_REQUIRED_JOURNAL_MODE", "") == "WAL"
        and getattr(settings, "BACKUP_SQLITE_REQUIRED_SYNCHRONOUS", "") == "FULL"
        and getattr(settings, "BACKUP_SQLITE_REQUIRE_LOCAL_STAGING", False) is True
    )


def _retention_ready():
    try:
        policy = RetentionPolicy.from_settings()
    except Exception:
        return False
    return bool(
        availability.RETENTION_ENGINE_READY
        and policy.daily_full_keep_count == DAILY_FULL_KEEP_COUNT == 5
    )


def _provider_check(operational, category):
    return next(check for check in operational.checks if check.category == category)


def assess_production_activation_readiness(
    *,
    attest_providers=False,
    key_provider=None,
    storage_provider=None,
    runtime_stack_factory=None,
):
    """Assess activation prerequisites without executing or persisting anything.

    Provider calls are limited to the existing DescribeKey/HeadBucket boundaries
    and occur only when ``attest_providers`` is explicitly true.
    """

    operational = assess_operational_readiness(
        attest_providers=attest_providers,
        key_provider=key_provider,
        storage_provider=storage_provider,
        runtime_stack_factory=runtime_stack_factory,
    )
    kms = _provider_check(operational, ReadinessCategory.KEY_MANAGEMENT)
    storage = _provider_check(operational, ReadinessCategory.DURABLE_STORAGE)

    code_ready = all(
        getattr(availability, capability_name, False) is True
        for capability_name in _CODE_CAPABILITY_NAMES
    )
    providers_configured = all(
        check.state != ReadinessState.NOT_READY for check in (kms, storage)
    )
    providers_attested = bool(
        operational.provider_attestation_performed
        and kms.state == ReadinessState.READY
        and storage.state == ReadinessState.READY
    )
    broker_ready = _broker_ready()
    backup_worker_ready = _route_ready(
        "apps.backups.tasks.execute_backup", "nexa.backups"
    ) and getattr(settings, "BACKUP_EXECUTION_QUEUE_NAME", "") == "nexa.backups"
    restore_worker_ready = _route_ready(
        "apps.backups.tasks.execute_restore", "nexa.restores"
    ) and getattr(settings, "BACKUP_RESTORE_QUEUE_NAME", "") == "nexa.restores"
    scheduler_ready = _route_ready(
        "apps.backups.tasks.dispatch_due_backup_schedules",
        "nexa.backup_scheduling",
    ) and _beat_has_single_owner(
        "apps.backups.tasks.dispatch_due_backup_schedules",
        "nexa.backup_scheduling",
    )
    reconciliation_ready = bool(
        availability.RECONCILIATION_READY
        and _route_ready(
            "apps.backups.tasks.reconcile_backup_control_plane",
            "nexa.backup_scheduling",
        )
        and _beat_has_single_owner(
            "apps.backups.tasks.reconcile_backup_control_plane",
            "nexa.backup_scheduling",
        )
    )
    staging_ready = _staging_ready()
    database_ready = _database_ready()
    retention_ready = _retention_ready()

    checks = (
        ActivationCheck(
            "CODE_CAPABILITY",
            code_ready,
            "Backup, restore, provider, retention, and reconciliation boundaries are code-ready."
            if code_ready
            else "One or more required backup code capabilities are incomplete.",
        ),
        ActivationCheck(
            "PROVIDER_CONFIGURATION",
            providers_configured,
            "Production KMS and private S3-compatible providers are structurally configured."
            if providers_configured
            else "Production KMS and private S3-compatible provider configuration is incomplete.",
        ),
        ActivationCheck(
            "PROVIDER_ATTESTATION",
            providers_attested,
            "Non-destructive KMS DescribeKey and storage HeadBucket attestations passed."
            if providers_attested
            else "Provider reachability has not been proven by this result.",
        ),
        ActivationCheck(
            "BROKER",
            broker_ready,
            "A dedicated non-eager production Redis broker is configured."
            if broker_ready
            else "A dedicated non-local production Redis broker is not configured.",
        ),
        ActivationCheck(
            "BACKUP_WORKER_ROUTE",
            backup_worker_ready,
            "The backup task is isolated on nexa.backups."
            if backup_worker_ready
            else "The exact backup worker route is missing.",
        ),
        ActivationCheck(
            "RESTORE_WORKER_ROUTE",
            restore_worker_ready,
            "The restore task is isolated on nexa.restores."
            if restore_worker_ready
            else "The exact restore worker route is missing.",
        ),
        ActivationCheck(
            "SCHEDULER",
            scheduler_ready,
            "The scheduler has one configured Beat entry on the control queue."
            if scheduler_ready
            else "The scheduler route or single-owner Beat contract is incomplete.",
        ),
        ActivationCheck(
            "RECONCILIATION",
            reconciliation_ready,
            "Reconciliation has one configured Beat entry on the control queue."
            if reconciliation_ready
            else "The reconciliation route or single-owner Beat contract is incomplete.",
        ),
        ActivationCheck(
            "PRIVATE_STAGING",
            staging_ready,
            "Staging is an absolute private path outside the application and public roots."
            if staging_ready
            else "Production staging must be an absolute private path outside the application and public roots.",
        ),
        ActivationCheck(
            "DATABASE_RUNTIME",
            database_ready,
            "The v1 SQLite WAL/FULL snapshot policy is selected."
            if database_ready
            else "The v1 backup runtime requires a local SQLite database with WAL/FULL policy.",
        ),
        ActivationCheck(
            "RETENTION_POLICY",
            retention_ready,
            "The locked policy keeps the latest five verified daily full backups."
            if retention_ready
            else "The locked five-backup retention policy is not configured.",
        ),
    )

    backup_requirements = {
        "CODE_CAPABILITY",
        "PROVIDER_CONFIGURATION",
        "PROVIDER_ATTESTATION",
        "BROKER",
        "BACKUP_WORKER_ROUTE",
        "SCHEDULER",
        "RECONCILIATION",
        "PRIVATE_STAGING",
        "DATABASE_RUNTIME",
        "RETENTION_POLICY",
    }
    backup_uat_blockers = tuple(
        check.summary
        for check in checks
        if check.identifier in backup_requirements and not check.ready
    )
    ready_for_backup_uat = not backup_uat_blockers

    restore_uat_blockers = list(backup_uat_blockers)
    if not restore_worker_ready:
        restore_uat_blockers.append("The dedicated restore worker route is required.")
    if not availability.engine_setting_enabled():
        restore_uat_blockers.append(
            "Backup execution must first be activated and proven for the UAT tenant."
        )
    if not availability.OPERATIONAL_PROVIDER_STACK_READY:
        restore_uat_blockers.append(
            "The operational provider-stack kill switch remains intentionally closed."
        )
    if not availability.restore_mutation_setting_enabled():
        restore_uat_blockers.append(
            "Restore mutation remains disabled until a controlled rehearsal window."
        )
    ready_for_restore_uat = not restore_uat_blockers

    infrastructure_configured = all(
        value
        for value in (
            providers_configured,
            broker_ready,
            backup_worker_ready,
            restore_worker_ready,
            scheduler_ready,
            reconciliation_ready,
            staging_ready,
            database_ready,
            retention_ready,
        )
    )
    infrastructure_ready = infrastructure_configured and providers_attested
    markers = []
    if code_ready:
        markers.append(ActivationMarker.CODE_READY)
    if not infrastructure_ready:
        markers.append(ActivationMarker.INFRASTRUCTURE_NOT_CONFIGURED)
    if ready_for_backup_uat:
        markers.append(ActivationMarker.READY_FOR_BACKUP_UAT)
    if ready_for_restore_uat:
        markers.append(ActivationMarker.READY_FOR_RESTORE_UAT)

    warnings = [
        "Configuration does not attest live worker processes or prove single live Beat ownership."
    ]
    if not availability.OPERATIONAL_PROVIDER_STACK_READY:
        warnings.append(
            "OPERATIONAL_PROVIDER_STACK_READY remains false pending approved production evidence."
        )
    if not availability.engine_setting_enabled():
        warnings.append("Backup execution remains disabled.")
    if not availability.restore_mutation_setting_enabled():
        warnings.append("Restore mutation remains disabled.")

    return ProductionActivationReadiness(
        operational_readiness=operational,
        markers=tuple(markers),
        checks=checks,
        code_ready=code_ready,
        infrastructure_configured=infrastructure_configured,
        infrastructure_ready=infrastructure_ready,
        ready_for_backup_uat=ready_for_backup_uat,
        ready_for_restore_uat=ready_for_restore_uat,
        backup_execution_enabled=availability.engine_setting_enabled(),
        restore_mutation_enabled=availability.restore_mutation_setting_enabled(),
        operational_provider_stack_ready=availability.OPERATIONAL_PROVIDER_STACK_READY,
        backup_uat_blockers=backup_uat_blockers,
        restore_uat_blockers=tuple(restore_uat_blockers),
        warnings=tuple(warnings),
    )


__all__ = [
    "ActivationCheck",
    "ActivationMarker",
    "ProductionActivationReadiness",
    "assess_production_activation_readiness",
]
