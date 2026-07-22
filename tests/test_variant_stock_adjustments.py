from decimal import Decimal

from django.urls import reverse

from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product, ProductVariant, Unit
from apps.core.money import D
from apps.inventory import services as inventory
from apps.inventory.models import (
    StockAdjustment,
    StockAdjustmentItem,
    StockMovement,
)
from tests.base import TenantTestCase


class VariantStockAdjustmentTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.piece_unit = (
            Unit.objects.for_business(self.business_a)
            .filter(allow_decimal=False, is_active=True)
            .first()
        )
        self.product = Product.objects.create(
            business=self.business_a,
            name="IMA",
            sku="IMA",
            product_type=Product.Type.VARIANT,
            unit=self.piece_unit,
        )
        self.variant = ProductVariant.objects.create(
            business=self.business_a,
            product=self.product,
            name="Color 3",
            sku="IMA-C3",
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.product,
            variant=self.variant,
            quantity=D("10"),
            unit_cost=D("2.000"),
            user=self.owner_a,
        )
        self.other_branch = Branch.objects.create(
            business=self.business_a,
            name="Second Branch",
            code="SECOND",
        )
        self.other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=self.other_branch,
            name="Second Warehouse",
            code="SECOND-WH",
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.other_warehouse,
            product=self.product,
            variant=self.variant,
            quantity=D("4"),
            unit_cost=D("2.000"),
            user=self.owner_a,
        )

    def post_adjustment(
        self,
        quantity,
        *,
        product=None,
        variant=None,
        warehouse=None,
        reason=StockAdjustment.Reason.DATA,
        notes="Variant stock correction",
    ):
        product = product or self.product
        variant = self.variant if variant is None and product == self.product else variant
        return self.client.post(
            reverse("inventory:adjustment_create"),
            {
                "warehouse": (warehouse or self.warehouse_a).pk,
                "reason": reason,
                "notes": notes,
                "product_id": product.pk,
                "variant_id": variant.pk if variant else "",
                "quantity": quantity,
            },
        )

    def test_variant_is_searchable_by_parent_and_variant_fields(self):
        for query in ("IMA", "Color 3", "IMA-C3"):
            with self.subTest(query=query):
                response = self.client.get(
                    reverse("inventory:item_search"), {"q": query}
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    {
                        "product_id": self.product.id,
                        "variant_id": self.variant.id,
                        "label": str(self.variant),
                        "sku": self.variant.sku,
                        "allow_decimal": False,
                    },
                    response.json()["results"],
                )

    def test_increase_updates_variant_and_writes_full_movement_history(self):
        response = self.post_adjustment("3", notes="Counted three extra")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product, self.variant
            ),
            D("13"),
        )
        adjustment = StockAdjustment.objects.for_business(self.business_a).get(
            notes="Counted three extra"
        )
        self.assertEqual(adjustment.reason, StockAdjustment.Reason.DATA)
        self.assertEqual(adjustment.warehouse, self.warehouse_a)
        self.assertEqual(adjustment.created_by, self.owner_a)
        item = StockAdjustmentItem.objects.get(adjustment=adjustment)
        self.assertEqual(item.product, self.product)
        self.assertEqual(item.variant, self.variant)
        self.assertEqual(item.previous_quantity, D("10"))
        self.assertEqual(item.change, D("3"))
        movement = StockMovement.objects.get(
            business=self.business_a,
            reference_type="Adjustment",
            reference_id=adjustment.adjustment_number,
        )
        self.assertEqual(movement.variant, self.variant)
        self.assertEqual(movement.warehouse, self.warehouse_a)
        self.assertEqual(movement.quantity, D("3"))
        self.assertEqual(movement.movement_type, StockMovement.Type.ADJUST_IN)
        self.assertEqual(movement.user, self.owner_a)
        self.assertEqual(movement.notes, "Counted three extra")
        self.assertIsNotNone(movement.created_at)

    def test_decrease_updates_variant_stock(self):
        response = self.post_adjustment(
            "-2", reason=StockAdjustment.Reason.DAMAGE
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product, self.variant
            ),
            D("8"),
        )
        movement = StockMovement.objects.for_business(self.business_a).get(
            reference_type="Adjustment"
        )
        self.assertEqual(movement.movement_type, StockMovement.Type.DAMAGE)
        self.assertEqual(movement.quantity, D("-2"))

    def test_adjustment_changes_only_selected_warehouse(self):
        response = self.post_adjustment("2", warehouse=self.other_warehouse)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.other_warehouse, self.product, self.variant
            ),
            D("6"),
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product, self.variant
            ),
            D("10"),
        )

    def test_other_tenant_variant_is_not_searchable_or_adjustable(self):
        foreign_product = Product.objects.create(
            business=self.business_b,
            name="Foreign Variant Product",
            product_type=Product.Type.VARIANT,
        )
        foreign_variant = ProductVariant.objects.create(
            business=self.business_b,
            product=foreign_product,
            name="Secret Shade",
            sku="FOREIGN-SHADE",
        )

        search = self.client.get(
            reverse("inventory:item_search"), {"q": "Secret Shade"}
        )
        self.assertEqual(search.json()["results"], [])
        response = self.post_adjustment(
            "1", product=foreign_product, variant=foreign_variant
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            StockAdjustment.objects.for_business(self.business_a).exists()
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_b,
                self.warehouse_b,
                foreign_product,
                foreign_variant,
            ),
            D("0"),
        )

    def test_user_without_adjust_permission_is_denied(self):
        self.client.force_login(self.cashier_a)

        response = self.post_adjustment("1")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product, self.variant
            ),
            D("10"),
        )

    def test_existing_simple_product_adjustment_still_works(self):
        response = self.post_adjustment(
            "2", product=self.product_a, variant=None, notes="Simple item"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product_a
            ),
            D("102"),
        )
        movement = StockMovement.objects.for_business(self.business_a).get(
            reference_type="Adjustment", notes="Simple item"
        )
        self.assertIsNone(movement.variant)

    def test_meter_variant_preserves_three_decimal_quantity(self):
        meter = Unit.objects.for_business(self.business_a).get(is_meter=True)
        fabric = Product.objects.create(
            business=self.business_a,
            name="Decimal Fabric",
            product_type=Product.Type.VARIANT,
            unit=meter,
            is_tailoring_item=True,
        )
        color = ProductVariant.objects.create(
            business=self.business_a,
            product=fabric,
            name="Blue",
            sku="DEC-BLUE",
        )

        response = self.post_adjustment(
            "1.275", product=fabric, variant=color, notes="Meter correction"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, fabric, color
            ),
            Decimal("1.275"),
        )
        movement = StockMovement.objects.for_business(self.business_a).get(
            reference_type="Adjustment", notes="Meter correction"
        )
        self.assertEqual(movement.quantity, Decimal("1.275"))

    def test_piece_variant_rejects_fractional_quantity(self):
        response = self.post_adjustment("1.250")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "requires a whole-number quantity")
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product, self.variant
            ),
            D("10"),
        )

    def test_excessive_decrease_is_rejected_when_negative_stock_is_blocked(self):
        response = self.post_adjustment(
            "-11", reason=StockAdjustment.Reason.DAMAGE
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Insufficient stock")
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product, self.variant
            ),
            D("10"),
        )
        self.assertFalse(
            StockAdjustment.objects.for_business(self.business_a).exists()
        )

    def test_product_detail_shows_stock_and_prefilled_adjustment_action(self):
        detail = self.client.get(
            reverse("catalog:product_detail", args=[self.product.public_id]),
            {"branch": self.branch_a.pk, "warehouse": self.warehouse_a.pk},
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Current stock")
        self.assertContains(detail, "Adjust Stock")
        self.assertContains(detail, "10.000")

        adjustment = self.client.get(
            reverse("inventory:adjustment_create"),
            {
                "branch": self.branch_a.pk,
                "warehouse": self.warehouse_a.pk,
                "product": self.product.pk,
                "variant": self.variant.pk,
            },
        )
        self.assertEqual(adjustment.status_code, 200)
        self.assertContains(adjustment, f'"variant_id": {self.variant.pk}')
