"""Django system checks for Phase 2A staging-root safety."""

from django.conf import settings
from django.core.checks import Error, Tags, register

from .exceptions import UnsafeWorkspacePath
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
