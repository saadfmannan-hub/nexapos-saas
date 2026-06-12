from django import forms

from apps.branches.models import Warehouse
from apps.catalog.models import Product

from .models import StockAdjustment, StockTransfer


class WarehouseScopedForm(forms.Form):
    def __init__(self, business, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        if "warehouse" in self.fields:
            self.fields["warehouse"].queryset = Warehouse.objects.for_business(
                business
            ).filter(is_active=True)


class TransferForm(WarehouseScopedForm):
    from_warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}))
    to_warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}))
    notes = forms.CharField(required=False, widget=forms.Textarea(
        attrs={"class": "form-control", "rows": 2}))

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        qs = Warehouse.objects.for_business(business).filter(is_active=True)
        self.fields["from_warehouse"].queryset = qs
        self.fields["to_warehouse"].queryset = qs

    def clean(self):
        data = super().clean()
        if data.get("from_warehouse") and data.get("from_warehouse") == data.get("to_warehouse"):
            raise forms.ValidationError("Source and destination must differ.")
        return data


class AdjustmentForm(WarehouseScopedForm):
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}))
    reason = forms.ChoiceField(choices=StockAdjustment.Reason.choices,
                               widget=forms.Select(attrs={"class": "form-select"}))
    notes = forms.CharField(required=False, widget=forms.Textarea(
        attrs={"class": "form-control", "rows": 2}))


class CountForm(WarehouseScopedForm):
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}))
    notes = forms.CharField(required=False, widget=forms.Textarea(
        attrs={"class": "form-control", "rows": 2}))


def parse_item_rows(request, business):
    """Parse repeated product_id[]/variant_id[]/quantity[] rows from POST.
    Returns list of dicts; raises forms.ValidationError on bad input."""
    from apps.catalog.models import ProductVariant
    from apps.core.money import D

    product_ids = request.POST.getlist("product_id")
    variant_ids = request.POST.getlist("variant_id")
    quantities = request.POST.getlist("quantity")
    rows = []
    for i, pid in enumerate(product_ids):
        if not pid:
            continue
        try:
            product = Product.objects.for_business(business).get(pk=int(pid))
        except (Product.DoesNotExist, ValueError):
            raise forms.ValidationError("Invalid product in line items.")
        variant = None
        vid = variant_ids[i] if i < len(variant_ids) else ""
        if vid:
            try:
                variant = ProductVariant.objects.for_business(business).get(
                    pk=int(vid), product=product)
            except (ProductVariant.DoesNotExist, ValueError):
                raise forms.ValidationError("Invalid variant in line items.")
        qty = D(quantities[i] if i < len(quantities) else 0)
        if qty == 0:
            continue
        rows.append({"product": product, "variant": variant, "quantity": qty})
    if not rows:
        raise forms.ValidationError("Add at least one line with a quantity.")
    return rows
