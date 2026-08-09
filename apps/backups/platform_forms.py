"""Validated filters and action forms for Platform Admin backups."""

from django import forms

from .enums import (
    ActivitySeverity,
    BackupScope,
    BackupStatus,
    BackupTrigger,
    IntegrityStatus,
)

INPUT_SM = {"class": "form-control form-control-sm"}
SELECT_SM = {"class": "form-select form-select-sm"}


class PlatformBackupFilterForm(forms.Form):
    business_name = forms.CharField(required=False, max_length=200, widget=forms.TextInput(attrs=INPUT_SM))
    business_uuid = forms.UUIDField(required=False, widget=forms.TextInput(attrs=INPUT_SM))
    backup_uuid = forms.UUIDField(required=False, widget=forms.TextInput(attrs=INPUT_SM))
    scope = forms.ChoiceField(
        required=False,
        choices=(("", "All scopes"), *BackupScope.choices),
        widget=forms.Select(attrs=SELECT_SM),
    )
    trigger = forms.ChoiceField(
        required=False,
        choices=(("", "All triggers"), *BackupTrigger.choices),
        widget=forms.Select(attrs=SELECT_SM),
    )
    status = forms.ChoiceField(
        required=False,
        choices=(("", "All statuses"), *BackupStatus.choices),
        widget=forms.Select(attrs=SELECT_SM),
    )
    integrity = forms.ChoiceField(
        required=False,
        choices=(("", "All integrity states"), *IntegrityStatus.choices),
        widget=forms.Select(attrs=SELECT_SM),
    )
    restore_readiness = forms.ChoiceField(
        required=False,
        choices=(
            ("", "All restore states"),
            ("ready", "Restore ready"),
            ("not_ready", "Not restore ready"),
        ),
        widget=forms.Select(attrs=SELECT_SM),
    )
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={**INPUT_SM, "type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={**INPUT_SM, "type": "date"}))

    def clean(self):
        values = super().clean()
        start = values.get("date_from")
        end = values.get("date_to")
        if start and end and start > end:
            raise forms.ValidationError("The start date must not be after the end date.")
        return values


class PlatformActivityFilterForm(forms.Form):
    business = forms.CharField(required=False, max_length=200, widget=forms.TextInput(attrs=INPUT_SM))
    event = forms.CharField(required=False, max_length=100, widget=forms.TextInput(attrs=INPUT_SM))
    severity = forms.ChoiceField(
        required=False,
        choices=(("", "All severities"), *ActivitySeverity.choices),
        widget=forms.Select(attrs=SELECT_SM),
    )
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={**INPUT_SM, "type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={**INPUT_SM, "type": "date"}))

    def clean(self):
        values = super().clean()
        start = values.get("date_from")
        end = values.get("date_to")
        if start and end and start > end:
            raise forms.ValidationError("The start date must not be after the end date.")
        return values
