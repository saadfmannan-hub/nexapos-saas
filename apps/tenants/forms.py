import zoneinfo

from django import forms
from django.contrib.auth.password_validation import validate_password

from apps.accounts.models import User
from apps.core.currencies import currency_choices, precision_for

from .models import Business, BusinessSettings

INPUT = {"class": "form-control"}
SELECT = {"class": "form-select"}

COMMON_CURRENCIES = currency_choices() + [("OTHER", "Other (enter code below)")]

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
        return precision_for(self.currency_code, default=2)


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
        for _name, f in self.fields.items():
            css = "form-select" if isinstance(f.widget, forms.Select) else "form-control"
            f.widget.attrs.setdefault("class", css)
        self.fields["currency_precision"].widget = forms.Select(
            choices=[(0, "0"), (2, "2"), (3, "3")], attrs=SELECT)
        self.fields["currency_code"] = forms.ChoiceField(
            choices=currency_choices(current=self.instance.currency_code),
            widget=forms.Select(attrs=SELECT),
            help_text="Symbol and decimals are applied automatically; "
                      "override the symbol below if needed.",
        )
        self.fields["currency_symbol"].required = False
        self.fields["timezone"] = forms.ChoiceField(
            choices=[(z, z) for z in sorted(zoneinfo.available_timezones())],
            widget=forms.Select(attrs=SELECT))

    def clean(self):
        data = super().clean()
        # Auto-adopt the registry precision when the currency changes and
        # the precision was left at the old currency's default.
        code = data.get("currency_code", "")
        if code and code != self.instance.currency_code:
            old_default = precision_for(self.instance.currency_code, default=None)
            if data.get("currency_precision") in (None, old_default):
                data["currency_precision"] = precision_for(code, default=2)
        return data


class BusinessSettingsForm(forms.ModelForm):
    FIELD_GROUPS = {
        "invoice": [
            "invoice_prefix", "invoice_include_branch_code", "invoice_footer",
            "receipt_footer", "terms_and_conditions", "show_logo_on_invoice",
        ],
        "vat": [
            "vat_enabled", "vat_percentage", "vat_registration_number",
            "show_vat_on_invoice_receipt", "prices_include_tax",
        ],
        "more_options": [f"more_option_label_{index}" for index in range(1, 21)],
        "policies": [
            "price_rounding", "max_discount_percent", "negative_stock_policy",
            "shared_fabric_warehouse",
            "return_window_days", "allow_sale_without_shift",
            "require_customer_for_credit",
        ],
        "alerts": [
            "expense_approval_threshold", "adjustment_requires_approval",
            "notify_low_stock", "notify_credit_overdue", "notify_support_access",
        ],
    }

    class Meta:
        model = BusinessSettings
        fields = [
            "vat_enabled",
            "vat_percentage",
            "vat_registration_number",
            "show_vat_on_invoice_receipt",
            "prices_include_tax",
            "more_option_label_1",
            "more_option_label_2",
            "more_option_label_3",
            "more_option_label_4",
            "more_option_label_5",
            "more_option_label_6",
            "more_option_label_7",
            "more_option_label_8",
            "more_option_label_9",
            "more_option_label_10",
            "more_option_label_11",
            "more_option_label_12",
            "more_option_label_13",
            "more_option_label_14",
            "more_option_label_15",
            "more_option_label_16",
            "more_option_label_17",
            "more_option_label_18",
            "more_option_label_19",
            "more_option_label_20",
            "invoice_prefix",
            "invoice_include_branch_code",
            "invoice_footer",
            "receipt_footer",
            "terms_and_conditions",
            "show_logo_on_invoice",
            "negative_stock_policy",
            "shared_fabric_warehouse",
            "allow_sale_without_shift",
            "max_discount_percent",
            "require_customer_for_credit",
            "return_window_days",
            "expense_approval_threshold",
            "adjustment_requires_approval",
            "price_rounding",
            "notify_low_stock",
            "notify_credit_overdue",
            "notify_support_access",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.branches.models import Branch, Warehouse

        business = self.instance.business
        self.fields["shared_fabric_warehouse"].queryset = (
            Warehouse.objects.for_business(business)
            .filter(
                is_active=True,
                branch__is_active=True,
                branch__usage_type=Branch.UsageType.WORKSHOP_STOCK,
            )
            .select_related("branch")
            .order_by("branch__name", "name")
        )
        self.fields["shared_fabric_warehouse"].required = False
        if not self.fields["shared_fabric_warehouse"].queryset.exists():
            self.fields["shared_fabric_warehouse"].help_text = (
                "No active warehouse is linked to a Workshop / Stock Location. "
                "Create or edit the Workshop warehouse first."
            )
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

        self.fields["vat_percentage"].widget.attrs.update({"min": "0", "step": "0.001"})
        self.fields["vat_registration_number"].label = "VAT Registration Number"
        self.fields["show_vat_on_invoice_receipt"].label = "Show VAT on Invoice/Receipt"
        for index in range(1, 21):
            field = self.fields[f"more_option_label_{index}"]
            field.label = f"Label {index}"
            field.required = False
