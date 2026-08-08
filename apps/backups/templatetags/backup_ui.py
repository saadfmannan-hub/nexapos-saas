"""Safe owner-facing labels and formatting for backup metadata."""

from django import template

from apps.backups.enums import BackupStatus, BackupTrigger

register = template.Library()

_STATUS_LABELS = {
    BackupStatus.QUEUED: "Queued",
    BackupStatus.PREPARING: "Preparing",
    BackupStatus.SNAPSHOTTING: "Creating backup",
    BackupStatus.PACKAGING: "Packaging",
    BackupStatus.UPLOADING: "Securing backup",
    BackupStatus.VERIFYING: "Verifying",
    BackupStatus.SUCCEEDED: "Ready",
    BackupStatus.FAILED: "Failed",
    BackupStatus.CANCELLED: "Cancelled",
    BackupStatus.DELETION_PENDING: "Unavailable",
    BackupStatus.DELETED: "Unavailable",
}

_TYPE_LABELS = {
    BackupTrigger.MANUAL: "Manual",
    BackupTrigger.SCHEDULED: "Automatic",
    BackupTrigger.PRE_RESTORE_SAFETY: "Safety Backup",
}


@register.filter
def owner_backup_status(value):
    return _STATUS_LABELS.get(value, "Unavailable")


@register.filter
def owner_backup_type(value):
    return _TYPE_LABELS.get(value, "Backup")


@register.filter
def duration_display(value):
    if value is None:
        return "—"
    seconds = max(0, int(value.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


@register.filter
def restore_eligible(backup):
    return bool(getattr(backup, "owner_restore_eligible", False))
