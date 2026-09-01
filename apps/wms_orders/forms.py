from django import forms

from apps.core.date_ranges import business_localdate
from apps.wms_core.models import WmsLocation

from .models import normalize_order_reference


def normalize_reference_batch(value):
    raw_values = value.splitlines() if isinstance(value, str) else value
    references = [
        normalize_order_reference(item)
        for item in raw_values
        if normalize_order_reference(item)
    ]
    duplicates = sorted(
        {
            reference
            for reference in references
            if references.count(reference) > 1
        }
    )
    if duplicates:
        raise forms.ValidationError(
            "Duplicate references in this batch: "
            f"{', '.join(duplicates)}."
        )
    if not references:
        raise forms.ValidationError(
            "Enter at least one order reference, one per line."
        )
    return references


def normalize_order_batch(value):
    rows = []
    seen = set()
    for line_number, raw_line in enumerate((value or "").splitlines(), start=1):
        if not raw_line.strip():
            continue
        parts = [part.strip() for part in raw_line.split("|")]
        if len(parts) != 2:
            raise forms.ValidationError(
                f"Line {line_number}: use Order Reference | PCS."
            )
        reference = normalize_order_reference(parts[0])
        if not reference:
            raise forms.ValidationError(
                f"Line {line_number}: order reference is required."
            )
        try:
            piece_count = int(parts[1])
        except (TypeError, ValueError) as exc:
            raise forms.ValidationError(
                f"Line {line_number}: PCS must be a whole number."
            ) from exc
        if piece_count <= 0:
            raise forms.ValidationError(
                f"Line {line_number}: PCS must be greater than zero."
            )
        if reference in seen:
            raise forms.ValidationError(
                f"Duplicate reference in this batch: {reference}."
            )
        seen.add(reference)
        rows.append(
            {
                "order_reference": reference,
                "eligible_piece_count": piece_count,
            }
        )
    if not rows:
        raise forms.ValidationError(
            "Enter at least one order as Order Reference | PCS."
        )
    return rows


class NewOrdersBatchForm(forms.Form):
    location = forms.ModelChoiceField(
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
        label="WMS location",
    )
    received_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    orders = forms.CharField(
        label="Orders",
        widget=forms.Textarea(
            attrs={
                "rows": 8,
                "placeholder": "MB-008 | 4\nMB-009 | 2\nAH-006 | 6",
            }
        ),
        help_text="Enter one order per line as Order Reference | PCS.",
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Optional operational notes applied to this batch.",
    )

    def __init__(self, business, user_access, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.user_access = user_access
        from .selectors import active_order_locations_for_access

        self.fields["location"].queryset = (
            active_order_locations_for_access(user_access)
        )
        if not self.is_bound:
            self.initial["received_date"] = business_localdate(business)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    def clean_orders(self):
        return normalize_order_batch(self.cleaned_data["orders"])

    def clean_notes(self):
        return (self.cleaned_data.get("notes") or "").strip()


class FinishOrdersBatchForm(forms.Form):
    finished_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    references = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 8,
                "placeholder": "MB-008\nMB-010\nAH-007",
            }
        ),
        help_text=(
            "Enter one existing In Process reference per line. "
            "The whole batch is validated before any order changes."
        ),
    )

    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["finished_date"] = business_localdate(business)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def clean_references(self):
        return normalize_reference_batch(self.cleaned_data["references"])


class OrderPieceCountForm(forms.Form):
    eligible_piece_count = forms.IntegerField(
        label="Eligible PCS",
        min_value=1,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "min": "1", "step": "1"}
        ),
        help_text=(
            "The maximum normal-production PCS for each operation on this order."
        ),
    )
