"""Non-mutating backup staging and SQLite policy system checks."""

from django.conf import settings
from django.core.checks import Error, Tags, register

from .exceptions import SQLiteSnapshotPolicyError, UnsafeWorkspacePath
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
