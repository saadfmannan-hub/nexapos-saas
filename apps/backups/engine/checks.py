"""Non-mutating backup configuration and provider-capability system checks."""

import os
import stat
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.files.storage import FileSystemStorage
from django.utils.module_loading import import_string

from . import availability
from .durable_storage_exceptions import (
    DurableStoragePolicyError,
    UnsafeDurableStorageRoot,
)
from .durable_storage_policy import (
    DurableStoragePolicy,
    validate_durable_storage_root,
)
from .encryption_exceptions import (
    EncryptionPolicyError,
    KeyProviderConfigurationError,
)
from .encryption_policy import EncryptionPolicy
from .exceptions import (
    LogicalExportPolicyError,
    LogicalExportRegistryError,
    MediaCapturePolicyError,
    SQLiteSnapshotPolicyError,
    UnsafeWorkspacePath,
)
from .key_management import LocalConfiguredKekProvider
from .logical_export_policy import LogicalExportPolicy
from .logical_export_registry import get_logical_export_registry
from .media_capture_policy import MediaCapturePolicy
from .retention_exceptions import RetentionPolicyError
from .retention_policy import RetentionPolicy
from .runtime_exceptions import RuntimeProviderStackError
from .snapshot_policy import SQLiteSnapshotPolicy
from .workspace import path_has_link_like_component, validate_staging_root


def _absolute_configuration_path(value):
    try:
        candidate = Path(value).expanduser()
    except (TypeError, ValueError, OSError):
        raise ValueError from None
    if not candidate.is_absolute():
        raise ValueError
    try:
        return Path(os.path.abspath(candidate))
    except (OSError, ValueError):
        raise ValueError from None


def _resolved_configuration_path(value):
    try:
        return _absolute_configuration_path(value).resolve(strict=False)
    except (OSError, RuntimeError):
        raise ValueError from None


def _is_relative_to(candidate, root):
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _public_root_is_inside_staging(staging_root):
    for setting_name in ("MEDIA_ROOT", "STATIC_ROOT"):
        configured = getattr(settings, setting_name, None)
        if not configured:
            continue
        try:
            public_root = _resolved_configuration_path(configured)
        except ValueError:
            # The root-specific check reports an invalid MEDIA_ROOT. STATIC_ROOT
            # validation remains outside the backup engine's ownership.
            continue
        if public_root == staging_root or _is_relative_to(public_root, staging_root):
            return True
    return False


def _validated_media_root():
    lexical_media_root = _absolute_configuration_path(
        getattr(settings, "MEDIA_ROOT", "")
    )
    if path_has_link_like_component(lexical_media_root):
        raise ValueError
    if not os.path.lexists(lexical_media_root):
        raise ValueError
    try:
        metadata = os.stat(lexical_media_root, follow_symlinks=False)
    except OSError:
        raise ValueError from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError
    try:
        resolved = lexical_media_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError from None
    if resolved != lexical_media_root:
        raise ValueError
    return resolved


def _configured_default_storage_location(media_root):
    configured_storages = getattr(settings, "STORAGES", None)
    if not isinstance(configured_storages, dict):
        raise ValueError
    default_configuration = configured_storages.get("default")
    if not isinstance(default_configuration, dict):
        raise ValueError
    backend_path = default_configuration.get("BACKEND")
    if not isinstance(backend_path, str):
        raise ValueError
    try:
        backend_class = import_string(backend_path)
    except (ImportError, AttributeError, ValueError):
        raise ValueError from None
    if backend_class is not FileSystemStorage:
        raise ValueError

    options = default_configuration.get("OPTIONS", {})
    if not isinstance(options, dict):
        raise ValueError
    storage_location = options.get("location", media_root)
    lexical_location = _absolute_configuration_path(storage_location)
    if path_has_link_like_component(lexical_location):
        raise ValueError
    try:
        resolved_location = lexical_location.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError from None
    if resolved_location != media_root:
        raise ValueError
    return resolved_location


