from django import forms
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from rest_framework.authtoken.models import Token

from apps.core.permissions import PERMISSIONS

from .models import Role, User

INPUT = {"class": "form-control"}
SELECT = {"class": "form-select"}


# Presentation order for the role editor. Permission labels and values still come
# from the central registry; new codes appear under Other Permissions.
ROLE_PERMISSION_GROUPS = (
    (_("Dashboard & Reports"), (
        "dashboard.view", "reports.view", "reports.sales", "reports.financial",
        "reports.export", "profit.view", "cost.view",
    )),
    (_("Sales & Returns"), (
        "sales.view", "sales.create", "sales.void", "sales.delete",
        "sales.refund", "sales.discount", "sales.price_override", "sales.credit",
        "credit.approve",
    )),
    (_("Tailoring / Workshop"), ("workshop.fabric_actual",)),
    (_("Products & Catalog"), (
        "products.view", "products.manage", "products.import", "products.archive",
        "products.delete", "products.export",
    )),
    (_("Inventory & Stock"), (
        "inventory.view", "inventory.export", "inventory.import", "inventory.adjust",
        "inventory.adjust_approve", "inventory.transfer", "inventory.transfer_approve",
        "inventory.count",
    )),
    (_("Purchasing & Suppliers"), (
        "purchases.view", "purchases.manage", "purchases.approve",
        "suppliers.view", "suppliers.manage",
    )),
    (_("Customers & Credit"), (
        "customers.view", "customers.manage", "customers.payments",
        "customers.export", "customers.import",
    )),
    (_("Expenses & Cash Management"), (
        "expenses.view", "expenses.manage", "expenses.approve", "registers.manage",
        "shifts.open", "shifts.close", "shifts.approve", "shifts.reopen",
    )),
    (_("Administration"), ("users.manage", "branches.manage", "settings.manage")),
    (_("System, Audit & Backup"), (
        "audit.view", "notifications.view", "backups.view", "backups.create",
        "backups.download", "backups.schedule", "backups.pin", "backups.restore",
    )),
)


class LoginForm(forms.Form):
    email = forms.EmailField(widget=forms.EmailInput(attrs={
        **INPUT, "placeholder": "you@example.com", "autofocus": True}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        **INPUT, "placeholder": "Password"}))
    remember_me = forms.BooleanField(required=False, widget=forms.CheckboxInput(
        attrs={"class": "form-check-input"}))


class StyledPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            f.widget.attrs.update(INPUT)


class StyledSetPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            f.widget.attrs.update(INPUT)

    def save(self, commit=True):
        user = super().save(commit=False)
        user.must_change_password = False
        if commit:
            user.save(update_fields=["password", "must_change_password"])
            Token.objects.filter(user=user).delete()
        return user


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["full_name", "phone"]
        widgets = {"full_name": forms.TextInput(attrs=INPUT),
                   "phone": forms.TextInput(attrs=INPUT)}


class EmployeeForm(forms.Form):
    """Create or edit an employee user inside the current business."""

    full_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs=INPUT))
    email = forms.EmailField(widget=forms.EmailInput(attrs=INPUT))
    phone = forms.CharField(max_length=30, required=False, widget=forms.TextInput(attrs=INPUT))
    password = forms.CharField(
        required=False, min_length=8,
        widget=forms.PasswordInput(attrs={**INPUT, "autocomplete": "new-password"}),
        help_text="Leave blank when editing to keep the current password.",
    )
    role = forms.ModelChoiceField(queryset=Role.objects.none(),
                                  widget=forms.Select(attrs=SELECT))
    branches = forms.ModelMultipleChoiceField(
        queryset=None, required=False,
        widget=forms.SelectMultiple(attrs={**SELECT, "size": "4"}),
        help_text="Leave empty to allow access to all branches.",
    )
    is_active = forms.BooleanField(required=False, initial=True,
                                   widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    def __init__(
        self, business, *args, editing=None, custom_roles_enabled=True, **kwargs
    ):
        super().__init__(*args, **kwargs)
        from apps.branches.models import Branch

        self.business = business
        self.editing = editing  # Membership being edited, or None
        role_qs = Role.objects.for_business(business)
        if not custom_roles_enabled:
            current_role_id = editing.role_id if editing is not None else None
            allowed_roles = Q(is_system=True) | Q(pk=current_role_id)
            if self.is_bound:
                raw_role_id = self.data.get(self.add_prefix("role"))
                try:
                    submitted_role_id = int(raw_role_id)
                except (TypeError, ValueError):
                    submitted_role_id = None
                if submitted_role_id is not None and 0 < submitted_role_id < 2**63:
                    # Let a valid tenant custom role reach the post-validation
                    # entitlement gate. Malformed and foreign IDs remain
                    # ordinary ModelChoiceField errors.
                    allowed_roles |= Q(pk=submitted_role_id)
            role_qs = role_qs.filter(allowed_roles)
        self.fields["role"].queryset = role_qs
        self.fields["branches"].queryset = Branch.objects.for_business(business).filter(
            is_active=True
        )
        if editing is None:
            self.fields["password"].required = True

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"])
        qs = User.objects.filter(email__iexact=email)
        if self.editing is not None:
            qs = qs.exclude(pk=self.editing.user_id)
        if qs.exists():
            raise forms.ValidationError(
                "An account with this email already exists."
            )
        return email

    def clean_role(self):
        role = self.cleaned_data["role"]
        if role.business_id != self.business.id:
            raise forms.ValidationError("Invalid role.")
        if role.is_owner and (self.editing is None or not self.editing.role.is_owner):
            raise forms.ValidationError(
                "The owner role cannot be assigned to employees."
            )
        return role


class RoleForm(forms.ModelForm):
    permissions = forms.MultipleChoiceField(
        choices=(),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = Role
        fields = ["name", "permissions"]
        widgets = {"name": forms.TextInput(attrs=INPUT)}

    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        choices = list(PERMISSIONS.items())
        known_codes = set(PERMISSIONS)
        if self.instance.pk:
            for code in self.instance.permissions or []:
                if code not in known_codes:
                    choices.append((code, code))
                    known_codes.add(code)
        self.fields["permissions"].choices = choices

    @property
    def permission_sections(self):
        """Group the actual bound checkboxes, retaining their IDs and checked state."""
        sections = [
            {"title": title, "checkboxes": []}
            for title, _codes in ROLE_PERMISSION_GROUPS
        ]
        other = {"title": _("Other Permissions"), "checkboxes": []}
        section_by_code = {
            code: section
            for section, (_title, codes) in zip(sections, ROLE_PERMISSION_GROUPS, strict=True)
            for code in codes
        }
        for checkbox in self["permissions"]:
            section_by_code.get(checkbox.data["value"], other)["checkboxes"].append(checkbox)
        if other["checkboxes"]:
            sections.append(other)
        return [section for section in sections if section["checkboxes"]]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Role.objects.for_business(self.business).filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A role with this name already exists.")
        return name
