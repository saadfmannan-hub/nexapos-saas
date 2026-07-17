"""Product catalog: categories, brands, units, taxes, products, variants."""
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


MAX_FABRIC_PER_GARMENT = Decimal("1000.000")


class Category(TenantModel):
    name = models.CharField(max_length=120)
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="children"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name", "parent"], name="uniq_category_per_business"
            )
        ]

    def __str__(self):
        return f"{self.parent.name} / {self.name}" if self.parent else self.name


class Brand(TenantModel):
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name"], name="uniq_brand_per_business"
            )
        ]

    def __str__(self):
        return self.name


class Unit(TenantModel):
    name = models.CharField(max_length=60)
    abbreviation = models.CharField(max_length=15)
    allow_decimal = models.BooleanField(default=False)
    # Stable internal semantic used by the locked tailoring workflow.  This
    # is intentionally not a user-facing mode/toggle: decimal-capable units
    # such as kg and litre must never be mistaken for fabric meters, and a
    # renamed canonical Meter unit must retain its meaning.
    is_meter = models.BooleanField(default=False, editable=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name"], name="uniq_unit_per_business"
            )
        ]

    def __str__(self):
        return self.name


class TaxRate(TenantModel):
    name = models.CharField(max_length=80)
    rate = models.DecimalField(max_digits=6, decimal_places=3, help_text="Percentage, e.g. 5.000")
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name"], name="uniq_taxrate_per_business"
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.rate}%)"


class Product(TenantModel):
    class Type(models.TextChoices):
        STANDARD = "standard", _("Standard product")
        VARIANT = "variant", _("Product with variants")
        SERVICE = "service", _("Service")
        NON_STOCK = "non_stock", _("Non-stock item")

    name = models.CharField(max_length=200)
    internal_code = models.CharField(max_length=40, blank=True)
    sku = models.CharField(max_length=60, blank=True)
    barcode = models.CharField(max_length=80, blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="products"
    )
    brand = models.ForeignKey(
        Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name="products"
    )
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT, null=True, blank=True, related_name="products"
    )
    product_type = models.CharField(max_length=12, choices=Type.choices, default=Type.STANDARD)

    purchase_price = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    sale_price = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    wholesale_price = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    minimum_sale_price = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    average_cost = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    tax_rate = models.ForeignKey(
        TaxRate, on_delete=models.SET_NULL, null=True, blank=True, related_name="products"
    )
    price_includes_tax = models.BooleanField(null=True, blank=True,
        help_text="Empty = follow business setting")

    reorder_level = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    track_inventory = models.BooleanField(default=True)
    allow_discount = models.BooleanField(default=True)
    is_tailoring_item = models.BooleanField(
        default=False,
        help_text=(
            "Require garment classification and a delivery date when this "
            "product is sold through POS."
        ),
    )
    estimated_adult_fabric = models.DecimalField(
        "Estimated Adult Fabric",
        max_digits=7,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(MAX_FABRIC_PER_GARMENT),
        ],
        help_text="Estimated meters required for one adult garment.",
    )
    estimated_child_fabric = models.DecimalField(
        "Estimated Child Fabric",
        max_digits=7,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(MAX_FABRIC_PER_GARMENT),
        ],
        help_text="Estimated meters required for one child garment.",
    )

    image = models.ImageField(upload_to="products/", blank=True, null=True)
    description = models.TextField(blank=True)
    preferred_supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="preferred_products",
    )

    is_active = models.BooleanField(default=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["business", "name"]),
            models.Index(fields=["business", "barcode"]),
            models.Index(fields=["business", "sku"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "sku"],
                condition=~models.Q(sku=""),
                name="uniq_product_sku_per_business",
            ),
            models.UniqueConstraint(
                fields=["business", "barcode"],
                condition=~models.Q(barcode=""),
                name="uniq_product_barcode_per_business",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(estimated_adult_fabric__isnull=True)
                    | models.Q(
                        estimated_adult_fabric__gte=0,
                        estimated_adult_fabric__lte=MAX_FABRIC_PER_GARMENT,
                    )
                ),
                name="product_adult_fabric_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(estimated_child_fabric__isnull=True)
                    | models.Q(
                        estimated_child_fabric__gte=0,
                        estimated_child_fabric__lte=MAX_FABRIC_PER_GARMENT,
                    )
                ),
                name="product_child_fabric_valid",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def has_variants(self):
        return self.product_type == self.Type.VARIANT

    @property
    def is_stocked(self):
        return self.track_inventory and self.product_type in (
            self.Type.STANDARD, self.Type.VARIANT
        )

    @property
    def is_meter_tailoring(self):
        """Whether new POS lines use the locked one-garment/meter workflow."""
        return bool(
            self.is_tailoring_item
            and self.unit_id is not None
            and self.unit.is_meter
        )

    @property
    def is_legacy_tailoring(self):
        """Null-unit tailoring products retained on the pre-Meter workflow."""
        return bool(self.is_tailoring_item and self.unit_id is None)

    def effective_tax_rate(self):
        return self.tax_rate.rate if self.tax_rate and self.tax_rate.is_active else 0


class ProductVariant(TenantModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    name = models.CharField(max_length=160, help_text='e.g. "Red / XL"')
    attributes = models.JSONField(default=dict, blank=True)
    sku = models.CharField(max_length=60, blank=True)
    barcode = models.CharField(max_length=80, blank=True)
    purchase_price = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    sale_price = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    average_cost = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    image = models.ImageField(upload_to="products/variants/", blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "sku"],
                condition=~models.Q(sku=""),
                name="uniq_variant_sku_per_business",
            ),
            models.UniqueConstraint(
                fields=["business", "barcode"],
                condition=~models.Q(barcode=""),
                name="uniq_variant_barcode_per_business",
            ),
        ]

    def __str__(self):
        return f"{self.product.name} — {self.name}"