@register(Tags.security)
def check_backup_staging_root(app_configs, **kwargs):
    root = getattr(settings, "BACKUP_STAGING_ROOT", "")
    try:
        staging_root = validate_staging_root(root)
        if _public_root_is_inside_staging(staging_root):
            raise UnsafeWorkspacePath(
                "Public roots cannot be located inside the backup staging root."
            )
    except UnsafeWorkspacePath as exc:
        return [
            Error(
                exc.sanitized_message,
                hint=(
                    "Configure an absolute private staging root outside "
                    "MEDIA_ROOT and STATIC_ROOT."
                ),
                id="backups.E020",
            )
        ]
    return []


@register(Tags.security)
def check_sqlite_snapshot_policy_settings(app_configs, **kwargs):
    try:
        SQLiteSnapshotPolicy.from_settings()
    except SQLiteSnapshotPolicyError as exc:
        return [
            Error(
                exc.sanitized_message,
                hint="Configure bounded fail-closed SQLite snapshot policy values.",
                id="backups.E021",
            )
        ]
    return []


@register(Tags.security)
def check_logical_export_policy_settings(app_configs, **kwargs):
    try:
        LogicalExportPolicy.from_settings()
    except LogicalExportPolicyError as exc:
        return [
            Error(
                exc.sanitized_message,
                hint="Configure bounded fail-closed logical export policy values.",
                id="backups.E022",
            )
        ]
    return []


@register(Tags.models)
def check_logical_export_registry(app_configs, **kwargs):
    try:
        get_logical_export_registry().validate_complete()
    except LogicalExportRegistryError as exc:
        return [
            Error(
                exc.sanitized_message,
                hint=(
                    "Classify every eligible logical model and field explicitly; "
                    "do not enable automatic export discovery."
                ),
                id="backups.E023",
            )
        ]
    return []


@register(Tags.security)
def check_media_capture_policy_settings(app_configs, **kwargs):
    try:
        MediaCapturePolicy.from_settings()
    except MediaCapturePolicyError as exc:
        return [
            Error(
                exc.sanitized_message,
                hint="Configure bounded fail-closed media capture policy values.",
                id="backups.E024",
            )
        ]
    return []


@register(Tags.security)
def check_media_storage_configuration(app_configs, **kwargs):
    try:
        media_root = _validated_media_root()
        _configured_default_storage_location(media_root)
    except ValueError:
        return [
            Error(
                "The configured media storage is not safe for local backup capture.",
                hint=(
                    "Use the exact Django FileSystemStorage backend with an absolute, "
                    "existing, real MEDIA_ROOT and no link or reparse components."
                ),
                id="backups.E025",
            )
        ]
    return []


@register(Tags.security)
def check_encryption_policy_settings(app_configs, **kwargs):
    try:
        EncryptionPolicy.from_settings()
    except EncryptionPolicyError as exc:
        return [
            Error(
                exc.sanitized_message,
                hint="Configure bounded fail-closed encrypted-artifact policy values.",
                id="backups.E027",
            )
        ]
    return []


@register(Tags.security)
def check_local_kek_configuration(app_configs, **kwargs):
    configured_values = (
        getattr(settings, "BACKUP_LOCAL_KEK_B64", ""),
        getattr(settings, "BACKUP_LOCAL_KEK_ID", ""),
        getattr(settings, "BACKUP_LOCAL_KEK_VERSION", ""),
    )
    encryption_configured_for_use = bool(
        availability.engine_setting_enabled() or any(configured_values)
    )
    if not encryption_configured_for_use:
        return []
    try:
        LocalConfiguredKekProvider.from_settings()
    except KeyProviderConfigurationError as exc:
        return [
            Error(
                exc.sanitized_message,
                hint=(
                    "Configure an exact Base64-encoded 32-byte local KEK plus "
                    "safe key identifier and version for internal development use."
                ),
                id="backups.E028",
            )
        ]
    return []


