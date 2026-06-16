from django import forms

from apps.branches.forms import TenantStyledModelForm

from .models import Customer, CustomerGroup


class CustomerForm(TenantStyledModelForm):
    MORE_OPTION_PREFIX = "more_option_"

    class Meta:
        model = Customer
        fields = ["full_name", "code", "mobile", "whatsapp", "email", "address",
                  "city", "country", "group", "tax_number", "credit_limit",
                  "notes", "is_active"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        self.fields["group"].queryset = CustomerGroup.objects.for_business(business)
        self.fields["group"].required = False
        self.fields["code"].required = False
        self.more_option_fields = []
        current_values = self.instance.more_options or {}
        for option in business.settings.more_option_labels:
            field_name = f"{self.MORE_OPTION_PREFIX}{option['key']}"
            self.fields[field_name] = forms.CharField(
                label=option["label"],
                required=False,
                max_length=255,
                initial=current_values.get(option["key"], ""),
                widget=forms.TextInput(attrs={"class": "form-control"}),
            )
            self.more_option_fields.append(field_name)

    def save(self, commit=True):
        customer = super().save(commit=False)
        customer.more_options = {
            name.removeprefix(self.MORE_OPTION_PREFIX): self.cleaned_data.get(name, "").strip()
            for name in self.more_option_fields
            if self.cleaned_data.get(name, "").strip()
        }
        if commit:
            customer.save()
            self.save_m2m()
        return customer

    def clean_code(self):
        code = self.cleaned_data.get("code", "").strip()
        if not code:
            return code
        qs = Customer.objects.for_business(self.business).filter(code__iexact=code)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This customer code is already in use.")
        return code

    def clean_mobile(self):
        mobile = self.cleaned_data.get("mobile", "").strip()
        if mobile:
            qs = Customer.objects.for_business(self.business).filter(mobile=mobile)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    "A customer with this mobile number already exists."
                )
        return mobile


class CustomerPaymentForm(forms.Form):
    amount = forms.DecimalField(min_value=0.001, decimal_places=3, max_digits=14,
                                widget=forms.NumberInput(attrs={
                                    "class": "form-control", "step": "any"}))
    payment_method = forms.ModelChoiceField(
        queryset=None, widget=forms.Select(attrs={"class": "form-select"}))
    reference = forms.CharField(required=False, max_length=120,
                                widget=forms.TextInput(attrs={"class": "form-control"}))
    notes = forms.CharField(required=False, max_length=300,
                            widget=forms.TextInput(attrs={"class": "form-control"}))

    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.sales.models import PaymentMethod

        self.fields["payment_method"].queryset = (
            PaymentMethod.objects.for_business(business)
            .filter(is_active=True)
            .exclude(kind__in=["customer_credit", "store_credit"])
        )
