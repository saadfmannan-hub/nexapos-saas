from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.api.serializers import SaleItemSerializer, SaleSerializer
from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Brand, ProductVariant, Unit
from apps.inventory import services as inventory
from apps.purchases.models import Purchase
from apps.reports.queries import (
    current_year_financial_summary,
    purchases_summary,
    sales_detailed,
    stock_movements_report,
    supplier_payments_cheques,
)
from apps.reports.templatetags.report_tags import report_cell_class
from apps.sales.models import Sale, SaleItem, SaleReturn, SaleReturnItem
from apps.suppliers.models import Supplier, SupplierPayment

from .base import TenantTestCase

D = Decimal


class LockedTailoringReportsApiTests(TenantTestCase):
    def setUp(self):
        self.brand = Brand.objects.create(
            business=self.business_a,
            name="Golden City",
        )
        self.product_a.brand = self.brand
        self.product_a.save(update_fields=["brand"])
        self.variant = ProductVariant.objects.create(
            business=self.business_a,
            product=self.product_a,
            name="Color 7",
            sku="WID-A-C7",
        )

    def sale_item(
        self,
        invoice_number,
        *,
        meter=None,
        branch=None,
        warehouse=None,
        status=Sale.Status.COMPLETED,
    ):
        branch = branch or self.branch_a
        warehouse = warehouse or self.warehouse_a
        sale = Sale.objects.create(
            business=self.business_a,
            branch=branch,
            warehouse=warehouse,
            cashier=self.owner_a,
            customer=self.walk_in_a,
            invoice_number=invoice_number,
            status=status,
            sale_date=timezone.now(),
            subtotal=D("25.000"),
            total=D("25.000"),
            amount_paid=D("25.000"),
        )
        item = SaleItem.objects.create(
            business=self.business_a,
            sale=sale,
            product=self.product_a,
            variant=self.variant,
            product_name=str(self.variant),
            sku=self.variant.sku,
            quantity=D("1.000"),
            unit_price=D("25.000"),
            line_total=D("25.000"),
            garment_classification=SaleItem.GarmentClassification.ADULT,
            collection_type=SaleItem.CollectionType.PREMIUM,
            fabric_meter_used=meter,
        )
        return sale, item

    def report_row(self, data, invoice_number):
        return next(row for row in data["rows"] if row[0] == invoice_number)

    def test_detailed_report_keeps_raw_and_net_meter_separate_from_legacy(self):
        sale, item = self.sale_item("LOCK-METER-001", meter=D("3.500"))
        item.estimated_fabric = D("3.250")
        item.actual_fabric_used = D("3.750")
        item.save(update_fields=["estimated_fabric", "actual_fabric_used"])

        data = sales_detailed(self.business_a, {})
        row = self.report_row(data, sale.invoice_number)
        columns = data["columns"]

        self.assertEqual(row[columns.index("POS Meter")], D("3.500"))
        self.assertEqual(row[columns.index("Net Meter Deducted")], D("3.500"))
        self.assertEqual(row[columns.index("Brand")], self.brand.name)
        self.assertEqual(row[columns.index("Variant / Color")], self.variant.name)
        self.assertEqual(row[columns.index("Warehouse")], self.warehouse_a.name)
        self.assertEqual(row[columns.index("Legacy Estimated Fabric")], D("3.250"))
        self.assertEqual(row[columns.index("Legacy Workshop Actual")], D("3.750"))

        summary = dict(data["summary"])
        self.assertEqual(summary["Net POS Meter Total"], D("3.500"))
        self.assertEqual(summary["Legacy Estimated Total"], D("3.250"))
        self.assertEqual(summary["Legacy Workshop Actual Total"], D("3.750"))
        self.assertTrue(data["wide_pdf"])
        self.assertIn("text-end", report_cell_class(["POS Meter"], 0))

    def test_net_meter_follows_void_and_restock_effects(self):
        void_sale, _void_item = self.sale_item(
            "LOCK-METER-VOID",
            meter=D("4.000"),
            status=Sale.Status.VOIDED,
        )
        restocked_sale, restocked_item = self.sale_item(
            "LOCK-METER-RESTOCK",
            meter=D("3.000"),
            status=Sale.Status.RETURNED,
        )
        retained_sale, retained_item = self.sale_item(
            "LOCK-METER-NORESTOCK",
            meter=D("2.000"),
            status=Sale.Status.RETURNED,
        )

        for sequence, (sale, item, restocked) in enumerate(
            (
                (restocked_sale, restocked_item, True),
                (retained_sale, retained_item, False),
            ),
            start=1,
        ):
            sale_return = SaleReturn.objects.create(
                business=self.business_a,
                return_number=f"LOCK-RET-{sequence}",
                sale=sale,
                customer=self.walk_in_a,
                branch=self.branch_a,
                warehouse=self.warehouse_a,
                refund_method=SaleReturn.RefundMethod.CASH,
                refund_amount=D("25.000"),
                restock=restocked,
                processed_by=self.owner_a,
            )
            SaleReturnItem.objects.create(
                business=self.business_a,
                sale_return=sale_return,
                sale_item=item,
                quantity=D("1.000"),
                refund_per_unit=D("25.000"),
                line_refund=D("25.000"),
                restocked=restocked,
            )
            item.returned_quantity = D("1.000")
            item.save(update_fields=["returned_quantity"])

        data = sales_detailed(self.business_a, {})
        net_index = data["columns"].index("Net Meter Deducted")
        self.assertEqual(
            self.report_row(data, void_sale.invoice_number)[net_index],
            D("0"),
        )
        self.assertEqual(
            self.report_row(data, restocked_sale.invoice_number)[net_index],
            D("0"),
        )
        self.assertEqual(
            self.report_row(data, retained_sale.invoice_number)[net_index],
            D("2.000"),
        )
        self.assertEqual(dict(data["summary"])["Net POS Meter Total"], D("2.000"))

    def test_sales_warehouse_filter_and_stock_report_context(self):
        second_branch = Branch.objects.create(
            business=self.business_a,
            name="Second Branch",
            code="LOCK-B2",
        )
        second_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=second_branch,
            name="Fabric Warehouse",
            code="LOCK-W2",
        )
        first_sale, _item = self.sale_item("LOCK-WH-001", meter=D("1.500"))
        second_sale, _item = self.sale_item(
            "LOCK-WH-002",
            meter=D("2.500"),
            branch=second_branch,
            warehouse=second_warehouse,
        )

        data = sales_detailed(
            self.business_a,
            {"warehouse_id": second_warehouse.id},
        )
        self.assertEqual(
            [row[0] for row in data["rows"]],
            [second_sale.invoice_number],
        )
        self.assertNotEqual(first_sale.invoice_number, second_sale.invoice_number)

        meter = Unit.objects.for_business(self.business_a).get(is_meter=True)
        self.product_a.unit = meter
        self.product_a.save(update_fields=["unit"])
        inventory.record_movement(
            business=self.business_a,
            warehouse=second_warehouse,
            product=self.product_a,
            variant=self.variant,
            movement_type="opening",
            quantity=D("9.000"),
            unit_cost=D("4.000"),
            reference_type="LockedReport",
            reference_id="LOCK-STOCK-CONTEXT",
            user=self.owner_a,
        )
        movements = stock_movements_report(
            self.business_a,
            {"warehouse_id": second_warehouse.id},
        )
        reference_index = movements["columns"].index("Reference")
        row = next(
            row
            for row in movements["rows"]
            if row[reference_index] == "LockedReport LOCK-STOCK-CONTEXT"
        )
        self.assertEqual(row[movements["columns"].index("Brand")], self.brand.name)
        self.assertEqual(
            row[movements["columns"].index("Variant / Color")],
            self.variant.name,
        )
        self.assertEqual(row[movements["columns"].index("Unit")], "m")

        self.client.force_login(self.owner_a)
        movement_page = self.client.get(
            reverse("inventory:movement_list"),
            {"q": "LOCK-STOCK-CONTEXT"},
        )
        self.assertEqual(movement_page.status_code, 200)
        self.assertContains(movement_page, "9.000 m")

        restricted = stock_movements_report(
            self.business_a,
            {"allowed_branch_ids": [self.branch_a.id]},
        )
        restricted_references = {
            row[restricted["columns"].index("Reference")]
            for row in restricted["rows"]
        }
        self.assertNotIn(
            "LockedReport LOCK-STOCK-CONTEXT",
            restricted_references,
        )

    def test_internal_sale_serializer_exposes_meter_and_historical_null(self):
        sale, _item = self.sale_item("LOCK-API-001", meter=D("3.125"))
        historical_sale, _item = self.sale_item("LOCK-API-LEGACY", meter=None)

        self.assertEqual(
            SaleSerializer(sale).data["items"][0]["fabric_meter_used"],
            "3.125",
        )
        self.assertIsNone(
            SaleSerializer(historical_sale).data["items"][0]["fabric_meter_used"]
        )

    def test_internal_sale_item_serializer_validates_meter_decimal(self):
        _sale, item = self.sale_item("LOCK-API-VALIDATE", meter=D("3.125"))

        valid = SaleItemSerializer(
            item,
            data={"fabric_meter_used": "3.500"},
            partial=True,
        )
        self.assertTrue(valid.is_valid(), valid.errors)
        self.assertEqual(valid.validated_data["fabric_meter_used"], D("3.500"))

        for value in ("0", "-0.001", "1000.001", "invalid"):
            with self.subTest(value=value):
                invalid = SaleItemSerializer(
                    item,
                    data={"fabric_meter_used": value},
                    partial=True,
                )
                self.assertFalse(invalid.is_valid())
                self.assertIn("fabric_meter_used", invalid.errors)

    def test_restricted_internal_outputs_hide_legacy_location_mismatches(self):
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Report Other Branch",
            code="LOCK-REPORT-B2",
        )
        other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Report Other Warehouse",
            code="LOCK-REPORT-W2",
        )
        central_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=None,
            name="Report Central Warehouse",
            code="LOCK-REPORT-CENTRAL",
        )
        mismatched_sale, _item = self.sale_item(
            "LOCK-REPORT-MISMATCH",
            meter=D("2.000"),
            branch=self.branch_a,
            warehouse=other_warehouse,
        )
        central_sale, _item = self.sale_item(
            "LOCK-REPORT-CENTRAL",
            meter=D("2.000"),
            branch=self.branch_a,
            warehouse=central_warehouse,
        )
        supplier = Supplier.objects.create(
            business=self.business_a,
            code="LOCK-REPORT-SUP",
            name="Report Location Supplier",
        )
        mismatched_purchase = Purchase.objects.create(
            business=self.business_a,
            purchase_number="LOCK-REPORT-PO-MISMATCH",
            supplier=supplier,
            branch=self.branch_a,
            warehouse=other_warehouse,
            purchase_date=timezone.localdate(),
            created_by=self.owner_a,
        )
        central_purchase = Purchase.objects.create(
            business=self.business_a,
            purchase_number="LOCK-REPORT-PO-CENTRAL",
            supplier=supplier,
            branch=self.branch_a,
            warehouse=central_warehouse,
            purchase_date=timezone.localdate(),
            created_by=self.owner_a,
        )
        SupplierPayment.objects.create(
            business=self.business_a,
            payment_number="LOCK-REPORT-PAY-MISMATCH",
            supplier=supplier,
            purchase=mismatched_purchase,
            amount=D("1.000"),
            method=SupplierPayment.Method.CASH,
            paid_by=self.owner_a,
        )
        SupplierPayment.objects.create(
            business=self.business_a,
            payment_number="LOCK-REPORT-PAY-CENTRAL",
            supplier=supplier,
            purchase=central_purchase,
            amount=D("1.000"),
            method=SupplierPayment.Method.CASH,
            paid_by=self.owner_a,
        )

        membership = self.membership_a()
        membership.branches.set([self.branch_a])
        filters = {"allowed_branch_ids": [self.branch_a.id]}

        sales_data = sales_detailed(self.business_a, filters)
        sale_numbers = {row[0] for row in sales_data["rows"]}
        self.assertNotIn(mismatched_sale.invoice_number, sale_numbers)
        self.assertIn(central_sale.invoice_number, sale_numbers)
        financial_summary = current_year_financial_summary(
            self.business_a,
            membership,
            today=timezone.localdate(),
        )
        self.assertEqual(financial_summary["total_sales"], D("25.000"))

        purchase_data = purchases_summary(self.business_a, filters)
        purchase_numbers = {row[0] for row in purchase_data["rows"]}
        self.assertNotIn(mismatched_purchase.purchase_number, purchase_numbers)
        self.assertIn(central_purchase.purchase_number, purchase_numbers)

        payment_data = supplier_payments_cheques(self.business_a, filters)
        payment_purchase_numbers = {row[2] for row in payment_data["rows"]}
        self.assertNotIn(mismatched_purchase.purchase_number, payment_purchase_numbers)
        self.assertIn(central_purchase.purchase_number, payment_purchase_numbers)

        plan = self.business_a.subscription.plan
        plan.feature_api_access = True
        plan.save(update_fields=["feature_api_access"])
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("api:sale-list"))
        self.assertEqual(response.status_code, 200, response.content)
        api_sale_ids = {row["public_id"] for row in response.json()["results"]}
        self.assertNotIn(str(mismatched_sale.public_id), api_sale_ids)
        self.assertIn(str(central_sale.public_id), api_sale_ids)
