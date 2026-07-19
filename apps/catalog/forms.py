from decimal import Decimal

from django import forms
from django.db.models import Q

from apps.branches.forms import TenantStyledModelForm
from apps.branches.models import Warehouse

from .models import Brand, Category, Product, ProductVariant, TaxRate, Unit


class ProductUnitSelect(forms.Select):
    """Expose each unit's display suffix to the product form UI."""

    def create_option(self, name, value, label, selected, index,
                      subindex=None, attrs=None):
        option = super().create_option(
            name, value, label, selected, index, subindex=subindex, attrs=attrs,
        )
        unit = getattr(value, "instance", None)
        if unit is not None:
            option["attrs"]["data-unit-label"] = unit.abbreviation or unit.name
            option["attrs"]["data-is-meter"] = "true" if unit.is_meter else "false"
        return option


class ProductIdentifierValidationMixin:
    def _unique_check(self, field, value):
        if not value:
            return value
        products = Product.objects.for_business(self.business).filter(**{field: value})
        variants = ProductVariant.objects.for_business(self.business).filter(**{field: value})
        if self.instance.pk:
            products = products.exclude(pk=self.instance.pk)
        elif getattr(self, "allow_product_reuse", False):
            # The branch onboarding service performs the authoritative safe
            # Product reuse check after all identifying fields are clean.
            # A ProductVariant identifier is never eligible for parent reuse.
            if variants.exists():
                raise forms.ValidationError(
                    f"This {field.upper()} belongs to a product variant."
                )
            return value
        if products.exists() or variants.exists():
            raise forms.ValidationError(f"This {field.upper()} is already in use.")
        return value

    def clean_sku(self):
        return self._unique_check("sku", self.cleaned_data.get("sku", "").strip())

    def clean_barcode(self):
        return self._unique_check("barcode", self.cleaned_data.get("barcode", "").strip())


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

    @staticmethod
    def _is_canonical_meter(name, abbreviation):
        return (
            str(name or "").strip().casefold() in {"meter", "metre"}
            or str(abbreviation or "").strip().casefold() == "m"
        )

    def clean(self):
        cleaned = super().clean()
        if (
            self.instance.pk
            and not self.instance.is_meter
            and self._is_canonical_meter(
                cleaned.get("name"), cleaned.get("abbreviation")
            )
        ):
            raise forms.ValidationError(
                "An existing unit cannot be converted into Meter because linked "
                "products could change workflow. Create a new Meter unit instead."
            )
        return cleaned

    def save(self, commit=True):
        unit = super().save(commit=False)
        # ``is_meter`` is deliberately not an advanced user-facing toggle.
        # Mark a new canonical Meter unit once, while retaining the semantic if
        # an existing Meter unit is later renamed. Existing non-Meter units are
        # never converted in place because that would reclassify linked stock.
        if (
            unit.is_meter
            or (
                not unit.pk
                and self._is_canonical_meter(unit.name, unit.abbreviation)
            )
        ):
            unit.is_meter = True
        if commit:
            unit.save()
            self.save_m2m()
        return unit


class TaxRateForm(TenantStyledModelForm):
    class Meta:
        model = TaxRate
        fields = ["name", "rate", "is_default", "is_active"]


