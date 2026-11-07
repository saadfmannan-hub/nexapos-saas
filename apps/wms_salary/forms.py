"""Access-scoped forms for WMS salary calculation."""

from django import forms
from django.utils.formats import date_format

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


class SalaryRevisionStartForm(forms.Form):
    reason = forms.CharField(
        label="Correction reason",
        max_length=1000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )


class SalaryRevisionCalculationForm(forms.Form):
    fingerprint = forms.CharField(widget=forms.HiddenInput, max_length=64)

    def __init__(self, *args, lines, **kwargs):
        super().__init__(*args, **kwargs)
        self.line_ids = {}
        for line in lines:
            field_name = f"rate_{line.public_id}"
            self.line_ids[field_name] = line.public_id
            day = date_format(line.entry.production_date, "DATE_FORMAT")
            order = line.order.order_reference if line.order_id else "No order"
            self.fields[field_name] = forms.DecimalField(
                min_value=0, max_digits=14, decimal_places=3,
                label=f"{day} · {order} · {line.category_name_snapshot} · {line.quantity} pieces",
                widget=forms.NumberInput(attrs={"class": "form-control"}),
            )

    def confirmed_rates(self):
        return {pk: self.cleaned_data[field] for field, pk in self.line_ids.items()}
