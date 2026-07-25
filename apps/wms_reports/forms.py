"""Access-scoped filter forms for WMS reports."""

from datetime import timedelta

from django import forms

from apps.core.date_ranges import business_localdate
from apps.wms_alterations.models import WmsAlteration
from apps.wms_core.models import WmsLocation
from apps.wms_core.selectors import historical_locations_for_access
from apps.wms_orders.models import WmsWorkshopOrder
from apps.wms_salary.models import WmsSalary
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsProductionCategory,
)
from apps.wms_workforce.selectors import (
    categories_for_business,
    employees_for_access,
)


class ReportFilterForm(forms.Form):
    """Shared styling and safe defaults for compact report filters."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css_class = (
                "form-select form-select-sm"
                if isinstance(field.widget, forms.Select)
                else "form-control form-control-sm"
            )
            field.widget.attrs["class"] = css_class


class _AccessScopedForm(ReportFilterForm):
    def __init__(self, business, user_access, *args, **kwargs):
        self.business = business
        self.user_access = user_access
        super().__init__(*args, **kwargs)

    def _configure_location(self, field_name="location"):
        self.fields[field_name].queryset = historical_locations_for_access(self.user_access)

    def _configure_employee(self, field_name="employee"):
        self.fields[field_name].queryset = employees_for_access(self.user_access).order_by(
            "full_name", "employee_code"
        )


class DailyProductionReportForm(_AccessScopedForm):
    report_date = forms.DateField(
        label="Date",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    location = forms.ModelChoiceField(
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted locations",
    )
    employee = forms.ModelChoiceField(
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted employees",
    )
    category = forms.ModelChoiceField(
        queryset=WmsProductionCategory.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All production categories",
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_location()
        self._configure_employee()
        self.fields["category"].queryset = categories_for_business(business).order_by(
            "display_order", "name"
        )
        if not self.is_bound:
            self.initial.setdefault("report_date", business_localdate(business))


class MonthlyProductionReportForm(_AccessScopedForm):
    report_month = forms.DateField(
        label="Month",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    employee = forms.ModelChoiceField(
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        empty_label="Select employee",
    )
    location = forms.ModelChoiceField(
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted locations",
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_employee()
        self._configure_location()
        if not self.is_bound:
            self.initial.setdefault(
                "report_month",
                business_localdate(business).replace(day=1),
            )

    def clean_report_month(self):
        return self.cleaned_data["report_month"].replace(day=1)


class AttendanceSummaryReportForm(_AccessScopedForm):
    EMPLOYEE_STATUS_CHOICES = (
        ("", "All employee statuses"),
        ("active", "Active employees"),
        ("inactive", "Inactive employees"),
    )

    date_from = forms.DateField(
        label="From",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        label="To",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    location = forms.ModelChoiceField(
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted locations",
    )
    employee_status = forms.ChoiceField(
        label="Employee status",
        required=False,
        choices=EMPLOYEE_STATUS_CHOICES,
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_location()
        if not self.is_bound:
            today = business_localdate(business)
            self.initial.setdefault("date_from", today.replace(day=1))
            self.initial.setdefault("date_to", today)

    def clean(self):
        cleaned_data = super().clean()
        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")
        if date_from and date_to:
            if date_to < date_from:
                self.add_error("date_to", "End date must be on or after start date.")
            elif date_to - date_from > timedelta(days=366):
                self.add_error(
                    "date_to",
                    "Choose a reporting range of 367 days or fewer.",
                )
        return cleaned_data


class IndividualAttendanceReportForm(_AccessScopedForm):
    report_month = forms.DateField(
        label="Month",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    employee = forms.ModelChoiceField(
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        empty_label="Select employee",
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_employee()
        if not self.is_bound:
            self.initial.setdefault(
                "report_month",
                business_localdate(business).replace(day=1),
            )

    def clean_report_month(self):
        return self.cleaned_data["report_month"].replace(day=1)


class DailyFinishedReportForm(_AccessScopedForm):
    report_date = forms.DateField(
        label="Date",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    location = forms.ModelChoiceField(
        label="Location / branch",
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted locations",
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_location()
        if not self.is_bound:
            self.initial.setdefault("report_date", business_localdate(business))


class DailyOrdersReportForm(_AccessScopedForm):
    report_date = forms.DateField(
        label="Date",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    location = forms.ModelChoiceField(
        label="Location / branch",
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted locations",
    )
    status = forms.ChoiceField(
        required=False,
        choices=(("", "All statuses"), *WmsWorkshopOrder.Status.choices),
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_location()
        if not self.is_bound:
            self.initial.setdefault("report_date", business_localdate(business))


class MonthlyOrdersReportForm(_AccessScopedForm):
    report_month = forms.DateField(
        label="Month",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    location = forms.ModelChoiceField(
        label="Location / branch",
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted locations",
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_location()
        if not self.is_bound:
            self.initial.setdefault(
                "report_month",
                business_localdate(business).replace(day=1),
            )

    def clean_report_month(self):
        return self.cleaned_data["report_month"].replace(day=1)


class DailyAlterationsReportForm(_AccessScopedForm):
    report_date = forms.DateField(
        label="Date",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    location = forms.ModelChoiceField(
        label="Location / branch",
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted locations",
    )
    reason = forms.ChoiceField(
        required=False,
        choices=(("", "All reasons"), *WmsAlteration.Reason.choices),
    )
    mistake_by_employee = forms.ModelChoiceField(
        label="Mistake by employee",
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="Any employee",
    )
    assigned_employee = forms.ModelChoiceField(
        label="Assigned to",
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted employees",
    )
    status = forms.ChoiceField(
        required=False,
        choices=(("", "All statuses"), *WmsAlteration.Status.choices),
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_location()
        self._configure_employee("mistake_by_employee")
        self._configure_employee("assigned_employee")
        if not self.is_bound:
            self.initial.setdefault("report_date", business_localdate(business))


class MonthlyAlterationsReportForm(_AccessScopedForm):
    report_month = forms.DateField(
        label="Month",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    location = forms.ModelChoiceField(
        label="Location / branch",
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted locations",
    )
    reason = forms.ChoiceField(
        required=False,
        choices=(("", "All reasons"), *WmsAlteration.Reason.choices),
    )
    employee = forms.ModelChoiceField(
        label="Assigned to",
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted employees",
    )
    status = forms.ChoiceField(
        required=False,
        choices=(("", "All statuses"), *WmsAlteration.Status.choices),
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_location()
        self._configure_employee()
        if not self.is_bound:
            self.initial.setdefault(
                "report_month",
                business_localdate(business).replace(day=1),
            )

    def clean_report_month(self):
        return self.cleaned_data["report_month"].replace(day=1)


class SalaryReportForm(_AccessScopedForm):
    report_month = forms.DateField(
        label="Month",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    employee = forms.ModelChoiceField(
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All permitted employees",
    )
    location = forms.ModelChoiceField(
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        required=False,
        empty_label="All contributing locations",
    )
    salary_type = forms.ChoiceField(
        label="Salary type",
        required=False,
        choices=(("", "All salary types"), *WmsEmployee.CompensationType.choices),
    )
    status = forms.ChoiceField(
        required=False,
        choices=(("", "All calculation statuses"), *WmsSalary.Status.choices),
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self._configure_employee()
        self._configure_location()
        if not self.is_bound:
            self.initial.setdefault(
                "report_month",
                business_localdate(business).replace(day=1),
            )

    def clean_report_month(self):
        return self.cleaned_data["report_month"].replace(day=1)
