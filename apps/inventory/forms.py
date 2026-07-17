from django import forms
from django.db.models import Q

from apps.branches.models import Warehouse
from apps.catalog.models import Product

from .models import StockAdjustment, StockTransfer


class WarehouseScopedForm(forms.Form):
    def __init__(self, business, *args, **kwargs):
        membership = kwargs.pop("membership", None)
        super().__init__(*args, **kwargs)
        self.business = business
        warehouse_qs = Warehouse.objects.for_business(business).filter(is_active=True)
        allowed = membership.allowed_branch_ids if membership is not None else None
        if allowed is not None:
            warehouse_qs = warehouse_qs.filter(
                Q(branch_id__in=allowed) | Q(branch__isnull=True)
            )
        self.warehouse_queryset = warehouse_qs
        if "warehouse" in self.fields:
            self.fields["warehouse"].queryset = warehouse_qs


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
        qs = self.warehouse_queryset
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


def parse_item_rows(
    request,
    business,
    *,
    allow_negative=False,
    allow_parent_meter_repair=False,
):
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
        if qty < 0 and not allow_negative:
            raise forms.ValidationError("Transfer quantities must be greater than zero.")
        if (
            product.is_meter_tailoring
            and product.has_variants
            and variant is None
            and not allow_parent_meter_repair
        ):
            raise forms.ValidationError(
                f"Select a variant/color for {product.name}."
            )
        rows.append({"product": product, "variant": variant, "quantity": qty})
    if not rows:
        raise forms.ValidationError("Add at least one line with a quantity.")
    return rows
