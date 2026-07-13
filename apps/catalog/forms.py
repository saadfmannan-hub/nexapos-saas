from django import forms

from apps.branches.forms import TenantStyledModelForm
from apps.branches.models import Warehouse

from .models import Brand, Category, Product, ProductVariant, TaxRate, Unit


class CategoryForm(TenantStyledModelForm):
    class Meta:
        model = Category
        fields = ["name", "parent", "is_active"]

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        qs = Category.objects.for_business(business).filter(parent__isnull=True)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = qs
        self.fields["parent"].required = False


class BrandForm(TenantStyledModelForm):
    class Meta:
        model = Brand
        fields = ["name", "is_active"]


class UnitForm(TenantStyledModelForm):
    class Meta:
        model = Unit
        fields = ["name", "abbreviation", "allow_decimal", "is_active"]


class TaxRateForm(TenantStyledModelForm):
    class Meta:
        model = TaxRate
        fields = ["name", "rate", "is_default", "is_active"]


class ProductForm(TenantStyledModelForm):
    auto_generate_sku = forms.BooleanField(
        required=False, label="Auto Generate SKU",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Generate a unique SKU automatically (e.g. NEX-000001). "
                  "Applies to this product and any generated variants.",
    )
    opening_stock = forms.DecimalField(
        required=False, min_value=0, decimal_places=3, max_digits=14,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "any"}),
        help_text="Optional. Creates an opening stock entry in the selected warehouse.",
    )
    opening_warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(), required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = Product
        fields = [
            "name", "product_type", "category", "brand", "unit", "internal_code",
            "sku", "barcode", "purchase_price", "sale_price", "wholesale_price",
            "minimum_sale_price", "tax_rate", "price_includes_tax", "reorder_level",
            "track_inventory", "allow_discount", "is_tailoring_item", "image", "description",
            "preferred_supplier", "is_active",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "price_includes_tax": forms.Select(choices=[
                (None, "Follow business setting"), (True, "Tax inclusive"),
                (False, "Tax exclusive"),
            ]),
        }

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        from apps.suppliers.models import Supplier

        self.fields["category"].queryset = Category.objects.for_business(business).filter(is_active=True)
        self.fields["brand"].queryset = Brand.objects.for_business(business).filter(is_active=True)
        self.fields["unit"].queryset = Unit.objects.for_business(business).filter(is_active=True)
        self.fields["tax_rate"].queryset = TaxRate.objects.for_business(business).filter(is_active=True)
        self.fields["preferred_supplier"].queryset = Supplier.objects.for_business(business).filter(is_active=True)
        self.fields["opening_warehouse"].queryset = Warehouse.objects.for_business(business).filter(is_active=True)
        for name in ("category", "brand", "unit", "tax_rate", "preferred_supplier"):
            self.fields[name].required = False
        # Alpine bindings for the dynamic variants UI / auto-SKU toggle.
        self.fields["product_type"].widget.attrs["x-model"] = "productType"
        self.fields["auto_generate_sku"].widget.attrs["x-model"] = "autoSku"
        self.fields["sku"].widget.attrs["x-bind:disabled"] = "autoSku"
        self.fields["sku"].widget.attrs["x-bind:placeholder"] = (
            "autoSku ? 'Auto-generated on save' : ''"
        )
        if self.instance.pk:  # opening stock only at creation
            del self.fields["opening_stock"]
            del self.fields["opening_warehouse"]

    def _unique_check(self, field, value):
        if not value:
            return value
        qs = Product.objects.for_business(self.business).filter(**{field: value})
        variant_qs = ProductVariant.objects.for_business(self.business).filter(**{field: value})
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists() or variant_qs.exists():
            raise forms.ValidationError(f"This {field.upper()} is already in use.")
        return value

    def clean_sku(self):
        return self._unique_check("sku", self.cleaned_data.get("sku", "").strip())

    def clean_barcode(self):
        return self._unique_check("barcode", self.cleaned_data.get("barcode", "").strip())


class VariantForm(TenantStyledModelForm):
    attr_size = forms.CharField(required=False, label="Size",
                                widget=forms.TextInput(attrs={"class": "form-control"}))
    attr_color = forms.CharField(required=False, label="Color",
                                 widget=forms.TextInput(attrs={"class": "form-control"}))
    attr_other = forms.CharField(required=False, label="Other attribute",
                                 widget=forms.TextInput(attrs={"class": "form-control",
                                                               "placeholder": "e.g. Material: Cotton"}))

    class Meta:
        model = ProductVariant
        fields = ["name", "sku", "barcode", "purchase_price", "sale_price",
                  "image", "is_active"]

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        if self.instance.pk and self.instance.attributes:
            attrs = self.instance.attributes
            self.fields["attr_size"].initial = attrs.get("Size", "")
            self.fields["attr_color"].initial = attrs.get("Color", "")

    def _unique_check(self, field, value):
        if not value:
            return value
        qs = ProductVariant.objects.for_business(self.business).filter(**{field: value})
        product_qs = Product.objects.for_business(self.business).filter(**{field: value})
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists() or product_qs.exists():
            raise forms.ValidationError(f"This {field.upper()} is already in use.")
        return value

    def clean_sku(self):
        return self._unique_check("sku", self.cleaned_data.get("sku", "").strip())

    def clean_barcode(self):
        return self._unique_check("barcode", self.cleaned_data.get("barcode", "").strip())

    def build_attributes(self):
        attrs = {}
        if self.cleaned_data.get("attr_size"):
            attrs["Size"] = self.cleaned_data["attr_size"]
        if self.cleaned_data.get("attr_color"):
            attrs["Color"] = self.cleaned_data["attr_color"]
        other = self.cleaned_data.get("attr_other", "")
        if other and ":" in other:
            k, v = other.split(":", 1)
            attrs[k.strip()] = v.strip()
        elif other:
            attrs["Attribute"] = other.strip()
        return attrs


class ProductImportForm(forms.Form):
    file = forms.FileField(
        label="Excel or CSV file",
        widget=forms.ClearableFileInput(attrs={"class": "form-control",
                                               "accept": ".xlsx,.csv"}),
    )
    match_by = forms.ChoiceField(
        choices=[("sku", "SKU"), ("barcode", "Barcode"), ("name", "Product name")],
        initial="sku", widget=forms.Select(attrs={"class": "form-select"}),
        help_text="Rows matching an existing product are skipped (no silent updates).",
    )
