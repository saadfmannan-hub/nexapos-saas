"""Focused regression coverage for the Customer Fabric POS workflow."""

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.api.serializers import SaleItemSerializer
from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product, ProductVariant, Unit
from apps.customers.models import Customer
from apps.inventory import services as inventory
from apps.inventory.models import StockLevel, StockMovement
from apps.reports.queries import sales_detailed
from apps.sales import services as sales_services
from apps.sales.models import HeldSale, Sale, SaleItem, SaleReturn
from apps.sales.services import SaleError
from apps.sales.views import _invoice_context, _job_card_context

from .base import TenantTestCase

D = Decimal
ZERO = D("0")
AUTO = object()


class CustomerSuppliedFabricTests(TenantTestCase):
    """The numbered tests map directly to the twenty requested regressions."""

    def setUp(self):
        settings_obj = self.business_a.settings
        settings_obj.allow_sale_without_shift = True
        settings_obj.vat_enabled = False
        settings_obj.prices_include_tax = False
        settings_obj.save(
            update_fields=[
                "allow_sale_without_shift",
                "vat_enabled",
                "prices_include_tax",
            ]
        )
        self.client.force_login(self.owner_a)

        self.meter = Unit.objects.for_business(self.business_a).get(name="Meter")
        self.piece = Unit.objects.for_business(self.business_a).get(name="Piece")
        self.fabric = Product.objects.create(
            business=self.business_a,
            name="Customer Fabric Test Cloth",
            sku="CUSTOMER-FABRIC-CLOTH",
            product_type=Product.Type.VARIANT,
            unit=self.meter,
            purchase_price=D("4.000"),
            sale_price=D("25.000"),
            track_inventory=True,
            is_tailoring_item=True,
        )
        self.color = ProductVariant.objects.create(
            business=self.business_a,
            product=self.fabric,
            name="Business Fabric Color",
            sku="CUSTOMER-FABRIC-COLOR",
            purchase_price=D("4.000"),
            sale_price=D("25.000"),
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.fabric,
            variant=self.color,
            quantity=D("10.000"),
            unit_cost=D("4.000"),
            user=self.owner_a,
        )

        self.retail = Product.objects.create(
            business=self.business_a,
            name="Finished Retail Item",
            sku="CUSTOMER-FABRIC-RETAIL",
            product_type=Product.Type.STANDARD,
            unit=self.piece,
            purchase_price=D("3.000"),
            sale_price=D("8.000"),
            track_inventory=True,
            is_tailoring_item=False,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.retail,
            quantity=D("5.000"),
            unit_cost=D("3.000"),
            user=self.owner_a,
        )

    def service_meter_line(self, **overrides):
        line = {
            "product": self.fabric,
            "variant": self.color,
            "quantity": D("1"),
            "unit_price": D("25.000"),
            "discount_amount": D("0"),
            "fabric_meter_used": "3.500",
            "garment_classification": "adult",
            "collection_type": "premium",
            "tailoring_details": {"customer_notes": "Customer-owned cloth"},
        }
        line.update(overrides)
        return line

    def service_customer_line(self, **overrides):
        line = self.service_meter_line(customer_supplied_fabric=True)
        line.pop("fabric_meter_used", None)
        line.update(overrides)
        return line

    def http_meter_line(self, **overrides):
        line = {
            "product_id": self.fabric.id,
            "variant_id": self.color.id,
            "quantity": "1",
            "unit_price": "25.000",
            "fabric_meter_used": "3.500",
            "garment_classification": "adult",
            "collection_type": "premium",
            "tailoring_details": {"customer_notes": "Customer-owned cloth"},
        }
        line.update(overrides)
        return line

    def http_customer_line(self, **overrides):
        line = self.http_meter_line(customer_supplied_fabric=True)
        line.pop("fabric_meter_used", None)
        line.update(overrides)
        return line

    @staticmethod
    def raw_total(items):
        return sum(
            (
                D(str(line.get("quantity", 0)))
                * D(str(line.get("unit_price", 0)))
            )
            for line in items
        )

    def complete(
        self,
        items=None,
        *,
        payment_amount=None,
        invoice_discount=ZERO,
        delivery_date=AUTO,
        branch=None,
        warehouse=None,
        customer=None,
        cashier=None,
        membership=AUTO,
        checkout_token=None,
    ):
        items = list(items if items is not None else [self.service_customer_line()])
        branch = branch or self.branch_a
        warehouse = warehouse or self.warehouse_a
        customer = customer or self.walk_in_a
        cashier = cashier or self.owner_a
        if membership is AUTO:
            membership = self.business_a.memberships.get(user=cashier)
        if delivery_date is AUTO:
            delivery_date = (
                timezone.localdate()
                if any(
                    line["product"].is_meter_tailoring
                    or line["product"].is_legacy_tailoring
                    for line in items
                )
                else None
            )
        if payment_amount is None:
            payment_amount = self.raw_total(items) - D(str(invoice_discount))
        return sales_services.complete_sale(
            business=self.business_a,
            branch=branch,
            warehouse=warehouse,
            cashier=cashier,
            customer=customer,
            items=items,
            payments=[{"method": self.cash_a, "amount": payment_amount}],
            membership=membership,
            invoice_discount=invoice_discount,
            delivery_date=delivery_date,
            checkout_token=checkout_token,
        )

    def checkout(
        self,
        items,
        *,
        payment_amount=None,
        invoice_discount="0",
        delivery_date=AUTO,
        branch=None,
        customer=None,
        held_id=None,
        token=None,
    ):
        branch = branch or self.branch_a
        customer = customer or self.walk_in_a
        if delivery_date is AUTO:
            delivery_date = (
                str(timezone.localdate())
                if any(
                    line.get("product_id") == self.fabric.id
                    or line.get("customer_supplied_fabric") is True
                    for line in items
                )
                else None
            )
        if payment_amount is None:
            payment_amount = self.raw_total(items) - D(str(invoice_discount))
        payload = {
            "branch_id": branch.id,
            "customer_id": customer.id,
            "items": items,
            "payments": [
                {"method_id": self.cash_a.id, "amount": str(payment_amount)}
            ],
            "invoice_discount": str(invoice_discount),
            "delivery_date": delivery_date,
            "priority": "normal",
            "checkout_token": token or f"customer-fabric-{uuid4().hex}",
        }
        if held_id is not None:
            payload["held_id"] = held_id
        return self.client.post(
            reverse("sales:pos_checkout"),
            json.dumps(payload),
            content_type="application/json",
        )

    def sale_from_response(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"], response.json())
        return Sale.objects.for_business(self.business_a).get(
            public_id=response.json()["sale"]["public_id"]
        )

    def fabric_stock(self):
        return inventory.get_stock(
            self.business_a,
            self.warehouse_a,
            self.fabric,
            self.color,
        )

    def test_01_customer_fabric_tailoring_checkout_completes(self):
        sale = self.sale_from_response(self.checkout([self.http_customer_line()]))
        item = sale.items.get()

        self.assertTrue(item.customer_supplied_fabric)
        self.assertEqual(item.quantity, D("1.000"))
        self.assertEqual(item.garment_classification, "adult")
        self.assertEqual(item.collection_type, "premium")
        self.assertEqual(sale.delivery_date, timezone.localdate())

    def test_02_customer_fabric_keeps_classification_collection_and_delivery_rules(self):
        cases = (
            (
                self.http_customer_line(garment_classification=""),
                AUTO,
                "items.0.garment_classification",
            ),
            (
                self.http_customer_line(collection_type=""),
                AUTO,
                "items.0.collection_type",
            ),
            (self.http_customer_line(), None, "delivery_date"),
        )
        sale_count = Sale.objects.for_business(self.business_a).count()

        for line, delivery_date, field in cases:
            with self.subTest(field=field):
                response = self.checkout([line], delivery_date=delivery_date)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(field, response.json().get("errors", {}))

        self.assertEqual(Sale.objects.for_business(self.business_a).count(), sale_count)
        self.assertEqual(self.fabric_stock(), D("10.000"))

    def test_03_customer_fabric_needs_no_meter_and_discards_submitted_meter(self):
        sale = self.complete(
            [self.service_customer_line(fabric_meter_used="999.000")]
        )
        item = sale.items.get()

        self.assertTrue(item.customer_supplied_fabric)
        self.assertIsNone(item.fabric_meter_used)
        self.assertEqual(item.inventory_quantity, D("0"))
        self.assertEqual(self.fabric_stock(), D("10.000"))

    def test_04_normal_business_fabric_still_requires_meter(self):
        line = self.service_meter_line()
        line.pop("fabric_meter_used")
        sale_count = Sale.objects.for_business(self.business_a).count()

        with self.assertRaises(SaleError) as caught:
            self.complete([line])

        self.assertIn("items.0.fabric_meter_used", caught.exception.errors)
        self.assertEqual(Sale.objects.for_business(self.business_a).count(), sale_count)
        self.assertEqual(self.fabric_stock(), D("10.000"))

    def test_05_normal_business_fabric_deducts_exact_meter(self):
        sale = self.complete(
            [
                self.service_meter_line(
                    customer_supplied_fabric=False,
                    fabric_meter_used="2.375",
                )
            ]
        )
        item = sale.items.get()

        self.assertFalse(item.customer_supplied_fabric)
        self.assertEqual(item.fabric_meter_used, D("2.375"))
        self.assertEqual(self.fabric_stock(), D("7.625"))
        movement = StockMovement.objects.for_business(self.business_a).get(
            movement_type=StockMovement.Type.SALE,
            reference_id=sale.invoice_number,
        )
        self.assertEqual(movement.quantity, D("-2.375"))

    def test_06_customer_fabric_creates_zero_inventory_movement(self):
        sale = self.complete()

        self.assertFalse(
            StockMovement.objects.for_business(self.business_a)
            .filter(reference_type="Sale", reference_id=sale.invoice_number)
            .exists()
        )
        self.assertEqual(self.fabric_stock(), D("10.000"))

    def test_07_customer_fabric_completes_at_zero_business_stock(self):
        inventory.record_movement(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.fabric,
            variant=self.color,
            movement_type=StockMovement.Type.ADJUST_OUT,
            quantity=D("-10.000"),
            reference_type="Test",
            reference_id="customer-fabric-zero-stock",
            user=self.owner_a,
        )
        self.assertEqual(self.fabric_stock(), D("0.000"))

        sale = self.complete()

        self.assertEqual(sale.status, Sale.Status.COMPLETED)
        self.assertEqual(self.fabric_stock(), D("0.000"))
        self.assertFalse(
            StockMovement.objects.for_business(self.business_a)
            .filter(reference_type="Sale", reference_id=sale.invoice_number)
            .exists()
        )

    def test_08_customer_fabric_material_cogs_is_zero(self):
        sale = self.complete()
        item = sale.items.get()

        self.assertEqual(item.unit_cost, D("0.000"))
        self.assertEqual(sale.total_cost, D("0.000"))

    def test_09_customer_fabric_profit_ignores_catalog_fabric_cost(self):
        sale = self.complete()
        item = sale.items.get()

        self.assertEqual(item.gross_profit, D("25.000"))
        self.assertEqual(sale.gross_profit, D("25.000"))

    def test_10_customer_fabric_flag_is_persisted_and_api_read_only(self):
        item = self.complete().items.get()
        context = {
            "request": SimpleNamespace(
                api_access_context=SimpleNamespace(
                    effective_modules=frozenset({"tailoring"})
                )
            )
        }

        data = SaleItemSerializer(item, context=context).data
        self.assertIs(data["customer_supplied_fabric"], True)
        attempted_update = SaleItemSerializer(
            item,
            data={"customer_supplied_fabric": False},
            partial=True,
            context=context,
        )
        self.assertTrue(attempted_update.is_valid(), attempted_update.errors)
        self.assertNotIn(
            "customer_supplied_fabric", attempted_update.validated_data
        )

    def test_11_customer_fabric_has_no_stock_warehouse_snapshot(self):
        item = self.complete().items.get()

        self.assertIsNone(item.stock_warehouse)

    def test_12_voiding_customer_fabric_never_increases_stock(self):
        sale = self.complete()
        before = self.fabric_stock()

        sales_services.void_sale(
            sale=sale,
            user=self.owner_a,
            reason="Customer cancelled",
            membership=self.membership_a(),
        )

        self.assertEqual(self.fabric_stock(), before)
        self.assertFalse(
            StockMovement.objects.for_business(self.business_a)
            .filter(
                movement_type=StockMovement.Type.SALE_RETURN,
                reference_type="Void",
                reference_id=sale.invoice_number,
            )
            .exists()
        )

    def test_13_returning_customer_fabric_never_restocks_customer_material(self):
        sale = self.complete()
        item = sale.items.get()
        before = self.fabric_stock()

        sale_return = sales_services.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1"), "restock": True}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
            restock=True,
            membership=self.membership_a(),
        )

        self.assertEqual(self.fabric_stock(), before)
        self.assertFalse(sale_return.items.get().restocked)
        self.assertFalse(
            StockMovement.objects.for_business(self.business_a)
            .filter(
                movement_type=StockMovement.Type.SALE_RETURN,
                reference_id=sale_return.return_number,
            )
            .exists()
        )

    def test_14_normal_fabric_void_and_return_still_restore_exact_meter(self):
        voided = self.complete(
            [self.service_meter_line(fabric_meter_used="2.000")]
        )
        self.assertEqual(self.fabric_stock(), D("8.000"))
        sales_services.void_sale(
            sale=voided,
            user=self.owner_a,
            reason="Normal fabric void regression",
            membership=self.membership_a(),
        )
        self.assertEqual(self.fabric_stock(), D("10.000"))

        returned = self.complete(
            [self.service_meter_line(fabric_meter_used="3.000")]
        )
        returned_item = returned.items.get()
        self.assertEqual(self.fabric_stock(), D("7.000"))
        sale_return = sales_services.process_return(
            sale=returned,
            items=[
                {
                    "sale_item": returned_item,
                    "quantity": D("1"),
                    "restock": True,
                }
            ],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
            restock=True,
            membership=self.membership_a(),
        )

        self.assertEqual(self.fabric_stock(), D("10.000"))
        self.assertTrue(sale_return.items.get().restocked)
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).get(
                movement_type=StockMovement.Type.SALE_RETURN,
                reference_id=sale_return.return_number,
            ).quantity,
            D("3.000"),
        )

    def test_15_held_sale_round_trip_preserves_customer_fabric(self):
        token = "held-customer-fabric-round-trip"
        line = self.http_customer_line(fabric_meter_used="8.000")
        line.update(
            {
                "name": "Customer Fabric Test Cloth",
                "is_tailoring_item": True,
                "is_meter_tailoring": True,
                "is_legacy_tailoring": False,
                "is_tailoring_workflow": True,
            }
        )
        response = self.client.post(
            reverse("sales:pos_hold"),
            json.dumps(
                {
                    "branch_id": self.branch_a.id,
                    "label": "Customer fabric held cart",
                    "cart": {"items": [line], "checkout_token": token},
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        held_id = response.json()["held_id"]
        stored_line = HeldSale.objects.get(pk=held_id).cart["items"][0]
        self.assertTrue(stored_line["customer_supplied_fabric"])
        self.assertIsNone(stored_line["fabric_meter_used"])

        held_payload = next(
            row
            for row in self.client.get(reverse("sales:pos_held_list")).json()[
                "held"
            ]
            if row["id"] == held_id
        )
        restored_line = held_payload["cart"]["items"][0]
        self.assertTrue(restored_line["customer_supplied_fabric"])
        self.assertEqual(restored_line["fabric_meter_used"], "")

        sale = self.sale_from_response(
            self.checkout(
                [restored_line],
                held_id=held_id,
                token=token,
            )
        )
        item = sale.items.get()
        self.assertTrue(item.customer_supplied_fabric)
        self.assertIsNone(item.fabric_meter_used)
        self.assertFalse(HeldSale.objects.filter(pk=held_id).exists())

    def test_16_pos_toggle_and_duplicate_contract_preserve_customer_fabric(self):
        html = self.client.get(reverse("sales:pos")).content.decode()

        self.assertIn("Customer Fabric", html)
        self.assertIn("Stitching Charge", html)
        self.assertIn("setCustomerFabric(line", html)
        self.assertIn(
            "line.is_meter_tailoring && !line.customer_supplied_fabric", html
        )
        self.assertIn("line.fabric_meter_used = '';", html)
        self.assertIn(
            "customer_supplied_fabric: source.customer_supplied_fabric === true",
            html,
        )
        self.assertIn("customer_supplied_fabric: (", html)

    def test_17_retail_and_non_boolean_customer_fabric_values_are_rejected(self):
        retail_response = self.checkout(
            [
                {
                    "product_id": self.retail.id,
                    "variant_id": None,
                    "quantity": "1",
                    "unit_price": "8.000",
                    "customer_supplied_fabric": True,
                    "garment_classification": "",
                    "collection_type": "",
                    "tailoring_details": {},
                }
            ]
        )
        self.assertEqual(retail_response.status_code, 400, retail_response.content)
        self.assertIn("Customer Fabric", retail_response.json()["error"])

        for value in ("true", 1, {"truthy": True}):
            with self.subTest(layer="service", value=value):
                with self.assertRaises(SaleError) as caught:
                    self.complete(
                        [
                            self.service_meter_line(
                                customer_supplied_fabric=value,
                                fabric_meter_used="1.000",
                            )
                        ]
                    )
                self.assertIn(
                    "items.0.customer_supplied_fabric", caught.exception.errors
                )
            with self.subTest(layer="http", value=value):
                response = self.checkout(
                    [
                        self.http_meter_line(
                            customer_supplied_fabric=value,
                            fabric_meter_used="1.000",
                        )
                    ]
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(
                    "items.0.customer_supplied_fabric",
                    response.json().get("errors", {}),
                )

        self.assertFalse(Sale.objects.for_business(self.business_a).exists())
        self.assertEqual(self.fabric_stock(), D("10.000"))

    def test_invalid_customer_fabric_hold_is_a_controlled_rejection(self):
        for line in (
            self.http_customer_line(customer_supplied_fabric="true"),
            {
                "product_id": self.retail.id,
                "quantity": "1",
                "unit_price": "8.000",
                "customer_supplied_fabric": True,
            },
        ):
            with self.subTest(line=line):
                response = self.client.post(
                    reverse("sales:pos_hold"),
                    json.dumps(
                        {
                            "branch_id": self.branch_a.id,
                            "label": "Invalid customer fabric hold",
                            "cart": {
                                "items": [line],
                                "checkout_token": f"invalid-hold-{uuid4().hex}",
                            },
                        }
                    ),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertFalse(response.json()["ok"])

        self.assertFalse(HeldSale.objects.exists())

    def test_18_missing_key_defaults_to_normal_business_fabric(self):
        line = self.http_meter_line(fabric_meter_used="1.250")
        line.pop("customer_supplied_fabric", None)
        sale = self.sale_from_response(self.checkout([line]))
        item = sale.items.get()

        self.assertFalse(item.customer_supplied_fabric)
        self.assertEqual(item.fabric_meter_used, D("1.250"))
        self.assertEqual(self.fabric_stock(), D("8.750"))
        self.assertIs(
            SaleItem._meta.get_field("customer_supplied_fabric").default,
            False,
        )

        historical_line = self.http_meter_line(fabric_meter_used="1.000")
        historical_line.pop("customer_supplied_fabric", None)
        historical = HeldSale.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            cashier=self.owner_a,
            label="Historical held cart",
            cart={
                "items": [historical_line],
                "checkout_token": "historical-held-no-customer-key",
            },
        )
        held_payload = next(
            row
            for row in self.client.get(reverse("sales:pos_held_list")).json()[
                "held"
            ]
            if row["id"] == historical.id
        )
        self.assertFalse(
            held_payload["cart"]["items"][0]["customer_supplied_fabric"]
        )
        self.assertEqual(
            held_payload["cart"]["items"][0]["fabric_meter_used"], "1.000"
        )

    def test_19_tax_discount_and_payment_calculations_are_unchanged(self):
        settings_obj = self.business_a.settings
        settings_obj.vat_enabled = True
        settings_obj.vat_percentage = D("5.000")
        settings_obj.prices_include_tax = False
        settings_obj.save(
            update_fields=[
                "vat_enabled",
                "vat_percentage",
                "prices_include_tax",
            ]
        )

        sale = self.complete(
            [self.service_customer_line(unit_price=D("100.000"))],
            invoice_discount=D("10.000"),
            payment_amount=D("94.500"),
        )
        item = sale.items.get()

        self.assertEqual(sale.subtotal, D("100.000"))
        self.assertEqual(sale.discount_amount, D("10.000"))
        self.assertEqual(item.tax_rate, D("5.000"))
        self.assertEqual(sale.tax_amount, D("4.500"))
        self.assertEqual(sale.total, D("94.500"))
        self.assertEqual(sale.amount_paid, D("94.500"))
        self.assertEqual(sale.payments.get().amount, D("94.500"))
        self.assertEqual(item.gross_profit, D("90.000"))
        self.assertEqual(sale.gross_profit, D("90.000"))

    def test_20_customer_fabric_cannot_bypass_tenant_or_branch_scope(self):
        sale_count = Sale.objects.for_business(self.business_a).count()
        tenant_response = self.checkout(
            [
                {
                    "product_id": self.product_b.id,
                    "variant_id": None,
                    "quantity": "1",
                    "unit_price": "5.000",
                    "customer_supplied_fabric": True,
                    "garment_classification": "adult",
                    "collection_type": "normal",
                    "tailoring_details": {},
                }
            ],
            payment_amount=D("5.000"),
        )
        self.assertEqual(tenant_response.status_code, 400, tenant_response.content)

        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Customer Fabric Restricted Branch",
            code="CF-RESTRICTED",
        )
        Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Customer Fabric Restricted Warehouse",
            code="CF-RESTRICTED-WH",
            is_default=True,
        )
        other_customer = Customer.objects.create(
            business=self.business_a,
            home_branch=other_branch,
            code="CF-RESTRICTED-CUSTOMER",
            full_name="Restricted Branch Customer",
        )
        self.cashier_membership.branches.set([self.branch_a])
        self.client.force_login(self.cashier_a)

        branch_response = self.checkout(
            [self.http_customer_line()],
            branch=other_branch,
            customer=other_customer,
        )
        self.assertEqual(branch_response.status_code, 403, branch_response.content)
        self.assertEqual(
            Sale.objects.for_business(self.business_a).count(), sale_count
        )
        self.assertEqual(self.fabric_stock(), D("10.000"))

    def test_service_null_unit_customer_fabric_uses_no_estimate_or_material_cost(self):
        service = Product.objects.create(
            business=self.business_a,
            name="Customer Fabric Stitching Service",
            sku="CUSTOMER-FABRIC-SERVICE",
            product_type=Product.Type.SERVICE,
            unit=None,
            purchase_price=D("12.000"),
            sale_price=D("30.000"),
            track_inventory=False,
            is_tailoring_item=True,
            estimated_adult_fabric=D("3.500"),
            estimated_child_fabric=D("2.250"),
        )
        sale = self.complete(
            [
                {
                    "product": service,
                    "variant": None,
                    "quantity": D("2"),
                    "unit_price": D("30.000"),
                    "discount_amount": D("0"),
                    "customer_supplied_fabric": True,
                    "garment_classification": "adult",
                    "collection_type": "normal",
                    "tailoring_details": {},
                }
            ],
            payment_amount=D("60.000"),
        )
        item = sale.items.get()

        self.assertTrue(item.customer_supplied_fabric)
        self.assertTrue(item.is_tailoring_line)
        self.assertIsNone(item.estimated_fabric)
        self.assertIsNone(item.fabric_meter_used)
        self.assertIsNone(item.stock_warehouse)
        self.assertEqual(item.unit_cost, D("0.000"))
        self.assertEqual(sale.total_cost, D("0.000"))
        self.assertEqual(sale.gross_profit, D("60.000"))
        self.assertFalse(
            StockMovement.objects.for_business(self.business_a)
            .filter(product=service)
            .exists()
        )

    def test_customer_fabric_actual_fabric_is_informational_only(self):
        item = self.complete().items.get()
        movement_count = StockMovement.objects.for_business(self.business_a).count()
        stock_before = self.fabric_stock()

        updated = sales_services.update_actual_fabric(
            sale_item=item,
            actual_fabric_used="2.750",
            user=self.owner_a,
            membership=self.membership_a(),
        )

        self.assertEqual(updated.actual_fabric_used, D("2.750"))
        self.assertEqual(self.fabric_stock(), stock_before)
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).count(),
            movement_count,
        )

        report = sales_detailed(self.business_a, {})
        actual_index = report["columns"].index("Legacy Workshop Actual")
        row = next(
            row for row in report["rows"] if row[5] == item.product_name
        )
        self.assertIsNone(row[actual_index])
        self.assertIsNone(
            dict(report["summary"])["Legacy Workshop Actual Total"]
        )

    def test_customer_fabric_product_without_stock_assignment_is_discoverable(self):
        unassigned = Product.objects.create(
            business=self.business_a,
            name="Unassigned Customer Fabric Garment",
            sku="UNASSIGNED-CUSTOMER-FABRIC",
            barcode="UNASSIGNED-CUSTOMER-FABRIC-BARCODE",
            product_type=Product.Type.STANDARD,
            unit=self.meter,
            sale_price=D("20.000"),
            track_inventory=True,
            is_tailoring_item=True,
        )

        grid = self.client.get(
            reverse("sales:pos_products"),
            {
                "warehouse_id": self.warehouse_a.id,
                "q": "Unassigned Customer Fabric Garment",
            },
        )
        self.assertEqual(grid.status_code, 200)
        card = next(
            item for item in grid.json()["items"]
            if item["product_id"] == unassigned.id
        )
        self.assertTrue(card["customer_fabric_only"])
        self.assertIsNone(card["variant_id"])

        barcode = self.client.get(
            reverse("sales:pos_barcode"),
            {
                "warehouse_id": self.warehouse_a.id,
                "code": unassigned.barcode,
            },
        )
        self.assertTrue(barcode.json()["found"])
        self.assertTrue(barcode.json()["item"]["customer_fabric_only"])

        sale = self.sale_from_response(
            self.checkout(
                [{
                    "product_id": unassigned.id,
                    "variant_id": None,
                    "quantity": "1",
                    "unit_price": "20.000",
                    "customer_supplied_fabric": True,
                    "garment_classification": "adult",
                    "collection_type": "normal",
                    "tailoring_details": {},
                }]
            )
        )
        self.assertTrue(sale.items.get().customer_supplied_fabric)
        self.assertFalse(
            StockMovement.objects.for_business(self.business_a)
            .filter(product=unassigned)
            .exists()
        )

    def test_customer_fabric_does_not_bypass_foreign_branch_assignment(self):
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Customer Fabric Other Branch",
            code="CF-OTHER",
        )
        other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Customer Fabric Other Warehouse",
            code="CF-OTHER-WH",
            is_default=True,
        )
        other_product = Product.objects.create(
            business=self.business_a,
            name="Other Branch Tailoring Fabric",
            sku="OTHER-BRANCH-TAILORING",
            barcode="OTHER-BRANCH-TAILORING-BARCODE",
            product_type=Product.Type.STANDARD,
            unit=self.meter,
            sale_price=D("20.000"),
            track_inventory=True,
            is_tailoring_item=True,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=other_warehouse,
            product=other_product,
            quantity=D("5.000"),
            unit_cost=D("4.000"),
            user=self.owner_a,
        )

        grid = self.client.get(
            reverse("sales:pos_products"),
            {
                "warehouse_id": self.warehouse_a.id,
                "q": other_product.name,
            },
        )
        self.assertEqual(grid.status_code, 200)
        self.assertNotIn(
            other_product.id,
            {item["product_id"] for item in grid.json()["items"]},
        )

        barcode = self.client.get(
            reverse("sales:pos_barcode"),
            {
                "warehouse_id": self.warehouse_a.id,
                "code": other_product.barcode,
            },
        )
        self.assertFalse(barcode.json()["found"])

        response = self.checkout(
            [{
                "product_id": other_product.id,
                "variant_id": None,
                "quantity": "1",
                "unit_price": "20.000",
                "customer_supplied_fabric": True,
                "garment_classification": "adult",
                "collection_type": "normal",
                "tailoring_details": {},
            }]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid product in cart.")

    def test_customer_only_variant_scan_is_parent_priced_and_tenant_safe(self):
        unassigned = Product.objects.create(
            business=self.business_a,
            name="Unassigned Legacy Variant Tailoring",
            sku="UNASSIGNED-LEGACY-PARENT",
            product_type=Product.Type.VARIANT,
            unit=None,
            sale_price=D("20.000"),
            track_inventory=True,
            is_tailoring_item=True,
        )
        variant = ProductVariant.objects.create(
            business=self.business_a,
            product=unassigned,
            name="Catalog Fabric Color",
            sku="UNASSIGNED-LEGACY-VARIANT",
            barcode="UNASSIGNED-LEGACY-VARIANT-BARCODE",
            sale_price=D("99.000"),
        )

        grid = self.client.get(
            reverse("sales:pos_products"),
            {
                "warehouse_id": self.warehouse_a.id,
                "q": unassigned.name,
            },
        ).json()["items"]
        card = next(item for item in grid if item["product_id"] == unassigned.id)
        self.assertIsNone(card["variant_id"])
        self.assertEqual(card["price"], "20.000")
        self.assertTrue(card["customer_fabric_only"])

        scan = self.client.get(
            reverse("sales:pos_barcode"),
            {
                "warehouse_id": self.warehouse_a.id,
                "code": variant.barcode,
            },
        ).json()["item"]
        self.assertIsNone(scan["variant_id"])
        self.assertEqual(scan["price"], card["price"])
        self.assertEqual(scan["sku"], unassigned.sku)
        self.assertTrue(scan["customer_fabric_only"])

        sibling_product = Product.objects.create(
            business=self.business_a,
            name="Sibling Variant Customer Fabric",
            sku="SIBLING-VARIANT-PARENT",
            product_type=Product.Type.VARIANT,
            unit=None,
            sale_price=D("30.000"),
            track_inventory=True,
            is_tailoring_item=True,
        )
        assigned_sibling = ProductVariant.objects.create(
            business=self.business_a,
            product=sibling_product,
            name="Assigned Catalog Color",
            sku="ASSIGNED-SIBLING-VARIANT",
            sale_price=D("35.000"),
        )
        unassigned_sibling = ProductVariant.objects.create(
            business=self.business_a,
            product=sibling_product,
            name="Unassigned Catalog Color",
            sku="UNASSIGNED-SIBLING-VARIANT",
            barcode="UNASSIGNED-SIBLING-VARIANT-BARCODE",
            sale_price=D("90.000"),
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=sibling_product,
            variant=assigned_sibling,
            quantity=D("2.000"),
            unit_cost=D("4.000"),
            user=self.owner_a,
        )
        sibling_scan = self.client.get(
            reverse("sales:pos_barcode"),
            {
                "warehouse_id": self.warehouse_a.id,
                "code": unassigned_sibling.barcode,
            },
        ).json()
        self.assertTrue(sibling_scan["found"])
        self.assertIsNone(sibling_scan["item"]["variant_id"])
        self.assertEqual(sibling_scan["item"]["price"], "30.000")
        self.assertTrue(sibling_scan["item"]["customer_fabric_only"])

        foreign_meter = Unit.objects.for_business(self.business_b).get(name="Meter")
        foreign_product = Product.objects.create(
            business=self.business_b,
            name="Foreign Tenant Tailoring Fabric",
            sku="FOREIGN-TENANT-TAILORING",
            product_type=Product.Type.VARIANT,
            unit=foreign_meter,
            sale_price=D("500.000"),
            track_inventory=True,
            is_tailoring_item=True,
        )
        malformed_variant = ProductVariant.objects.create(
            business=self.business_a,
            product=foreign_product,
            name="Cross Tenant Variant",
            sku="CROSS-TENANT-VARIANT",
            barcode="CROSS-TENANT-VARIANT-BARCODE",
            sale_price=D("500.000"),
        )
        foreign_scan = self.client.get(
            reverse("sales:pos_barcode"),
            {
                "warehouse_id": self.warehouse_a.id,
                "code": malformed_variant.barcode,
            },
        )
        self.assertFalse(foreign_scan.json()["found"])

        local_product = Product.objects.create(
            business=self.business_a,
            name="Local Product With Foreign Variant",
            sku="LOCAL-WITH-FOREIGN-VARIANT",
            product_type=Product.Type.VARIANT,
            unit=self.meter,
            sale_price=D("20.000"),
            track_inventory=True,
            is_tailoring_item=True,
        )
        foreign_variant = ProductVariant.objects.create(
            business=self.business_b,
            product=local_product,
            name="Foreign Variant On Local Product",
            sku="FOREIGN-VARIANT-ON-LOCAL",
        )
        StockLevel.objects.create(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=local_product,
            variant=foreign_variant,
            quantity=D("1.000"),
        )
        malformed_grid = self.client.get(
            reverse("sales:pos_products"),
            {
                "warehouse_id": self.warehouse_a.id,
                "q": local_product.name,
            },
        ).json()["items"]
        self.assertNotIn(
            local_product.id,
            {item["product_id"] for item in malformed_grid},
        )

    def test_shared_workshop_routing_does_not_emit_unusable_customer_card(self):
        workshop_branch = Branch.objects.create(
            business=self.business_a,
            name="Customer Fabric Workshop",
            code="CF-WORKSHOP",
            usage_type=Branch.UsageType.WORKSHOP_STOCK,
        )
        workshop_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=workshop_branch,
            name="Customer Fabric Workshop Warehouse",
            code="CF-WORKSHOP-WH",
            is_default=True,
        )
        settings_obj = self.business_a.settings
        settings_obj.shared_fabric_warehouse = workshop_warehouse
        settings_obj.save(update_fields=["shared_fabric_warehouse"])

        branch_only = Product.objects.create(
            business=self.business_a,
            name="Branch Only Meter Variant",
            sku="BRANCH-ONLY-METER-PARENT",
            product_type=Product.Type.VARIANT,
            unit=self.meter,
            sale_price=D("20.000"),
            track_inventory=True,
            is_tailoring_item=True,
        )
        branch_variant = ProductVariant.objects.create(
            business=self.business_a,
            product=branch_only,
            name="Branch Only Color",
            sku="BRANCH-ONLY-METER-VARIANT",
            barcode="BRANCH-ONLY-METER-VARIANT-BARCODE",
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=branch_only,
            variant=branch_variant,
            quantity=D("2.000"),
            unit_cost=D("4.000"),
            user=self.owner_a,
        )

        grid = self.client.get(
            reverse("sales:pos_products"),
            {
                "warehouse_id": self.warehouse_a.id,
                "q": branch_only.name,
            },
        ).json()["items"]
        self.assertNotIn(
            branch_only.id,
            {item["product_id"] for item in grid},
        )
        barcode = self.client.get(
            reverse("sales:pos_barcode"),
            {
                "warehouse_id": self.warehouse_a.id,
                "code": branch_variant.barcode,
            },
        )
        self.assertFalse(barcode.json()["found"])

    def test_customer_fabric_wording_is_clear_on_detail_prints_and_job_card(self):
        sale = self.complete()
        item = sale.items.select_related("product__unit", "variant").get()

        detail = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        invoice = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        receipt = self.client.get(reverse("sales:receipt", args=[sale.public_id]))
        for response in (detail, invoice, receipt):
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Customer Fabric")

        self.assertNotContains(detail, "Legacy estimate:")
        self.assertNotContains(detail, "Legacy workshop actual:")
        self.assertNotContains(detail, "Meter:")

        receipt_58 = render_to_string(
            "invoices/receipt_58mm.html",
            _invoice_context(sale),
        )
        self.assertIn("Customer Fabric", receipt_58)

        request = RequestFactory().get("/sales/customer-fabric/job-card/")
        request.business = self.business_a
        job_card = render_to_string(
            "invoices/workshop_job_card.html",
            _job_card_context(sale, request, [item], sale_item=item),
        )
        self.assertIn("Customer Fabric", job_card)
        self.assertIn("No fabric consumption", job_card)
        self.assertNotIn("No Shumukh fabric consumption", job_card)
        self.assertNotIn("Legacy Fabric Record", job_card)
        self.assertNotIn("POS-entered fabric consumption", job_card)
