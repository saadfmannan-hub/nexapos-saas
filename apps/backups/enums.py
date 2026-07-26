"""Stable vocabulary for backup and restore metadata.

The values in this module are persisted in the database and will also be
written to future backup manifests.  Changing a value therefore requires an
explicit compatibility decision and a data migration.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class BackupScope(models.TextChoices):
    POS = "POS", _("POS")
    WMS = "WMS", _("WMS")
    ALL_ENABLED = "ALL_ENABLED", _("All enabled products")


class BackupTrigger(models.TextChoices):
    MANUAL = "MANUAL", _("Manual")
    SCHEDULED = "SCHEDULED", _("Scheduled")
    PRE_RESTORE_SAFETY = "PRE_RESTORE_SAFETY", _("Pre-restore safety")


# Backwards-friendly domain alias.  ``trigger`` is the canonical field name.
BackupType = BackupTrigger


class BackupStatus(models.TextChoices):
    QUEUED = "QUEUED", _("Queued")
    PREPARING = "PREPARING", _("Preparing")
    SNAPSHOTTING = "SNAPSHOTTING", _("Snapshotting")
    PACKAGING = "PACKAGING", _("Packaging")
    UPLOADING = "UPLOADING", _("Uploading")
    VERIFYING = "VERIFYING", _("Verifying")
    SUCCEEDED = "SUCCEEDED", _("Succeeded")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")
    DELETION_PENDING = "DELETION_PENDING", _("Deletion pending")
    DELETED = "DELETED", _("Deleted")


class IntegrityStatus(models.TextChoices):
    NOT_CHECKED = "NOT_CHECKED", _("Not checked")
    VERIFYING = "VERIFYING", _("Verifying")
    VERIFIED = "VERIFIED", _("Verified")
    FAILED = "FAILED", _("Failed")
    CORRUPTED = "CORRUPTED", _("Corrupted")


class RestoreStatus(models.TextChoices):
    QUEUED = "QUEUED", _("Queued")
    AUTHORIZING = "AUTHORIZING", _("Authorizing")
    LOCKING = "LOCKING", _("Locking")
    SAFETY_BACKUP = "SAFETY_BACKUP", _("Creating safety backup")
    VALIDATING = "VALIDATING", _("Validating")
    RESTORING = "RESTORING", _("Restoring")
    VERIFYING = "VERIFYING", _("Verifying")
    SUCCEEDED = "SUCCEEDED", _("Succeeded")
    FAILED = "FAILED", _("Failed")
    ROLLING_BACK = "ROLLING_BACK", _("Rolling back")
    ROLLED_BACK = "ROLLED_BACK", _("Rolled back")
    INDETERMINATE = "INDETERMINATE", _("Indeterminate")


class OperationKind(models.TextChoices):
    BACKUP = "BACKUP", _("Backup")
    RESTORE = "RESTORE", _("Restore")
    RETENTION = "RETENTION", _("Retention")
    DOWNLOAD = "DOWNLOAD", _("Download")
    VERIFICATION = "VERIFICATION", _("Verification")


class ProductOwner(models.TextChoices):
    SHARED = "SHARED", _("Shared")
    POS = "POS", _("POS")
    WMS = "WMS", _("WMS")


class RestoreBehavior(models.TextChoices):
    REPLACEABLE = "REPLACEABLE", _("Replaceable")
    REFERENCE_ONLY = "REFERENCE_ONLY", _("Reference only")
    DEPENDENCY_ONLY = "DEPENDENCY_ONLY", _("Dependency only")
    NON_RESTORABLE = "NON_RESTORABLE", _("Non-restorable")


class CompatibilityStatus(models.TextChoices):
    NOT_CHECKED = "NOT_CHECKED", _("Not checked")
    COMPATIBLE = "COMPATIBLE", _("Compatible")
    REQUIRES_UPGRADE = "REQUIRES_UPGRADE", _("Requires upgrade")
    INCOMPATIBLE = "INCOMPATIBLE", _("Incompatible")


class DependencyCheckStatus(models.TextChoices):
    NOT_CHECKED = "NOT_CHECKED", _("Not checked")
    CHECKING = "CHECKING", _("Checking")
    PASSED = "PASSED", _("Passed")
    FAILED = "FAILED", _("Failed")


class ActivitySeverity(models.TextChoices):
    INFO = "INFO", _("Info")
    WARNING = "WARNING", _("Warning")
    ERROR = "ERROR", _("Error")
    CRITICAL = "CRITICAL", _("Critical")