class ProductForm(ProductIdentifierValidationMixin, TenantStyledModelForm):
    METER_HIDDEN_FIELDS = (
        "sale_price",
        "wholesale_price",
        "minimum_sale_price",
        "tax_rate",
        "price_includes_tax",
        "allow_discount",
        "reorder_level",
        "estimated_adult_fabric",
        "estimated_child_fabric",
    )
    METER_CREATE_DEFAULTS = {
        "sale_price": Decimal("0"),
        "wholesale_price": Decimal("0"),
        "minimum_sale_price": Decimal("0"),
        "tax_rate": None,
        "price_includes_tax": None,
        "allow_discount": False,
        "reorder_level": Decimal("0"),
        "estimated_adult_fabric": None,
        "estimated_child_fabric": None,
    }

    auto_generate_sku = forms.BooleanField(
        required=False, label="Auto Generate SKU",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Generate a unique SKU automatically (e.g. NEX-000001). "
                  "Applies to this product and any generated variants.",
    )
    opening_stock = forms.DecimalField(
        required=True, min_value=0, decimal_places=3, max_digits=14,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "any"}),
        help_text="Required when creating a product. Uses the selected product unit.",
        error_messages={
            "required": "Enter the opening stock.",
            "invalid": "Enter a valid opening stock quantity.",
            "min_value": "Opening stock cannot be negative.",
        },
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
            "track_inventory", "allow_discount", "is_tailoring_item",
            "estimated_adult_fabric", "estimated_child_fabric", "image", "description",
            "preferred_supplier", "is_active",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "unit": ProductUnitSelect(),
            "price_includes_tax": forms.Select(choices=[
                (None, "Follow business setting"), (True, "Tax inclusive"),
                (False, "Tax exclusive"),
            ]),
        }

    def __init__(
        self,
        business,
        *args,
        allowed_warehouse_ids=None,
        tailoring_enabled=True,
        selected_warehouse=None,
        require_branch_warehouse=False,
        allow_product_reuse=False,
        lock_global_fields=False,
        **kwargs,
    ):
        super().__init__(business, *args, **kwargs)
        from apps.suppliers.models import Supplier

        self.tailoring_enabled = bool(tailoring_enabled)
        self.allow_product_reuse = bool(allow_product_reuse)
        self.fields["category"].queryset = Category.objects.for_business(business).filter(is_active=True)
        self.fields["brand"].queryset = Brand.objects.for_business(business).filter(is_active=True)
        unit_qs = Unit.objects.for_business(business).filter(is_active=True)
        if self.instance.pk and self.instance.unit_id:
            unit_qs = Unit.objects.for_business(business).filter(
                Q(is_active=True) | Q(pk=self.instance.unit_id)
            )
        if not self.tailoring_enabled:
            # Meter is the canonical trigger for the locked tailoring workflow.
            # Preserve an existing historical Meter retail product, but never
            # offer Meter as a way to activate Tailoring on a retail-only plan.
            legacy_meter_retail_id = (
                self.instance.unit_id
                if self.instance.pk
                and self.instance.unit_id
                and self.instance.unit.is_meter
                and not self.instance.is_tailoring_item
                else None
            )
            unit_qs = unit_qs.filter(
                Q(is_meter=False) | Q(pk=legacy_meter_retail_id)
            )
        self.fields["unit"].queryset = unit_qs
        self.fields["tax_rate"].queryset = TaxRate.objects.for_business(business).filter(is_active=True)
        self.fields["preferred_supplier"].queryset = Supplier.objects.for_business(business).filter(is_active=True)
        warehouse_qs = Warehouse.objects.for_business(business).filter(is_active=True)
        if allowed_warehouse_ids is not None:
            warehouse_qs = warehouse_qs.filter(pk__in=allowed_warehouse_ids)
        self.fields["opening_warehouse"].queryset = warehouse_qs
        self.fields["opening_warehouse"].label = "Warehouse"
        self.fields["opening_warehouse"].required = bool(
            require_branch_warehouse
        )
        if selected_warehouse is not None:
            self.fields["opening_warehouse"].initial = selected_warehouse
        for name in ("category", "brand", "unit", "tax_rate", "preferred_supplier"):
            self.fields[name].required = False

        selected_unit = None
        if self.is_bound:
            unit_id = self.data.get(self.add_prefix("unit"), "")
            if str(unit_id).isdigit():
                selected_unit = self.fields["unit"].queryset.filter(pk=unit_id).first()
        elif self.instance.pk:
            selected_unit = self.instance.unit
        self._legacy_meter_retail = bool(
            self.instance.pk
            and selected_unit
            and selected_unit.pk == self.instance.unit_id
            and selected_unit.is_meter
            and not self.instance.is_tailoring_item
        )
        self._meter_selected = bool(
            selected_unit
            and selected_unit.is_meter
            and not self._legacy_meter_retail
        )
        if self._meter_selected:
            for name in self.METER_HIDDEN_FIELDS:
                self.fields[name].required = False
            if "opening_stock" in self.fields:
                self.fields["opening_stock"].required = False

        # Alpine bindings for the dynamic variants UI / auto-SKU toggle.
        self.fields["product_type"].widget.attrs["x-model"] = "productType"
        self.fields["is_tailoring_item"].widget.attrs["x-model"] = "isTailoring"
        self.fields["track_inventory"].widget.attrs["x-model"] = "tracksInventory"
        fabric_labels = {
            "estimated_adult_fabric": "Estimated Adult Fabric (Meters)",
            "estimated_child_fabric": "Estimated Child Fabric (Meters)",
        }
        for name, label in fabric_labels.items():
            self.fields[name].widget.attrs.update({"step": "0.001", "min": "0", "max": "1000"})
            self.fields[name].label = label
        self.fields["auto_generate_sku"].widget.attrs["x-model"] = "autoSku"
        self.fields["unit"].widget.attrs.update({
            "x-model": "unitId",
            "x-ref": "productUnit",
        })
        for name in self.METER_HIDDEN_FIELDS:
            self.fields[name].widget.attrs["x-bind:disabled"] = "isMeterUnit()"
        if "opening_stock" in self.fields:
            self.fields["opening_stock"].widget.attrs["x-bind:disabled"] = "isMeterUnit()"
        self.fields["is_tailoring_item"].widget.attrs["x-bind:disabled"] = "isMeterUnit()"
        self.fields["track_inventory"].widget.attrs["x-bind:disabled"] = "isMeterUnit()"
        self.fields["sku"].widget.attrs["x-bind:disabled"] = "autoSku"
        self.fields["sku"].widget.attrs["x-bind:placeholder"] = (
            "autoSku ? 'Auto-generated on save' : ''"
        )
        if not self.tailoring_enabled:
            self.fields.pop("is_tailoring_item", None)
            self.fields.pop("estimated_adult_fabric", None)
            self.fields.pop("estimated_child_fabric", None)
        if self.instance.pk:  # parent opening stock only at creation
            del self.fields["opening_stock"]
            selected_type = (
                self.data.get(self.add_prefix("product_type"), "")
                if self.is_bound
                else self.instance.product_type
            )
            if selected_type != Product.Type.VARIANT:
                del self.fields["opening_warehouse"]

        if lock_global_fields and self.instance.pk:
            for name, field in self.fields.items():
                if name not in {"opening_warehouse", "auto_generate_sku"}:
                    field.disabled = True

    def clean(self):
        cleaned = super().clean()
        fabric_fields = ("estimated_adult_fabric", "estimated_child_fabric")
        unit = cleaned.get("unit")
        is_legacy_meter_retail = bool(
            self.instance.pk
            and self.instance.unit_id
            and unit
            and unit.pk == self.instance.unit_id
            and self.instance.unit.is_meter
            and not self.instance.is_tailoring_item
        )
        is_meter = bool(unit and unit.is_meter and not is_legacy_meter_retail)

        if is_meter:
            cleaned["is_tailoring_item"] = True
            cleaned["track_inventory"] = True
            product_type = cleaned.get("product_type")
            if product_type not in (Product.Type.STANDARD, Product.Type.VARIANT):
                self.add_error(
                    "product_type",
                    "Meter products must be a standard product or a product with variants.",
                )

            # Disabled fields are absent from POST. Preserve historical values
            # on edits; only new Meter products receive neutral defaults.
            for name, default in self.METER_CREATE_DEFAULTS.items():
                cleaned[name] = getattr(self.instance, name) if self.instance.pk else default

            if "opening_stock" in self.fields:
                opening_stock = cleaned.get("opening_stock")
                if opening_stock is not None and opening_stock > 0:
                    self.add_error(
                        "opening_stock",
                        "Parent opening stock is not allowed for Meter products. "
                        "Enter stock for each variant instead.",
                    )
                cleaned["opening_stock"] = Decimal("0")
        elif is_legacy_meter_retail:
            # Canonical Meter units existed before this locked workflow. Keep
            # an explicitly non-tailoring historical product fully retail and
            # editable instead of changing its pricing or stock semantics.
            cleaned["is_tailoring_item"] = False
        elif cleaned.get("is_tailoring_item"):
            for name in fabric_fields:
                is_existing_legacy = bool(
                    self.instance.pk
                    and self.instance.is_tailoring_item
                    and self.instance.unit_id is None
                )
                if cleaned.get(name) is None and not is_existing_legacy:
                    self.add_error(name, "Enter the estimated fabric in meters.")
        else:
            for name in fabric_fields:
                cleaned[name] = None

        opening_stock = cleaned.get("opening_stock")
        if opening_stock is not None and opening_stock > 0:
            product_type = cleaned.get("product_type")
            tracks_inventory = cleaned.get("track_inventory")
            if product_type and not (
                tracks_inventory
                and product_type in (Product.Type.STANDARD, Product.Type.VARIANT)
            ):
                self.add_error(
                    "opening_stock",
                    "Opening stock must be 0 for products that do not track inventory.",
                )
            elif (
                product_type
                and not cleaned.get("opening_warehouse")
                and "opening_warehouse" not in self.errors
            ):
                self.add_error(
                    "opening_warehouse",
                    "Select a warehouse for the opening stock.",
                )

        if self.instance.pk:
            from . import services as catalog_services

            try:
                catalog_services.validate_meter_product_shape(
                    self.instance,
                    target_unit=unit,
                    target_type=cleaned.get("product_type"),
                    target_tailoring=cleaned.get("is_tailoring_item"),
                )
            except forms.ValidationError as exc:
                self.add_error("product_type", exc)
        return cleaned

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

    def __init__(self, business, *args, product=None, **kwargs):
        super().__init__(business, *args, **kwargs)
        self.product = product or (
            self.instance.product if self.instance.pk else None
        )
        self._meter_product = bool(
            self.product and self.product.is_meter_tailoring
        )
        if self._meter_product:
            self.fields["sale_price"].required = False
        if self.instance.pk and self.instance.attributes:
            attrs = self.instance.attributes
            self.fields["attr_size"].initial = attrs.get("Size", "")
            self.fields["attr_color"].initial = attrs.get("Color", "")

    def clean(self):
        cleaned = super().clean()
        if self._meter_product:
            # Sale price is not an active commercial field for Meter fabric.
            # Preserve historical values when editing; new colors start neutral.
            cleaned["sale_price"] = (
                self.instance.sale_price if self.instance.pk else Decimal("0")
            )
        return cleaned

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


