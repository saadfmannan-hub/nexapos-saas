"""Tenant-scoped Nexa WMS Phase 1 foundation models."""

from datetime import time

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from apps.core.models import TenantModel

from .permissions import validate_wms_permissions


class ValidatedTenantModel(TenantModel):
    """Apply model validation consistently to WMS mutation paths."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class WmsLocation(ValidatedTenantModel):
    class LocationType(models.TextChoices):
        SOURCE = "SOURCE", "Source"
        WORKSHOP = "WORKSHOP", "Workshop"
        BOTH = "BOTH", "Source and Workshop"

    branch = models.ForeignKey(
        "branches.Branch",
        on_delete=models.PROTECT,
        related_name="wms_locations",
    )
    location_type = models.CharField(
        max_length=10,
        choices=LocationType.choices,
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["branch__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "branch"],
                name="uniq_wms_location_branch_per_business",
            ),
            models.CheckConstraint(
                condition=models.Q(location_type__in=("SOURCE", "WORKSHOP", "BOTH")),
                name="valid_wms_location_type",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "is_active"],
                name="wms_loc_business_active_idx",
            ),
        ]

    def clean(self):
        super().clean()
        if self.branch_id and self.business_id:
            if self.branch.business_id != self.business_id:
                raise ValidationError(
                    {"branch": "The branch must belong to the same business."}
                )
            if self.is_active and not self.branch.is_active:
                raise ValidationError(
                    {"branch": "An active WMS location requires an active branch."}
                )

    @property
    def can_be_workshop(self):
        return self.location_type in {
            self.LocationType.WORKSHOP,
            self.LocationType.BOTH,
        }

    def __str__(self):
        return f"{self.branch.name} ({self.get_location_type_display()})"


class WmsSettings(ValidatedTenantModel):
    default_workshop_location = models.ForeignKey(
        WmsLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_for_settings",
    )
    first_shift_start = models.TimeField(default=time(10, 0))
    first_shift_end = models.TimeField(default=time(13, 0))
    second_shift_start = models.TimeField(default=time(16, 30))
    second_shift_end = models.TimeField(default=time(22, 0))
    grace_period_minutes = models.PositiveIntegerField(default=15)

    class Meta:
        verbose_name_plural = "WMS settings"
        constraints = [
            models.UniqueConstraint(
                fields=["business"],
                name="uniq_wms_settings_per_business",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        location = self.default_workshop_location
        if location is not None and (
            location.business_id != self.business_id
            or not location.is_active
            or not location.branch.is_active
            or not location.can_be_workshop
        ):
            errors["default_workshop_location"] = (
                "Select an active Workshop or Source and Workshop "
                "location from this business."
            )
        shift_values = (
            self.first_shift_start,
            self.first_shift_end,
            self.second_shift_start,
            self.second_shift_end,
            self.grace_period_minutes,
        )
        if all(value is not None for value in shift_values):
            if self.first_shift_start >= self.first_shift_end:
                errors["first_shift_end"] = (
                    "Morning shift end must be after its start."
                )
            if self.second_shift_start >= self.second_shift_end:
                errors["second_shift_end"] = (
                    "Evening shift end must be after its start."
                )
            if self.first_shift_end > self.second_shift_start:
                errors["second_shift_start"] = (
                    "Evening shift must start after the morning shift ends."
                )
            first_minutes = (
                self.first_shift_end.hour * 60
                + self.first_shift_end.minute
                - self.first_shift_start.hour * 60
                - self.first_shift_start.minute
            )
            second_minutes = (
                self.second_shift_end.hour * 60
                + self.second_shift_end.minute
                - self.second_shift_start.hour * 60
                - self.second_shift_start.minute
            )
            if (
                first_minutes > 0
                and second_minutes > 0
                and self.grace_period_minutes
                >= min(first_minutes, second_minutes)
            ):
                errors["grace_period_minutes"] = (
                    "Grace period must be shorter than each shift."
                )
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"WMS settings for {self.business}"


class WmsRole(ValidatedTenantModel):
    name = models.CharField(max_length=80)
    code = models.SlugField(max_length=80)
    permissions = models.JSONField(default=list, blank=True)
    is_system = models.BooleanField(default=False)
    is_admin = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "business",
                name="uniq_wms_role_name_ci_per_business",
            ),
            models.UniqueConstraint(
                fields=["business", "code"],
                name="uniq_wms_role_code_per_business",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "is_active"],
                name="wms_role_business_active_idx",
            ),
        ]

    def clean(self):
        super().clean()
        self.name = self.name.strip()
        self.code = self.code.strip().lower()
        self.permissions = validate_wms_permissions(self.permissions)

    def has_perm(self, code):
        return self.is_active and code in set(self.permissions or [])

    def __str__(self):
        return self.name


class WmsUserAccess(ValidatedTenantModel):
    membership = models.ForeignKey(
        "accounts.Membership",
        on_delete=models.CASCADE,
        related_name="wms_access_records",
    )
    role = models.ForeignKey(
        WmsRole,
        on_delete=models.PROTECT,
        related_name="user_access_records",
    )
    is_active = models.BooleanField(default=True)
    allowed_locations = models.ManyToManyField(
        WmsLocation,
        blank=True,
        related_name="user_access_records",
        help_text="Empty means access to all active WMS locations.",
    )

    class Meta:
        verbose_name_plural = "WMS user access"
        constraints = [
            models.UniqueConstraint(
                fields=["business", "membership"],
                name="uniq_wms_access_membership_per_business",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "is_active"],
                name="wms_access_business_active_idx",
            ),
        ]

    def clean(self):
        super().clean()
        if self.membership_id and self.business_id:
            if self.membership.business_id != self.business_id:
                raise ValidationError(
                    {"membership": "The membership must belong to the same business."}
                )
        if self.role_id and self.business_id:
            if self.role.business_id != self.business_id:
                raise ValidationError(
                    {"role": "The WMS role must belong to the same business."}
                )

    @property
    def permission_set(self):
        if not self.is_active or not self.role.is_active:
            return set()
        return set(self.role.permissions or [])

    def has_perm(self, code):
        return code in self.permission_set

    @property
    def allowed_location_ids(self):
        ids = set(self.allowed_locations.values_list("id", flat=True))
        return ids or None

    def can_access_location(self, location):
        if (
            location is None
            or location.business_id != self.business_id
            or not location.is_active
            or not location.branch.is_active
        ):
            return False
        allowed = self.allowed_location_ids
        return allowed is None or location.id in allowed

    def __str__(self):
        return f"{self.membership.user} / {self.role}"


@receiver(
    m2m_changed,
    sender=WmsUserAccess.allowed_locations.through,
    dispatch_uid="validate_wms_access_location_scope",
)
def validate_wms_access_location_scope(
    sender,
    instance,
    action,
    pk_set,
    reverse,
    **kwargs,
):
    """Reject cross-tenant M2M writes even outside the WMS forms/services."""

    if action != "pre_add" or not pk_set:
        return
    model = WmsUserAccess if reverse else WmsLocation
    invalid = model.objects.filter(pk__in=pk_set).exclude(
        business_id=instance.business_id
    )
    if invalid.exists():
        raise ValidationError(
            "Every allowed WMS location must belong to the access business."
        )
