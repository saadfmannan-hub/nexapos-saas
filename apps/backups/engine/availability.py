"""Central capability guard for the deliberately disabled Phase 2A engine."""

from dataclasses import dataclass

from django.conf import settings

from .exceptions import BackupEngineDisabled

# This is a code capability, not a deployment toggle.  It remains false until
# the required snapshot, export, package, verification, and storage providers
# are implemented and reviewed in later phases.
OPERATIONAL_PROVIDER_STACK_READY = False
PHASE_2A_DISABLED_REASON = (
    "Phase 2A provides planning contracts only; snapshot, export, packaging, "
    "verification, and storage providers are not operational."
)


@dataclass(frozen=True, slots=True)
class BackupEngineCapability:
    setting_enabled: bool
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
        reason = PHASE_2A_DISABLED_REASON
    return BackupEngineCapability(
        setting_enabled=setting_enabled,
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
