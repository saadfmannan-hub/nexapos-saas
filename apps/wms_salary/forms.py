"""Access-scoped forms for WMS salary calculation."""

from django import forms

from apps.core.date_ranges import business_localdate
from apps.wms_workforce.models import WmsEmployee

from . import selectors


class SalaryCalculationForm(forms.Form):
    employee = forms.ModelChoiceField(
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        label="Employee",
    )
    salary_month = forms.DateField(
        input_formats=["%Y-%m"],
        widget=forms.DateInput(
            format="%Y-%m",
            attrs={"type": "month"},
        ),
        label="Salary month",
    )

    def __init__(self, business, user_access, *args, **kwargs):
        self.business = business
        self.user_access = user_access
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = (
            selectors.salary_employees_for_access(user_access)
        )
        self.fields["employee"].widget.attrs["class"] = "form-select"
        self.fields["salary_month"].widget.attrs["class"] = "form-control"
        if not self.is_bound and not self.initial.get("salary_month"):
            today = business_localdate(business)
            self.initial["salary_month"] = today.replace(day=1)

    def clean_salary_month(self):
        value = self.cleaned_data["salary_month"]
        return value.replace(day=1)

    def clean(self):
        cleaned_data = super().clean()
        employee = cleaned_data.get("employee")
        salary_month = cleaned_data.get("salary_month")
        if (
            employee is not None
            and salary_month is not None
            and (
                salary_month.year,
                salary_month.month,
            )
            < (
                employee.joining_date.year,
                employee.joining_date.month,
            )
        ):
            self.add_error(
                "salary_month",
                "Salary cannot be calculated for a month before joining.",
            )
        return cleaned_data
