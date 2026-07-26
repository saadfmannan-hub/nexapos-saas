"""Entitlement-aware, non-operational forms for the Phase 1 owner UI."""

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
    """Show allowed scopes without offering an operational submit action.

    Phase 1 deliberately does not post this form.  The disabled widget makes
    the safety boundary visible in rendered HTML; ``clean_scope`` still
    delegates to the entitlement service so this form cannot become an
    authorization bypass if it is reused later.
    """

    scope = forms.ChoiceField(
        label="Backup scope",
        choices=(),
        required=False,
        widget=forms.Select(
            attrs={
                **SELECT,
                "disabled": "disabled",
                "aria-disabled": "true",
            }
        ),
        help_text="Only products enabled for this business are listed.",
    )

    def __init__(self, business, *args, **kwargs):
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

    def clean_scope(self):
        value = self.cleaned_data.get("scope")
        if not value:
            raise forms.ValidationError("No backup scope is available.")
        return services.resolve_requested_scope(self.business, value).scope


# The longer name is useful to future orchestration call sites while the
# concise name remains the public UI form used in this phase.
CreateBackupRequestForm = CreateBackupForm


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