@register(Tags.security)
def check_durable_storage_policy_settings(app_configs, **kwargs):
    try:
        DurableStoragePolicy.from_settings()
    except DurableStoragePolicyError as exc:
        return [
            Error(
                exc.sanitized_message,
                hint="Configure bounded fail-closed durable-storage policy values.",
                id="backups.E029",
            )
        ]
    return []


@register(Tags.security)
def check_durable_storage_root(app_configs, **kwargs):
    try:
        policy = DurableStoragePolicy.from_settings()
        validate_durable_storage_root(
            policy.root,
            staging_root=getattr(settings, "BACKUP_STAGING_ROOT", None),
            media_root=getattr(settings, "MEDIA_ROOT", None),
            static_root=getattr(settings, "STATIC_ROOT", None),
            require_local=policy.require_local,
        )
    except (DurableStoragePolicyError, UnsafeDurableStorageRoot) as exc:
        return [
            Error(
                exc.sanitized_message,
                hint=(
                    "Configure an absolute confirmed-local private durable root "
                    "that does not overlap staging, MEDIA_ROOT, or STATIC_ROOT."
                ),
                id="backups.E030",
            )
        ]
    return []


@register(Tags.security)
def check_retention_policy_settings(app_configs, **kwargs):
    try:
        RetentionPolicy.from_settings()
    except RetentionPolicyError as exc:
        return [
            Error(
                exc.sanitized_message,
                hint="Configure bounded fail-closed backup retention policy values.",
                id="backups.E031",
            )
        ]
    return []


@register(Tags.security)
def check_runtime_execution_settings(app_configs, **kwargs):
    lease_seconds = getattr(settings, "BACKUP_EXECUTION_LOCK_LEASE_SECONDS", None)
    if type(lease_seconds) is not int or not 300 <= lease_seconds <= 86_400:
        return [
            Error(
                "The backup execution lock lease is invalid.",
                hint="Configure a bounded 300 to 86400 second tenant lease.",
                id="backups.E032",
            )
        ]
    return []


@register(Tags.security)
def check_runtime_provider_stack_configuration(app_configs, **kwargs):
    if not availability.engine_setting_enabled():
        return []
    try:
        from .runtime import build_runtime_provider_stack

        build_runtime_provider_stack()
    except RuntimeProviderStackError:
        return [
            Error(
                "The backup runtime provider stack cannot be composed safely.",
                hint=(
                    "Validate the private roots, policies, local development KEK, "
                    "and exact provider composition before enabling execution."
                ),
                id="backups.E033",
            )
        ]
    return []


@register(Tags.security)
def check_restore_preflight_configuration(app_configs, **kwargs):
    """Validate restore prerequisites without retrieving or decrypting an object."""

    try:
        staging_root = validate_staging_root(
            getattr(settings, "BACKUP_STAGING_ROOT", "")
        )
        encryption_policy = EncryptionPolicy.from_settings()
        durable_policy = DurableStoragePolicy.from_settings()
        validate_durable_storage_root(
            durable_policy.root,
            staging_root=staging_root,
            media_root=getattr(settings, "MEDIA_ROOT", None),
            static_root=getattr(settings, "STATIC_ROOT", None),
            require_local=durable_policy.require_local,
        )
        if encryption_policy.maximum_artifact_bytes > durable_policy.maximum_object_bytes:
            raise ValueError
        configured_key_values = (
            getattr(settings, "BACKUP_LOCAL_KEK_B64", ""),
            getattr(settings, "BACKUP_LOCAL_KEK_ID", ""),
            getattr(settings, "BACKUP_LOCAL_KEK_VERSION", ""),
        )
        if availability.engine_setting_enabled() or any(configured_key_values):
            LocalConfiguredKekProvider.from_settings()
    except Exception:
        return [
            Error(
                "Restore-preflight provider configuration is not safe.",
                hint=(
                    "Configure compatible private staging, durable retrieval, "
                    "encryption policy, and KEK settings before restore preflight."
                ),
                id="backups.E034",
            )
        ]
    return []


