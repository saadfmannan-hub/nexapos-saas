"""Strict, entitlement-aware forms for the tenant-owner backup UI."""

from django import forms

from . import services
from .enums import BackupScope, BackupStatus, BackupTrigger

SELECT = {"class": "form-select"}
SELECT_SM = {"class": "form-select form-select-sm"}


def _scope_value(scope) -> str:
    return scope.value if isinstance(scope, BackupScope) else str(scope)


def _scope_label(scope) -> str:
    value = _scope_value(scope)
    try:
        return str(BackupScope(value).label)
    except ValueError:
        # This should be unreachable when the entitlement service is working
        # correctly. Keep the display fail-closed instead of echoing an
        # unregistered value into the owner UI.
        return "Unavailable scope"


class CreateBackupForm(forms.Form):
    """Offer only currently entitled scopes and revalidate on POST."""

    scope = forms.ChoiceField(
        label="Backup scope",
        choices=(),
        required=True,
        widget=forms.Select(
            attrs={
                **SELECT,
            }
        ),
        help_text="Only products enabled for this business are listed.",
    )

    def __init__(self, business, *args, enabled=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        allowed_scopes = tuple(services.available_backup_scopes(business))
        choices = [(_scope_value(scope), _scope_label(scope)) for scope in allowed_scopes]
        if not choices:
            choices = [("", "No entitled backup scopes")]
        self.fields["scope"].choices = choices

        preferred = BackupScope.ALL_ENABLED
        preferred_value = _scope_value(preferred)
        allowed_values = {value for value, _label in choices}
        self.initial["scope"] = (
            preferred_value if preferred_value in allowed_values else next(iter(allowed_values), "")
        )
        if not enabled:
            self.fields["scope"].widget.attrs.update(
                {"disabled": "disabled", "aria-disabled": "true"}
            )

    def clean_scope(self):
        value = self.cleaned_data.get("scope")
        if not value:
            raise forms.ValidationError("No backup scope is available.")
        return services.resolve_requested_scope(self.business, value).scope


# The longer name is useful to future orchestration call sites while the
# concise name remains the public UI form used in this phase.
CreateBackupRequestForm = CreateBackupForm


class RestorePreflightForm(forms.Form):
    reason = forms.CharField(
        label="Reason for restore",
        max_length=500,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Briefly explain why this restore is needed",
            }
        ),
    )


class RestoreConfirmationForm(forms.Form):
    acknowledge_replacement = forms.BooleanField(
        required=True,
        label="I understand that current business data will be replaced.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    confirmation = forms.CharField(
        label='Type "RESTORE" to confirm',
        max_length=20,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
                "spellcheck": "false",
            }
        ),
    )

    def clean_confirmation(self):
        value = self.cleaned_data.get("confirmation", "")
        if value != "RESTORE":
            raise forms.ValidationError('Enter "RESTORE" exactly to continue.')
        return value


class BackupHistoryFilterForm(forms.Form):
    """GET filters whose scope choices are constrained by current entitlement."""

    scope = forms.ChoiceField(
        label="Scope",
        required=False,
        choices=(),
        widget=forms.Select(attrs=SELECT_SM),
    )
    status = forms.ChoiceField(
        label="Status",
        required=False,
        choices=(("", "All statuses"), *BackupStatus.choices),
        widget=forms.Select(attrs=SELECT_SM),
    )
    trigger = forms.ChoiceField(
        label="Trigger",
        required=False,
        choices=(("", "All triggers"), *BackupTrigger.choices),
        widget=forms.Select(attrs=SELECT_SM),
    )

    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        allowed_scopes = services.available_backup_scopes(business)
        self.fields["scope"].choices = (
            ("", "All available scopes"),
            *((_scope_value(scope), _scope_label(scope)) for scope in allowed_scopes),
        )

    def clean_scope(self):
        value = self.cleaned_data.get("scope")
        if not value:
            return ""
        resolution = services.resolve_requested_scope(self.business, value)
        return _scope_value(resolution.scope)
