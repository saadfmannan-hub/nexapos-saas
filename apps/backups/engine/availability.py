"""Central capability guard for the deliberately incomplete backup engine."""

from dataclasses import dataclass

from django.conf import settings

from .exceptions import BackupEngineDisabled

# These code capabilities are internal building blocks. They do not permit a
# commercial workflow to run until production infrastructure is attested and a
# separately approved operational activation opens the final kill switch.
SQLITE_SNAPSHOT_PROVIDER_READY = True
TENANT_LOGICAL_EXPORT_PROVIDER_READY = True
MEDIA_CAPTURE_PROVIDER_READY = True
CANONICAL_MANIFEST_PROVIDER_READY = True
DETERMINISTIC_PACKAGE_PROVIDER_READY = True
INDEPENDENT_PACKAGE_VERIFIER_READY = True
ENCRYPTED_ARTIFACT_PROVIDER_READY = True
PRODUCTION_KEY_PROVIDER_READY = True
DURABLE_STORAGE_PROVIDER_READY = True
PRODUCTION_DURABLE_STORAGE_PROVIDER_READY = True
RETENTION_ENGINE_READY = True
# Phase 3A can re-attest historical Phase 2G objects for non-mutating preflight.
# Destructive operational retention and restore mutation remain unavailable.
RUNTIME_ORCHESTRATOR_READY = True
ASYNC_EXECUTION_BOUNDARY_READY = True
SCHEDULE_DISPATCHER_READY = True
RUNTIME_COMPOSITION_READY = True
RESTORE_PREFLIGHT_ENGINE_READY = True
RESTORE_MUTATION_ENGINE_READY = True
RESTORE_ASYNC_EXECUTION_BOUNDARY_READY = True
RECONCILIATION_READY = True
OPERATIONAL_PROVIDER_STACK_READY = False
INCOMPLETE_PROVIDER_STACK_REASON = (
    "SQLite snapshot, tenant logical export, local media capture, canonical "
    "manifest, deterministic plaintext package, independent package "
    "verification, production-capable key management and S3-compatible durable "
    "storage, immutable daily-full retention, and operational coordination are "
    "available internally. Guarded restore preflight and tenant restore mutation "
    "with a mandatory protected safety backup and a restart-safe dedicated restore "
    "worker are also code-complete, but restore mutation remains disabled by "
    "default. Dispatch reconciliation is code-ready. Live provider and process "
    "evidence has not been approved, so the operational activation kill switch "
    "remains closed. Backup download is intentionally deferred from v1."
)
# Backward-compatible import retained for Phase 2A callers and tests.
PHASE_2A_DISABLED_REASON = INCOMPLETE_PROVIDER_STACK_REASON


@dataclass(frozen=True, slots=True)
class BackupEngineCapability:
    setting_enabled: bool
    snapshot_provider_ready: bool
    logical_export_provider_ready: bool
    media_capture_provider_ready: bool
    canonical_manifest_provider_ready: bool
    deterministic_package_provider_ready: bool
    independent_package_verifier_ready: bool
    encrypted_artifact_provider_ready: bool
    durable_storage_provider_ready: bool
    retention_engine_ready: bool
    runtime_orchestrator_ready: bool
    async_execution_boundary_ready: bool
    schedule_dispatcher_ready: bool
    reconciliation_ready: bool
    runtime_composition_ready: bool
    restore_preflight_engine_ready: bool
    restore_mutation_engine_ready: bool
    restore_mutation_setting_enabled: bool
    async_configuration_ready: bool
    runtime_configuration_ready: bool
    runtime_snapshot_policy_ready: bool | None
    provider_stack_ready: bool
    real_execution_available: bool
    disabled_reason: str


def engine_setting_enabled() -> bool:
    """Honor the canonical setting and the Phase 1 compatibility alias."""

    return bool(
        getattr(settings, "BACKUP_EXECUTION_ENGINE_ENABLED", False)
        or getattr(settings, "BACKUP_ENGINE_ENABLED", False)
    )


def restore_mutation_setting_enabled() -> bool:
    """Return true only for an explicit boolean restore-mutation opt-in."""

    return getattr(settings, "BACKUP_RESTORE_MUTATION_ENABLED", False) is True


def provider_environment_checks_required() -> bool:
    """Require concrete provider infrastructure only for an operational path."""

    return bool(
        engine_setting_enabled()
        or restore_mutation_setting_enabled()
        or OPERATIONAL_PROVIDER_STACK_READY
    )


def restore_provider_checks_required() -> bool:
    """Require restore composition only when restore can be operational."""

    return bool(restore_mutation_setting_enabled() or OPERATIONAL_PROVIDER_STACK_READY)


def restore_preflight_configuration_ready() -> bool:
    """Validate preflight composition without retrieving or mutating backup data."""

    if not RESTORE_PREFLIGHT_ENGINE_READY:
        return False
    try:
        from .restore_preflight import build_restore_preflight_provider_stack

        build_restore_preflight_provider_stack().validated()
    except Exception:
        return False
    return True


