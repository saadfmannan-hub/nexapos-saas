from django import forms

from apps.accounts.models import Membership
from apps.branches.models import Branch

from .models import WmsLocation, WmsRole, WmsSettings, WmsUserAccess
from .permissions import WMS_PERMISSIONS


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")


class WmsSettingsForm(StyledModelForm):
    class Meta:
        model = WmsSettings
        fields = [
            "default_workshop_location",
            "first_shift_start",
            "first_shift_end",
            "second_shift_start",
            "second_shift_end",
            "grace_period_minutes",
        ]
        widgets = {
            "first_shift_start": forms.TimeInput(attrs={"type": "time"}),
            "first_shift_end": forms.TimeInput(attrs={"type": "time"}),
            "second_shift_start": forms.TimeInput(attrs={"type": "time"}),
            "second_shift_end": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_workshop_location"].queryset = (
            WmsLocation.objects.for_business(business)
            .filter(
                is_active=True,
                location_type__in=[
                    WmsLocation.LocationType.WORKSHOP,
                    WmsLocation.LocationType.BOTH,
                ],
            )
            .select_related("branch")
            .order_by("branch__name")
        )


class WmsLocationForm(StyledModelForm):
    class Meta:
        model = WmsLocation
        fields = ["branch", "location_type", "is_active"]

    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        used = WmsLocation.objects.for_business(business)
        if self.instance.pk:
            used = used.exclude(pk=self.instance.pk)
        self.fields["branch"].queryset = (
            Branch.objects.for_business(business)
            .filter(is_active=True)
            .exclude(pk__in=used.values("branch_id"))
            .order_by("name")
        )


class WmsUserAccessForm(StyledModelForm):
    class Meta:
        model = WmsUserAccess
        fields = ["membership", "role", "is_active", "allowed_locations"]
        widgets = {
            "allowed_locations": forms.CheckboxSelectMultiple(),
        }

    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        used = WmsUserAccess.objects.for_business(business)
        if self.instance.pk:
            used = used.exclude(pk=self.instance.pk)
        self.fields["membership"].queryset = (
            Membership.objects.for_business(business)
            .filter(is_active=True)
            .exclude(pk__in=used.values("membership_id"))
            .select_related("user")
            .order_by("user__full_name", "user__email")
        )
        self.fields["role"].queryset = WmsRole.objects.for_business(business).filter(
            is_active=True
        )
        self.fields["allowed_locations"].queryset = (
            WmsLocation.objects.for_business(business)
            .filter(is_active=True)
            .select_related("branch")
            .order_by("branch__name")
        )
        self.fields["allowed_locations"].help_text = (
            "Leave empty to allow all active WMS locations."
        )


class WmsRoleForm(StyledModelForm):
    permissions = forms.MultipleChoiceField(
        choices=tuple(WMS_PERMISSIONS.items()),
        widget=forms.CheckboxSelectMultiple(),
    )

    class Meta:
        model = WmsRole
        fields = ["name", "code", "permissions", "is_admin", "is_active"]
