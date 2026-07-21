from django import forms

from .models import Branch, Warehouse


class TenantStyledModelForm(forms.ModelForm):
    """ModelForm that styles widgets and scopes FK choices to the tenant."""

    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        for f in self.fields.values():
            if isinstance(f.widget, forms.CheckboxInput):
                f.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(f.widget, (forms.Select, forms.SelectMultiple)):
                f.widget.attrs.setdefault("class", "form-select")
            else:
                f.widget.attrs.setdefault("class", "form-control")


class BranchForm(TenantStyledModelForm):
    class Meta:
        model = Branch
        fields = ["name", "code", "usage_type", "address", "phone", "email",
                  "invoice_prefix", "receipt_footer", "is_active"]
        widgets = {"receipt_footer": forms.Textarea(attrs={"rows": 2})}
        labels = {"usage_type": "Location type"}

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        qs = Branch.objects.for_business(self.business).filter(code__iexact=code)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This branch code is already in use.")
        return code


class WarehouseForm(TenantStyledModelForm):
    class Meta:
        model = Warehouse
        fields = ["name", "code", "branch", "address", "is_default", "is_active"]

    def __init__(self, business, *args, membership=None, **kwargs):
        super().__init__(business, *args, **kwargs)
        branches = Branch.objects.for_business(business).filter(is_active=True)
        if membership is not None and membership.allowed_branch_ids is not None:
            branches = branches.filter(pk__in=membership.allowed_branch_ids)
        self.fields["branch"].queryset = branches
        self.fields["branch"].required = False

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        qs = Warehouse.objects.for_business(self.business).filter(code__iexact=code)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This warehouse code is already in use.")
        return code