def production_key_provider_configuration_ready() -> bool:
    """Require a structurally valid non-development provider for activation."""

    try:
        from .key_management import validate_key_provider_settings

        return validate_key_provider_settings() != "local"
    except Exception:
        return False


def production_durable_storage_configuration_ready() -> bool:
    """Require structurally valid S3-compatible storage for activation."""

    try:
        from .storage_registry import validate_storage_provider_settings

        return validate_storage_provider_settings() == "s3"
    except Exception:
        return False


def restore_async_configuration_ready() -> bool:
    """Require a broker, non-eager delivery, and the exact restore queue."""

    routes = getattr(settings, "CELERY_TASK_ROUTES", {})
    route = (
        routes.get("apps.backups.tasks.execute_restore")
        if isinstance(routes, dict)
        else None
    )
    soft_limit = getattr(
        settings,
        "BACKUP_RESTORE_TASK_SOFT_TIME_LIMIT_SECONDS",
        None,
    )
    hard_limit = getattr(settings, "BACKUP_RESTORE_TASK_TIME_LIMIT_SECONDS", None)
    provider_hard_limit = getattr(
        settings,
        "BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS",
        None,
    )
    return bool(
        getattr(settings, "CELERY_BROKER_URL", "")
        and getattr(settings, "CELERY_TASK_ALWAYS_EAGER", True) is False
        and getattr(settings, "BACKUP_RESTORE_QUEUE_NAME", "") == "nexa.restores"
        and isinstance(route, dict)
        and route.get("queue") == "nexa.restores"
        and type(soft_limit) is int
        and type(hard_limit) is int
        and type(provider_hard_limit) is int
        and provider_hard_limit < soft_limit < hard_limit <= 90_000
    )


def restore_runtime_configuration_ready() -> bool:
    """Compose and validate restore providers without performing a restore."""

    if not (
        RESTORE_PREFLIGHT_ENGINE_READY
        and RESTORE_MUTATION_ENGINE_READY
        and RESTORE_ASYNC_EXECUTION_BOUNDARY_READY
    ):
        return False
    try:
        from .restore_mutation import build_restore_runtime_stack

        build_restore_runtime_stack().validated()
    except Exception:
        return False
    return True


def restore_execution_available() -> bool:
    """Return whether the worker-only destructive boundary is fully safe."""

    return bool(
        restore_mutation_setting_enabled()
        and production_key_provider_configuration_ready()
        and production_durable_storage_configuration_ready()
        and RESTORE_MUTATION_ENGINE_READY
        and RESTORE_ASYNC_EXECUTION_BOUNDARY_READY
        and restore_async_configuration_ready()
        and restore_runtime_configuration_ready()
    )


def async_configuration_ready() -> bool:
    """Assess the non-mutating Celery routing prerequisites."""

    routes = getattr(settings, "CELERY_TASK_ROUTES", {})
    execution_route = (
        routes.get("apps.backups.tasks.execute_backup")
        if isinstance(routes, dict)
        else None
    )
    dispatch_route = (
        routes.get("apps.backups.tasks.dispatch_due_backup_schedules")
        if isinstance(routes, dict)
        else None
    )
    reconciliation_route = (
        routes.get("apps.backups.tasks.reconcile_backup_control_plane")
        if isinstance(routes, dict)
        else None
    )
    cadence = getattr(settings, "BACKUP_SCHEDULE_DISPATCH_INTERVAL_SECONDS", None)
    soft_limit = getattr(settings, "BACKUP_EXECUTION_TASK_SOFT_TIME_LIMIT_SECONDS", None)
    hard_limit = getattr(settings, "BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS", None)
    beat = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
    beat_entry = (
        beat.get("dispatch-due-backup-schedules")
        if isinstance(beat, dict)
        else None
    )
    reconciliation_entry = (
        beat.get("reconcile-backup-control-plane")
        if isinstance(beat, dict)
        else None
    )
    reconciliation_cadence = getattr(
        settings, "BACKUP_RECONCILIATION_INTERVAL_SECONDS", None
    )
    return bool(
        getattr(settings, "CELERY_BROKER_URL", "")
        and getattr(settings, "CELERY_TASK_ALWAYS_EAGER", True) is False
        and getattr(settings, "BACKUP_EXECUTION_QUEUE_NAME", "") == "nexa.backups"
        and getattr(settings, "BACKUP_SCHEDULER_QUEUE_NAME", "")
        == "nexa.backup_scheduling"
        and isinstance(execution_route, dict)
        and execution_route.get("queue") == "nexa.backups"
        and isinstance(dispatch_route, dict)
        and dispatch_route.get("queue") == "nexa.backup_scheduling"
        and isinstance(reconciliation_route, dict)
        and reconciliation_route.get("queue") == "nexa.backup_scheduling"
        and type(cadence) is int
        and 60 <= cadence <= 3_600
        and type(soft_limit) is int
        and type(hard_limit) is int
        and 3_600 <= soft_limit < hard_limit <= 90_000
        and isinstance(beat_entry, dict)
        and beat_entry.get("task")
        == "apps.backups.tasks.dispatch_due_backup_schedules"
        and beat_entry.get("schedule") == float(cadence)
        and isinstance(beat_entry.get("options"), dict)
        and beat_entry["options"].get("queue") == "nexa.backup_scheduling"
        and type(reconciliation_cadence) is int
        and 300 <= reconciliation_cadence <= 900
        and isinstance(reconciliation_entry, dict)
        and reconciliation_entry.get("task")
        == "apps.backups.tasks.reconcile_backup_control_plane"
        and reconciliation_entry.get("schedule") == float(reconciliation_cadence)
        and isinstance(reconciliation_entry.get("options"), dict)
        and reconciliation_entry["options"].get("queue")
        == "nexa.backup_scheduling"
    )