@register(Tags.security)
def check_backup_capability_consistency(app_configs, **kwargs):
    capability = availability.get_engine_capability()
    consistent = (
        availability.SQLITE_SNAPSHOT_PROVIDER_READY is True
        and availability.TENANT_LOGICAL_EXPORT_PROVIDER_READY is True
        and availability.MEDIA_CAPTURE_PROVIDER_READY is True
        and availability.CANONICAL_MANIFEST_PROVIDER_READY is True
        and availability.DETERMINISTIC_PACKAGE_PROVIDER_READY is True
        and availability.INDEPENDENT_PACKAGE_VERIFIER_READY is True
        and availability.ENCRYPTED_ARTIFACT_PROVIDER_READY is True
        and availability.DURABLE_STORAGE_PROVIDER_READY is True
        and availability.RETENTION_ENGINE_READY is True
        and availability.RUNTIME_ORCHESTRATOR_READY is True
        and availability.ASYNC_EXECUTION_BOUNDARY_READY is True
        and availability.SCHEDULE_DISPATCHER_READY is True
        and availability.RUNTIME_COMPOSITION_READY is True
        and availability.RESTORE_PREFLIGHT_ENGINE_READY is True
        and availability.RESTORE_MUTATION_ENGINE_READY is True
        and availability.RESTORE_ASYNC_EXECUTION_BOUNDARY_READY is True
        and availability.OPERATIONAL_PROVIDER_STACK_READY is False
        and capability.snapshot_provider_ready
        is availability.SQLITE_SNAPSHOT_PROVIDER_READY
        and capability.logical_export_provider_ready
        is availability.TENANT_LOGICAL_EXPORT_PROVIDER_READY
        and capability.media_capture_provider_ready
        is availability.MEDIA_CAPTURE_PROVIDER_READY
        and capability.canonical_manifest_provider_ready
        is availability.CANONICAL_MANIFEST_PROVIDER_READY
        and capability.deterministic_package_provider_ready
        is availability.DETERMINISTIC_PACKAGE_PROVIDER_READY
        and capability.independent_package_verifier_ready
        is availability.INDEPENDENT_PACKAGE_VERIFIER_READY
        and capability.encrypted_artifact_provider_ready
        is availability.ENCRYPTED_ARTIFACT_PROVIDER_READY
        and capability.durable_storage_provider_ready
        is availability.DURABLE_STORAGE_PROVIDER_READY
        and capability.retention_engine_ready is availability.RETENTION_ENGINE_READY
        and capability.runtime_orchestrator_ready
        is availability.RUNTIME_ORCHESTRATOR_READY
        and capability.async_execution_boundary_ready
        is availability.ASYNC_EXECUTION_BOUNDARY_READY
        and capability.schedule_dispatcher_ready
        is availability.SCHEDULE_DISPATCHER_READY
        and capability.runtime_composition_ready
        is availability.RUNTIME_COMPOSITION_READY
        and capability.restore_preflight_engine_ready
        is availability.RESTORE_PREFLIGHT_ENGINE_READY
        and capability.restore_mutation_engine_ready
        is availability.RESTORE_MUTATION_ENGINE_READY
        and capability.restore_mutation_setting_enabled
        is availability.restore_mutation_setting_enabled()
        and capability.async_configuration_ready
        is availability.async_configuration_ready()
        and capability.runtime_configuration_ready is False
        and capability.provider_stack_ready
        is availability.OPERATIONAL_PROVIDER_STACK_READY
        and capability.real_execution_available is False
        and availability.real_execution_available() is False
        and availability.restore_execution_available() is False
    )
    if not consistent:
        return [
            Error(
                "Backup provider capability flags are not internally consistent.",
                hint=(
                    "Keep internal backup, restore-preflight, and guarded restore "
                    "mutation foundations ready while the operational provider stack "
                    "and real production execution remain disabled."
                ),
                id="backups.E026",
            )
        ]
    return []
