"""Tenant-scoped WMS salary calculations and immutable snapshots."""

from calendar import monthrange
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.money import money
from apps.wms_attendance.models import WmsAttendance
from apps.wms_core.models import ValidatedTenantModel, WmsLocation
from apps.wms_production.models import (
    WmsProductionEntry,
    WmsProductionEntryLine,
)
from apps.wms_workforce.models import WmsEmployee


class WmsSalary(ValidatedTenantModel):
    class Status(models.TextChoices):
        CALCULATED = "CALCULATED", "Calculated"
        FINALIZED = "FINALIZED", "Finalized"

    employee = models.ForeignKey(
        WmsEmployee,
        on_delete=models.PROTECT,
        related_name="salary_records",
    )
    salary_year = models.PositiveSmallIntegerField()
    salary_month = models.PositiveSmallIntegerField()
    period_start = models.DateField(editable=False)
    period_end = models.DateField(editable=False)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.CALCULATED,
    )

    employee_code_snapshot = models.CharField(max_length=40, editable=False)
    employee_name_snapshot = models.CharField(max_length=160, editable=False)
    employee_joining_date_snapshot = models.DateField(editable=False)
    compensation_type_snapshot = models.CharField(
        max_length=20,
        choices=WmsEmployee.CompensationType.choices,
        editable=False,
    )
    fixed_monthly_salary_snapshot = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
        editable=False,
    )
    default_per_piece_rate_snapshot = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
        editable=False,
    )
    currency_code_snapshot = models.CharField(max_length=10, editable=False)
    currency_symbol_snapshot = models.CharField(
        max_length=10,
        blank=True,
        editable=False,
    )
    currency_precision_snapshot = models.PositiveSmallIntegerField(
        editable=False
    )
    total_eligible_quantity = models.PositiveIntegerField(
        default=0,
        editable=False,
    )
    gross_salary = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        default=Decimal("0"),
        editable=False,
    )
    calculated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="wms_salaries_calculated",
        editable=False,
    )
    calculated_at = models.DateTimeField(editable=False)
    finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_salaries_finalized",
        editable=False,
    )
    finalized_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-salary_year", "-salary_month", "employee_name_snapshot"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "employee", "salary_year", "salary_month"],
                name="uniq_wms_salary_emp_month",
            ),
            models.CheckConstraint(
                condition=models.Q(salary_year__gte=1, salary_year__lte=9999),
                name="valid_wms_salary_year",
            ),
            models.CheckConstraint(
                condition=models.Q(salary_month__gte=1, salary_month__lte=12),
                name="valid_wms_salary_month",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=("CALCULATED", "FINALIZED")),
                name="valid_wms_salary_status",
            ),
            models.CheckConstraint(
                condition=models.Q(total_eligible_quantity__gte=0),
                name="wms_salary_qty_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(gross_salary__gte=0),
                name="wms_salary_gross_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    currency_precision_snapshot__gte=0,
                    currency_precision_snapshot__lte=3,
                ),
                name="valid_wms_salary_precision",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        compensation_type_snapshot="fixed_salary",
                        fixed_monthly_salary_snapshot__isnull=False,
                        default_per_piece_rate_snapshot__isnull=True,
                    )
                    | models.Q(
                        compensation_type_snapshot="per_piece",
                        fixed_monthly_salary_snapshot__isnull=True,
                        default_per_piece_rate_snapshot__isnull=False,
                    )
                ),
                name="valid_wms_salary_comp",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="CALCULATED",
                        finalized_at__isnull=True,
                        finalized_by__isnull=True,
                    )
                    | models.Q(
                        status="FINALIZED",
                        finalized_at__isnull=False,
                    )
                ),
                name="valid_wms_salary_finalized",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "salary_year", "salary_month", "status"],
                name="wms_salary_period_status_idx",
            ),
            models.Index(
                fields=["business", "employee", "salary_year", "salary_month"],
                name="wms_salary_emp_period_idx",
            ),
        ]

    @property
    def currency_display_snapshot(self):
        return self.currency_symbol_snapshot or self.currency_code_snapshot

    def _immutable_state(self):
        return {
            field: getattr(self, field)
            for field in (
                "business_id",
                "employee_id",
                "salary_year",
                "salary_month",
                "period_start",
                "period_end",
                "status",
                "employee_code_snapshot",
                "employee_name_snapshot",
                "employee_joining_date_snapshot",
                "compensation_type_snapshot",
                "fixed_monthly_salary_snapshot",
                "default_per_piece_rate_snapshot",
                "currency_code_snapshot",
                "currency_symbol_snapshot",
                "currency_precision_snapshot",
                "total_eligible_quantity",
                "gross_salary",
                "calculated_by_id",
                "calculated_at",
                "finalized_by_id",
                "finalized_at",
            )
        }

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.business_id
            and self.employee_id
            and self.employee.business_id != self.business_id
        ):
            errors["employee"] = "The employee must belong to the same business."

        try:
            expected_end_day = monthrange(self.salary_year, self.salary_month)[1]
        except (TypeError, ValueError):
            errors["salary_month"] = "Choose a valid calendar month."
        else:
            if (
                self.period_start is not None
                and (
                    self.period_start.year != self.salary_year
                    or self.period_start.month != self.salary_month
                    or self.period_start.day != 1
                )
            ):
                errors["period_start"] = (
                    "Salary period start must be the first day of the month."
                )
            if (
                self.period_end is not None
                and (
                    self.period_end.year != self.salary_year
                    or self.period_end.month != self.salary_month
                    or self.period_end.day != expected_end_day
                )
            ):
                errors["period_end"] = (
                    "Salary period end must be the last day of the month."
                )

        if self.gross_salary is not None and self.gross_salary < 0:
            errors["gross_salary"] = "Gross salary cannot be negative."
        if (
            self.currency_precision_snapshot is not None
            and not 0 <= self.currency_precision_snapshot <= 3
        ):
            errors["currency_precision_snapshot"] = (
                "Currency precision must be between 0 and 3."
            )
        if self.compensation_type_snapshot == WmsEmployee.CompensationType.FIXED_SALARY:
            if self.fixed_monthly_salary_snapshot is None:
                errors["fixed_monthly_salary_snapshot"] = (
                    "A fixed salary snapshot is required."
                )
            if self.default_per_piece_rate_snapshot is not None:
                errors["default_per_piece_rate_snapshot"] = (
                    "Fixed salaries cannot contain a default piece-rate snapshot."
                )
        elif self.compensation_type_snapshot == WmsEmployee.CompensationType.PER_PIECE:
            if self.default_per_piece_rate_snapshot is None:
                errors["default_per_piece_rate_snapshot"] = (
                    "A default piece-rate snapshot is required."
                )
            if self.fixed_monthly_salary_snapshot is not None:
                errors["fixed_monthly_salary_snapshot"] = (
                    "Per-piece salaries cannot contain a fixed salary snapshot."
                )

        original = None
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).first()
        if original is None and self.status != self.Status.CALCULATED:
            errors["status"] = "New salary records must start Calculated."
        elif original is not None:
            if original.status == self.Status.FINALIZED:
                if original._immutable_state() != self._immutable_state():
                    errors["status"] = "Finalized salary records are immutable."
            elif self.status not in (self.Status.CALCULATED, self.Status.FINALIZED):
                errors["status"] = "Only salary finalization is allowed."

        if self.status == self.Status.CALCULATED:
            if self.finalized_at is not None or self.finalized_by_id is not None:
                errors["status"] = (
                    "Calculated salaries cannot contain finalization details."
                )
        elif self.status == self.Status.FINALIZED:
            if self.finalized_at is None or self.finalized_by_id is None:
                errors["status"] = (
                    "Finalized salaries require a finalizer and timestamp."
                )
        if errors:
            raise ValidationError(errors)

    def delete(self, *args, **kwargs):
        raise ValidationError("Salary records cannot be deleted.")

    def __str__(self):
        return (
            f"{self.employee_code_snapshot} — "
            f"{self.salary_year:04d}-{self.salary_month:02d}"
        )


