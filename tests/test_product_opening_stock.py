"""Focused coverage for product opening-stock validation and unit display."""
from decimal import Decimal

from django.urls import reverse

from apps.catalog.forms import ProductForm
from apps.catalog.models import Product, Unit
from apps.inventory import services as inventory
from apps.inventory.models import StockMovement

from .base import TenantTestCase

D = Decimal


class ProductOpeningStockTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.pcs = Unit.objects.for_business(self.business_a).get(name="Piece")
        self.pcs.abbreviation = "pcs"
        self.pcs.save(update_fields=["abbreviation"])
        self.meter = Unit.objects.for_business(self.business_a).get(name="Meter")

    def payload(self, **overrides):
        data = {
            "name": "Opening Stock Product",
            "product_type": Product.Type.STANDARD,
            "unit": self.pcs.pk,
            "sku": "OPEN-001",
            "purchase_price": "4.000",
            "sale_price": "10.000",
            "wholesale_price": "0.000",
            "minimum_sale_price": "0.000",
            "reorder_level": "0.000",
            "opening_stock": "12.000",
            "opening_warehouse": self.warehouse_a.pk,
            "track_inventory": "on",
            "allow_discount": "on",
            "is_active": "on",
        }
        data.update(overrides)
        return data

    def test_create_form_requires_opening_stock(self):
        form = ProductForm(self.business_a)
        self.assertTrue(form.fields["opening_stock"].required)

    def test_missing_opening_stock_is_rejected_with_field_error(self):
        data = self.payload()
        data.pop("opening_stock")
        response = self.client.post(reverse("catalog:product_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "opening_stock", "Enter the opening stock.")
        self.assertFalse(Product.objects.filter(sku="OPEN-001").exists())

    def test_invalid_opening_stock_is_rejected_with_field_error(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.payload(opening_stock="not-a-number"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "opening_stock",
            "Enter a valid opening stock quantity.",
        )

    def test_negative_opening_stock_is_rejected(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.payload(opening_stock="-0.001"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "opening_stock",
            "Opening stock cannot be negative.",
        )

    def test_pcs_product_creates_exact_opening_stock(self):
        response = self.client.post(reverse("catalog:product_create"), self.payload())
        self.assertEqual(response.status_code, 302)
        product = Product.objects.for_business(self.business_a).get(sku="OPEN-001")
        self.assertEqual(product.unit, self.pcs)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, product),
            D("12.000"),
        )

    def test_meter_parent_opening_stock_is_rejected(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.payload(
                name="Fabric Roll",
                sku="FABRIC-125",
                unit=self.meter.pk,
                opening_stock="125.750",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "opening_stock",
            "Parent opening stock is not allowed for Meter products. "
            "Enter stock for each variant instead.",
        )
        self.assertFalse(Product.objects.filter(sku="FABRIC-125").exists())

    def test_single_warehouse_is_auto_selected(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.payload(opening_warehouse=""),
        )
        self.assertEqual(response.status_code, 302)
        product = Product.objects.for_business(self.business_a).get(
            sku="OPEN-001"
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, product
            ),
            D("12.000"),
        )

    def test_zero_opening_stock_is_valid_without_warehouse_or_movement(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.payload(opening_stock="0.000", opening_warehouse=""),
        )
        self.assertEqual(response.status_code, 302)
        product = Product.objects.for_business(self.business_a).get(sku="OPEN-001")
        self.assertFalse(StockMovement.objects.filter(product=product).exists())

    def test_non_stock_product_cannot_silently_discard_positive_opening_stock(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.payload(product_type=Product.Type.NON_STOCK, track_inventory=""),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "opening_stock",
            "Opening stock must be 0 for products that do not track inventory.",
        )

    def test_cross_tenant_warehouse_is_rejected(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.payload(opening_warehouse=self.warehouse_b.pk),
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Product.objects.filter(sku="OPEN-001").exists())

    def test_cross_tenant_unit_is_rejected(self):
        other_unit = Unit.objects.for_business(self.business_b).get(name="Kilogram")
        response = self.client.post(
            reverse("catalog:product_create"),
            self.payload(unit=other_unit.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("unit", response.context["form"].errors)
        self.assertFalse(Product.objects.filter(sku="OPEN-001").exists())

    def test_unit_options_expose_abbreviation_and_name_fallback(self):
        fallback = Unit.objects.create(
            business=self.business_a,
            name="Carton",
            abbreviation="",
        )
        html = str(ProductForm(self.business_a)["unit"])
        self.assertIn(f'value="{self.pcs.pk}" data-unit-label="pcs"', html)
        self.assertIn(f'value="{self.meter.pk}" data-unit-label="m"', html)
        self.assertIn(f'value="{fallback.pk}" data-unit-label="Carton"', html)

    def test_validation_rerender_preserves_unit_and_opening_stock(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.payload(name="", unit=self.meter.pk, opening_stock="125.750"),
        )
        self.assertEqual(response.status_code, 200)
        unit_html = str(response.context["form"]["unit"])
        self.assertIn(
            f'value="{self.meter.pk}" selected data-unit-label="m"',
            unit_html,
        )
        self.assertContains(response, 'value="125.750"')
        self.assertContains(response, "openingStockUnitLabel() || 'unit'")

    def test_edit_form_does_not_expose_opening_stock_fields(self):
        form = ProductForm(self.business_a, instance=self.product_a)
        self.assertNotIn("opening_stock", form.fields)
        self.assertNotIn("opening_warehouse", form.fields)

    def test_edit_does_not_duplicate_movements_or_reset_current_stock(self):
        inventory.record_movement(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.product_a,
            movement_type=StockMovement.Type.ADJUST_IN,
            quantity=D("7.250"),
            unit_cost=self.product_a.purchase_price,
            user=self.owner_a,
        )
        stock_before = inventory.get_stock(
            self.business_a, self.warehouse_a, self.product_a,
        )
        movements_before = StockMovement.objects.for_business(self.business_a).filter(
            product=self.product_a,
        ).count()

        response = self.client.post(
            reverse("catalog:product_edit", args=[self.product_a.public_id]),
            self.payload(
                name="Edited Existing Product",
                sku=self.product_a.sku,
                unit=self.pcs.pk,
                opening_stock="999.000",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            stock_before,
        )
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).filter(
                product=self.product_a,
            ).count(),
            movements_before,
        )
