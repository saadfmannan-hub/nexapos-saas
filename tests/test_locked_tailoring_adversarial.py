"""Adversarial regressions for the locked tailoring inventory lifecycle."""

import json
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product, ProductVariant, Unit
from apps.inventory import services as inventory
from apps.inventory import workflows
from apps.inventory.models import (
    StockAdjustment,
    StockLevel,
    StockMovement,
    StockTransfer,
)
from apps.purchases import services as purchases
from apps.purchases.models import Purchase
from apps.sales import services as sales
from apps.sales.models import HeldSale, Sale, SaleReturn
from apps.sales.services import SaleError
from apps.suppliers.models import Supplier

from .base import TenantTestCase

D = Decimal


class LockedTailoringAdversarialTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        settings_obj = self.business_a.settings
        settings_obj.vat_enabled = False
        settings_obj.negative_stock_policy = "block"
        settings_obj.save(update_fields=["vat_enabled", "negative_stock_policy"])
        self.client.force_login(self.owner_a)

        self.meter = Unit.objects.for_business(self.business_a).get(is_meter=True)
        self.fabric = Product.objects.create(
            business=self.business_a,
            name="Adversarial Meter Fabric",
            sku="ADV-METER",
            product_type=Product.Type.VARIANT,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=True,
        )
        self.color = ProductVariant.objects.create(
            business=self.business_a,
            product=self.fabric,
            name="Color 9",
            sku="ADV-METER-C9",
            purchase_price=D("2.000"),
            average_cost=D("2.000"),
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.fabric,
            variant=self.color,
            quantity=D("20.000"),
            unit_cost=D("2.000"),
            user=self.owner_a,
        )

    def meter_line(self, **overrides):
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

    def meter_http_line(self, **overrides):
        line = {
            "product_id": self.fabric.id,
            "variant_id": self.color.id,
            "quantity": "1",
            "unit_price": "25.000",
            "discount_amount": "0",
            "fabric_meter_used": "3.500",
            "garment_classification": "adult",
            "collection_type": "normal",
            "tailoring_details": {},
        }
        line.update(overrides)
        return line

    def complete_meter(self, *, branch=None, warehouse=None, token=None):
        return sales.complete_sale(
            business=self.business_a,
            branch=branch or self.branch_a,
            warehouse=warehouse or self.warehouse_a,
            cashier=self.owner_a,
            customer=self.walk_in_a,
            items=[self.meter_line()],
            payments=[{"method": self.cash_a, "amount": D("25.000")}],
            membership=self.membership_a(),
            delivery_date=timezone.localdate(),
            checkout_token=token,
        )

    def checkout_payload(self, *, token, held_id=None):
        payload = {
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [self.meter_http_line()],
            "payments": [{"method_id": self.cash_a.id, "amount": "25.000"}],
            "invoice_discount": "0",
            "delivery_date": str(timezone.localdate()),
            "priority": "normal",
            "checkout_token": token,
        }
        if held_id is not None:
            payload["held_id"] = held_id
        return payload

    def post_checkout(self, payload):
        return self.client.post(
            reverse("sales:pos_checkout"),
            json.dumps(payload),
            content_type="application/json",
        )

    def fabric_stock(self, warehouse=None):
        return inventory.get_stock(
            self.business_a,
            warehouse or self.warehouse_a,
            self.fabric,
            self.color,
        )

    def test_record_movement_rejects_parent_level_meter_variant_stock(self):
        movement_count = StockMovement.objects.for_business(self.business_a).filter(
            product=self.fabric,
        ).count()

        with self.assertRaises(ValidationError):
            inventory.record_movement(
                business=self.business_a,
                warehouse=self.warehouse_a,
                product=self.fabric,
                variant=None,
                movement_type=StockMovement.Type.ADJUST_IN,
                quantity=D("1.000"),
                user=self.owner_a,
            )

        self.assertFalse(
            StockLevel.objects.for_business(self.business_a).filter(
                warehouse=self.warehouse_a,
                product=self.fabric,
                variant__isnull=True,
            ).exists()
        )
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).filter(
                product=self.fabric,
            ).count(),
            movement_count,
        )
        self.assertEqual(self.fabric_stock(), D("20.000"))

    def test_record_movement_rejects_cross_tenant_variant_even_for_same_parent(self):
        rogue_variant = ProductVariant.objects.create(
            business=self.business_b,
            product=self.fabric,
            name="Cross-tenant color",
            sku="ADV-CROSS-TENANT",
        )

        with self.assertRaisesMessage(ValidationError, "Variant does not belong"):
            inventory.record_movement(
                business=self.business_a,
                warehouse=self.warehouse_a,
                product=self.fabric,
                variant=rogue_variant,
                movement_type=StockMovement.Type.ADJUST_IN,
                quantity=D("1.000"),
                user=self.owner_a,
            )

        self.assertEqual(self.fabric_stock(), D("20.000"))

    def test_meter_inventory_export_round_trips_without_reorder_input(self):
        dataset = inventory.inventory_export_dataset(self.business_a, {})
        row = next(
            row
            for row in dataset["rows"]
            if row[dataset["columns"].index("Variant SKU")] == self.color.sku
        )
        minimum = row[dataset["columns"].index("Minimum Stock Level")]
        quantity = row[dataset["columns"].index("Current Stock")]
        warehouse = row[dataset["columns"].index("Warehouse")]

        self.assertEqual(minimum, "")
        summary, errors = inventory.import_inventory(
            business=self.business_a,
            rows=[{
                "variant sku": self.color.sku,
                "warehouse": warehouse,
                "quantity": str(quantity),
                "minimum stock level": minimum,
            }],
            mode="replace",
            user=self.owner_a,
        )

        self.assertEqual(errors, [])
        self.assertEqual(summary["imported"], 1)
        self.assertEqual(self.fabric_stock(), D("20.000"))

    def test_barcode_only_meter_variant_export_round_trips(self):
        barcode_only = ProductVariant.objects.create(
            business=self.business_a,
            product=self.fabric,
            name="Barcode-only Color",
            sku="",
            barcode="ADV-METER-BARCODE-ONLY",
            purchase_price=D("2.000"),
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.fabric,
            variant=barcode_only,
            quantity=D("4.250"),
            unit_cost=D("2.000"),
            user=self.owner_a,
        )
        dataset = inventory.inventory_export_dataset(self.business_a, {})
        row = next(
            row
            for row in dataset["rows"]
            if row[dataset["columns"].index("Variant Barcode")]
            == barcode_only.barcode
        )

        summary, errors = inventory.import_inventory(
            business=self.business_a,
            rows=[{
                "sku": row[dataset["columns"].index("SKU")],
                "variant sku": "",
                "variant barcode": barcode_only.barcode,
                "warehouse": row[dataset["columns"].index("Warehouse")],
                "quantity": str(row[dataset["columns"].index("Current Stock")]),
                "minimum stock level": "",
            }],
            mode="replace",
            user=self.owner_a,
        )

        self.assertEqual(errors, [])
        self.assertEqual(summary["imported"], 1)
        self.assertEqual(
            inventory.get_stock(
                self.business_a,
                self.warehouse_a,
                self.fabric,
                barcode_only,
            ),
            D("4.250"),
        )

    def test_legacy_meter_parent_balance_can_only_be_repaired_to_zero(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Legacy Parent Balance",
            sku="LEGACY-PARENT-BALANCE",
            product_type=Product.Type.STANDARD,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=True,
        )
        inventory.record_movement(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=product,
            movement_type=StockMovement.Type.OPENING,
            quantity=D("2.500"),
            unit_cost=D("1.000"),
            user=self.owner_a,
        )
        Product.objects.filter(pk=product.pk).update(
            product_type=Product.Type.VARIANT
        )
        product.refresh_from_db()
        search = self.client.get(
            reverse("inventory:item_search"),
            {"q": "Legacy Parent", "parent_meter_repair": "1"},
        )
        self.assertTrue(
            any(
                row["product_id"] == product.id and row["variant_id"] is None
                for row in search.json()["results"]
            )
        )

        with self.assertRaisesMessage(ValidationError, "exactly to zero"):
            workflows.create_adjustment(
                business=self.business_a,
                warehouse=self.warehouse_a,
                reason="other",
                rows=[{
                    "product": product,
                    "variant": None,
                    "quantity": D("-1.000"),
                }],
                user=self.owner_a,
            )

        workflows.create_adjustment(
            business=self.business_a,
            warehouse=self.warehouse_a,
            reason="other",
            rows=[{
                "product": product,
                "variant": None,
                "quantity": D("-2.500"),
            }],
            user=self.owner_a,
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, product, None
            ),
            D("0"),
        )

        with self.assertRaises(ValidationError):
            workflows.create_adjustment(
                business=self.business_a,
                warehouse=self.warehouse_a,
                reason="other",
                rows=[{
                    "product": product,
                    "variant": None,
                    "quantity": D("1.000"),
                }],
                user=self.owner_a,
            )

    def test_transfer_rejects_negative_meter_quantity(self):
        destination = Warehouse.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            name="Negative Transfer Destination",
            code="NEG-DEST",
        )

        with self.assertRaisesMessage(ValidationError, "greater than zero"):
            workflows.create_transfer(
                business=self.business_a,
                from_warehouse=self.warehouse_a,
                to_warehouse=destination,
                rows=[{
                    "product": self.fabric,
                    "variant": self.color,
                    "quantity": D("-1.000"),
                }],
                user=self.owner_a,
            )

        self.assertEqual(self.fabric_stock(), D("20.000"))

    def test_inventory_history_creation_refreshes_locked_product_shape(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Stale Inventory Shape",
            sku="STALE-INVENTORY-SHAPE",
            product_type=Product.Type.STANDARD,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=True,
        )
        stale_product = Product.objects.get(pk=product.pk)
        Product.objects.filter(pk=product.pk).update(
            product_type=Product.Type.VARIANT
        )
        other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            name="Transfer Destination",
            code="ADV-DEST",
        )

        for workflow, before_count in (
            (
                lambda: workflows.create_transfer(
                    business=self.business_a,
                    from_warehouse=self.warehouse_a,
                    to_warehouse=other_warehouse,
                    rows=[{
                        "product": stale_product,
                        "variant": None,
                        "quantity": D("1.000"),
                    }],
                    user=self.owner_a,
                ),
                StockTransfer.objects.count,
            ),
            (
                lambda: workflows.create_adjustment(
                    business=self.business_a,
                    warehouse=self.warehouse_a,
                    reason="other",
                    rows=[{
                        "product": stale_product,
                        "variant": None,
                        "quantity": D("1.000"),
                    }],
                    user=self.owner_a,
                    requires_approval=True,
                ),
                StockAdjustment.objects.count,
            ),
        ):
            with self.subTest(workflow=workflow):
                count = before_count()
                with self.assertRaises(ValidationError):
                    workflow()
                self.assertEqual(before_count(), count)

    def test_purchase_service_rejects_cross_tenant_supplier(self):
        supplier_b = Supplier.objects.create(
            business=self.business_b,
            code="ADV-SUP-B",
            name="Other Tenant Supplier",
        )
        purchase_count = Purchase.objects.count()

        with self.assertRaisesMessage(ValidationError, "belong to this business"):
            purchases.create_purchase(
                business=self.business_a,
                supplier=supplier_b,
                branch=self.branch_a,
                warehouse=self.warehouse_a,
                rows=[{
                    "product": self.fabric,
                    "variant": self.color,
                    "quantity": D("1.000"),
                    "unit_cost": D("2.000"),
                }],
                user=self.owner_a,
                purchase_date=date.today(),
            )

        self.assertEqual(Purchase.objects.count(), purchase_count)

    def test_direct_meter_line_discount_is_rejected_atomically(self):
        sale_count = Sale.objects.for_business(self.business_a).count()

        with self.assertRaisesMessage(SaleError, "Discounts are not allowed"):
            sales.complete_sale(
                business=self.business_a,
                branch=self.branch_a,
                warehouse=self.warehouse_a,
                cashier=self.owner_a,
                customer=self.walk_in_a,
                items=[self.meter_line(discount_amount=D("1.000"))],
                payments=[{"method": self.cash_a, "amount": D("24.000")}],
                membership=self.membership_a(),
                delivery_date=timezone.localdate(),
            )

        self.assertEqual(Sale.objects.for_business(self.business_a).count(), sale_count)
        self.assertEqual(self.fabric_stock(), D("20.000"))

    def test_complete_sale_rejects_stale_deactivated_variant_atomically(self):
        stale_variant = ProductVariant.objects.get(pk=self.color.pk)
        ProductVariant.objects.filter(pk=self.color.pk).update(is_active=False)
        sale_count = Sale.objects.for_business(self.business_a).count()
        movement_count = StockMovement.objects.for_business(self.business_a).count()
        stock_before = self.fabric_stock()

        with self.assertRaisesMessage(SaleError, "Invalid variant in cart"):
            sales.complete_sale(
                business=self.business_a,
                branch=self.branch_a,
                warehouse=self.warehouse_a,
                cashier=self.owner_a,
                customer=self.walk_in_a,
                items=[self.meter_line(variant=stale_variant)],
                payments=[{"method": self.cash_a, "amount": D("25.000")}],
                membership=self.membership_a(),
                delivery_date=timezone.localdate(),
            )

        self.assertEqual(Sale.objects.for_business(self.business_a).count(), sale_count)
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).count(),
            movement_count,
        )
        self.assertEqual(self.fabric_stock(), stock_before)

    def test_partial_non_restock_meter_return_is_rejected_atomically(self):
        sale = self.complete_meter()
        item = sale.items.get()
        deducted_stock = self.fabric_stock()

        with self.assertRaisesMessage(SaleError, "must be fully returned"):
            sales.process_return(
                sale=sale,
                items=[
                    {
                        "sale_item": item,
                        "quantity": D("0.500"),
                        "restock": False,
                    }
                ],
                refund_method=SaleReturn.RefundMethod.CASH,
                user=self.owner_a,
            )

        item.refresh_from_db()
        self.assertEqual(item.returned_quantity, D("0.000"))
        self.assertFalse(SaleReturn.objects.filter(sale=sale).exists())
        self.assertEqual(self.fabric_stock(), deducted_stock)

    def test_successful_held_checkout_deletes_held_sale(self):
        token = "adversarial-held-success"
        held = HeldSale.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            cashier=self.owner_a,
            label="Held tailoring checkout",
            cart={
                "items": [self.meter_http_line()],
                "checkout_token": token,
            },
        )

        response = self.post_checkout(
            self.checkout_payload(token=token, held_id=held.id)
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(HeldSale.objects.filter(pk=held.id).exists())
        self.assertEqual(
            Sale.objects.for_business(self.business_a).filter(
                checkout_token=token,
            ).count(),
            1,
        )
        self.assertEqual(self.fabric_stock(), D("16.500"))

    def test_http_checkout_requires_idempotency_token(self):
        sale_count = Sale.objects.for_business(self.business_a).count()
        stock = self.fabric_stock()

        response = self.post_checkout(self.checkout_payload(token=""))

        self.assertEqual(response.status_code, 400)
        self.assertIn("checkout token", response.json()["error"].lower())
        self.assertEqual(
            Sale.objects.for_business(self.business_a).count(), sale_count
        )
        self.assertEqual(self.fabric_stock(), stock)

    def test_held_checkout_rejects_a_different_token(self):
        held = HeldSale.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            cashier=self.owner_a,
            label="Token-bound hold",
            cart={
                "items": [self.meter_http_line()],
                "checkout_token": "held-original-token",
            },
        )
        stock = self.fabric_stock()

        response = self.post_checkout(
            self.checkout_payload(token="held-other-token", held_id=held.id)
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(HeldSale.objects.filter(pk=held.pk).exists())
        self.assertFalse(
            Sale.objects.for_business(self.business_a).filter(
                checkout_token="held-other-token"
            ).exists()
        )
        self.assertEqual(self.fabric_stock(), stock)

    def test_idempotent_replay_cleans_matching_stale_held_sale(self):
        token = "adversarial-held-replay"
        sale = self.complete_meter(token=token)
        deducted_stock = self.fabric_stock()
        movement_count = StockMovement.objects.for_business(self.business_a).filter(
            movement_type=StockMovement.Type.SALE,
            reference_id=sale.invoice_number,
        ).count()
        stale = HeldSale.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            cashier=self.owner_a,
            label="Stale completed hold",
            cart={
                "items": [self.meter_http_line()],
                "checkout_token": token,
            },
        )

        response = self.post_checkout(
            self.checkout_payload(token=token, held_id=stale.id)
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["sale"]["public_id"], str(sale.public_id))
        self.assertFalse(HeldSale.objects.filter(pk=stale.id).exists())
        self.assertEqual(
            Sale.objects.for_business(self.business_a).filter(
                checkout_token=token,
            ).count(),
            1,
        )
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).filter(
                movement_type=StockMovement.Type.SALE,
                reference_id=sale.invoice_number,
            ).count(),
            movement_count,
        )
        self.assertEqual(self.fabric_stock(), deducted_stock)

    def test_idempotent_replay_does_not_delete_unrelated_held_sale(self):
        token = "adversarial-unrelated-held-replay"
        sale = self.complete_meter(token=token)
        unrelated = HeldSale.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            cashier=self.owner_a,
            label="Unrelated hold",
            cart={
                "items": [self.meter_http_line()],
                "checkout_token": "another-held-token",
            },
        )

        response = self.post_checkout(
            self.checkout_payload(token=token, held_id=unrelated.id)
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["sale"]["public_id"], str(sale.public_id))
        self.assertTrue(HeldSale.objects.filter(pk=unrelated.pk).exists())

    def test_legacy_null_unit_tailoring_http_checkout_still_works(self):
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item"])
        token = "adversarial-legacy-http"
        payload = {
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [
                {
                    "product_id": self.product_a.id,
                    "variant_id": None,
                    "quantity": "2",
                    "unit_price": "10.000",
                    "garment_classification": "adult",
                    "collection_type": "normal",
                    "tailoring_details": {},
                }
            ],
            "payments": [{"method_id": self.cash_a.id, "amount": "21.000"}],
            "invoice_discount": "0",
            "delivery_date": str(timezone.localdate()),
            "priority": "normal",
            "checkout_token": token,
        }

        response = self.post_checkout(payload)

        self.assertEqual(response.status_code, 200, response.content)
        sale = Sale.objects.for_business(self.business_a).get(checkout_token=token)
        item = sale.items.get()
        self.assertEqual(item.quantity, D("2.000"))
        self.assertEqual(item.estimated_fabric, D("7.000"))
        self.assertIsNone(item.fabric_meter_used)
        self.assertEqual(
            inventory.get_stock(
                self.business_a,
                self.warehouse_a,
                self.product_a,
            ),
            D("98.000"),
        )

    def test_restricted_member_cannot_read_void_or_return_other_branch_sale(self):
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Adversarial Other Branch",
            code="ADV-B2",
        )
        other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Adversarial Other Warehouse",
            code="ADV-W2",
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=other_warehouse,
            product=self.fabric,
            variant=self.color,
            quantity=D("20.000"),
            unit_cost=D("2.000"),
            user=self.owner_a,
        )
        sale = self.complete_meter(branch=other_branch, warehouse=other_warehouse)
        item = sale.items.get()
        deducted_stock = self.fabric_stock(other_warehouse)

        membership = self.membership_a()
        membership.branches.set([self.branch_a])

        detail = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        void = self.client.post(
            reverse("sales:void", args=[sale.public_id]),
            {"reason": "Unauthorized branch mutation"},
        )
        returned = self.client.post(
            reverse("sales:return_create", args=[sale.public_id]),
            {
                f"qty_{item.pk}": "1",
                f"restock_{item.pk}": "on",
                "refund_method": SaleReturn.RefundMethod.CASH,
            },
        )

        for response in (detail, void, returned):
            self.assertIn(response.status_code, (403, 404))
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.COMPLETED)
        self.assertFalse(SaleReturn.objects.filter(sale=sale).exists())
        self.assertEqual(self.fabric_stock(other_warehouse), deducted_stock)
        self.assertFalse(
            StockMovement.objects.for_business(self.business_a).filter(
                movement_type=StockMovement.Type.SALE_RETURN,
                reference_id=sale.invoice_number,
            ).exists()
        )

    def test_restricted_member_cannot_access_legacy_location_mismatches(self):
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Legacy Other Branch",
            code="ADV-LEGACY-B2",
        )
        other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Legacy Other Warehouse",
            code="ADV-LEGACY-W2",
        )
        central_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=None,
            name="Legacy Central Warehouse",
            code="ADV-LEGACY-CENTRAL",
        )
        sale = self.complete_meter(token="legacy-location-mismatch")
        Sale.objects.filter(pk=sale.pk).update(warehouse=other_warehouse)
        sale.refresh_from_db()
        sale_item = sale.items.get()

        supplier = Supplier.objects.create(
            business=self.business_a,
            code="ADV-LEGACY-SUP",
            name="Legacy Location Supplier",
        )
        purchase = purchases.create_purchase(
            business=self.business_a,
            supplier=supplier,
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            rows=[{
                "product": self.fabric,
                "variant": self.color,
                "quantity": D("2.000"),
                "unit_cost": D("2.000"),
            }],
            user=self.owner_a,
            purchase_date=date.today(),
        )
        Purchase.objects.filter(pk=purchase.pk).update(warehouse=other_warehouse)
        purchase.refresh_from_db()

        central_sale = Sale.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            warehouse=central_warehouse,
            cashier=self.owner_a,
            customer=self.walk_in_a,
            invoice_number="ADV-LEGACY-CENTRAL-SALE",
            status=Sale.Status.COMPLETED,
            sale_date=timezone.now(),
        )
        central_purchase = Purchase.objects.create(
            business=self.business_a,
            purchase_number="ADV-LEGACY-CENTRAL-PO",
            supplier=supplier,
            branch=self.branch_a,
            warehouse=central_warehouse,
            purchase_date=date.today(),
            created_by=self.owner_a,
        )

        membership = self.membership_a()
        membership.branches.set([self.branch_a])

        denied_responses = (
            self.client.get(reverse("sales:detail", args=[sale.public_id])),
            self.client.post(
                reverse("sales:void", args=[sale.public_id]),
                {"reason": "Legacy warehouse mismatch"},
            ),
            self.client.post(
                reverse(
                    "sales:sale_item_update_fabric",
                    args=[sale.public_id, sale_item.pk],
                ),
                {"actual_fabric_used": "4.000"},
            ),
            self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            ),
            self.client.get(reverse("purchases:detail", args=[purchase.public_id])),
            self.client.post(reverse("purchases:cancel", args=[purchase.public_id])),
        )
        for response in denied_responses:
            self.assertIn(response.status_code, (403, 404))

        sale.refresh_from_db()
        sale_item.refresh_from_db()
        purchase.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.COMPLETED)
        self.assertIsNone(sale_item.actual_fabric_used)
        self.assertEqual(purchase.status, Purchase.Status.ORDER)
        self.assertEqual(
            self.client.get(
                reverse("sales:detail", args=[central_sale.public_id])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("purchases:detail", args=[central_purchase.public_id])
            ).status_code,
            200,
        )

    def test_restricted_member_cannot_view_export_or_import_other_branch_stock(self):
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Inventory Restricted Branch",
            code="ADV-INV-B2",
        )
        other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Inventory Restricted Warehouse",
            code="ADV-INV-W2",
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=other_warehouse,
            product=self.fabric,
            variant=self.color,
            quantity=D("7.000"),
            unit_cost=D("2.000"),
            user=self.owner_a,
        )
        membership = self.membership_a()
        membership.branches.set([self.branch_a])
        stock_before = self.fabric_stock(other_warehouse)
        movements_before = (
            StockMovement.objects.for_business(self.business_a)
            .filter(warehouse=other_warehouse)
            .count()
        )

        stock_page = self.client.get(reverse("inventory:stock_list"))
        export = self.client.get(
            reverse("inventory:export"),
            {"warehouse": other_warehouse.id, "branch": other_branch.id},
        )
        csv_file = SimpleUploadedFile(
            "restricted.csv",
            (
                "variant sku,branch,warehouse,quantity\n"
                f"{self.color.sku},{other_branch.name},"
                f"{other_warehouse.name},1.000\n"
            ).encode(),
            content_type="text/csv",
        )
        imported = self.client.post(
            reverse("inventory:import"),
            {"file": csv_file, "mode": "add"},
        )

        self.assertEqual(stock_page.status_code, 200)
        self.assertNotContains(stock_page, other_warehouse.name)
        self.assertEqual(export.status_code, 200)
        self.assertNotIn(other_warehouse.name, export.content.decode())
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.context["results"]["summary"]["failed"], 1)
        self.assertEqual(self.fabric_stock(other_warehouse), stock_before)
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a)
            .filter(warehouse=other_warehouse)
            .count(),
            movements_before,
        )
