"""Central capability guard for the deliberately incomplete backup engine."""

from dataclasses import dataclass

from django.conf import settings

from .exceptions import BackupEngineDisabled

# Phase 2B implements the SQLite snapshot provider, but this is a code
# capability rather than permission to run a commercial backup workflow.
SQLITE_SNAPSHOT_PROVIDER_READY = True
OPERATIONAL_PROVIDER_STACK_READY = False
INCOMPLETE_PROVIDER_STACK_REASON = (
    "SQLite snapshot support is available internally, but logical export, "
    "packaging, verification, encryption, and storage providers remain incomplete."
)
# Backward-compatible import retained for Phase 2A callers and tests.
PHASE_2A_DISABLED_REASON = INCOMPLETE_PROVIDER_STACK_REASON


@dataclass(frozen=True, slots=True)
class BackupEngineCapability:
    setting_enabled: bool
    snapshot_provider_ready: bool
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


def get_engine_capability() -> BackupEngineCapability:
    setting_enabled = engine_setting_enabled()
    available = bool(setting_enabled and OPERATIONAL_PROVIDER_STACK_READY)
    if available:
        reason = ""
    elif not setting_enabled:
        reason = "Backup execution is disabled by application configuration."
    else:
        reason = INCOMPLETE_PROVIDER_STACK_REASON
    return BackupEngineCapability(
        setting_enabled=setting_enabled,
        snapshot_provider_ready=SQLITE_SNAPSHOT_PROVIDER_READY,
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