class QuickProductForm(ProductIdentifierValidationMixin, TenantStyledModelForm):
    """Essential product fields for creation within a purchase order."""

    class Meta:
        model = Product
        fields = [
            "name", "sku", "category", "unit", "purchase_price", "sale_price",
            "tax_rate", "price_includes_tax", "track_inventory",
        ]
        widgets = {
            "unit": ProductUnitSelect(),
            "price_includes_tax": forms.Select(choices=[
                (None, "Follow business setting"), (True, "Tax inclusive"),
                (False, "Tax exclusive"),
            ]),
        }

    def __init__(self, business, *args, tailoring_enabled=True, **kwargs):
        super().__init__(business, *args, **kwargs)
        self.tailoring_enabled = bool(tailoring_enabled)
        self.fields["category"].queryset = Category.objects.for_business(
            business).filter(is_active=True)
        unit_qs = Unit.objects.for_business(business).filter(is_active=True)
        if not self.tailoring_enabled:
            unit_qs = unit_qs.filter(is_meter=False)
        self.fields["unit"].queryset = unit_qs
        self.fields["tax_rate"].queryset = TaxRate.objects.for_business(
            business).filter(is_active=True)
        self.fields["category"].required = False
        self.fields["unit"].required = True
        self.fields["tax_rate"].required = False
        self.fields["price_includes_tax"].required = False
        self.fields["track_inventory"].initial = True

        selected_unit = None
        if self.is_bound:
            unit_id = self.data.get(self.add_prefix("unit"), "")
            if str(unit_id).isdigit():
                selected_unit = self.fields["unit"].queryset.filter(pk=unit_id).first()
        self._meter_selected = bool(selected_unit and selected_unit.is_meter)
        if self._meter_selected:
            self.fields["sale_price"].required = False

        self.fields["name"].label = "Product Name"
        self.fields["sku"].label = "SKU / Product Code"
        self.fields["unit"].label = "Product Unit"
        self.fields["purchase_price"].label = "Cost Price"
        self.fields["sale_price"].label = "Selling Price"
        self.fields["sku"].help_text = "Optional. Must be unique when entered."
        self.fields["name"].error_messages["required"] = "Enter the product name."
        self.fields["unit"].error_messages["required"] = "Select a product unit."
        for name, field in self.fields.items():
            field.widget.attrs["data-quick-field"] = name
        self.fields["unit"].widget.attrs.update({
            "x-model": "quickUnitId",
            "x-ref": "quickProductUnit",
        })
        for name in ("sale_price", "tax_rate", "price_includes_tax", "track_inventory"):
            self.fields[name].widget.attrs["x-bind:disabled"] = "quickIsMeterUnit()"

    def clean(self):
        cleaned = super().clean()
        unit = cleaned.get("unit")
        if unit and unit.is_meter:
            cleaned["sale_price"] = Decimal("0")
            cleaned["tax_rate"] = None
            cleaned["price_includes_tax"] = None
            cleaned["track_inventory"] = True
        return cleaned


class ProductImportForm(forms.Form):
    file = forms.FileField(
        label="Excel or CSV file",
        widget=forms.ClearableFileInput(attrs={"class": "form-control",
                                               "accept": ".xlsx,.csv"}),
    )
    match_by = forms.ChoiceField(
        choices=[("sku", "SKU"), ("barcode", "Barcode"), ("name", "Product name")],
        initial="sku", widget=forms.Select(attrs={"class": "form-select"}),
        help_text="Existing Products and variants are reused only when identity matches safely.",
    )
