from django import forms
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm

from apps.core.permissions import PERMISSIONS

from .models import Membership, Role, User

INPUT = {"class": "form-control"}
SELECT = {"class": "form-select"}


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

    def __init__(self, business, *args, editing=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.branches.models import Branch

        self.business = business
        self.editing = editing  # Membership being edited, or None
        self.fields["role"].queryset = Role.objects.for_business(business)
        self.fields["branches"].queryset = Branch.objects.for_business(business).filter(
            is_active=True
        )
        if editing is None:
            self.fields["password"].required = True

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        qs = User.objects.filter(email__iexact=email)
        if self.editing is not None:
            qs = qs.exclude(pk=self.editing.user_id)
        existing = qs.first()
        if existing is not None:
            # Allow attaching an existing platform user only if they are not
            # already a member of this business.
            if Membership.objects.filter(business=self.business, user=existing).exists():
                raise forms.ValidationError(
                    "A user with this email is already a member of this business."
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
        choices=[(code, label) for code, label in PERMISSIONS.items()],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Role
        fields = ["name", "permissions"]
        widgets = {"name": forms.TextInput(attrs=INPUT)}

    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Role.objects.for_business(self.business).filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A role with this name already exists.")
        return name
