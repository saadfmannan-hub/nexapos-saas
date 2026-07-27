from django.apps import AppConfig


class BackupsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.backups"
    verbose_name = "Backup & Restore"

    def ready(self):
        # Importing registers fail-closed registry, workspace, and async checks.
        from . import registry, tasks  # noqa: F401
        from .engine import checks  # noqa: F401
