from django import forms

from .models import MAX_FABRIC_TOTAL


class ActualFabricForm(forms.Form):
    actual_fabric_used = forms.DecimalField(
        required=False,
        min_value=0,
        max_value=MAX_FABRIC_TOTAL,
        max_digits=14,
        decimal_places=3,
        label="Actual Fabric Used",
        widget=forms.NumberInput(attrs={
            "class": "form-control form-control-sm",
            "min": "0",
            "max": str(MAX_FABRIC_TOTAL),
            "step": "0.001",
            "placeholder": "Meters",
        }),
    )
