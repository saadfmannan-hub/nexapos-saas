"""Tenant-scoped alterations kept separate from normal WMS production."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.wms_core.models import ValidatedTenantModel, WmsLocation
from apps.wms_workforce.models import WmsEmployee


def normalize_alteration_reference(value):
    return (value or "").strip().upper()


class WmsAlteration(ValidatedTenantModel):
    class Reason(models.TextChoices):
        SIZE = "SIZE", "Size"
        DARAZ = "DARAZ", "Daraz"
        FINISHING = "FINISHING", "Finishing"
        BUTTON = "BUTTON", "Button"
        VIP_DESIGN = "VIP_DESIGN", "VIP Design"
        COMPUTER_DESIGN = "COMPUTER_DESIGN", "Computer Design"
        IRON = "IRON", "Iron"
        OTHER = "OTHER", "Other"

    class MistakeBy(models.TextChoices):
        EMPLOYEE = "EMPLOYEE", "Employee"
        CUSTOMER = "CUSTOMER", "Customer"
        UNKNOWN = "UNKNOWN", "Unknown"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        COMPLETED = "COMPLETED", "Completed"

    location = models.ForeignKey(
        WmsLocation,
        on_delete=models.PROTECT,
        related_name="alterations",
    )
    original_order_reference = models.CharField(max_length=80)
    alteration_reference = models.CharField(max_length=80, blank=True)
    reason = models.CharField(max_length=24, choices=Reason.choices)
    mistake_by = models.CharField(
        max_length=16,
        choices=MistakeBy.choices,
    )
    mistake_by_employee = models.ForeignKey(
        WmsEmployee,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="alterations_mistake_by",
    )
    assigned_employee = models.ForeignKey(
        WmsEmployee,
        on_delete=models.PROTECT,
        related_name="assigned_alterations",
    )
    alteration_date = models.DateField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )
    notes = models.TextField(blank=True)
    is_corrected = models.BooleanField(default=False, editable=False)
    correction_reason = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_alterations_completed",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_alterations_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_alterations_updated",
    )

    class Meta:
        ordering = ["-alteration_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    reason__in=(
                        "SIZE",
                        "DARAZ",
                        "FINISHING",
                        "BUTTON",
                        "VIP_DESIGN",
                        "COMPUTER_DESIGN",
                        "IRON",
                        "OTHER",
                    )
                ),
                name="valid_wms_alteration_reason",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=("OPEN", "IN_PROGRESS", "COMPLETED")
                ),
                name="valid_wms_alteration_status",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        mistake_by="EMPLOYEE",
                        mistake_by_employee__isnull=False,
                    )
                    | models.Q(
                        mistake_by__in=("CUSTOMER", "UNKNOWN"),
                        mistake_by_employee__isnull=True,
                    )
                ),
                name="valid_wms_alteration_mistake",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="COMPLETED",
                        completed_at__isnull=False,
                    )
                    | models.Q(
                        status__in=("OPEN", "IN_PROGRESS"),
                        completed_at__isnull=True,
                    )
                ),
                name="valid_wms_alteration_completion",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "alteration_date"],
                name="wms_alt_business_date_idx",
            ),
            models.Index(
                fields=["business", "location", "alteration_date"],
                name="wms_alt_location_date_idx",
            ),
            models.Index(
                fields=["business", "assigned_employee", "alteration_date"],
                name="wms_alt_assigned_date_idx",
            ),
            models.Index(
                fields=["business", "status", "alteration_date"],
                name="wms_alt_status_date_idx",
            ),
            models.Index(
                fields=["business", "reason", "alteration_date"],
                name="wms_alt_reason_date_idx",
            ),
            models.Index(
                fields=["business", "original_order_reference"],
                name="wms_alt_order_ref_idx",
            ),
        ]

    def _original_state(self):
        if not self.pk:
            return None
        return (
            type(self)
            .objects.filter(pk=self.pk)
            .values(
                "location_id",
                "original_order_reference",
                "alteration_reference",
                "mistake_by",
                "mistake_by_employee_id",
                "assigned_employee_id",
                "status",
                "completed_at",
                "completed_by_id",
            )
            .first()
        )

    def _validate_employee(
        self,
        employee,
        *,
        original_employee_id=None,
        original_location_id=None,
    ):
        if employee.business_id != self.business_id:
            return "The employee must belong to the same business."
        if employee.location_id != self.location_id:
            return "The employee must belong to the alteration location."
        unchanged_historical = (
            original_employee_id == employee.pk
            and original_location_id == self.location_id
        )
        if unchanged_historical:
            return ""
        if (
            not employee.is_active
            or not employee.location.is_active
            or not employee.location.branch.is_active
        ):
            return "Select an active employee at an active WMS location."
        return ""

    def clean(self):
        super().clean()
        errors = {}
        raw_original_reference = self.original_order_reference or ""
        raw_alteration_reference = self.alteration_reference or ""
        self.original_order_reference = normalize_alteration_reference(
            raw_original_reference
        )
        self.alteration_reference = normalize_alteration_reference(
            raw_alteration_reference
        )
        self.notes = (self.notes or "").strip()
        self.correction_reason = (self.correction_reason or "").strip()

        if not self.original_order_reference:
            errors["original_order_reference"] = (
                "Original order reference is required."
            )
        if (
            "\n" in raw_original_reference
            or "\r" in raw_original_reference
            or "\n" in raw_alteration_reference
            or "\r" in raw_alteration_reference
        ):
            errors["original_order_reference"] = (
                "Operational references must use one line."
            )

        if (
            self.business_id
            and self.location_id
            and self.location.business_id != self.business_id
        ):
            errors["location"] = (
                "The WMS location must belong to the same business."
            )

        original = self._original_state()
        if original is None:
            if self.status != self.Status.OPEN:
                errors["status"] = "New alterations must start Open."
            if self.completed_at is not None or self.completed_by_id is not None:
                errors["status"] = (
                    "New alterations cannot contain completion details."
                )
            if self.location_id and (
                not self.location.is_active
                or not self.location.branch.is_active
            ):
                errors["location"] = (
                    "Inactive WMS locations cannot receive alterations."
                )
        else:
            if (
                normalize_alteration_reference(
                    original["original_order_reference"]
                )
                != self.original_order_reference
                or normalize_alteration_reference(
                    original["alteration_reference"]
                )
                != self.alteration_reference
            ):
                errors["original_order_reference"] = (
                    "Operational references cannot change after creation."
                )
            transition = (original["status"], self.status)
            allowed_transitions = {
                (self.Status.OPEN, self.Status.OPEN),
                (self.Status.OPEN, self.Status.IN_PROGRESS),
                (self.Status.IN_PROGRESS, self.Status.IN_PROGRESS),
                (self.Status.IN_PROGRESS, self.Status.COMPLETED),
                (self.Status.COMPLETED, self.Status.COMPLETED),
            }
            if transition not in allowed_transitions:
                errors["status"] = (
                    "Alterations can move only from Open to In Progress "
                    "to Completed."
                )
            if original["status"] == self.Status.COMPLETED and (
                original["completed_at"] != self.completed_at
                or original["completed_by_id"] != self.completed_by_id
            ):
                errors["status"] = (
                    "Completed alterations cannot change completion details."
                )

        if self.status == self.Status.COMPLETED:
            if self.completed_at is None:
                errors["status"] = (
                    "Completed alterations require a completion timestamp."
                )
        elif self.completed_at is not None or self.completed_by_id is not None:
            errors["status"] = (
                "Only Completed alterations may contain completion details."
            )

        if self.is_corrected and not self.correction_reason:
            errors["correction_reason"] = (
                "A correction reason is required for corrected alterations."
            )

        original_location_id = (
            original["location_id"] if original is not None else None
        )
        original_assigned_id = (
            original["assigned_employee_id"] if original is not None else None
        )
        if (
            self.business_id
            and self.location_id
            and self.assigned_employee_id
        ):
            assigned_error = self._validate_employee(
                self.assigned_employee,
                original_employee_id=original_assigned_id,
                original_location_id=original_location_id,
            )
            if assigned_error:
                errors["assigned_employee"] = assigned_error

        if self.mistake_by == self.MistakeBy.EMPLOYEE:
            if not self.mistake_by_employee_id:
                errors["mistake_by_employee"] = (
                    "Select the employee responsible for the mistake."
                )
            elif self.business_id and self.location_id:
                original_mistake_id = (
                    original["mistake_by_employee_id"]
                    if original is not None
                    else None
                )
                mistake_error = self._validate_employee(
                    self.mistake_by_employee,
                    original_employee_id=original_mistake_id,
                    original_location_id=original_location_id,
                )
                if mistake_error:
                    errors["mistake_by_employee"] = mistake_error
        elif self.mistake_by_employee_id:
            errors["mistake_by_employee"] = (
                "Mistake By Employee is available only for Employee."
            )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return (
            f"{self.original_order_reference} — "
            f"{self.get_reason_display()}"
        )
