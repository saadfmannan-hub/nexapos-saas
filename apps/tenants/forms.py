import zoneinfo

from django import forms
from django.contrib.auth.password_validation import validate_password

from apps.accounts.models import User

from .models import Business, BusinessSettings

INPUT = {"class": "form-control"}
SELECT = {"class": "form-select"}

COMMON_CURRENCIES = [
    ("USD", "USD — US Dollar (2 dp)"), ("EUR", "EUR — Euro (2 dp)"),
    ("GBP", "GBP — British Pound (2 dp)"), ("AED", "AED — UAE Dirham (2 dp)"),
    ("SAR", "SAR — Saudi Riyal (2 dp)"), ("OMR", "OMR — Omani Rial (3 dp)"),
    ("KWD", "KWD — Kuwaiti Dinar (3 dp)"), ("BHD", "BHD — Bahraini Dinar (3 dp)"),
    ("QAR", "QAR — Qatari Riyal (2 dp)"), ("INR", "INR — Indian Rupee (2 dp)"),
    ("PKR", "PKR — Pakistani Rupee (2 dp)"), ("EGP", "EGP — Egyptian Pound (2 dp)"),
    ("KES", "KES — Kenyan Shilling (2 dp)"), ("NGN", "NGN — Nigerian Naira (2 dp)"),
    ("OTHER", "Other (enter code below)"),
]

THREE_DP = {"OMR", "KWD", "BHD"}

BUSINESS_CATEGORIES = [
    "Clothing", "Perfumes", "Mobile & Accessories", "Electronics", "Grocery",
    "General Trading", "Gifts", "Hardware", "Tailoring", "Services",
    "Wholesale", "Other",
]


class RegistrationForm(forms.Form):
    business_name = forms.CharField(max_length=150, widget=forms.TextInput(
        attrs={**INPUT, "placeholder": "e.g. Sunrise Trading"}))
    owner_name = forms.CharField(max_length=150, label="Owner full name",
                                 widget=forms.TextInput(attrs=INPUT))
    email = forms.EmailField(widget=forms.EmailInput(attrs=INPUT))
    phone = forms.CharField(max_length=30, label="Mobile number",
                            widget=forms.TextInput(attrs=INPUT))
    country = forms.CharField(max_length=100, required=False,
                              widget=forms.TextInput(attrs=INPUT))
    timezone_name = forms.ChoiceField(
        label="Time zone", initial="UTC",
        choices=[(z, z) for z in sorted(zoneinfo.available_timezones())],
        widget=forms.Select(attrs=SELECT),
    )
    currency = forms.ChoiceField(choices=COMMON_CURRENCIES, initial="USD",
                                 widget=forms.Select(attrs=SELECT))
    currency_other = forms.CharField(max_length=10, required=False,
                                     label="Currency code (if Other)",
                                     widget=forms.TextInput(attrs=INPUT))
    business_category = forms.ChoiceField(
        choices=[(c, c) for c in BUSINESS_CATEGORIES],
        widget=forms.Select(attrs=SELECT))
    expected_branches = forms.IntegerField(min_value=1, initial=1,
                                           widget=forms.NumberInput(attrs=INPUT))
    password = forms.CharField(widget=forms.PasswordInput(
        attrs={**INPUT, "autocomplete": "new-password"}))
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs=INPUT))
    accept_terms = forms.BooleanField(
        label="I accept the terms of service and privacy policy",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "An account with this email already exists. Sign in instead."
            )
        return email

    def clean(self):
        data = super().clean()
        if data.get("password") and data.get("confirm_password"):
            if data["password"] != data["confirm_password"]:
                self.add_error("confirm_password", "Passwords do not match.")
            else:
                validate_password(data["password"])
        if data.get("currency") == "OTHER" and not data.get("currency_other"):
            self.add_error("currency_other", "Enter your currency code.")
        return data

    @property
    def currency_code(self):
        c = self.cleaned_data["currency"]
        return (self.cleaned_data.get("currency_other") or "USD").upper() if c == "OTHER" else c

    @property
    def currency_precision(self):
        return 3 if self.currency_code in THREE_DP else 2


class BusinessProfileForm(forms.ModelForm):
    class Meta:
        model = Business
        fields = [
            "name", "legal_name", "logo", "email", "phone", "whatsapp", "website",
            "address", "city", "state", "postal_code", "country", "timezone",
            "commercial_registration", "tax_registration_number",
            "currency_code", "currency_symbol", "currency_precision",
            "business_category", "financial_year_start_month",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, f in self.fields.items():
            css = "form-select" if isinstance(f.widget, forms.Select) else "form-control"
            f.widget.attrs.setdefault("class", css)
        self.fields["currency_precision"].widget = forms.Select(
            choices=[(0, "0"), (2, "2"), (3, "3")], attrs=SELECT)
        self.fields["timezone"] = forms.ChoiceField(
            choices=[(z, z) for z in sorted(zoneinfo.available_timezones())],
            widget=forms.Select(attrs=SELECT))


class BusinessSettingsForm(forms.ModelForm):
    class Meta:
        model = BusinessSettings
        exclude = ["business", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            if isinstance(f.widget, forms.CheckboxInput):
                f.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(f.widget, forms.Select):
                f.widget.attrs.setdefault("class", "form-select")
            elif isinstance(f.widget, forms.Textarea):
                f.widget.attrs.setdefault("class", "form-control")
                f.widget.attrs.setdefault("rows", 2)
            else:
                f.widget.attrs.setdefault("class", "form-control")
