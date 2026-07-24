from django import forms
from django.db.models import Q

from apps.wms_core.models import WmsLocation
from apps.wms_core.selectors import (
    historical_locations_for_access,
    locations_for_access,
)

from .models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
    WmsProductionCategory,
)


class TenantStyledModelForm(forms.ModelForm):
    def __init__(self, business, *args, **kwargs):
        self.business = business
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(
                field.widget,
                (forms.Select, forms.SelectMultiple),
            ):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")


class WmsEmployeeForm(TenantStyledModelForm):
    class Meta:
        model = WmsEmployee
        fields = [
            "location",
            "employee_code",
            "full_name",
            "mobile",
            "joining_date",
            "compensation_type",
            "fixed_monthly_salary",
            "default_per_piece_rate",
            "notes",
        ]
        widgets = {
            "joining_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "employee_code": "Employee code",
            "joining_date": "Joining date",
            "fixed_monthly_salary": "Fixed monthly salary",
            "default_per_piece_rate": "Default per-piece rate",
        }
        help_texts = {
            "default_per_piece_rate": (
                "Used when an assigned category has no special rate."
            ),
        }

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        choices = locations_for_access(user_access)
        if self.instance.pk and self.instance.location_id:
            historical = historical_locations_for_access(user_access).filter(
                pk=self.instance.location_id
            )
            choices = WmsLocation.objects.for_business(business).filter(
                Q(pk__in=choices.values("pk")) | Q(pk__in=historical.values("pk"))
            ).select_related("branch").order_by("branch__name")
        self.fields["location"].queryset = choices

    def clean_employee_code(self):
        code = " ".join(self.cleaned_data["employee_code"].split()).upper()
        qs = WmsEmployee.objects.for_business(self.business).filter(
            employee_code__iexact=code
        )
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "This employee code is already in use."
            )
        return code


class WmsProductionCategoryForm(TenantStyledModelForm):
    class Meta:
        model = WmsProductionCategory
        fields = ["name", "code", "display_order", "description"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "display_order": "Display order",
        }

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        qs = WmsProductionCategory.objects.for_business(self.business).filter(
            name__iexact=name
        )
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "This production category already exists."
            )
        return name

    def clean_code(self):
        code = " ".join((self.cleaned_data.get("code") or "").split()).upper()
        if not code:
            return ""
        qs = WmsProductionCategory.objects.for_business(self.business).filter(
            code__iexact=code
        )
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "This category code is already in use."
            )
        return code


class WmsAssignmentForm(TenantStyledModelForm):
    class Meta:
        model = WmsEmployeeCategoryAssignment
        fields = ["category", "per_piece_rate"]
        labels = {
            "per_piece_rate": "Category-specific per-piece rate",
        }

    def __init__(self, business, employee, *args, **kwargs):
        self.employee = employee
        super().__init__(business, *args, **kwargs)
        active_category_ids = employee.category_assignments.filter(
            is_active=True
        ).values("category_id")
        categories = (
            WmsProductionCategory.objects.for_business(business)
            .filter(is_active=True)
            .exclude(pk__in=active_category_ids)
        )
        if self.instance.pk:
            categories = WmsProductionCategory.objects.for_business(
                business
            ).filter(
                Q(is_active=True) | Q(pk=self.instance.category_id)
            )
            self.fields["category"].disabled = True
        self.fields["category"].queryset = categories.order_by(
            "display_order",
            "name",
        )
        if employee.compensation_type != WmsEmployee.CompensationType.PER_PIECE:
            self.fields["per_piece_rate"].disabled = True
            self.fields["per_piece_rate"].help_text = (
                "Category rates apply only to Per Piece employees."
            )
        else:
            self.fields["per_piece_rate"].help_text = (
                "Optional. Leave blank to use the employee default rate."
            )

    def clean(self):
        cleaned_data = super().clean()
        if not self.employee.is_active:
            raise forms.ValidationError(
                "Inactive employees cannot receive active assignments."
            )
        if (
            not self.employee.location.is_active
            or not self.employee.location.branch.is_active
        ):
            raise forms.ValidationError(
                "The employee must have an active WMS location."
            )
        if (
            self.employee.compensation_type
            != WmsEmployee.CompensationType.PER_PIECE
        ):
            cleaned_data["per_piece_rate"] = None
        return cleaned_data


class WmsAssignmentRateForm(forms.Form):
    per_piece_rate = forms.DecimalField(
        max_digits=14,
        decimal_places=3,
        min_value=0,
        required=False,
    )

    def __init__(self, employee, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        self.fields["per_piece_rate"].widget.attrs["class"] = (
            "form-control form-control-sm"
        )

    def clean_per_piece_rate(self):
        rate = self.cleaned_data.get("per_piece_rate")
        if (
            rate is not None
            and self.employee.compensation_type
            != WmsEmployee.CompensationType.PER_PIECE
        ):
            raise forms.ValidationError(
                "Category rates apply only to Per Piece employees."
            )
        return rate
