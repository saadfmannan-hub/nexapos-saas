"""Persistent metadata for the Backup & Restore bounded context.

Phase 1 stores orchestration metadata only.  These models intentionally do not
create snapshots, packages, encryption material, storage objects, downloads,
or tenant-data mutations.
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TenantManager, TenantQuerySet, TimeStampedModel

from .enums import (
    ActivitySeverity,
    BackupScope,
    BackupStatus,
    BackupTrigger,
    CompatibilityStatus,
    DependencyCheckStatus,
    IntegrityStatus,
    OperationKind,
    ProductOwner,
    RestoreStatus,
)


class EvidenceQuerySet(TenantQuerySet):
    """Tenant queryset that preserves audit/evidence rows."""

    def delete(self):
        raise ValueError("Backup and restore evidence cannot be hard-deleted.")


class BackupRecordQuerySet(EvidenceQuerySet):
    _IMMUTABLE_FIELDS = frozenset(
        {"business", "business_id", "public_id", "tenant_public_id_snapshot"}
    )

    def update(self, **kwargs):
        if self._IMMUTABLE_FIELDS.intersection(kwargs):
            raise ValueError("Immutable backup identity fields cannot be updated.")
        return super().update(**kwargs)


class BackupRecordManager(models.Manager.from_queryset(BackupRecordQuerySet)):
    pass


class RestoreOperationManager(models.Manager.from_queryset(EvidenceQuerySet)):
    pass


class BackupRecord(TimeStampedModel):
    """One durable backup identity and its lifecycle metadata."""

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    business = models.ForeignKey(
        "tenants.Business",
        on_delete=models.PROTECT,
        related_name="backup_records",
        db_index=True,
    )
    tenant_public_id_snapshot = models.UUIDField(editable=False, db_index=True)

    scope = models.CharField(max_length=20, choices=BackupScope.choices)
    included_products = models.JSONField(default=list, blank=True)
    included_components = models.JSONField(default=list, blank=True)
    trigger = models.CharField(
        max_length=24,
        choices=BackupTrigger.choices,
        default=BackupTrigger.MANUAL,
        db_index=True,
    )
    scheduled_local_date = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=24,
        choices=BackupStatus.choices,
        default=BackupStatus.QUEUED,
        db_index=True,
    )
    integrity_status = models.CharField(
        max_length=20,
        choices=IntegrityStatus.choices,
        default=IntegrityStatus.NOT_CHECKED,
        db_index=True,
    )
    pinned = models.BooleanField(default=False, db_index=True)
    retention_eligible = models.BooleanField(default=False, db_index=True)
    protected = models.BooleanField(default=False, db_index=True)

    format_version = models.CharField(max_length=32)
    application_version = models.CharField(max_length=64)
    schema_fingerprint = models.CharField(max_length=64)
    minimum_restore_version = models.CharField(max_length=64)
    compatibility_status = models.CharField(
        max_length=24,
        choices=CompatibilityStatus.choices,
        default=CompatibilityStatus.NOT_CHECKED,
    )
    restore_compatibility_reason = models.CharField(max_length=500, blank=True)

    # Restart-persistent durable-object and envelope-encryption evidence.
    storage_backend_identifier = models.CharField(max_length=80, blank=True)
    opaque_object_key = models.CharField(max_length=500, blank=True)
    storage_bucket_identifier = models.CharField(max_length=255, blank=True)
    storage_object_version_identifier = models.CharField(max_length=1024, blank=True)
    encryption_key_identifier = models.CharField(max_length=255, blank=True)
    encrypted_data_key_envelope = models.TextField(blank=True)
    whole_artifact_hash = models.CharField(max_length=128, blank=True)

    total_row_count = models.PositiveBigIntegerField(default=0)
    component_count = models.PositiveIntegerField(default=0)
    media_count = models.PositiveBigIntegerField(default=0)
    backup_size_bytes = models.PositiveBigIntegerField(default=0)
    duration = models.DurationField(null=True, blank=True)

    queued_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backup_records_created",
    )
    creator_actor_snapshot = models.JSONField(default=dict, blank=True)
    system_actor = models.BooleanField(default=False)
    failure_code = models.CharField(max_length=80, blank=True)
    sanitized_failure_summary = models.TextField(blank=True)

    parent_restore_operation = models.ForeignKey(
        "backups.RestoreOperation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="safety_backup_records",
    )
    idempotency_key = models.CharField(max_length=128)

    objects = BackupRecordManager()

    class Meta:
        ordering = ["-created_at"]
        permissions = [
            ("platform_view_metadata", "Can view platform backup metadata"),
            ("platform_manage_backups", "Can manage platform backups"),
            ("platform_approve_restore", "Can approve platform restores"),
            ("platform_cleanup_backups", "Can clean up platform backup artifacts"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "idempotency_key"],
                name="uniq_backup_idempotency",
            ),
            models.UniqueConstraint(
                fields=["opaque_object_key"],
                condition=~Q(opaque_object_key=""),
                name="uniq_backup_object_key",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(trigger=BackupTrigger.SCHEDULED)
                    | Q(scheduled_local_date__isnull=False)
                ),
                name="scheduled_backup_has_date",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(trigger=BackupTrigger.PRE_RESTORE_SAFETY)
                    | (
                        Q(protected=True)
                        & Q(retention_eligible=False)
                        & Q(parent_restore_operation__isnull=False)
                    )
                ),
                name="safety_backup_is_protected",
            ),
            models.CheckConstraint(
                condition=(
                    Q(retention_eligible=False)
                    | (
                        Q(trigger=BackupTrigger.SCHEDULED)
                        & Q(scope=BackupScope.ALL_ENABLED)
                        & Q(status=BackupStatus.SUCCEEDED)
                        & Q(integrity_status=IntegrityStatus.VERIFIED)
                        & Q(pinned=False)
                        & Q(protected=False)
                    )
                ),
                name="valid_backup_retention",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=BackupStatus.SUCCEEDED)
                    | Q(
                        integrity_status__in=(
                            IntegrityStatus.VERIFIED,
                            IntegrityStatus.CORRUPTED,
                        )
                    )
                ),
                name="successful_backup_verified",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=BackupStatus.DELETED)
                    | Q(deleted_at__isnull=False)
                ),
                name="deleted_backup_has_time",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "-created_at"],
                name="backup_business_created_idx",
            ),
            models.Index(
                fields=["business", "status", "integrity_status"],
                name="backup_business_state_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if not self.tenant_public_id_snapshot and self.business_id:
                self.tenant_public_id_snapshot = self.business.public_id
        else:
            original = type(self).objects.only(
                "business_id", "public_id", "tenant_public_id_snapshot"
            ).get(pk=self.pk)
            if (
                self.business_id != original.business_id
                or self.public_id != original.public_id
                or self.tenant_public_id_snapshot != original.tenant_public_id_snapshot
            ):
                raise ValidationError("Backup identity fields are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Backup records are tombstoned; they cannot be hard-deleted.")

    @property
    def backup_size(self):
        """Compatibility alias for callers that do not need the unit in the name."""

        return self.backup_size_bytes

    def __str__(self):
        return f"{self.business} {self.scope} backup {self.public_id}"


class BackupComponent(models.Model):
    """Per-component package metadata reserved for the future engine."""

    backup = models.ForeignKey(
        BackupRecord,
        on_delete=models.PROTECT,
        related_name="components",
    )
    component_key = models.CharField(max_length=120)
    product_category = models.CharField(max_length=12, choices=ProductOwner.choices)
    component_version = models.CharField(max_length=32)
    row_count = models.PositiveBigIntegerField(default=0)
    media_count = models.PositiveBigIntegerField(default=0)
    uncompressed_size = models.PositiveBigIntegerField(default=0)
    compressed_size = models.PositiveBigIntegerField(default=0)
    component_hash = models.CharField(max_length=128, blank=True)
    verification_status = models.CharField(
        max_length=20,
        choices=IntegrityStatus.choices,
        default=IntegrityStatus.NOT_CHECKED,
    )
    verification_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["backup_id", "component_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["backup", "component_key"],
                name="uniq_backup_component",
            )
        ]

    def __str__(self):
        return f"{self.backup.public_id}: {self.component_key}"


class BackupSchedule(TimeStampedModel):
    """One tenant-local daily schedule configuration for v1."""

    business = models.OneToOneField(
        "tenants.Business",
        on_delete=models.PROTECT,
        related_name="backup_schedule",
    )
    enabled = models.BooleanField(default=False, db_index=True)
    timezone_name = models.CharField(max_length=64)
    local_execution_time = models.TimeField()
    next_run = models.DateTimeField(null=True, blank=True, db_index=True)
    last_claimed_run = models.DateTimeField(null=True, blank=True)
    last_successful_backup = models.ForeignKey(
        BackupRecord,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="schedules_as_last_success",
    )
    last_failed_backup = models.ForeignKey(
        BackupRecord,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="schedules_as_last_failure",
    )
    scope = models.CharField(
        max_length=20,
        choices=BackupScope.choices,
        default=BackupScope.ALL_ENABLED,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backup_schedules_created",
    )

    objects = TenantManager()

    class Meta:
        ordering = ["business_id"]

    def clean(self):
        super().clean()
        errors = {}
        for field_name in ("last_successful_backup", "last_failed_backup"):
            backup = getattr(self, field_name)
            if backup is not None and backup.business_id != self.business_id:
                errors[field_name] = "The referenced backup belongs to another business."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Daily backup schedule for {self.business}"


class RestoreOperation(TimeStampedModel):
    """A durable restore request and future execution lifecycle."""

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    business = models.ForeignKey(
        "tenants.Business",
        on_delete=models.PROTECT,
        related_name="restore_operations",
        db_index=True,
    )
    source_backup = models.ForeignKey(
        BackupRecord,
        on_delete=models.PROTECT,
        related_name="restore_operations",
    )
    safety_backup = models.ForeignKey(
        BackupRecord,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="restore_operations_as_safety",
    )
    requested_scope = models.CharField(max_length=20, choices=BackupScope.choices)
    status = models.CharField(
        max_length=24,
        choices=RestoreStatus.choices,
        default=RestoreStatus.QUEUED,
        db_index=True,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="restore_operations_requested",
    )
    actor_identity_snapshot = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=500)
    dependency_check_status = models.CharField(
        max_length=20,
        choices=DependencyCheckStatus.choices,
        default=DependencyCheckStatus.NOT_CHECKED,
    )
    compatibility_status = models.CharField(
        max_length=24,
        choices=CompatibilityStatus.choices,
        default=CompatibilityStatus.NOT_CHECKED,
    )
    compatibility_reason = models.CharField(max_length=500, blank=True)
    rollback_attempted = models.BooleanField(default=False)
    rollback_result = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=80, blank=True)
    sanitized_failure_summary = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=128)

    objects = RestoreOperationManager()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "idempotency_key"],
                name="uniq_restore_idempotency",
            ),
            models.CheckConstraint(
                condition=(
                    Q(safety_backup__isnull=True)
                    | ~Q(safety_backup=models.F("source_backup"))
                ),
                name="restore_safety_not_source",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "-created_at"],
                name="restore_business_created_idx",
            ),
            models.Index(
                fields=["business", "status"],
                name="restore_business_state_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.source_backup_id and self.source_backup.business_id != self.business_id:
            errors["source_backup"] = "The source backup belongs to another business."
        if self.safety_backup_id:
            if self.safety_backup.business_id != self.business_id:
                errors["safety_backup"] = "The safety backup belongs to another business."
            elif (
                self.safety_backup.trigger != BackupTrigger.PRE_RESTORE_SAFETY
                or not self.safety_backup.protected
            ):
                errors["safety_backup"] = "The safety backup must be protected safety metadata."
        if errors:
            raise ValidationError(errors)

    def delete(self, *args, **kwargs):
        raise ValueError("Restore operation evidence cannot be hard-deleted.")

    def __str__(self):
        return f"{self.business} restore {self.public_id}"


class TenantOperationLock(models.Model):
    """Lease-based exclusive tenant operation lock.

    The conditional unique constraint is the atomic concurrency primitive on
    both SQLite and PostgreSQL.  Services use compare-and-swap updates and do
    not depend solely on ``select_for_update``.
    """

    business = models.ForeignKey(
        "tenants.Business",
        on_delete=models.PROTECT,
        related_name="operation_locks",
        db_index=True,
    )
    operation_kind = models.CharField(max_length=20, choices=OperationKind.choices)
    operation_public_id = models.UUIDField(db_index=True)
    lock_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    worker_task_identifier = models.CharField(max_length=255, blank=True)
    acquired_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_expires_at = models.DateTimeField(db_index=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True, db_index=True)

    objects = TenantManager()

    class Meta:
        ordering = ["-acquired_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business"],
                condition=Q(active=True),
                name="uniq_active_tenant_op_lock",
            ),
            models.CheckConstraint(
                condition=Q(lease_expires_at__gt=models.F("acquired_at")),
                name="lock_lease_after_acquired",
            ),
            models.CheckConstraint(
                condition=Q(active=False) | Q(released_at__isnull=True),
                name="active_lock_not_released",
            ),
        ]

    def __str__(self):
        return f"{self.business} {self.operation_kind} lock"


class AppendOnlyActivityQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValueError("Backup activities are append-only.")

    def delete(self):
        raise ValueError("Backup activities are append-only.")


class AppendOnlyActivityManager(models.Manager.from_queryset(AppendOnlyActivityQuerySet)):
    pass


class BackupActivity(models.Model):
    """Detailed append-only backup/restore evidence."""

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    business = models.ForeignKey(
        "tenants.Business",
        on_delete=models.PROTECT,
        related_name="backup_activities",
        db_index=True,
    )
    backup = models.ForeignKey(
        BackupRecord,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="activities",
    )
    restore = models.ForeignKey(
        RestoreOperation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="activities",
    )
    event_type = models.CharField(max_length=100, db_index=True)
    severity = models.CharField(
        max_length=12,
        choices=ActivitySeverity.choices,
        default=ActivitySeverity.INFO,
        db_index=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backup_activities",
    )
    actor_identity_snapshot = models.JSONField(default=dict, blank=True)
    support_actor_identity_snapshot = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    sanitized_message = models.CharField(max_length=500, blank=True)
    structured_metadata = models.JSONField(default=dict, blank=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = AppendOnlyActivityManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["business", "-created_at"],
                name="activity_business_created_idx",
            )
        ]

    def __str__(self):
        return f"{self.event_type} at {self.created_at}"

    def save(self, *args, **kwargs):
        if self.pk or not self._state.adding:
            raise ValueError("Backup activities are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Backup activities are append-only.")

    def clean(self):
        super().clean()
        errors = {}
        if self.backup_id and self.backup.business_id != self.business_id:
            errors["backup"] = "The backup belongs to another business."
        if self.restore_id and self.restore.business_id != self.business_id:
            errors["restore"] = "The restore operation belongs to another business."
        if errors:
            raise ValidationError(errors)


class DownloadGrant(models.Model):
    """Authorization metadata only; no download endpoint exists in Phase 1."""

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    backup = models.ForeignKey(
        BackupRecord,
        on_delete=models.PROTECT,
        related_name="download_grants",
    )
    business = models.ForeignKey(
        "tenants.Business",
        on_delete=models.PROTECT,
        related_name="backup_download_grants",
        db_index=True,
    )
    issued_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backup_download_grants",
    )
    token_hash = models.CharField(max_length=128, unique=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    single_use = models.BooleanField(default=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = TenantManager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Download grant {self.public_id}"

    def clean(self):
        super().clean()
        if self.backup_id and self.backup.business_id != self.business_id:
            raise ValidationError(
                {"backup": "The backup belongs to another business."}
            )

    @property
    def is_active(self):
        return (
            self.revoked_at is None
            and self.expires_at > timezone.now()
            and (not self.single_use or self.used_at is None)
        )