def runtime_configuration_ready() -> bool:
    """Run the non-mutating policy/root checks without constructing providers."""

    if not (
        engine_setting_enabled()
        and OPERATIONAL_PROVIDER_STACK_READY
        and ASYNC_EXECUTION_BOUNDARY_READY
        and SCHEDULE_DISPATCHER_READY
        and RUNTIME_COMPOSITION_READY
        and async_configuration_ready()
    ):
        return False
    try:
        from . import checks

        validators = (
            checks.check_backup_staging_root,
            checks.check_sqlite_snapshot_policy_settings,
            checks.check_logical_export_policy_settings,
            checks.check_logical_export_registry,
            checks.check_media_capture_policy_settings,
            checks.check_media_storage_configuration,
            checks.check_encryption_policy_settings,
            checks.check_local_kek_configuration,
            checks.check_production_key_provider_selection,
            checks.check_durable_storage_policy_settings,
            checks.check_storage_provider_configuration,
            checks.check_production_storage_provider_selection,
            checks.check_durable_storage_root,
            checks.check_retention_policy_settings,
            checks.check_runtime_execution_settings,
        )
        if any(validator(None) for validator in validators):
            return False
    except Exception:
        return False
    return True


def get_engine_capability() -> BackupEngineCapability:
    setting_enabled = engine_setting_enabled()
    async_ready = async_configuration_ready()
    runtime_ready = runtime_configuration_ready()
    available = bool(
        setting_enabled
        and OPERATIONAL_PROVIDER_STACK_READY
        and ASYNC_EXECUTION_BOUNDARY_READY
        and SCHEDULE_DISPATCHER_READY
        and RUNTIME_COMPOSITION_READY
        and production_durable_storage_configuration_ready()
        and async_ready
        and runtime_ready
    )
    if available:
        reason = ""
    elif not setting_enabled:
        reason = "Backup execution is disabled by application configuration."
    elif not OPERATIONAL_PROVIDER_STACK_READY:
        reason = INCOMPLETE_PROVIDER_STACK_REASON
    elif not async_ready:
        reason = "Backup execution requires its dedicated non-eager Celery queues."
    else:
        reason = "Backup runtime configuration is not safe."
    return BackupEngineCapability(
        setting_enabled=setting_enabled,
        snapshot_provider_ready=SQLITE_SNAPSHOT_PROVIDER_READY,
        logical_export_provider_ready=TENANT_LOGICAL_EXPORT_PROVIDER_READY,
        media_capture_provider_ready=MEDIA_CAPTURE_PROVIDER_READY,
        canonical_manifest_provider_ready=CANONICAL_MANIFEST_PROVIDER_READY,
        deterministic_package_provider_ready=DETERMINISTIC_PACKAGE_PROVIDER_READY,
        independent_package_verifier_ready=INDEPENDENT_PACKAGE_VERIFIER_READY,
        encrypted_artifact_provider_ready=ENCRYPTED_ARTIFACT_PROVIDER_READY,
        durable_storage_provider_ready=DURABLE_STORAGE_PROVIDER_READY,
        retention_engine_ready=RETENTION_ENGINE_READY,
        runtime_orchestrator_ready=RUNTIME_ORCHESTRATOR_READY,
        async_execution_boundary_ready=ASYNC_EXECUTION_BOUNDARY_READY,
        schedule_dispatcher_ready=SCHEDULE_DISPATCHER_READY,
        reconciliation_ready=RECONCILIATION_READY,
        runtime_composition_ready=RUNTIME_COMPOSITION_READY,
        restore_preflight_engine_ready=RESTORE_PREFLIGHT_ENGINE_READY,
        restore_mutation_engine_ready=RESTORE_MUTATION_ENGINE_READY,
        restore_mutation_setting_enabled=restore_mutation_setting_enabled(),
        async_configuration_ready=async_ready,
        runtime_configuration_ready=runtime_ready,
        # Runtime readiness depends on the selected database and private
        # workspace. Planning deliberately does not perform that assessment.
        runtime_snapshot_policy_ready=None,
        provider_stack_ready=OPERATIONAL_PROVIDER_STACK_READY,
        real_execution_available=available,
        disabled_reason=reason,
    )


def real_execution_available() -> bool:
    return get_engine_capability().real_execution_available


def assert_real_execution_available() -> BackupEngineCapability:
    capability = get_engine_capability()
    if not capability.real_execution_available:
        raise BackupEngineDisabled(capability.disabled_reason)
    return capability
