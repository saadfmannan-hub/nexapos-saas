from django import forms

from apps.accounts.models import Membership, User
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


# Controlled IANA timezones for the markets Nexa WMS currently serves.
BUSINESS_TIMEZONE_CHOICES = (
    ("Asia/Muscat", "Oman — Asia/Muscat (GST +04:00)"),
    ("Asia/Dubai", "United Arab Emirates — Asia/Dubai (GST +04:00)"),
    ("Asia/Riyadh", "Saudi Arabia — Asia/Riyadh (AST +03:00)"),
    ("Asia/Qatar", "Qatar — Asia/Qatar (AST +03:00)"),
    ("Asia/Bahrain", "Bahrain — Asia/Bahrain (AST +03:00)"),
    ("Asia/Kuwait", "Kuwait — Asia/Kuwait (AST +03:00)"),
    ("UTC", "Coordinated Universal Time (UTC)"),
)


class WmsSettingsForm(StyledModelForm):
    business_timezone = forms.ChoiceField(
        label="Business time zone",
        choices=BUSINESS_TIMEZONE_CHOICES,
        help_text=(
            "Used for attendance days, reports, dashboard dates, and printed "
            "timestamps across this business."
        ),
    )

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
        current_timezone = (business.timezone or "").strip()
        if current_timezone and current_timezone not in dict(
            BUSINESS_TIMEZONE_CHOICES
        ):
            # Preserve an already-configured timezone outside the GCC list.
            self.fields["business_timezone"].choices = (
                (current_timezone, current_timezone),
                *BUSINESS_TIMEZONE_CHOICES,
            )
        self.initial.setdefault("business_timezone", current_timezone or "UTC")


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

    def __init__(self, business, *args, acting_access=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.acting_access = acting_access
        self.fields["permissions"].widget.attrs["class"] = "form-check-input"
        self.fields["code"].help_text = (
            "Short unique identifier, e.g. floor-supervisor."
        )

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        existing = WmsRole.objects.for_business(self.business).filter(
            name__iexact=name
        )
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("A WMS role with this name already exists.")
        return name

    def clean_code(self):
        code = self.cleaned_data["code"].strip().lower()
        existing = WmsRole.objects.for_business(self.business).filter(code=code)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("A WMS role with this code already exists.")
        return code

    def clean(self):
        cleaned_data = super().clean()
        # Lockout protection: an admin cannot strip user management from, or
        # deactivate, the role their own access currently depends on.
        if (
            self.acting_access is not None
            and self.instance.pk
            and self.acting_access.role_id == self.instance.pk
        ):
            if not cleaned_data.get("is_active", True):
                self.add_error(
                    "is_active",
                    "You cannot deactivate the role assigned to your own access.",
                )
            if "wms.users.manage" not in cleaned_data.get("permissions", []):
                self.add_error(
                    "permissions",
                    "Your own role must keep the 'Manage WMS user access' "
                    "permission to avoid locking yourself out.",
                )
        return cleaned_data


class WmsUserForm(forms.Form):
    """Owner-facing create/edit form for WMS staff accounts.

    Reuses the platform account model and the existing WMS role and
    location-scope architecture; it never touches POS permissions.
    """

    full_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    email = forms.EmailField(
        label="Login email",
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )
    password = forms.CharField(
        required=False,
        min_length=8,
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "autocomplete": "new-password"}
        ),
    )
    password_confirm = forms.CharField(
        label="Confirm password",
        required=False,
        min_length=8,
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "autocomplete": "new-password"}
        ),
    )
    role = forms.ModelChoiceField(
        label="WMS role",
        queryset=WmsRole.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    allowed_locations = forms.ModelMultipleChoiceField(
        queryset=WmsLocation.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        help_text="Leave empty to allow all active WMS locations.",
    )
    is_active = forms.BooleanField(
        label="WMS access active",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, business, *args, access_record=None, acting_access=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.access_record = access_record  # WmsUserAccess being edited, or None
        self.acting_access = acting_access
        self.target_is_owner = bool(
            access_record is not None
            and access_record.membership.user_id == business.owner_id
        )
        self.editing_self = bool(
            access_record is not None
            and acting_access is not None
            and access_record.pk == acting_access.pk
        )
        self.fields["role"].queryset = (
            WmsRole.objects.for_business(business)
            .filter(is_active=True)
            .order_by("name")
        )
        self.fields["allowed_locations"].queryset = (
            WmsLocation.objects.for_business(business)
            .filter(is_active=True)
            .select_related("branch")
            .order_by("branch__name")
        )
        if access_record is None:
            self.fields["password"].required = True
            self.fields["password_confirm"].required = True
            self.fields["password"].help_text = (
                "At least 8 characters. Used only when this email is not "
                "already registered on the platform."
            )
        else:
            self.fields["email"].disabled = True
            self.fields["password"].label = "New password"
            self.fields["password_confirm"].label = "Confirm new password"
            self.fields["password"].help_text = (
                "Leave blank to keep the current password."
            )
        if self.target_is_owner:
            # The owner account cannot be demoted, deactivated, or have its
            # password changed from the WMS staff screen.
            for field_name in ("full_name", "password", "password_confirm", "role", "is_active"):
                self.fields[field_name].disabled = True

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"])
        if self.access_record is not None:
            return email
        existing = User.objects.filter(email__iexact=email).first()
        if existing is not None and Membership.objects.filter(
            business=self.business, user=existing
        ).exists():
            raise forms.ValidationError(
                "This email already belongs to a member of this business. "
                "Use 'Grant WMS access to existing user' instead."
            )
        return email

    def clean_role(self):
        role = self.cleaned_data["role"]
        if role.business_id != self.business.id:
            raise forms.ValidationError("Invalid WMS role.")
        return role

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password") or ""
        password_confirm = cleaned_data.get("password_confirm") or ""
        if password != password_confirm:
            self.add_error("password_confirm", "The passwords do not match.")
        if self.editing_self:
            if not cleaned_data.get("is_active", True):
                self.add_error(
                    "is_active",
                    "You cannot deactivate your own WMS access.",
                )
            role = cleaned_data.get("role")
            if role is not None and not role.has_perm("wms.users.manage"):
                self.add_error(
                    "role",
                    "Your own role must keep the 'Manage WMS user access' "
                    "permission to avoid locking yourself out.",
                )
        return cleaned_data