class WmsSalaryLocationSnapshot(ValidatedTenantModel):
    salary = models.ForeignKey(
        WmsSalary,
        on_delete=models.PROTECT,
        related_name="location_snapshots",
    )
    location = models.ForeignKey(
        WmsLocation,
        on_delete=models.PROTECT,
        related_name="salary_location_snapshots",
    )
    location_name_snapshot = models.CharField(max_length=150, editable=False)
    location_type_snapshot = models.CharField(max_length=10, editable=False)

    class Meta:
        ordering = ["location_name_snapshot"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "salary", "location"],
                name="uniq_wms_salary_location",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "location", "salary"],
                name="wms_salary_location_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.business_id
            and self.salary_id
            and self.salary.business_id != self.business_id
        ):
            errors["salary"] = "The salary belongs to another business."
        if (
            self.business_id
            and self.location_id
            and self.location.business_id != self.business_id
        ):
            errors["location"] = "The location belongs to another business."
        if self.salary_id and self.salary.status == WmsSalary.Status.FINALIZED:
            errors["salary"] = "Finalized salary snapshots are immutable."
        if errors:
            raise ValidationError(errors)

    def delete(self, *args, **kwargs):
        if self.salary.status == WmsSalary.Status.FINALIZED:
            raise ValidationError("Finalized salary snapshots are immutable.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.salary} — {self.location_name_snapshot}"


class WmsSalaryDay(ValidatedTenantModel):
    salary = models.ForeignKey(
        WmsSalary,
        on_delete=models.PROTECT,
        related_name="days",
    )
    salary_date = models.DateField()
    location = models.ForeignKey(
        WmsLocation,
        on_delete=models.PROTECT,
        related_name="salary_days",
    )
    attendance = models.ForeignKey(
        WmsAttendance,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="salary_days",
    )
    production_entry = models.ForeignKey(
        WmsProductionEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="salary_days",
    )
    morning_status_snapshot = models.CharField(
        max_length=10,
        blank=True,
        editable=False,
    )
    evening_status_snapshot = models.CharField(
        max_length=10,
        blank=True,
        editable=False,
    )
    morning_time_in_snapshot = models.TimeField(
        null=True,
        blank=True,
        editable=False,
    )
    morning_time_out_snapshot = models.TimeField(
        null=True,
        blank=True,
        editable=False,
    )
    evening_time_in_snapshot = models.TimeField(
        null=True,
        blank=True,
        editable=False,
    )
    evening_time_out_snapshot = models.TimeField(
        null=True,
        blank=True,
        editable=False,
    )
    worked_minutes_snapshot = models.PositiveIntegerField(
        default=0,
        editable=False,
    )
    missing_minutes_snapshot = models.PositiveIntegerField(
        default=0,
        editable=False,
    )
    eligible_quantity = models.PositiveIntegerField(default=0, editable=False)
    daily_amount = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        default=Decimal("0"),
        editable=False,
    )

    class Meta:
        ordering = ["salary_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "salary", "salary_date"],
                name="uniq_wms_salary_day",
            ),
            models.CheckConstraint(
                condition=models.Q(eligible_quantity__gte=0),
                name="wms_salary_day_qty_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(daily_amount__gte=0),
                name="wms_salary_day_amt_nonneg",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "salary", "salary_date"],
                name="wms_salary_day_date_idx",
            ),
            models.Index(
                fields=["business", "location", "salary_date"],
                name="wms_salary_day_loc_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        salary = self.salary if self.salary_id else None
        if salary is not None and salary.business_id != self.business_id:
            errors["salary"] = "The salary belongs to another business."
        if (
            self.location_id
            and self.location.business_id != self.business_id
        ):
            errors["location"] = "The location belongs to another business."
        if salary is not None:
            if not salary.period_start <= self.salary_date <= salary.period_end:
                errors["salary_date"] = (
                    "Salary day must fall inside the salary period."
                )
            if salary.status == WmsSalary.Status.FINALIZED:
                errors["salary"] = "Finalized salary snapshots are immutable."
            if (
                salary.compensation_type_snapshot
                == WmsEmployee.CompensationType.FIXED_SALARY
            ):
                if self.production_entry_id is not None:
                    errors["production_entry"] = (
                        "Fixed salary days cannot contain production."
                    )
                if self.daily_amount != money(0):
                    errors["daily_amount"] = (
                        "Fixed salary is not allocated or deducted by day."
                    )
            elif (
                salary.compensation_type_snapshot
                == WmsEmployee.CompensationType.PER_PIECE
            ):
                if self.production_entry_id is None:
                    errors["production_entry"] = (
                        "Per-piece salary days require a production entry."
                    )
                if self.attendance_id is not None:
                    errors["attendance"] = (
                        "Per-piece salary days do not use attendance."
                    )

        if self.attendance_id:
            attendance = self.attendance
            if attendance.business_id != self.business_id:
                errors["attendance"] = "Attendance belongs to another business."
            elif salary is not None and (
                attendance.employee_id != salary.employee_id
                or attendance.attendance_date != self.salary_date
                or attendance.location_id != self.location_id
            ):
                errors["attendance"] = (
                    "Attendance must match the salary employee, date, and location."
                )
        if self.production_entry_id:
            entry = self.production_entry
            if entry.business_id != self.business_id:
                errors["production_entry"] = (
                    "Production belongs to another business."
                )
            elif salary is not None and (
                entry.employee_id != salary.employee_id
                or entry.production_date != self.salary_date
                or entry.location_id != self.location_id
            ):
                errors["production_entry"] = (
                    "Production must match the salary employee, date, and location."
                )
        if self.daily_amount is not None and self.daily_amount < 0:
            errors["daily_amount"] = "Daily amount cannot be negative."
        if errors:
            raise ValidationError(errors)

    def delete(self, *args, **kwargs):
        if self.salary.status == WmsSalary.Status.FINALIZED:
            raise ValidationError("Finalized salary snapshots are immutable.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.salary} — {self.salary_date:%Y-%m-%d}"


class WmsSalaryPieceLine(ValidatedTenantModel):
    class RateSource(models.TextChoices):
        ASSIGNMENT = "ASSIGNMENT", "Assignment Override"
        EMPLOYEE_DEFAULT = "EMPLOYEE_DEFAULT", "Employee Default"

    salary_day = models.ForeignKey(
        WmsSalaryDay,
        on_delete=models.PROTECT,
        related_name="piece_lines",
    )
    production_line = models.ForeignKey(
        WmsProductionEntryLine,
        on_delete=models.PROTECT,
        related_name="salary_piece_lines",
    )
    assignment_public_id_snapshot = models.UUIDField(editable=False)
    category_name_snapshot = models.CharField(max_length=100, editable=False)
    category_code_snapshot = models.CharField(
        max_length=40,
        blank=True,
        editable=False,
    )
    rate_source = models.CharField(
        max_length=20,
        choices=RateSource.choices,
        editable=False,
    )
    applied_rate = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        editable=False,
    )
    quantity = models.PositiveIntegerField(editable=False)
    line_amount = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        editable=False,
    )

    class Meta:
        ordering = [
            "salary_day__salary_date",
            "category_name_snapshot",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "salary_day", "production_line"],
                name="uniq_wms_salary_piece_line",
            ),
            models.CheckConstraint(
                condition=models.Q(applied_rate__gte=0),
                name="wms_salary_rate_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0),
                name="wms_salary_piece_qty_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(line_amount__gte=0),
                name="wms_salary_piece_amt_nonneg",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "salary_day"],
                name="wms_salary_piece_day_idx",
            ),
            models.Index(
                fields=["business", "production_line"],
                name="wms_salary_piece_src_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        day = self.salary_day if self.salary_day_id else None
        line = self.production_line if self.production_line_id else None
        if day is not None and day.business_id != self.business_id:
            errors["salary_day"] = "The salary day belongs to another business."
        if line is not None and line.business_id != self.business_id:
            errors["production_line"] = (
                "The production line belongs to another business."
            )
        if day is not None:
            if day.salary.status == WmsSalary.Status.FINALIZED:
                errors["salary_day"] = "Finalized salary snapshots are immutable."
            if (
                day.salary.compensation_type_snapshot
                != WmsEmployee.CompensationType.PER_PIECE
            ):
                errors["salary_day"] = (
                    "Piece lines are valid only for per-piece salaries."
                )
        if day is not None and line is not None:
            if day.production_entry_id != line.entry_id:
                errors["production_line"] = (
                    "The production line must belong to the salary day entry."
                )
            elif line.entry.employee_id != day.salary.employee_id:
                errors["production_line"] = (
                    "The production line belongs to another employee."
                )
            if not self.pk and self.quantity != line.quantity:
                errors["quantity"] = (
                    "Salary quantity must snapshot the production line quantity."
                )
        if self.applied_rate is not None and self.applied_rate < 0:
            errors["applied_rate"] = "Applied rate cannot be negative."
        if self.line_amount is not None and self.line_amount < 0:
            errors["line_amount"] = "Line amount cannot be negative."
        if (
            self.applied_rate is not None
            and self.quantity is not None
            and self.line_amount != money(self.applied_rate * self.quantity)
        ):
            errors["line_amount"] = "Line amount must equal quantity times rate."
        if errors:
            raise ValidationError(errors)

    def delete(self, *args, **kwargs):
        if self.salary_day.salary.status == WmsSalary.Status.FINALIZED:
            raise ValidationError("Finalized salary snapshots are immutable.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.salary_day} — {self.category_name_snapshot}: "
            f"{self.quantity}"
        )
