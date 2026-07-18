import json
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Warehouse
from apps.catalog import services as catalog_services
from apps.catalog.forms import QuickProductForm
from apps.catalog.models import Product, ProductVariant, Unit
from apps.inventory import services as inventory_services
from apps.inventory import workflows as inventory_workflows
from apps.inventory.models import StockAdjustment, StockMovement, StockTransfer
from apps.purchases import services as purchase_services
from apps.sales import services as sales_services
from apps.sales.models import Sale, SaleReturn
from apps.subscriptions.exceptions import ModuleAccessDenied
from apps.subscriptions.models import Subscription
from apps.suppliers.models import Supplier

from .base import TenantTestCase

D = Decimal


class TailoringRolesBarcodeEnforcementTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.subscription = Subscription.objects.select_related("plan").get(
            business=self.business_a
        )
        self.plan = self.subscription.plan
        self.set_modules(
            feature_sales=True,
            feature_inventory=True,
            feature_tailoring_module=True,
            feature_barcode_printing=True,
            feature_custom_roles=True,
            max_users=0,
        )
        self.tailoring_product = Product.objects.create(
            business=self.business_a,
            name="Enforcement Tailoring Garment",
            sku="ENF-TAIL",
            barcode="ENF-TAIL-BARCODE",
            product_type=Product.Type.NON_STOCK,
            track_inventory=False,
            sale_price=D("12.000"),
            is_tailoring_item=True,
            estimated_adult_fabric=D("3.500"),
            estimated_child_fabric=D("2.250"),
        )
        self.client.force_login(self.owner_a)

    def set_modules(self, **values):
        for field, value in values.items():
            setattr(self.plan, field, value)
        self.plan.save(update_fields=list(values))

    def tailoring_sale(self):
        return self.make_sale(
            items=[
                {
                    "product": self.tailoring_product,
                    "quantity": D("1"),
                    "unit_price": D("12.000"),
                    "garment_classification": "adult",
                    "collection_type": "normal",
                }
            ],
            payments=[{"method": self.cash_a, "amount": D("12.000")}],
            delivery_date=timezone.localdate(),
        )

    def employee_payload(self, *, email, role, full_name="Enforcement User"):
        return {
            "full_name": full_name,
            "email": email,
            "phone": "12345678",
            "password": "StrongPass123!",
            "role": str(role.pk),
            "branches": [str(self.branch_a.pk)],
            "is_active": "on",
        }

    def stocked_tailoring_product(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Stocked Tailoring Enforcement Product",
            sku="STOCKED-TAIL-ENF",
            product_type=Product.Type.STANDARD,
            track_inventory=True,
            is_tailoring_item=True,
        )
        inventory_services.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=product,
            quantity=D("10.000"),
            unit_cost=D("2.000"),
            user=self.owner_a,
            membership=self.membership_a(),
        )
        return product

    def test_tailoring_off_blocks_direct_transfer_lifecycle_mutations(self):
        product = self.stocked_tailoring_product()
        destination = Warehouse.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            name="Tailoring Enforcement Destination",
            code="TAIL-DEST",
        )
        rows = [{"product": product, "variant": None, "quantity": D("2.000")}]
        draft = inventory_workflows.create_transfer(
            business=self.business_a,
            from_warehouse=self.warehouse_a,
            to_warehouse=destination,
            rows=rows,
            user=self.owner_a,
            membership=self.membership_a(),
        )
        dispatched = inventory_workflows.create_transfer(
            business=self.business_a,
            from_warehouse=self.warehouse_a,
            to_warehouse=destination,
            rows=rows,
            user=self.owner_a,
            membership=self.membership_a(),
        )
        inventory_workflows.dispatch_transfer(
            transfer=dispatched,
            user=self.owner_a,
            membership=self.membership_a(),
        )
        source_before = inventory_services.get_stock(
            self.business_a, self.warehouse_a, product
        )
        destination_before = inventory_services.get_stock(
            self.business_a, destination, product
        )
        transfer_count = StockTransfer.objects.for_business(self.business_a).count()
        self.set_modules(feature_tailoring_module=False)

        with self.assertRaises(ModuleAccessDenied):
            inventory_workflows.create_transfer(
                business=self.business_a,
                from_warehouse=self.warehouse_a,
                to_warehouse=destination,
                rows=rows,
                user=self.owner_a,
                membership=self.membership_a(),
            )
        for workflow, transfer in (
            (inventory_workflows.dispatch_transfer, draft),
            (inventory_workflows.cancel_transfer, draft),
            (inventory_workflows.receive_transfer, dispatched),
            (inventory_workflows.cancel_transfer, dispatched),
        ):
            with self.subTest(workflow=workflow.__name__, transfer=transfer.status):
                with self.assertRaises(ModuleAccessDenied):
                    workflow(
                        transfer=transfer,
                        user=self.owner_a,
                        membership=self.membership_a(),
                    )

        draft.refresh_from_db()
        dispatched.refresh_from_db()
        self.assertEqual(draft.status, StockTransfer.Status.DRAFT)
        self.assertEqual(dispatched.status, StockTransfer.Status.DISPATCHED)
        self.assertEqual(
            StockTransfer.objects.for_business(self.business_a).count(), transfer_count
        )
        self.assertEqual(
            inventory_services.get_stock(self.business_a, self.warehouse_a, product),
            source_before,
        )
        self.assertEqual(
            inventory_services.get_stock(self.business_a, destination, product),
            destination_before,
        )

    def test_tailoring_off_blocks_direct_adjustment_lifecycle_mutations(self):
        product = self.stocked_tailoring_product()
        pending = inventory_workflows.create_adjustment(
            business=self.business_a,
            warehouse=self.warehouse_a,
            reason=StockAdjustment.Reason.OTHER,
            rows=[{"product": product, "variant": None, "quantity": D("1.000")}],
            user=self.owner_a,
            requires_approval=True,
            membership=self.membership_a(),
        )
        stock_before = inventory_services.get_stock(
            self.business_a, self.warehouse_a, product
        )
        adjustment_count = StockAdjustment.objects.for_business(self.business_a).count()
        self.set_modules(feature_tailoring_module=False)

        with self.assertRaises(ModuleAccessDenied):
            inventory_workflows.create_adjustment(
                business=self.business_a,
                warehouse=self.warehouse_a,
                reason=StockAdjustment.Reason.OTHER,
                rows=[{"product": product, "variant": None, "quantity": D("1.000")}],
                user=self.owner_a,
                membership=self.membership_a(),
            )
        for workflow in (
            inventory_workflows.approve_adjustment,
            inventory_workflows.reject_adjustment,
        ):
            with self.subTest(workflow=workflow.__name__):
                with self.assertRaises(ModuleAccessDenied):
                    workflow(
                        adjustment=pending,
                        user=self.owner_a,
                        membership=self.membership_a(),
                    )

        pending.refresh_from_db()
        self.assertEqual(pending.status, StockAdjustment.Status.PENDING)
        self.assertEqual(
            StockAdjustment.objects.for_business(self.business_a).count(), adjustment_count
        )
        self.assertEqual(
            inventory_services.get_stock(self.business_a, self.warehouse_a, product),
            stock_before,
        )

    def test_counts_exclude_tailoring_when_off_and_block_downgrade_corrections(self):
        product = self.stocked_tailoring_product()
        self.set_modules(feature_tailoring_module=False)
        retail_count = inventory_workflows.start_count(
            business=self.business_a,
            warehouse=self.warehouse_a,
            user=self.owner_a,
            membership=self.membership_a(),
        )
        self.assertTrue(retail_count.items.filter(product=self.product_a).exists())
        self.assertFalse(retail_count.items.filter(product=product).exists())
        retail_item = retail_count.items.get(product=self.product_a)
        retail_item.counted_quantity = D("99.000")
        retail_item.save(update_fields=["counted_quantity", "updated_at"])
        inventory_workflows.approve_count(
            count=retail_count,
            user=self.owner_a,
            membership=self.membership_a(),
        )
        self.assertEqual(
            inventory_services.get_stock(
                self.business_a, self.warehouse_a, self.product_a
            ),
            D("99.000"),
        )

        self.set_modules(feature_tailoring_module=True)
        mixed_count = inventory_workflows.start_count(
            business=self.business_a,
            warehouse=self.warehouse_a,
            user=self.owner_a,
            membership=self.membership_a(),
        )
        mixed_retail = mixed_count.items.get(product=self.product_a)
        mixed_tailoring = mixed_count.items.get(product=product)
        mixed_retail.counted_quantity = D("98.000")
        mixed_retail.save(update_fields=["counted_quantity", "updated_at"])
        mixed_tailoring.counted_quantity = D("9.000")
        mixed_tailoring.save(update_fields=["counted_quantity", "updated_at"])
        self.set_modules(feature_tailoring_module=False)

        with self.assertRaises(ModuleAccessDenied):
            inventory_workflows.approve_count(
                count=mixed_count,
                user=self.owner_a,
                membership=self.membership_a(),
            )

        mixed_count.refresh_from_db()
        self.assertEqual(mixed_count.status, mixed_count.Status.OPEN)
        self.assertEqual(
            inventory_services.get_stock(
                self.business_a, self.warehouse_a, self.product_a
            ),
            D("99.000"),
        )
        self.assertEqual(
            inventory_services.get_stock(self.business_a, self.warehouse_a, product),
            D("10.000"),
        )

    def test_tailoring_off_preserves_retail_pos_and_hides_tailoring_catalog(self):
        held = self.client.post(
            reverse("sales:pos_hold"),
            data=json.dumps(
                {
                    "branch_id": self.branch_a.id,
                    "label": "Tailoring cart before downgrade",
                    "cart": {
                        "checkout_token": "tailoring-held-before-downgrade",
                        "items": [{"product_id": str(self.tailoring_product.id)}],
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(held.status_code, 200)
        self.set_modules(feature_tailoring_module=False)

        pos = self.client.get(reverse("sales:pos"))
        self.assertEqual(pos.status_code, 200)
        self.assertEqual(len(pos.context["held_sales"]), 0)
        self.assertEqual(self.client.get(reverse("sales:pos_held_list")).json()["held"], [])
        products = self.client.get(reverse("sales:pos_products")).json()["items"]
        product_ids = {row["product_id"] for row in products}
        self.assertIn(self.product_a.id, product_ids)
        self.assertNotIn(self.tailoring_product.id, product_ids)

        retail_scan = self.client.get(
            reverse("sales:pos_barcode"), {"code": self.product_a.barcode}
        )
        tailoring_scan = self.client.get(
            reverse("sales:pos_barcode"),
            {"code": self.tailoring_product.barcode},
        )
        self.assertTrue(retail_scan.json()["found"])
        self.assertFalse(tailoring_scan.json()["found"])

        product_list = self.client.get(reverse("catalog:product_list"))
        self.assertContains(product_list, self.product_a.name)
        self.assertNotContains(product_list, self.tailoring_product.name)
        exported = self.client.get(reverse("catalog:product_export"), {"format": "csv"})
        self.assertEqual(exported.status_code, 200)
        self.assertContains(exported, self.product_a.name)
        self.assertNotContains(exported, self.tailoring_product.name)
        self.assertEqual(
            self.client.get(
                reverse(
                    "catalog:product_detail",
                    args=[self.tailoring_product.public_id],
                )
            ).status_code,
            403,
        )

        retail_form = self.client.get(reverse("catalog:product_create"))
        self.assertEqual(retail_form.status_code, 200)
        self.assertNotContains(retail_form, 'name="is_tailoring_item"')
        self.assertNotContains(retail_form, "Tailoring garment")

    def test_tailoring_off_hides_historical_inventory_reads_and_blocks_import(self):
        tailoring_product = self.stocked_tailoring_product()
        tailoring_product.reorder_level = D("20.000")
        tailoring_product.save(update_fields=["reorder_level", "updated_at"])
        self.product_a.reorder_level = D("110.000")
        self.product_a.save(update_fields=["reorder_level", "updated_at"])
        historical_count = inventory_workflows.start_count(
            business=self.business_a,
            warehouse=self.warehouse_a,
            user=self.owner_a,
            membership=self.membership_a(),
        )
        tailoring_count_item = historical_count.items.get(product=tailoring_product)
        retail_only_value = inventory_services.stock_value(
            self.business_a,
            include_tailoring=False,
        )
        retail_stock_before = inventory_services.get_stock(
            self.business_a, self.warehouse_a, self.product_a
        )
        tailoring_stock_before = inventory_services.get_stock(
            self.business_a, self.warehouse_a, tailoring_product
        )
        self.set_modules(feature_tailoring_module=False)

        for url_name in ("inventory:stock_list", "inventory:movement_list"):
            with self.subTest(url=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, self.product_a.name)
                self.assertNotContains(response, tailoring_product.name)

        exported = self.client.get(reverse("inventory:export"), {"format": "csv"})
        self.assertEqual(exported.status_code, 200)
        self.assertContains(exported, self.product_a.name)
        self.assertNotContains(exported, tailoring_product.name)

        for key in ("current_stock", "low_stock", "stock_movements"):
            with self.subTest(report=key):
                response = self.client.get(reverse("reports:view", args=[key]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, self.product_a.name)
                self.assertNotContains(response, tailoring_product.name)

        count_detail = self.client.get(
            reverse("inventory:count_detail", args=[historical_count.public_id])
        )
        self.assertEqual(count_detail.status_code, 200)
        self.assertContains(count_detail, self.product_a.name)
        self.assertNotContains(count_detail, tailoring_product.name)
        save_hidden_count = self.client.post(
            reverse("inventory:count_detail", args=[historical_count.public_id]),
            {
                "action": "save",
                f"counted_{tailoring_count_item.pk}": "9.000",
            },
        )
        self.assertEqual(save_hidden_count.status_code, 302)
        tailoring_count_item.refresh_from_db()
        self.assertIsNone(tailoring_count_item.counted_quantity)

        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.context["kpis"]["stock_value"], retail_only_value)
        self.assertNotIn(
            tailoring_product.name,
            {row.product.name for row in dashboard.context["widgets"]["low_stock_items"]},
        )
        self.assertEqual(sum(dashboard.context["chart_movement"]["stock_in"]), 100.0)

        with self.assertRaises(ModuleAccessDenied):
            inventory_services.import_inventory(
                business=self.business_a,
                rows=[
                    {
                        "sku": self.product_a.sku,
                        "warehouse": self.warehouse_a.name,
                        "quantity": "1.000",
                    },
                    {
                        "sku": tailoring_product.sku,
                        "warehouse": self.warehouse_a.name,
                        "quantity": "1.000",
                    },
                ],
                mode="add",
                user=self.owner_a,
                membership=self.membership_a(),
            )
        tailoring_movements_before = StockMovement.objects.for_business(
            self.business_a
        ).filter(product=tailoring_product).count()
        with self.assertRaises(ModuleAccessDenied):
            inventory_services.set_opening_stock(
                business=self.business_a,
                warehouse=self.warehouse_a,
                product=tailoring_product,
                quantity=D("1.000"),
                unit_cost=D("2.000"),
                user=self.owner_a,
                membership=self.membership_a(),
            )
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a)
            .filter(product=tailoring_product)
            .count(),
            tailoring_movements_before,
        )
        self.assertEqual(
            inventory_services.get_stock(
                self.business_a, self.warehouse_a, self.product_a
            ),
            retail_stock_before,
        )
        self.assertEqual(
            inventory_services.get_stock(
                self.business_a, self.warehouse_a, tailoring_product
            ),
            tailoring_stock_before,
        )

    def test_tailoring_checkout_http_and_service_bypasses_are_denied_atomically(self):
        self.set_modules(feature_tailoring_module=False)
        before = Sale.objects.for_business(self.business_a).count()

        with self.assertRaises(ModuleAccessDenied):
            self.tailoring_sale()
        self.assertEqual(Sale.objects.for_business(self.business_a).count(), before)

        response = self.client.post(
            reverse("sales:pos_checkout"),
            data=json.dumps(
                {
                    "branch_id": self.branch_a.id,
                    "customer_id": self.walk_in_a.id,
                    "checkout_token": "tailoring-off-http-token",
                    "items": [
                        {
                            "product_id": self.tailoring_product.id,
                            "quantity": "1",
                            "unit_price": "12.000",
                            "garment_classification": "adult",
                            "collection_type": "normal",
                        }
                    ],
                    "payments": [{"method_id": self.cash_a.id, "amount": "12.000"}],
                    "delivery_date": timezone.localdate().isoformat(),
                    "invoice_discount": "0",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Sale.objects.for_business(self.business_a).count(), before)

    def test_tailoring_job_cards_and_workshop_update_require_tailoring(self):
        sale = self.tailoring_sale()
        item = sale.items.get()
        self.set_modules(feature_tailoring_module=False)

        bulk_url = reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
        item_url = reverse(
            "sales:sale_item_workshop_job_card_pdf",
            args=[sale.public_id, item.id],
        )
        actual_url = reverse("sales:sale_item_update_fabric", args=[sale.public_id, item.id])
        self.assertEqual(self.client.get(bulk_url).status_code, 403)
        self.assertEqual(self.client.get(item_url).status_code, 403)
        self.assertEqual(
            self.client.post(actual_url, {"actual_fabric_used": "3.500"}).status_code,
            403,
        )
        with self.assertRaises(ModuleAccessDenied):
            sales_services.update_actual_fabric(
                sale_item=item,
                actual_fabric_used="3.500",
                user=self.owner_a,
                membership=self.membership_a(),
            )
        item.refresh_from_db()
        self.assertIsNone(item.actual_fabric_used)

        detail = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, "Download All Job Cards")
        self.assertNotContains(detail, 'name="actual_fabric_used"')
        self.assertNotContains(detail, "Garment: Adult")
        self.assertNotContains(detail, "<th>Priority</th>", html=True)
        self.assertNotContains(detail, "<div class=\"card-header\">Delivery</div>", html=True)

    def test_tailoring_sale_and_purchase_mutations_recheck_after_downgrade(self):
        sale = self.tailoring_sale()
        retail_sale = self.make_sale()
        sale_item = sale.items.get()
        supplier = Supplier.objects.create(
            business=self.business_a,
            name="Tailoring Enforcement Supplier",
            code="TAIL-ENF-SUP",
        )
        purchase = purchase_services.create_purchase(
            business=self.business_a,
            supplier=supplier,
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            rows=[{
                "product": self.tailoring_product,
                "variant": None,
                "quantity": D("2.000"),
                "unit_cost": D("4.000"),
            }],
            user=self.owner_a,
            membership=self.membership_a(),
            purchase_date=timezone.localdate(),
        )
        purchase_item = purchase.items.get()
        self.set_modules(feature_tailoring_module=False)

        with self.assertRaises(ModuleAccessDenied):
            sales_services.set_delivery_status(
                sale=sale,
                status=Sale.DeliveryStatus.DELIVERED,
                user=self.owner_a,
                membership=self.membership_a(),
            )
        with self.assertRaises(ModuleAccessDenied):
            sales_services.set_delivery_status(
                sale=retail_sale,
                status=Sale.DeliveryStatus.DELIVERED,
                user=self.owner_a,
                membership=self.membership_a(),
            )
        with self.assertRaises(ModuleAccessDenied):
            sales_services.void_sale(
                sale=sale,
                user=self.owner_a,
                reason="Must remain unchanged",
                membership=self.membership_a(),
            )
        with self.assertRaises(ModuleAccessDenied):
            sales_services.process_return(
                sale=sale,
                items=[{"sale_item": sale_item, "quantity": D("1.000")}],
                refund_method=SaleReturn.RefundMethod.CASH,
                user=self.owner_a,
                membership=self.membership_a(),
            )
        with self.assertRaises(ModuleAccessDenied):
            purchase_services.receive_purchase(
                purchase=purchase,
                quantities={purchase_item.pk: D("1.000")},
                user=self.owner_a,
                membership=self.membership_a(),
            )

        self.set_modules(feature_tailoring_module=True)
        purchase_services.receive_purchase(
            purchase=purchase,
            quantities={purchase_item.pk: D("1.000")},
            user=self.owner_a,
            membership=self.membership_a(),
        )
        self.set_modules(feature_tailoring_module=False)
        with self.assertRaises(ModuleAccessDenied):
            purchase_services.return_purchase(
                purchase=purchase,
                quantities={purchase_item.pk: D("1.000")},
                user=self.owner_a,
                membership=self.membership_a(),
            )

        sale.refresh_from_db()
        retail_sale.refresh_from_db()
        purchase_item.refresh_from_db()
        self.assertNotEqual(sale.delivery_status, Sale.DeliveryStatus.DELIVERED)
        self.assertNotEqual(
            retail_sale.delivery_status,
            Sale.DeliveryStatus.DELIVERED,
        )
        self.assertNotEqual(sale.status, Sale.Status.VOIDED)
        self.assertFalse(sale.returns.exists())
        self.assertEqual(purchase_item.quantity_received, D("1.000"))
        self.assertFalse(purchase.purchase_returns.exists())

    def test_tailoring_enabled_preserves_job_card_and_workshop_behavior(self):
        sale = self.tailoring_sale()
        item = sale.items.get()

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF-1.4\n"):
            response = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        updated = sales_services.update_actual_fabric(
            sale_item=item,
            actual_fabric_used="3.400",
            user=self.owner_a,
            membership=self.membership_a(),
        )
        self.assertEqual(updated.actual_fabric_used, D("3.400"))

    def test_catalog_services_cannot_create_or_change_tailoring_when_disabled(self):
        self.set_modules(feature_tailoring_module=False)
        membership = self.membership_a()
        unsaved = Product(
            business=self.business_a,
            name="Forged Tailoring Product",
            product_type=Product.Type.NON_STOCK,
            is_tailoring_item=True,
        )
        with self.assertRaises(ModuleAccessDenied):
            catalog_services.save_product(
                product=unsaved,
                business=self.business_a,
                user=self.owner_a,
                membership=membership,
            )
        self.assertFalse(
            Product.objects.for_business(self.business_a)
            .filter(name="Forged Tailoring Product")
            .exists()
        )

        meter = Unit.objects.for_business(self.business_a).get(is_meter=True)
        forged_meter = Product(
            business=self.business_a,
            name="Forged Meter Product",
            product_type=Product.Type.STANDARD,
            unit=meter,
            track_inventory=True,
            is_tailoring_item=False,
        )
        with self.assertRaises(ModuleAccessDenied):
            catalog_services.save_product(
                product=forged_meter,
                business=self.business_a,
                user=self.owner_a,
                membership=membership,
            )
        self.assertFalse(
            Product.objects.for_business(self.business_a)
            .filter(name="Forged Meter Product")
            .exists()
        )

        retail = Product(
            business=self.business_a,
            name="Allowed Retail Product",
            product_type=Product.Type.NON_STOCK,
        )
        retail = catalog_services.save_product(
            product=retail,
            business=self.business_a,
            user=self.owner_a,
            membership=membership,
        )
        self.assertIsNotNone(retail.pk)

        with self.assertRaises(ModuleAccessDenied):
            catalog_services.save_variant(
                variant=ProductVariant(name="Forbidden Tailoring Variant"),
                product=self.tailoring_product,
                user=self.owner_a,
                membership=membership,
            )

        with self.assertRaises(ModuleAccessDenied):
            catalog_services.import_products(
                business=self.business_a,
                rows=[
                    {
                        "product name": "Retail Before Forbidden Meter",
                        "sku": "RETAIL-BEFORE-FORBIDDEN-METER",
                        "product type": "standard",
                    },
                    {
                        "product name": "Forbidden Meter Import",
                        "sku": "FORBIDDEN-METER",
                        "product type": "standard",
                        "unit": "Meter",
                    },
                ],
                match_by="sku",
                user=self.owner_a,
                membership=membership,
            )
        self.assertFalse(
            Product.objects.for_business(self.business_a).filter(sku="FORBIDDEN-METER").exists()
        )
        self.assertFalse(
            Product.objects.for_business(self.business_a)
            .filter(sku="RETAIL-BEFORE-FORBIDDEN-METER")
            .exists()
        )

    def test_purchase_quick_add_cannot_bypass_tailoring_with_meter_unit(self):
        meter_unit = Unit.objects.for_business(self.business_a).filter(is_meter=True).first()
        if meter_unit is None:
            meter_unit = Unit.objects.create(
                business=self.business_a,
                name="Meter",
                abbreviation="m",
                is_meter=True,
            )
        payload = {
            "name": "Quick Meter Fabric",
            "sku": "QUICK-METER-FABRIC",
            "category": "",
            "unit": str(meter_unit.pk),
            "purchase_price": "2.000",
            "sale_price": "0",
            "tax_rate": "",
            "price_includes_tax": "",
            "track_inventory": "on",
        }
        self.set_modules(feature_tailoring_module=False)

        response = self.client.post(reverse("purchases:quick_add_product"), payload)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            Product.objects.for_business(self.business_a).filter(sku="QUICK-METER-FABRIC").exists()
        )

        forged_form = QuickProductForm(
            self.business_a,
            payload,
            tailoring_enabled=True,
        )
        self.assertTrue(forged_form.is_valid(), forged_form.errors)
        with self.assertRaises(ModuleAccessDenied):
            purchase_services.quick_add_product(
                business=self.business_a,
                form=forged_form,
                user=self.owner_a,
                membership=self.membership_a(),
            )

    def test_barcode_printing_off_blocks_outputs_but_not_fields_or_scanning(self):
        self.set_modules(feature_barcode_printing=False)

        detail = self.client.get(reverse("catalog:product_detail", args=[self.product_a.public_id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, self.product_a.barcode)
        self.assertNotContains(detail, "Print labels")
        self.assertNotContains(
            detail,
            reverse("catalog:product_barcode", args=[self.product_a.public_id]),
        )
        self.assertEqual(
            self.client.get(
                reverse("catalog:product_labels", args=[self.product_a.public_id])
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("catalog:product_barcode", args=[self.product_a.public_id])
            ).status_code,
            403,
        )
        scan = self.client.get(reverse("sales:pos_barcode"), {"code": self.product_a.barcode})
        self.assertTrue(scan.json()["found"])

    def test_barcode_printing_enabled_preserves_outputs_and_tenant_secrecy(self):
        labels = self.client.get(reverse("catalog:product_labels", args=[self.product_a.public_id]))
        svg = self.client.get(reverse("catalog:product_barcode", args=[self.product_a.public_id]))
        foreign = self.client.get(
            reverse("catalog:product_labels", args=[self.product_b.public_id])
        )
        self.assertEqual(labels.status_code, 200)
        self.assertEqual(svg.status_code, 200)
        self.assertEqual(svg["Content-Type"], "image/svg+xml")
        self.assertEqual(foreign.status_code, 404)

    def test_custom_roles_off_preserves_system_role_staff_administration(self):
        custom_role = Role.objects.create(
            business=self.business_a,
            name="Existing Custom Enforcement Role",
            permissions=["sales.view"],
        )
        self.set_modules(feature_custom_roles=False)

        role_list = self.client.get(reverse("accounts:role_list"))
        self.assertEqual(role_list.status_code, 200)
        self.assertContains(role_list, "Cashier")
        self.assertContains(role_list, custom_role.name)
        self.assertNotContains(role_list, reverse("accounts:role_create"))
        self.assertNotContains(role_list, "available on higher plans")
        self.assertEqual(self.client.get(reverse("accounts:role_create")).status_code, 403)
        self.assertEqual(
            self.client.get(
                reverse("accounts:role_edit", args=[custom_role.public_id])
            ).status_code,
            403,
        )

        cashier = Role.objects.for_business(self.business_a).get(name="Cashier")
        self.assertEqual(
            self.client.get(reverse("accounts:role_edit", args=[cashier.public_id])).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                reverse("accounts:role_edit", args=[cashier.public_id]),
                {"name": cashier.name, "permissions": cashier.permissions},
            ).status_code,
            302,
        )

        custom_assignment = self.client.post(
            reverse("accounts:user_create"),
            self.employee_payload(email="forbidden-custom-role@example.com", role=custom_role),
        )
        self.assertEqual(custom_assignment.status_code, 403)
        self.assertFalse(
            Membership.objects.filter(
                business=self.business_a,
                user__email="forbidden-custom-role@example.com",
            ).exists()
        )

        system_assignment = self.client.post(
            reverse("accounts:user_create"),
            self.employee_payload(email="allowed-system-role@example.com", role=cashier),
        )
        self.assertEqual(system_assignment.status_code, 302)
        membership = Membership.objects.get(
            business=self.business_a,
            user__email="allowed-system-role@example.com",
        )
        self.assertEqual(membership.role, cashier)
        self.assertTrue(Role.objects.filter(pk=custom_role.pk).exists())

    def test_existing_custom_assignment_is_preserved_when_module_is_off(self):
        custom_role = Role.objects.create(
            business=self.business_a,
            name="Preserved Custom Role",
            permissions=["sales.view"],
        )
        user = User.objects.create_user(
            email="preserved-custom@example.com",
            password="StrongPass123!",
            full_name="Preserved Custom User",
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=user,
            role=custom_role,
        )
        membership.branches.add(self.branch_a)
        self.set_modules(feature_custom_roles=False)

        payload = self.employee_payload(
            email=user.email,
            role=custom_role,
            full_name="Preserved Custom User Updated",
        )
        payload["password"] = ""
        response = self.client.post(
            reverse("accounts:user_edit", args=[membership.public_id]), payload
        )
        self.assertEqual(response.status_code, 302)
        membership.refresh_from_db()
        self.assertEqual(membership.role, custom_role)
        self.assertEqual(membership.user.full_name, "Preserved Custom User Updated")

    def test_custom_roles_enabled_allows_custom_mutation_and_system_role_edit(self):
        create = self.client.post(
            reverse("accounts:role_create"),
            {"name": "Enabled Custom Role", "permissions": ["sales.view"]},
        )
        self.assertEqual(create.status_code, 302)
        custom_role = Role.objects.get(business=self.business_a, name="Enabled Custom Role")
        self.assertFalse(custom_role.is_system)

        edit = self.client.post(
            reverse("accounts:role_edit", args=[custom_role.public_id]),
            {"name": "Enabled Custom Role Updated", "permissions": ["sales.view"]},
        )
        self.assertEqual(edit.status_code, 302)
        custom_role.refresh_from_db()
        self.assertEqual(custom_role.name, "Enabled Custom Role Updated")

        cashier = Role.objects.for_business(self.business_a).get(name="Cashier")
        self.assertEqual(
            self.client.get(reverse("accounts:role_edit", args=[cashier.public_id])).status_code,
            200,
        )

        owner = Role.objects.for_business(self.business_a).get(is_owner=True)
        self.assertEqual(
            self.client.get(reverse("accounts:role_edit", args=[owner.public_id])).status_code,
            302,
        )

    def test_read_only_allows_safe_outputs_and_blocks_tailoring_role_writes(self):
        sale = self.tailoring_sale()
        item = sale.items.get()
        self.subscription.status = Subscription.Status.PAST_DUE
        self.subscription.save(update_fields=["status"])

        labels = self.client.get(reverse("catalog:product_labels", args=[self.product_a.public_id]))
        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF-1.4\n"):
            job_card = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
        actual = self.client.post(
            reverse("sales:sale_item_update_fabric", args=[sale.public_id, item.id]),
            {"actual_fabric_used": "3.500"},
        )
        self.assertEqual(labels.status_code, 200)
        self.assertEqual(job_card.status_code, 200)
        self.assertEqual(actual.status_code, 403)
        self.assertEqual(self.client.get(reverse("accounts:role_create")).status_code, 403)
