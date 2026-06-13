"""Phase 2.1 tests: customer import/export, product export,
inventory import/export, customer statement PDF."""
import io
from decimal import Decimal

from django.urls import reverse

from apps.audit.models import AuditLog
from apps.catalog.models import Product
from apps.customers.models import Customer
from apps.inventory import services as inventory

from .base import TenantTestCase

D = Decimal


def _csv_upload(text, name="import.csv"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


class CustomerExportTests(TenantTestCase):
    def setUp(self):
        Customer.objects.create(business=self.business_a, code="CUST-1",
                                full_name="Alpha Buyer", mobile="900111",
                                credit_limit=D("100"))
        self.client.force_login(self.owner_a)

    def test_csv_export_contains_customer(self):
        r = self.client.get(reverse("customers:export"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        body = r.content.decode()
        self.assertIn("Customer Code", body)
        self.assertIn("Alpha Buyer", body)

    def test_xlsx_export(self):
        r = self.client.get(reverse("customers:export"), {"format": "xlsx"})
        self.assertIn("spreadsheetml", r["Content-Type"])
        self.assertTrue(r.content.startswith(b"PK"))

    def test_export_audited(self):
        self.client.get(reverse("customers:export"))
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="customer.exported").exists())

    def test_export_requires_permission(self):
        self.client.force_login(self.cashier_a)  # no customers.export
        r = self.client.get(reverse("customers:export"))
        self.assertEqual(r.status_code, 403)


class CustomerImportTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def test_template_download(self):
        r = self.client.get(reverse("customers:import_template"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Customer Name", r.content.decode())

    def test_import_creates_customers(self):
        csv_text = (
            "customer code,customer name,mobile,email,credit limit\n"
            "NEW-1,Imported One,955001,one@example.com,50\n"
            "NEW-2,Imported Two,955002,,0\n"
        )
        r = self.client.post(reverse("customers:import"),
                             {"file": _csv_upload(csv_text), "mode": "skip"})
        self.assertEqual(r.status_code, 200)
        summary = r.context["results"]["summary"]
        self.assertEqual(summary["imported"], 2)
        self.assertTrue(Customer.objects.for_business(self.business_a).filter(
            code="NEW-1", mobile="955001").exists())
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="customer.imported").exists())

    def test_skip_vs_update_modes(self):
        Customer.objects.create(business=self.business_a, code="EX-1",
                                full_name="Original", mobile="900900")
        csv_text = ("customer code,customer name,mobile\n"
                    "EX-1,Changed Name,900900\n")
        # skip
        r = self.client.post(reverse("customers:import"),
                             {"file": _csv_upload(csv_text), "mode": "skip"})
        self.assertEqual(r.context["results"]["summary"]["skipped"], 1)
        self.assertEqual(Customer.objects.get(code="EX-1").full_name, "Original")
        # update
        r = self.client.post(reverse("customers:import"),
                             {"file": _csv_upload(csv_text), "mode": "update"})
        self.assertEqual(r.context["results"]["summary"]["updated"], 1)
        self.assertEqual(Customer.objects.get(code="EX-1").full_name, "Changed Name")

    def test_validation_errors_reported_not_imported(self):
        csv_text = (
            "customer code,customer name,mobile,email\n"
            ",,900,bad@\n"                         # missing name
            "V-1,Valid,901,not-an-email\n"        # invalid email
            "V-2,Good Row,902,good@example.com\n"  # ok
        )
        r = self.client.post(reverse("customers:import"),
                             {"file": _csv_upload(csv_text), "mode": "skip"})
        summary = r.context["results"]["summary"]
        self.assertEqual(summary["imported"], 1)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(len(r.context["results"]["errors"]), 2)

    def test_duplicate_mobile_in_file_rejected(self):
        csv_text = ("customer name,mobile\nA,500\nB,500\n")
        r = self.client.post(reverse("customers:import"),
                             {"file": _csv_upload(csv_text), "mode": "skip"})
        summary = r.context["results"]["summary"]
        self.assertEqual(summary["imported"], 1)
        self.assertEqual(summary["failed"], 1)

    def test_error_report_download(self):
        csv_text = "customer name,mobile\n,500\n"  # missing name
        self.client.post(reverse("customers:import"),
                         {"file": _csv_upload(csv_text), "mode": "skip"})
        r = self.client.get(reverse("customers:import"), {"errors": "1"})
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("Error", r.content.decode())

    def test_import_requires_permission(self):
        self.client.force_login(self.cashier_a)
        r = self.client.get(reverse("customers:import"))
        self.assertEqual(r.status_code, 403)


class ProductExportTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.client.force_login(self.owner_a)

    def test_csv_export_with_stock(self):
        r = self.client.get(reverse("catalog:product_export"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Current Stock", body)
        self.assertIn("Widget A", body)
        # 100 opening stock from base fixture
        self.assertIn("100", body)

    def test_low_stock_filter(self):
        # Widget A has 100 in stock, reorder 0 → not low; create a low one
        low = Product.objects.create(
            business=self.business_a, name="Low Item", sku="LOW-1",
            reorder_level=D("10"))
        inventory.set_opening_stock(business=self.business_a,
                                    warehouse=self.warehouse_a, product=low,
                                    quantity=D("3"), unit_cost=D("1"),
                                    user=self.owner_a)
        r = self.client.get(reverse("catalog:product_export"), {"status": "low"})
        body = r.content.decode()
        self.assertIn("Low Item", body)
        self.assertNotIn("Widget A", body)

    def test_xlsx_export(self):
        r = self.client.get(reverse("catalog:product_export"), {"format": "xlsx"})
        self.assertTrue(r.content.startswith(b"PK"))

    def test_export_requires_permission(self):
        self.client.force_login(self.cashier_a)
        r = self.client.get(reverse("catalog:product_export"))
        self.assertEqual(r.status_code, 403)

    def test_export_audited(self):
        self.client.get(reverse("catalog:product_export"))
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="product.exported").exists())


class InventoryExportTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def test_csv_export_columns_and_stock(self):
        r = self.client.get(reverse("inventory:export"))
        body = r.content.decode()
        for col in ["Current Stock", "Available Stock", "Stock Value",
                    "Warehouse", "Branch"]:
            self.assertIn(col, body)
        self.assertIn("Widget A", body)

    def test_export_requires_permission(self):
        self.client.force_login(self.cashier_a)
        r = self.client.get(reverse("inventory:export"))
        self.assertEqual(r.status_code, 403)


class InventoryImportTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def _post(self, csv_text, mode):
        return self.client.post(reverse("inventory:import"),
                               {"file": _csv_upload(csv_text), "mode": mode})

    def test_add_mode_increases_stock(self):
        csv_text = ("sku,warehouse,quantity\nWID-A,Main Warehouse,25\n")
        r = self._post(csv_text, "add")
        self.assertEqual(r.context["results"]["summary"]["imported"], 1)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("125"))

    def test_replace_mode_sets_absolute(self):
        csv_text = ("sku,warehouse,quantity\nWID-A,Main Warehouse,40\n")
        self._post(csv_text, "replace")
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("40"))

    def test_minimum_only_mode_no_stock_change(self):
        csv_text = ("sku,warehouse,minimum stock level\nWID-A,Main Warehouse,15\n")
        r = self._post(csv_text, "minimum")
        self.assertEqual(r.context["results"]["summary"]["updated"], 1)
        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.reorder_level, D("15"))
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("100"))

    def test_unknown_product_reported(self):
        csv_text = ("sku,warehouse,quantity\nNOPE,Main Warehouse,5\n")
        r = self._post(csv_text, "add")
        summary = r.context["results"]["summary"]
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["imported"], 0)

    def test_invalid_quantity_reported(self):
        csv_text = ("sku,warehouse,quantity\nWID-A,Main Warehouse,abc\n")
        r = self._post(csv_text, "add")
        self.assertEqual(r.context["results"]["summary"]["failed"], 1)

    def test_duplicate_rows_rejected(self):
        csv_text = ("sku,warehouse,quantity\n"
                    "WID-A,Main Warehouse,5\nWID-A,Main Warehouse,5\n")
        r = self._post(csv_text, "add")
        summary = r.context["results"]["summary"]
        self.assertEqual(summary["imported"], 1)
        self.assertEqual(summary["failed"], 1)

    def test_import_creates_audit_and_movements(self):
        csv_text = ("sku,warehouse,quantity,notes\nWID-A,Main Warehouse,10,restock\n")
        self._post(csv_text, "add")
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="inventory.imported").exists())
        self.assertTrue(inventory.StockMovement.objects.for_business(
            self.business_a).filter(reference_type="Import").exists())

    def test_import_requires_permission(self):
        self.client.force_login(self.cashier_a)
        r = self.client.get(reverse("inventory:import"))
        self.assertEqual(r.status_code, 403)

    def test_cross_tenant_product_not_matched(self):
        # business B's product sku must not be importable into business A
        csv_text = ("sku,warehouse,quantity\nWID-B,Main Warehouse,5\n")
        r = self._post(csv_text, "add")
        self.assertEqual(r.context["results"]["summary"]["failed"], 1)


