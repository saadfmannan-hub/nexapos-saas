from django import forms

from apps.core.date_ranges import business_localdate
from apps.wms_core.models import WmsSettings

from . import selectors
from .models import WmsAttendance


class AttendanceStyledModelForm(forms.ModelForm):
    def __init__(self, business, *args, **kwargs):
        self.business = business
        super().__init__(*args, **kwargs)
        self.instance.business = business
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")


class AttendanceEntryForm(AttendanceStyledModelForm):
    class Meta:
        model = WmsAttendance
        fields = [
            "employee",
            "attendance_date",
            "morning_time_in",
            "morning_time_out",
            "evening_time_in",
            "evening_time_out",
        ]
        widgets = {
            "attendance_date": forms.DateInput(attrs={"type": "date"}),
            "morning_time_in": forms.TimeInput(attrs={"type": "time"}),
            "morning_time_out": forms.TimeInput(attrs={"type": "time"}),
            "evening_time_in": forms.TimeInput(attrs={"type": "time"}),
            "evening_time_out": forms.TimeInput(attrs={"type": "time"}),
        }
        labels = {
            "attendance_date": "Attendance date",
            "morning_time_in": "Morning Time In",
            "morning_time_out": "Morning Time Out",
            "evening_time_in": "Evening Time In",
            "evening_time_out": "Evening Time Out",
        }

    def __init__(self, business, user_access, *args, **kwargs):
        self.user_access = user_access
        super().__init__(business, *args, **kwargs)
        self.fields["employee"].queryset = (
            selectors.active_employees_for_attendance(user_access)
        )
        self.fields["employee"].empty_label = "Select employee"
        if not self.is_bound:
            self.initial.setdefault("attendance_date", business_localdate(business))
        self.settings_obj = WmsSettings.objects.for_business(business).first()

    def clean(self):
        cleaned_data = super().clean()
        employee = cleaned_data.get("employee")
        attendance_date = cleaned_data.get("attendance_date")
        if employee is not None:
            if (
                not employee.is_active
                or not employee.location.is_active
                or not employee.location.branch.is_active
            ):
                self.add_error(
                    "employee",
                    "Select an active employee at an active WMS location.",
                )
        if employee is not None and attendance_date is not None:
            duplicate = WmsAttendance.objects.for_business(self.business).filter(
                employee=employee,
                attendance_date=attendance_date,
            )
            if duplicate.exists():
                self.add_error(
                    "attendance_date",
                    "Attendance already exists for this employee on this date.",
                )
        _validate_shift_times(self, cleaned_data, self.settings_obj)
        return cleaned_data


class AttendanceCorrectionForm(AttendanceStyledModelForm):
    class Meta:
        model = WmsAttendance
        fields = [
            "morning_time_in",
            "morning_time_out",
            "evening_time_in",
            "evening_time_out",
            "correction_reason",
        ]
        widgets = {
            "morning_time_in": forms.TimeInput(attrs={"type": "time"}),
            "morning_time_out": forms.TimeInput(attrs={"type": "time"}),
            "evening_time_in": forms.TimeInput(attrs={"type": "time"}),
            "evening_time_out": forms.TimeInput(attrs={"type": "time"}),
            "correction_reason": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "morning_time_in": "Morning Time In",
            "morning_time_out": "Morning Time Out",
            "evening_time_in": "Evening Time In",
            "evening_time_out": "Evening Time Out",
            "correction_reason": "Correction reason",
        }

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        self.fields["correction_reason"].required = True
        self.fields["correction_reason"].help_text = (
            "Required. Explain why the saved attendance is being changed."
        )

    def clean_correction_reason(self):
        reason = (self.cleaned_data.get("correction_reason") or "").strip()
        if not reason:
            raise forms.ValidationError("Enter a correction reason.")
        return reason

    def clean(self):
        cleaned_data = super().clean()
        settings_snapshot = type(
            "AttendanceShiftSnapshot",
            (),
            {
                "first_shift_end": self.instance.morning_shift_end,
                "second_shift_end": self.instance.evening_shift_end,
            },
        )()
        _validate_shift_times(self, cleaned_data, settings_snapshot)
        return cleaned_data


def _validate_shift_times(form, cleaned_data, settings_obj):
    pairs = (
        ("morning_time_in", "morning_time_out", "Morning"),
        ("evening_time_in", "evening_time_out", "Evening"),
    )
    for in_field, out_field, label in pairs:
        time_in = cleaned_data.get(in_field)
        time_out = cleaned_data.get(out_field)
        if time_in is not None and time_out is not None and time_out <= time_in:
            form.add_error(out_field, f"{label} Time Out must be after Time In.")
    if settings_obj is None:
        return
    morning_time_in = cleaned_data.get("morning_time_in")
    evening_time_in = cleaned_data.get("evening_time_in")
    if (
        morning_time_in is not None
        and morning_time_in >= settings_obj.first_shift_end
    ):
        form.add_error(
            "morning_time_in",
            "Morning Time In must be before the morning shift ends.",
        )
    if (
        evening_time_in is not None
        and evening_time_in >= settings_obj.second_shift_end
    ):
        form.add_error(
            "evening_time_in",
            "Evening Time In must be before the evening shift ends.",
        )
