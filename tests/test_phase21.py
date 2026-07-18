"""Phase 2.1 tests: customer import/export, product export,
inventory import/export, customer statement PDF."""
from decimal import Decimal

from django.urls import reverse

from apps.audit.models import AuditLog
from apps.catalog.models import Product, ProductVariant
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

    def test_export_get_does_not_write_audit_log(self):
        logs = AuditLog.objects.filter(
            business=self.business_a, action="customer.exported"
        )
        before = logs.count()

        response = self.client.get(reverse("customers:export"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(logs.count(), before)

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
        body = r.content.decode()
        self.assertIn("data-import-form", body)
        self.assertIn("Uploading...", body)
        self.assertIn("Please wait while we process your file.", body)
        self.assertIn("Import completed successfully.", body)
        self.assertIn("Rows processed", body)
        self.assertIn("Rows created", body)
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
        body = r.content.decode()
        self.assertIn("Import failed. Please check your file and try again.", body)
        self.assertIn("Errors count", body)
        self.assertIn("Row 2:", body)
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
        self.assertIn("Row,Field,Error", r.content.decode())
        self.assertIn("Error", r.content.decode())

    def test_import_export_more_options_notes_and_status(self):
        settings_obj = self.business_a.settings
        settings_obj.more_option_label_1 = "Measurement"
        settings_obj.save(update_fields=["more_option_label_1"])
        csv_text = (
            "customer code,customer name,mobile,opening balance,notes,active,measurement\n"
            "MEAS-1,Measured Customer,999001,25.000,Prefers SMS,No,42cm\n"
        )
        r = self.client.post(reverse("customers:import"),
                             {"file": _csv_upload(csv_text), "mode": "skip"})
        self.assertEqual(r.context["results"]["summary"]["imported"], 1)
        customer = Customer.objects.for_business(self.business_a).get(code="MEAS-1")
        self.assertEqual(customer.balance, D("25.000"))
        self.assertEqual(customer.notes, "Prefers SMS")
        self.assertFalse(customer.is_active)
        self.assertEqual(customer.more_options, {"1": "42cm"})

        export = self.client.get(reverse("customers:export"))
        body = export.content.decode()
        self.assertIn("Measurement", body)
        self.assertIn("42cm", body)

    def test_import_multiple_tailoring_more_options(self):
        settings_obj = self.business_a.settings
        labels = [
            "Toul", "Shoulders", "Chest", "Side", "Sleeves", "Design 3d No",
            "Daraz (1,2,3) Line", "Computer Design",
        ]
        for index, label in enumerate(labels, start=1):
            setattr(settings_obj, f"more_option_label_{index}", label)
        settings_obj.save(update_fields=[
            f"more_option_label_{index}" for index in range(1, len(labels) + 1)
        ])
        csv_text = (
            "customer code,customer name,mobile,Toul, shoulders ,CHEST,Side,"
            "Sleeves,Design 3D No,Daraz 1 2 3 Line,Computer Design\n"
            "TAILOR-1,Tailoring Customer,999002,60,18,40,22,24,D3-10,Line A,Logo\n"
        )
        r = self.client.post(reverse("customers:import"),
                             {"file": _csv_upload(csv_text), "mode": "skip"})
        self.assertEqual(r.context["results"]["summary"]["imported"], 1)
        customer = Customer.objects.for_business(self.business_a).get(
            code="TAILOR-1")
        self.assertEqual(customer.more_options, {
            "1": "60",
            "2": "18",
            "3": "40",
            "4": "22",
            "5": "24",
            "6": "D3-10",
            "7": "Line A",
            "8": "Logo",
        })

    def test_import_updates_existing_more_options_and_skips_blanks(self):
        settings_obj = self.business_a.settings
        settings_obj.more_option_label_1 = "Toul"
        settings_obj.more_option_label_2 = "Shoulders"
        settings_obj.more_option_label_3 = "Chest"
        settings_obj.save(update_fields=[
            "more_option_label_1", "more_option_label_2", "more_option_label_3",
        ])
        Customer.objects.create(
            business=self.business_a, code="TAILOR-2",
            full_name="Existing Tailor", mobile="999003",
            more_options={"1": "55", "2": "17", "3": "38"},
        )
        csv_text = (
            "customer code,customer name,mobile,Toul,Shoulders,Chest\n"
            "TAILOR-2,Existing Tailor Updated,999003,56,,39\n"
        )
        r = self.client.post(reverse("customers:import"),
                             {"file": _csv_upload(csv_text), "mode": "update"})
        self.assertEqual(r.context["results"]["summary"]["updated"], 1)
        customer = Customer.objects.for_business(self.business_a).get(
            code="TAILOR-2")
        self.assertEqual(customer.full_name, "Existing Tailor Updated")
        self.assertEqual(customer.more_options, {
            "1": "56",
            "2": "17",
            "3": "39",
        })

    def test_import_reports_unmapped_custom_field_columns(self):
        csv_text = (
            "customer code,customer name,mobile,Custom Sleeve\n"
            "TAILOR-3,Unmapped Custom,999004,24\n"
        )
        r = self.client.post(reverse("customers:import"),
                             {"file": _csv_upload(csv_text), "mode": "skip"})
        self.assertEqual(r.context["results"]["summary"]["failed"], 1)
        self.assertIn(
            "Unmapped customer custom field column",
            r.context["results"]["errors"][0][1],
        )
        self.assertFalse(Customer.objects.for_business(self.business_a).filter(
            code="TAILOR-3").exists())

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
        self.assertIn("Tax/VAT Rate", body)
        self.assertIn("Track Inventory", body)
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

    def test_export_get_does_not_write_audit_log(self):
        logs = AuditLog.objects.filter(
            business=self.business_a, action="product.exported"
        )
        before = logs.count()

        response = self.client.get(reverse("catalog:product_export"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(logs.count(), before)


class ProductImportTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def _post(self, csv_text, match_by="sku"):
        return self.client.post(
            reverse("catalog:product_import"),
            {"file": _csv_upload(csv_text), "match_by": match_by},
        )

    def test_template_headers_include_commercial_columns(self):
        r = self.client.get(reverse("catalog:import_template"))
        body = r.content.decode()
        for header in [
            "Product Name", "Tax/Vat Rate", "Tax Inclusive", "Track Inventory",
            "Opening Stock", "Warehouse", "Variant Parent", "Variant Sku",
        ]:
            self.assertIn(header, body)
        xlsx = self.client.get(reverse("catalog:import_template"), {"format": "xlsx"})
        self.assertTrue(xlsx.content.startswith(b"PK"))

    def test_product_import_with_vat_and_opening_stock(self):
        csv_text = (
            "product name,sku,barcode,category,brand,product type,unit,"
            "purchase price,sale price,cost price,tax/vat rate,tax inclusive,"
            "track inventory,opening stock,minimum stock,branch,warehouse\n"
            "Imported VAT,IMP-VAT,IMP-BC,Imported Cat,Imported Brand,standard,Piece,"
            "3.000,9.000,3.000,5,Yes,Yes,7,2,Head Office,Main Warehouse\n"
        )
        r = self._post(csv_text)
        body = r.content.decode()
        self.assertIn("data-import-form", body)
        self.assertIn("Uploading...", body)
        self.assertIn("Please wait while we process your file.", body)
        self.assertIn("Import completed successfully.", body)
        self.assertIn("Rows processed", body)
        self.assertIn("Rows created", body)
        self.assertEqual(r.context["results"]["summary"]["created"], 1)
        product = Product.objects.for_business(self.business_a).get(sku="IMP-VAT")
        self.assertEqual(product.tax_rate.rate, D("5.000"))
        self.assertTrue(product.price_includes_tax)
        self.assertEqual(product.reorder_level, D("2.000"))
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, product),
            D("7.000"),
        )

    def test_duplicate_sku_and_barcode_are_row_errors(self):
        csv_text = (
            "product name,sku,barcode\n"
            "Duplicate SKU,WID-A,UNIQUE-BC\n"
            "Duplicate Barcode,UNIQUE-SKU,1000000000017\n"
        )
        r = self._post(csv_text, match_by="name")
        body = r.content.decode()
        self.assertIn("Import failed. Please check your file and try again.", body)
        self.assertIn("Errors count", body)
        self.assertIn("Duplicate SKU", body)
        summary = r.context["results"]["summary"]
        self.assertEqual(summary["failed"], 2)
        messages = [msg for _row, msg in r.context["results"]["errors"]]
        self.assertTrue(any("Duplicate SKU" in msg for msg in messages))
        self.assertTrue(any("Duplicate barcode" in msg for msg in messages))

    def test_variant_import_and_export(self):
        csv_text = (
            "product name,sku,product type,unit,purchase price,sale price,"
            "variant option name,variant option value,variant sku,variant barcode,"
            "opening stock,warehouse\n"
            "Imported Shirt,IMP-SHIRT,variant,Piece,4.000,12.000,"
            "Size,Large,IMP-SHIRT-L,IMP-SHIRT-L-BC,3,Main Warehouse\n"
        )
        r = self._post(csv_text)
        self.assertEqual(r.context["results"]["summary"]["created"], 2)
        product = Product.objects.for_business(self.business_a).get(sku="IMP-SHIRT")
        variant = ProductVariant.objects.for_business(self.business_a).get(
            sku="IMP-SHIRT-L")
        self.assertEqual(variant.product, product)
        self.assertEqual(variant.attributes, {"Size": "Large"})
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, product, variant),
            D("3.000"),
        )
        export = self.client.get(reverse("catalog:product_export"))
        body = export.content.decode()
        self.assertIn("Variant Parent", body)
        self.assertIn("IMP-SHIRT-L", body)

    def test_error_report_download(self):
        self._post("product name,sku\n,WID-X\n")
        r = self.client.get(reverse("catalog:product_import"), {"errors": "1"})
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("Row,Field,Error", r.content.decode())


class InventoryExportTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def test_csv_export_columns_and_stock(self):
        r = self.client.get(reverse("inventory:export"))
        body = r.content.decode()
        for col in ["Current Stock", "Available Stock", "Stock Value",
                    "Warehouse", "Branch", "Variant SKU", "Unit Cost"]:
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
        body = r.content.decode()
        self.assertIn("data-import-form", body)
        self.assertIn("Uploading...", body)
        self.assertIn("Please wait while we process your file.", body)
        self.assertIn("Import completed successfully.", body)
        self.assertIn("Rows processed", body)
        self.assertIn("Rows applied", body)
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
        body = r.content.decode()
        self.assertIn("Import failed. Please check your file and try again.", body)
        self.assertIn("Errors count", body)
        self.assertIn("Invalid quantity", body)
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

    def test_template_headers_include_variant_and_unit_cost(self):
        r = self.client.get(reverse("inventory:import_template"))
        body = r.content.decode()
        self.assertIn("Variant Sku", body)
        self.assertIn("Variant Barcode", body)
        self.assertIn("Reason / Notes", body)
        self.assertIn("Unit Cost", body)

    def test_invalid_branch_warehouse_pair_rejected(self):
        from apps.branches.models import Branch, Warehouse

        other_branch = Branch.objects.create(
            business=self.business_a, name="Other Branch", code="OB")
        Warehouse.objects.create(
            business=self.business_a, branch=other_branch, name="Other Warehouse",
            code="OW")
        csv_text = (
            "sku,branch,warehouse,quantity\n"
            "WID-A,Other Branch,Main Warehouse,5\n"
        )
        r = self._post(csv_text, "add")
        self.assertEqual(r.context["results"]["summary"]["failed"], 1)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("100"),
        )

    def test_variant_sku_import_and_unit_cost_validation(self):
        parent = Product.objects.create(
            business=self.business_a, name="Variant Import Parent",
            sku="VAR-PARENT", product_type="variant", purchase_price=D("2.000"),
            sale_price=D("6.000"),
        )
        variant = ProductVariant.objects.create(
            business=self.business_a, product=parent, name="Blue",
            sku="VAR-BLUE", purchase_price=D("2.000"), sale_price=D("6.000"),
        )
        csv_text = (
            "variant sku,warehouse,quantity,unit cost,reason / notes\n"
            "VAR-BLUE,Main Warehouse,4,2.500,variant load\n"
            "VAR-BLUE,Main Warehouse,1,bad,bad cost\n"
        )
        r = self._post(csv_text, "add")
        summary = r.context["results"]["summary"]
        self.assertEqual(summary["imported"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, parent, variant),
            D("4.000"),
        )


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