class StatementPdfTests(TenantTestCase):
    def setUp(self):
        from apps.sales import services as sales

        self.allow_no_shift()
        self.customer = Customer.objects.create(
            business=self.business_a, code="PDF-C", full_name="PDF Customer",
            mobile="900222", credit_limit=D("1000"))
        # A credit sale with a long invoice ref + later payment
        self.sale = sales.complete_sale(
            business=self.business_a, branch=self.branch_a,
            warehouse=self.warehouse_a, cashier=self.owner_a,
            customer=self.customer,
            items=[{"product": self.product_a, "quantity": D("3"),
                    "unit_price": D("10.000")}],
            payments=[{"method": self.credit_a, "amount": D("31.500")}],
            membership=self.membership_a(),
        )
        self.client.force_login(self.owner_a)

    def test_statement_pdf_renders(self):
        r = self.client.get(
            reverse("customers:statement", args=[self.customer.public_id]),
            {"export": "pdf"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_statement_pdf_audited(self):
        self.client.get(
            reverse("customers:statement", args=[self.customer.public_id]),
            {"export": "pdf"})
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a,
            action="customer.statement_exported").exists())

    def test_statement_csv_has_closing_balance(self):
        r = self.client.get(
            reverse("customers:statement", args=[self.customer.public_id]),
            {"export": "csv"})
        body = r.content.decode()
        self.assertIn("CLOSING BALANCE", body)
        self.assertIn("Debit", body)
