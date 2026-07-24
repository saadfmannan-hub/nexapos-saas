"""Tenant-scoped WMS attendance records with stable shift snapshots."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.wms_core.models import ValidatedTenantModel, WmsLocation, WmsSettings
from apps.wms_workforce.models import WmsEmployee

from .calculations import ABSENT, LATE, PRESENT, calculate_attendance


class WmsAttendance(ValidatedTenantModel):
    class Status(models.TextChoices):
        PRESENT = PRESENT, "Present"
        LATE = LATE, "Late"
        ABSENT = ABSENT, "Absent"

    location = models.ForeignKey(
        WmsLocation,
        on_delete=models.PROTECT,
        related_name="attendance_records",
    )
    employee = models.ForeignKey(
        WmsEmployee,
        on_delete=models.PROTECT,
        related_name="attendance_records",
    )
    attendance_date = models.DateField()

    morning_time_in = models.TimeField(null=True, blank=True)
    morning_time_out = models.TimeField(null=True, blank=True)
    evening_time_in = models.TimeField(null=True, blank=True)
    evening_time_out = models.TimeField(null=True, blank=True)

    morning_shift_start = models.TimeField(editable=False)
    morning_shift_end = models.TimeField(editable=False)
    evening_shift_start = models.TimeField(editable=False)
    evening_shift_end = models.TimeField(editable=False)
    grace_period_minutes = models.PositiveIntegerField(editable=False)

    morning_status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ABSENT,
        editable=False,
    )
    evening_status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ABSENT,
        editable=False,
    )
    morning_worked_minutes = models.PositiveIntegerField(default=0, editable=False)
    evening_worked_minutes = models.PositiveIntegerField(default=0, editable=False)
    worked_minutes = models.PositiveIntegerField(default=0, editable=False)
    missing_minutes = models.PositiveIntegerField(default=0, editable=False)

    correction_flag = models.BooleanField(default=False, editable=False)
    correction_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_attendance_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_attendance_updated",
    )

    class Meta:
        ordering = ["-attendance_date", "employee__full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "employee", "attendance_date"],
                name="uniq_wms_attendance_employee_date",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    morning_status__in=(PRESENT, LATE, ABSENT)
                ),
                name="valid_wms_morning_status",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    evening_status__in=(PRESENT, LATE, ABSENT)
                ),
                name="valid_wms_evening_status",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    worked_minutes=(
                        models.F("morning_worked_minutes")
                        + models.F("evening_worked_minutes")
                    )
                ),
                name="valid_wms_attendance_total",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "attendance_date"],
                name="wms_att_business_date_idx",
            ),
            models.Index(
                fields=["business", "location", "attendance_date"],
                name="wms_att_location_date_idx",
            ),
            models.Index(
                fields=["business", "employee", "attendance_date"],
                name="wms_att_employee_date_idx",
            ),
        ]

    @property
    def scheduled_minutes(self):
        return self.worked_minutes + self.missing_minutes

    def _populate_shift_snapshot(self):
        snapshot = (
            self.morning_shift_start,
            self.morning_shift_end,
            self.evening_shift_start,
            self.evening_shift_end,
            self.grace_period_minutes,
        )
        if all(value is not None for value in snapshot):
            return
        if any(value is not None for value in snapshot):
            raise ValidationError("Attendance shift snapshot is incomplete.")
        if not self.business_id:
            raise ValidationError("Attendance requires a business.")
        try:
            settings_obj = WmsSettings.objects.for_business(self.business).get()
        except WmsSettings.DoesNotExist as exc:
            raise ValidationError(
                "Configure WMS shift settings before recording attendance."
            ) from exc
        self.morning_shift_start = settings_obj.first_shift_start
        self.morning_shift_end = settings_obj.first_shift_end
        self.evening_shift_start = settings_obj.second_shift_start
        self.evening_shift_end = settings_obj.second_shift_end
        self.grace_period_minutes = settings_obj.grace_period_minutes

    def recalculate(self):
        calculation = calculate_attendance(
            morning_time_in=self.morning_time_in,
            morning_time_out=self.morning_time_out,
            evening_time_in=self.evening_time_in,
            evening_time_out=self.evening_time_out,
            morning_shift_start=self.morning_shift_start,
            morning_shift_end=self.morning_shift_end,
            evening_shift_start=self.evening_shift_start,
            evening_shift_end=self.evening_shift_end,
            grace_period_minutes=self.grace_period_minutes,
        )
        self.morning_status = calculation.morning_status
        self.evening_status = calculation.evening_status
        self.morning_worked_minutes = calculation.morning_worked_minutes
        self.evening_worked_minutes = calculation.evening_worked_minutes
        self.worked_minutes = calculation.worked_minutes
        self.missing_minutes = calculation.missing_minutes

    def clean(self):
        super().clean()
        errors = {}
        if self.business_id and self.location_id:
            if self.location.business_id != self.business_id:
                errors["location"] = (
                    "The WMS location must belong to the same business."
                )
        if self.business_id and self.employee_id:
            if self.employee.business_id != self.business_id:
                errors["employee"] = "The employee must belong to the same business."

        original = None
        if self.pk:
            original = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("employee_id", "location_id")
                .first()
            )
        identity_changed = (
            original is None
            or original["employee_id"] != self.employee_id
            or original["location_id"] != self.location_id
        )
        if identity_changed and self.employee_id and self.location_id:
            if self.employee.location_id != self.location_id:
                errors["employee"] = (
                    "Attendance must use the employee's assigned WMS location."
                )
            elif not self.employee.is_active:
                errors["employee"] = (
                    "Inactive employees cannot receive new attendance."
                )
            elif not self.location.is_active or not self.location.branch.is_active:
                errors["location"] = (
                    "Select an employee at an active WMS location."
                )

        pairs = (
            ("morning_time_in", "morning_time_out", "Morning"),
            ("evening_time_in", "evening_time_out", "Evening"),
        )
        for in_field, out_field, label in pairs:
            time_in = getattr(self, in_field)
            time_out = getattr(self, out_field)
            if time_in is not None and time_out is not None and time_out <= time_in:
                errors[out_field] = f"{label} Time Out must be after Time In."
        if (
            self.morning_time_in is not None
            and self.morning_shift_end is not None
            and self.morning_time_in >= self.morning_shift_end
        ):
            errors["morning_time_in"] = (
                "Morning Time In must be before the morning shift ends."
            )
        if (
            self.evening_time_in is not None
            and self.evening_shift_end is not None
            and self.evening_time_in >= self.evening_shift_end
        ):
            errors["evening_time_in"] = (
                "Evening Time In must be before the evening shift ends."
            )
        self.correction_reason = (self.correction_reason or "").strip()
        if self.correction_flag and not self.correction_reason:
            errors["correction_reason"] = (
                "A correction reason is required for corrected attendance."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._populate_shift_snapshot()
        self.recalculate()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.employee.employee_code} — {self.attendance_date:%Y-%m-%d}"
