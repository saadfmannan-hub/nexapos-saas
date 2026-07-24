"""Tenant-scoped daily WMS production records and assignment-linked lines."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.wms_core.models import ValidatedTenantModel, WmsLocation
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
    WmsProductionCategory,
)


class WmsProductionEntry(ValidatedTenantModel):
    location = models.ForeignKey(
        WmsLocation,
        on_delete=models.PROTECT,
        related_name="production_entries",
    )
    employee = models.ForeignKey(
        WmsEmployee,
        on_delete=models.PROTECT,
        related_name="production_entries",
    )
    production_date = models.DateField()
    daily_total_pieces = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)
    is_corrected = models.BooleanField(default=False, editable=False)
    correction_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_production_entries_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_production_entries_updated",
    )

    class Meta:
        ordering = ["-production_date", "employee__full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "employee", "production_date"],
                name="uniq_wms_production_employee_date",
            ),
            models.CheckConstraint(
                condition=models.Q(daily_total_pieces__gte=0),
                name="nonnegative_wms_daily_total",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "production_date"],
                name="wms_prod_business_date_idx",
            ),
            models.Index(
                fields=["business", "location", "production_date"],
                name="wms_prod_location_date_idx",
            ),
            models.Index(
                fields=["business", "employee", "production_date"],
                name="wms_prod_employee_date_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        self.notes = (self.notes or "").strip()
        self.correction_reason = (self.correction_reason or "").strip()

        if (
            self.business_id
            and self.location_id
            and self.location.business_id != self.business_id
        ):
            errors["location"] = (
                "The WMS location must belong to the same business."
            )
        if (
            self.business_id
            and self.employee_id
            and self.employee.business_id != self.business_id
        ):
            errors["employee"] = "The employee must belong to the same business."

        original = None
        if self.pk:
            original = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("employee_id", "location_id", "production_date")
                .first()
            )
        identity_changed = original is not None and (
            original["employee_id"] != self.employee_id
            or original["location_id"] != self.location_id
            or original["production_date"] != self.production_date
        )
        if identity_changed:
            errors["employee"] = (
                "Employee, location, and date cannot be changed after creation."
            )
        if original is None and self.employee_id and self.location_id:
            if self.employee.location_id != self.location_id:
                errors["employee"] = (
                    "Production location must match the employee's WMS location."
                )
            elif not self.employee.is_active:
                errors["employee"] = (
                    "Inactive employees cannot receive new production."
                )
            elif not self.location.is_active or not self.location.branch.is_active:
                errors["location"] = (
                    "Select an employee at an active WMS location."
                )

        if self.is_corrected and not self.correction_reason:
            errors["correction_reason"] = (
                "A correction reason is required for corrected production."
            )
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.employee.employee_code} — {self.production_date:%Y-%m-%d}"


class WmsProductionEntryLine(ValidatedTenantModel):
    entry = models.ForeignKey(
        WmsProductionEntry,
        on_delete=models.PROTECT,
        related_name="lines",
    )
    assignment = models.ForeignKey(
        WmsEmployeeCategoryAssignment,
        on_delete=models.PROTECT,
        related_name="production_lines",
    )
    category = models.ForeignKey(
        WmsProductionCategory,
        on_delete=models.PROTECT,
        related_name="production_lines",
    )
    category_name_snapshot = models.CharField(max_length=100, editable=False)
    category_code_snapshot = models.CharField(
        max_length=40,
        blank=True,
        editable=False,
    )
    quantity = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = [
            "assignment__category__display_order",
            "category_name_snapshot",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "entry", "assignment"],
                name="uniq_wms_prod_entry_assignment",
            ),
            models.UniqueConstraint(
                fields=["business", "entry", "category"],
                name="uniq_wms_prod_entry_category",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0),
                name="nonnegative_wms_prod_quantity",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "category"],
                name="wms_prod_line_category_idx",
            ),
            models.Index(
                fields=["business", "assignment"],
                name="wms_prod_line_assign_idx",
            ),
        ]

    def _populate_category_snapshot(self):
        if not self.category_id:
            return
        if not self.category_name_snapshot:
            self.category_name_snapshot = self.category.name
        if not self.pk:
            self.category_code_snapshot = self.category.code

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.business_id
            and self.entry_id
            and self.entry.business_id != self.business_id
        ):
            errors["entry"] = "The production entry belongs to another business."
        if (
            self.business_id
            and self.assignment_id
            and self.assignment.business_id != self.business_id
        ):
            errors["assignment"] = (
                "The category assignment belongs to another business."
            )
        if (
            self.business_id
            and self.category_id
            and self.category.business_id != self.business_id
        ):
            errors["category"] = "The category belongs to another business."

        original = None
        if self.pk:
            original = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("entry_id", "assignment_id", "category_id")
                .first()
            )
        identity_changed = original is not None and (
            original["entry_id"] != self.entry_id
            or original["assignment_id"] != self.assignment_id
            or original["category_id"] != self.category_id
        )
        if identity_changed:
            errors["assignment"] = (
                "Production line identity cannot be changed after creation."
            )
        if (
            original is None
            and self.entry_id
            and self.assignment_id
            and self.category_id
        ):
            if self.assignment.employee_id != self.entry.employee_id:
                errors["assignment"] = (
                    "The category assignment must belong to the entry employee."
                )
            elif self.assignment.category_id != self.category_id:
                errors["category"] = (
                    "The category must match the employee assignment."
                )
            elif not self.assignment.is_active:
                errors["assignment"] = (
                    "Inactive category assignments cannot receive new production."
                )
            elif not self.category.is_active:
                errors["category"] = (
                    "Inactive categories cannot receive new production."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._populate_category_snapshot()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.entry} — {self.category_name_snapshot}: {self.quantity}"
