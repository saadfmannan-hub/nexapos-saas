from django import forms

from apps.core.date_ranges import business_localdate
from apps.wms_core.models import WmsLocation
from apps.wms_workforce.models import WmsEmployee

from . import selectors
from .models import WmsAlteration, normalize_alteration_reference


class AlterationStyledModelForm(forms.ModelForm):
    location = forms.ModelChoiceField(
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        label="WMS location",
    )
    assigned_employee = forms.ModelChoiceField(
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        label="Assigned To",
    )
    mistake_by_employee = forms.ModelChoiceField(
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
        required=False,
        label="Mistake By Employee",
    )

    def __init__(self, business, user_access, *args, **kwargs):
        self.business = business
        self.user_access = user_access
        super().__init__(*args, **kwargs)
        self.instance.business = business
        self.fields["location"].queryset = (
            selectors.active_alteration_locations_for_access(user_access)
        )
        employees = selectors.active_alteration_employees_for_access(
            user_access
        )
        self.fields["assigned_employee"].queryset = employees
        self.fields["mistake_by_employee"].queryset = employees
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    def clean(self):
        cleaned_data = super().clean()
        location = cleaned_data.get("location")
        assigned_employee = cleaned_data.get("assigned_employee")
        mistake_by = cleaned_data.get("mistake_by")
        mistake_by_employee = cleaned_data.get("mistake_by_employee")

        if location is not None and assigned_employee is not None:
            if assigned_employee.location_id != location.pk:
                self.add_error(
                    "assigned_employee",
                    "Assigned To must belong to the selected WMS location.",
                )
        if mistake_by == WmsAlteration.MistakeBy.EMPLOYEE:
            if mistake_by_employee is None:
                self.add_error(
                    "mistake_by_employee",
                    "Select the employee responsible for the mistake.",
                )
            elif (
                location is not None
                and mistake_by_employee.location_id != location.pk
            ):
                self.add_error(
                    "mistake_by_employee",
                    "Mistake By Employee must belong to the selected location.",
                )
        else:
            cleaned_data["mistake_by_employee"] = None
        return cleaned_data


class AlterationCreateForm(AlterationStyledModelForm):
    class Meta:
        model = WmsAlteration
        fields = [
            "location",
            "original_order_reference",
            "alteration_reference",
            "reason",
            "mistake_by",
            "mistake_by_employee",
            "assigned_employee",
            "alteration_date",
            "notes",
        ]
        widgets = {
            "alteration_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "original_order_reference": "Original Order Reference",
            "alteration_reference": "Alteration Reference",
            "mistake_by": "Mistake By",
            "alteration_date": "Alteration Date",
        }
        help_texts = {
            "original_order_reference": (
                "Duplicates are allowed when the same order returns again."
            ),
            "alteration_reference": (
                "Optional. Operational references cannot change after saving."
            ),
        }

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        if not self.is_bound:
            self.initial["alteration_date"] = business_localdate(business)

    def clean_original_order_reference(self):
        reference = normalize_alteration_reference(
            self.cleaned_data["original_order_reference"]
        )
        if not reference:
            raise forms.ValidationError(
                "Enter the original order reference."
            )
        return reference

    def clean_alteration_reference(self):
        return normalize_alteration_reference(
            self.cleaned_data.get("alteration_reference")
        )


class AlterationCorrectionForm(AlterationStyledModelForm):
    class Meta:
        model = WmsAlteration
        fields = [
            "location",
            "reason",
            "mistake_by",
            "mistake_by_employee",
            "assigned_employee",
            "alteration_date",
            "status",
            "notes",
            "correction_reason",
        ]
        widgets = {
            "alteration_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "correction_reason": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "mistake_by": "Mistake By",
            "assigned_employee": "Assigned To",
            "alteration_date": "Alteration Date",
            "correction_reason": "Correction Reason",
        }

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(business, user_access, *args, **kwargs)
        self.fields["correction_reason"].required = True
        self.fields["correction_reason"].help_text = (
            "Required. Explain why this saved alteration is changing."
        )
        if self.instance.status == WmsAlteration.Status.OPEN:
            self.fields["status"].choices = (
                (WmsAlteration.Status.OPEN, "Open"),
                (WmsAlteration.Status.IN_PROGRESS, "In Progress"),
            )
        elif self.instance.status == WmsAlteration.Status.IN_PROGRESS:
            self.fields["status"].choices = (
                (WmsAlteration.Status.IN_PROGRESS, "In Progress"),
            )
        else:
            self.fields["status"].choices = (
                (WmsAlteration.Status.COMPLETED, "Completed"),
            )

    def clean_correction_reason(self):
        reason = (self.cleaned_data.get("correction_reason") or "").strip()
        if not reason:
            raise forms.ValidationError("Enter a correction reason.")
        return reason
