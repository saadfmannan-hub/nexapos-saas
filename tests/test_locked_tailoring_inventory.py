"""Focused lifecycle coverage for the locked tailoring-meter workflow."""

from decimal import Decimal

from django.utils import timezone

from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product, ProductVariant, TaxRate, Unit
from apps.customers.models import Customer
from apps.inventory import services as inventory
from apps.inventory.models import StockMovement
from apps.sales import services as sales
from apps.sales.models import Sale, SaleItem, SaleReturn
from apps.sales.services import SaleError

from .base import TenantTestCase

D = Decimal


class LockedTailoringInventoryTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.meter = Unit.objects.for_business(self.business_a).get(name="Meter")
        self.piece = Unit.objects.for_business(self.business_a).get(name="Piece")
        self.assertTrue(self.meter.is_meter)
        self.fabric = Product.objects.create(
            business=self.business_a,
            name="Golden City",
            sku="GOLDEN-CITY",
            product_type=Product.Type.VARIANT,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=True,
        )
        self.color = ProductVariant.objects.create(
            business=self.business_a,
            product=self.fabric,
            name="Color 2",
            sku="GOLDEN-CITY-C2",
            purchase_price=D("2.000"),
            average_cost=D("2.000"),
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.fabric,
            variant=self.color,
            quantity=D("10.000"),
            unit_cost=D("2.000"),
            user=self.owner_a,
        )

    def line(self, **overrides):
        line = {
            "product": self.fabric,
            "variant": self.color,
            "quantity": D("1"),
            "unit_price": D("25.000"),
            "discount_amount": D("0"),
            "fabric_meter_used": "3.500",
            "garment_classification": "adult",
            "collection_type": "normal",
            "tailoring_details": {},
        }
        line.update(overrides)
        return line

    def complete(
        self,
        *,
        items=None,
        total=None,
        checkout_token=None,
        cashier=None,
        customer=None,
        branch=None,
        warehouse=None,
        membership=None,
        invoice_discount=None,
    ):
        invoice_discount = D("0") if invoice_discount is None else invoice_discount
        items = items if items is not None else [self.line()]
        if total is None:
            total = sum(
                (D(str(item.get("unit_price", 0))) * D(str(item.get("quantity", 0))))
                for item in items
            )
        cashier = cashier or self.owner_a
        customer = customer or self.walk_in_a
        branch = branch or self.branch_a
        warehouse = warehouse or self.warehouse_a
        if membership is None and cashier == self.owner_a:
            membership = self.membership_a()
        return sales.complete_sale(
            business=self.business_a,
            branch=branch,
            warehouse=warehouse,
            cashier=cashier,
            customer=customer,
            membership=membership,
            items=items,
            payments=[{"method": self.cash_a, "amount": total}],
            invoice_discount=invoice_discount,
            delivery_date=timezone.localdate(),
            checkout_token=checkout_token,
        )

    def stock(self):
        return inventory.get_stock(
            self.business_a,
            self.warehouse_a,
            self.fabric,
            self.color,
        )

    def test_exact_meter_is_persisted_deducted_and_used_for_cost_only(self):
        sale = self.complete()
        item = sale.items.get()

        self.assertEqual(item.quantity, D("1.000"))
        self.assertEqual(item.fabric_meter_used, D("3.500"))
        self.assertEqual(item.inventory_quantity, D("3.500"))
        self.assertIsNone(item.estimated_fabric)
        self.assertEqual(sale.subtotal, D("25.000"))
        self.assertEqual(sale.total, D("25.000"))
        self.assertEqual(sale.total_cost, D("7.000"))
        self.assertEqual(sale.gross_profit, D("18.000"))
        self.assertEqual(self.stock(), D("6.500"))
        movement = StockMovement.objects.for_business(self.business_a).get(
            movement_type=StockMovement.Type.SALE,
            reference_id=sale.invoice_number,
        )
        self.assertEqual(movement.variant, self.color)
        self.assertEqual(movement.quantity, D("-3.500"))

    def test_hidden_legacy_price_flags_do_not_block_meter_order_price(self):
        self.fabric.minimum_sale_price = D("99.000")
        self.fabric.allow_discount = False
        self.fabric.save(update_fields=["minimum_sale_price", "allow_discount"])

        sale = self.complete(
            total=D("20.000"),
            invoice_discount=D("5.000"),
        )

        self.assertEqual(sale.subtotal, D("25.000"))
        self.assertEqual(sale.discount_amount, D("5.000"))
        self.assertEqual(sale.total, D("20.000"))
        self.assertEqual(self.stock(), D("6.500"))

    def test_meter_uses_business_vat_and_invoice_discount_not_hidden_tax_flags(self):
        hidden_tax = TaxRate.objects.create(
            business=self.business_a,
            name="Historical Meter VAT",
            rate=D("12.000"),
        )
        self.fabric.sale_price = D("88.000")
        self.fabric.tax_rate = hidden_tax
        self.fabric.price_includes_tax = True
        self.fabric.save(
            update_fields=["sale_price", "tax_rate", "price_includes_tax"]
        )
        settings_obj = self.business_a.settings
        settings_obj.vat_enabled = True
        settings_obj.vat_percentage = D("5.000")
        settings_obj.prices_include_tax = False
        settings_obj.save(
            update_fields=["vat_enabled", "vat_percentage", "prices_include_tax"]
        )

        sale = self.complete(
            items=[self.line(unit_price=D("100.000"))],
            total=D("94.500"),
            invoice_discount=D("10.000"),
        )
        item = sale.items.get()

        self.assertEqual(sale.subtotal, D("100.000"))
        self.assertEqual(sale.discount_amount, D("10.000"))
        self.assertEqual(sale.tax_amount, D("4.500"))
        self.assertEqual(sale.total, D("94.500"))
        self.assertEqual(item.tax_rate, D("5.000"))
        self.assertEqual(item.unit_price, D("100.000"))

    def test_duplicate_lines_remain_separate_and_deduct_their_sum(self):
        sale = self.complete(
            items=[
                self.line(fabric_meter_used="3.500"),
                self.line(
                    fabric_meter_used="2.250",
                    garment_classification="child",
                    collection_type="premium",
                ),
            ],
            total=D("50.000"),
        )

        self.assertEqual(sale.items.count(), 2)
        self.assertEqual(
            list(sale.items.order_by("id").values_list("fabric_meter_used", flat=True)),
            [D("3.500"), D("2.250")],
        )
        self.assertEqual(self.stock(), D("4.250"))
        self.assertEqual(
            list(
                StockMovement.objects.for_business(self.business_a)
                .filter(movement_type="sale", reference_id=sale.invoice_number)
                .order_by("id")
                .values_list("quantity", flat=True)
            ),
            [D("-3.500"), D("-2.250")],
        )

    def test_meter_quantity_greater_than_one_is_rejected_atomically(self):
        sale_count = Sale.objects.for_business(self.business_a).count()
        movement_count = StockMovement.objects.for_business(self.business_a).count()

        with self.assertRaisesMessage(
            SaleError, "Quantity must be 1 for meter tailoring garments."
        ):
            self.complete(items=[self.line(quantity=D("2"))], total=D("50.000"))

        self.assertEqual(Sale.objects.for_business(self.business_a).count(), sale_count)
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).count(), movement_count
        )
        self.assertEqual(self.stock(), D("10.000"))

    def test_meter_validation_is_strict_and_atomic(self):
        invalid_values = (
            None,
            "",
            "0",
            "-0.001",
            "invalid",
            "NaN",
            "Infinity",
            "1000.001",
            "3.5000",
        )
        sale_count = Sale.objects.for_business(self.business_a).count()
        movement_count = StockMovement.objects.for_business(self.business_a).count()

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(SaleError):
                    self.complete(items=[self.line(fabric_meter_used=value)])

        self.assertEqual(Sale.objects.for_business(self.business_a).count(), sale_count)
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).count(), movement_count
        )
        self.assertEqual(self.stock(), D("10.000"))

    def test_meter_variant_and_explicit_operational_choices_are_required(self):
        for overrides, message in (
            ({"variant": None}, "Select a fabric color"),
            ({"garment_classification": ""}, "Select Adult or Child"),
            ({"collection_type": ""}, "Select Normal or Premium"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesMessage(SaleError, message):
                    self.complete(items=[self.line(**overrides)])
        self.assertEqual(self.stock(), D("10.000"))

    def test_meter_tailoring_product_must_track_inventory(self):
        service = Product.objects.create(
            business=self.business_a,
            name="Unstocked Fabric",
            product_type=Product.Type.NON_STOCK,
            unit=self.meter,
            track_inventory=False,
            is_tailoring_item=True,
        )
        line = self.line(product=service, variant=None)
        with self.assertRaisesMessage(SaleError, "must track inventory"):
            self.complete(items=[line])

    def test_insufficient_stock_uses_entered_meter_not_sale_quantity(self):
        sale_count = Sale.objects.for_business(self.business_a).count()
        with self.assertRaises(inventory.InsufficientStock):
            self.complete(items=[self.line(fabric_meter_used="10.001")])
        self.assertEqual(Sale.objects.for_business(self.business_a).count(), sale_count)
        self.assertEqual(self.stock(), D("10.000"))

    def test_explicit_piece_is_retail_and_meter_only_data_is_ignored(self):
        retail = Product.objects.create(
            business=self.business_a,
            name="Finished Kumma",
            sku="FINISHED-KUMMA",
            product_type=Product.Type.NON_STOCK,
            unit=self.piece,
            track_inventory=False,
            is_tailoring_item=True,
            sale_price=D("10.000"),
        )
        line = {
            "product": retail,
            "quantity": D("2"),
            "unit_price": D("10.000"),
            "fabric_meter_used": "3.500",
        }

        sale = self.complete(items=[line], total=D("20.000"))
        item = sale.items.get()
        self.assertEqual(item.quantity, D("2.000"))
        self.assertIsNone(item.fabric_meter_used)
        self.assertEqual(item.garment_classification, "")
        self.assertEqual(item.collection_type, "")
        self.assertFalse(item.is_tailoring_line)

    def test_explicit_piece_cannot_activate_tailoring_metadata(self):
        retail = Product.objects.create(
            business=self.business_a,
            name="Finished Musar",
            product_type=Product.Type.NON_STOCK,
            unit=self.piece,
            track_inventory=False,
            is_tailoring_item=True,
        )
        with self.assertRaisesMessage(SaleError, "not configured as a tailoring garment"):
            self.complete(
                items=[{
                    "product": retail,
                    "quantity": D("1"),
                    "unit_price": D("10"),
                    "garment_classification": "adult",
                    "collection_type": "normal",
                }],
                total=D("10"),
            )

    def test_null_unit_legacy_tailoring_call_keeps_estimate_and_quantity(self):
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item"])
        sale = self.complete(
            items=[{
                "product": self.product_a,
                "quantity": D("2"),
                "unit_price": D("10.000"),
                "garment_classification": "adult",
            }],
            total=D("21.000"),
        )
        item = sale.items.get()
        self.assertEqual(item.quantity, D("2.000"))
        self.assertEqual(item.estimated_fabric, D("7.000"))
        self.assertIsNone(item.fabric_meter_used)
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product_a
            ),
            D("98.000"),
        )

    def test_checkout_token_replay_returns_same_sale_without_double_deduction(self):
        token = "tailoring-checkout-retry-1"
        sale = self.complete(checkout_token=token)
        movement_count = StockMovement.objects.for_business(self.business_a).filter(
            movement_type="sale",
            reference_id=sale.invoice_number,
        ).count()

        replay = self.complete(checkout_token=token)

        self.assertEqual(replay.pk, sale.pk)
        self.assertEqual(Sale.objects.filter(checkout_token=token).count(), 1)
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).filter(
                movement_type="sale",
                reference_id=sale.invoice_number,
            ).count(),
            movement_count,
        )
        self.assertEqual(self.stock(), D("6.500"))

    def test_checkout_token_cannot_be_reused_for_another_context(self):
        token = "tailoring-checkout-context-1"
        sale = self.complete(checkout_token=token)
        other_customer = Customer.objects.create(
            business=self.business_a,
            code="IDEMP-OTHER",
            full_name="Other Customer",
        )

        with self.assertRaisesMessage(SaleError, "Invalid checkout token"):
            self.complete(checkout_token=token, customer=other_customer)

        self.assertEqual(Sale.objects.filter(checkout_token=token).count(), 1)
        self.assertEqual(Sale.objects.get(checkout_token=token).pk, sale.pk)
        self.assertEqual(self.stock(), D("6.500"))

    def test_checkout_rejects_warehouse_from_another_branch(self):
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Other Branch",
            code="OTHER-BRANCH",
        )
        other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Other Warehouse",
            code="OTHER-WH",
        )

        with self.assertRaisesMessage(
            SaleError, "Warehouse does not belong to this branch"
        ):
            self.complete(warehouse=other_warehouse)
        self.assertEqual(self.stock(), D("10.000"))

    def test_void_locks_fresh_state_and_restores_exact_meter_once(self):
        sale = self.complete()
        stale_sale = Sale.objects.get(pk=sale.pk)

        sales.void_sale(sale=sale, user=self.owner_a, reason="Customer cancelled")
        self.assertEqual(self.stock(), D("10.000"))
        movement = StockMovement.objects.for_business(self.business_a).get(
            movement_type="sale_return",
            reference_type="Void",
            reference_id=sale.invoice_number,
        )
        self.assertEqual(movement.quantity, D("3.500"))

        with self.assertRaisesMessage(SaleError, "already voided"):
            sales.void_sale(
                sale=stale_sale,
                user=self.owner_a,
                reason="Repeated request",
            )
        self.assertEqual(self.stock(), D("10.000"))

    def test_restock_return_restores_exact_meter_and_stale_replay_is_rejected(self):
        sale = self.complete()
        item = sale.items.get()
        stale_item = SaleItem.objects.get(pk=item.pk)

        result = sales.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1"), "restock": True}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        self.assertEqual(result.refund_amount, D("25.000"))
        self.assertEqual(self.stock(), D("10.000"))
        movement = StockMovement.objects.for_business(self.business_a).get(
            movement_type="sale_return",
            reference_id=result.return_number,
        )
        self.assertEqual(movement.quantity, D("3.500"))

        with self.assertRaises(SaleError):
            sales.process_return(
                sale=sale,
                items=[{
                    "sale_item": stale_item,
                    "quantity": D("1"),
                    "restock": True,
                }],
                refund_method=SaleReturn.RefundMethod.CASH,
                user=self.owner_a,
            )
        self.assertEqual(self.stock(), D("10.000"))

    def test_partial_restock_return_is_rejected_atomically(self):
        sale = self.complete()
        item = sale.items.get()

        with self.assertRaisesMessage(SaleError, "must be fully returned"):
            sales.process_return(
                sale=sale,
                items=[{
                    "sale_item": item,
                    "quantity": D("0.500"),
                    "restock": True,
                }],
                refund_method=SaleReturn.RefundMethod.CASH,
                user=self.owner_a,
            )

        item.refresh_from_db()
        self.assertEqual(item.returned_quantity, D("0.000"))
        self.assertFalse(SaleReturn.objects.filter(sale=sale).exists())
        self.assertEqual(self.stock(), D("6.500"))

    def test_non_restock_return_keeps_meter_out_of_stock(self):
        sale = self.complete()
        item = sale.items.get()

        result = sales.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1"), "restock": False}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
            restock=True,
        )

        self.assertEqual(self.stock(), D("6.500"))
        self.assertFalse(result.items.get().restocked)
        self.assertFalse(
            StockMovement.objects.for_business(self.business_a).filter(
                movement_type="sale_return",
                reference_id=result.return_number,
            ).exists()
        )

    def test_workshop_actual_cannot_replace_pos_meter(self):
        item = self.complete().items.get()

        with self.assertRaisesMessage(SaleError, "Meter was recorded at POS"):
            sales.update_actual_fabric(
                sale_item=item,
                actual_fabric_used="3.250",
                user=self.owner_a,
                membership=self.membership_a(),
            )
        item.refresh_from_db()
        self.assertIsNone(item.actual_fabric_used)
        self.assertEqual(item.fabric_meter_used, D("3.500"))
