"""Non-mutating backup staging and SQLite policy system checks."""

from django.conf import settings
from django.core.checks import Error, Tags, register

from .exceptions import (
    LogicalExportPolicyError,
    LogicalExportRegistryError,
    SQLiteSnapshotPolicyError,
    UnsafeWorkspacePath,
)
from .logical_export_policy import LogicalExportPolicy
from .logical_export_registry import get_logical_export_registry
from .snapshot_policy import SQLiteSnapshotPolicy
from .workspace import validate_staging_root


@register(Tags.security)
def check_backup_staging_root(app_configs, **kwargs):
    root = getattr(settings, "BACKUP_STAGING_ROOT", "")
    try:
        validate_staging_root(root)
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
