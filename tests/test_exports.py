"""Export and report tests."""
from decimal import Decimal

from django.urls import reverse

from .base import TenantTestCase

D = Decimal


class ExportTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.sale = self.make_sale()
        self.client.force_login(self.owner_a)

    def test_csv_export(self):
        response = self.client.get(
            reverse("reports:view", args=["sales_detailed"]) + "?export=csv"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn(self.sale.invoice_number, response.content.decode())

    def test_xlsx_export(self):
        response = self.client.get(
            reverse("reports:view", args=["sales_summary"]) + "?export=xlsx"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertTrue(response.content.startswith(b"PK"))  # zip magic

    def test_pdf_export(self):
        response = self.client.get(
            reverse("reports:view", args=["sales_summary"]) + "?export=pdf"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_daily_sales_report_screen_uses_fixed_column_layout(self):
        response = self.client.get(reverse("reports:view", args=["sales_summary"]))
        self.assertContains(response, "report-table-sales_summary")
        self.assertContains(response, "report-col-date")
        self.assertContains(response, "report-col-invoice")
        self.assertContains(response, "report-col-receivable")
        self.assertContains(response, self.sale.invoice_number)

    def test_daily_sales_report_pdf_uses_landscape_column_layout(self):
        from django.template.loader import render_to_string

        html = render_to_string("reports/report_pdf.html", {
            "title": "Daily Sales Report",
            "business": self.business_a,
            "filters_label": "2026-07-01 -> 2026-07-07",
            "data": {
                "columns": [
                    "Date", "Invoice No", "Sales Amount", "Bank Transfer",
                    "Card", "Cash", "Credit / Receivable", "Discount",
                    "VAT", "Gross",
                ],
                "rows": [[
                    "2026-07-07", "DT-2026-0000000001", D("30.840"),
                    D("10.840"), D("8.000"), D("12.000"), D("0.000"),
                    D("0.000"), D("1.468"), D("17.367"),
                ]],
                "totals": [
                    "TOTAL", "", D("30.840"), D("10.840"), D("8.000"),
                    D("12.000"), D("0.000"), D("0.000"), D("1.468"),
                    D("17.367"),
                ],
            },
        })
        self.assertIn("@page { size: A4 landscape; margin: 10mm; }", html)
        self.assertIn('style="width:13%; text-align:left;"', html)
        self.assertIn('class="nowrap">Invoice No</th>', html)
        self.assertIn('style="width:12%; text-align:right;"', html)
        self.assertIn('style="width:10%; text-align:right;"', html)
        self.assertIn("DT-2026-0000000001", html)

    def test_export_respects_date_filter(self):
        response = self.client.get(
            reverse("reports:view", args=["sales_detailed"])
            + "?from=1990-01-01&to=1990-01-02&export=csv"
        )
        self.assertNotIn(self.sale.invoice_number, response.content.decode())

    def test_export_totals_match_screen(self):
        screen = self.client.get(reverse("reports:view", args=["sales_summary"]))
        csv_resp = self.client.get(
            reverse("reports:view", args=["sales_summary"]) + "?export=csv"
        )
        totals = screen.context["data"]["totals"]
        self.assertIn(str(totals[2]), csv_resp.content.decode())

    def test_daily_sales_report_invoice_wise_payment_breakdown(self):
        from apps.customers.models import Customer
        from apps.reports.queries import sales_summary
        from apps.sales.models import PaymentMethod

        bank = PaymentMethod.objects.for_business(self.business_a).get(kind="bank")
        customer = Customer.objects.create(
            business=self.business_a, code="DAILY-CR",
            full_name="Daily Credit Customer", credit_limit=D("500.000"),
        )
        card_sale = self.make_sale(
            payments=[{"method": self.card_a, "amount": D("21.000")}],
        )
        bank_sale = self.make_sale(
            payments=[{"method": bank, "amount": D("21.000")}],
        )
        split_sale = self.make_sale(
            customer=customer,
            payments=[
                {"method": self.cash_a, "amount": D("5.000")},
                {"method": self.card_a, "amount": D("7.000")},
                {"method": bank, "amount": D("3.000")},
                {"method": self.credit_a, "amount": D("6.000")},
            ],
        )
        partial_sale = self.make_sale(
            customer=customer,
            payments=[
                {"method": self.cash_a, "amount": D("8.000")},
                {"method": self.credit_a, "amount": D("13.000")},
            ],
        )
        unpaid_sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )

        data = sales_summary(self.business_a, {
            "date_from": None, "date_to": None, "branch_id": None,
            "warehouse_id": None,
        })
        self.assertEqual(data["columns"], [
            "Date", "Invoice No", "Sales Amount", "Bank Transfer", "Card",
            "Cash", "Credit / Receivable", "Discount", "VAT", "Gross",
        ])
        rows = {row[1]: row for row in data["rows"]}

        cash_row = rows[self.sale.invoice_number]
        self.assertEqual(cash_row[2:7], [D("21.000"), D("0.000"), D("0.000"),
                                         D("21.000"), D("0.000")])

        card_row = rows[card_sale.invoice_number]
        self.assertEqual(card_row[2:7], [D("21.000"), D("0.000"), D("21.000"),
                                         D("0.000"), D("0.000")])

        bank_row = rows[bank_sale.invoice_number]
        self.assertEqual(bank_row[2:7], [D("21.000"), D("21.000"), D("0.000"),
                                         D("0.000"), D("0.000")])

        split_row = rows[split_sale.invoice_number]
        self.assertEqual(split_row[2:7], [D("21.000"), D("3.000"), D("7.000"),
                                          D("5.000"), D("6.000")])

        partial_row = rows[partial_sale.invoice_number]
        self.assertEqual(partial_row[2:7], [D("21.000"), D("0.000"), D("0.000"),
                                            D("8.000"), D("13.000")])

        unpaid_row = rows[unpaid_sale.invoice_number]
        self.assertEqual(unpaid_row[2:7], [D("21.000"), D("0.000"), D("0.000"),
                                           D("0.000"), D("21.000")])

        self.assertEqual(data["totals"][2:7], [D("126.000"), D("24.000"),
                                               D("28.000"), D("34.000"),
                                               D("40.000")])
        self.assertEqual(data["totals"][7:10], [D("0.000"), D("6.000"),
                                                D("72.000")])

        response = self.client.get(
            reverse("reports:view", args=["sales_summary"]) + "?export=csv"
        )
        csv_body = response.content.decode()
        self.assertIn(
            "Date,Invoice No,Sales Amount,Bank Transfer,Card,Cash,"
            "Credit / Receivable,Discount,VAT,Gross",
            csv_body,
        )
        self.assertIn(split_sale.invoice_number, csv_body)

    def test_commercial_report_columns_separate_credit_from_income(self):
        from apps.customers.models import Customer
        from apps.reports.queries import (
            customer_receivables,
            customer_sales,
            payment_methods_report,
            product_sales,
            tax_report,
        )
        from apps.sales.models import PaymentMethod

        bank = PaymentMethod.objects.for_business(self.business_a).get(kind="bank")
        customer = Customer.objects.create(
            business=self.business_a, code="GCC-001",
            full_name="GCC Tailoring Customer", mobile="+96890000001",
            credit_limit=D("500.000"),
        )
        split_sale = self.make_sale(
            customer=customer,
            payments=[
                {"method": self.cash_a, "amount": D("5.000")},
                {"method": self.card_a, "amount": D("7.000")},
                {"method": bank, "amount": D("3.000")},
                {"method": self.credit_a, "amount": D("6.000")},
            ],
        )
        filters = {
            "date_from": None, "date_to": None, "branch_id": None,
            "warehouse_id": None,
        }

        customer_data = customer_sales(self.business_a, filters)
        self.assertEqual(customer_data["columns"], [
            "Customer Name", "Phone Number", "Invoices", "Sales Amount",
            "Paid Amount", "Credit / Receivable", "Discount", "VAT",
            "Gross Profit",
        ])
        customer_row = [
            row for row in customer_data["rows"]
            if row[0] == "GCC Tailoring Customer"
        ][0]
        self.assertEqual(customer_row[1], "+96890000001")
        self.assertEqual(customer_row[4], D("15.000"))
        self.assertEqual(customer_row[5], D("6.000"))

        payment_data = payment_methods_report(self.business_a, filters)
        self.assertEqual(payment_data["columns"], [
            "Date", "Invoice No", "Customer", "Phone Number", "Cash", "Card",
            "Bank Transfer", "Customer Credit", "Total Received",
        ])
        payment_row = [
            row for row in payment_data["rows"]
            if row[1] == split_sale.invoice_number
        ][0]
        self.assertEqual(payment_row[4:9], [
            D("5.000"), D("7.000"), D("3.000"), D("6.000"), D("15.000"),
        ])

        receivables = customer_receivables(self.business_a, filters)
        self.assertEqual(receivables["columns"], [
            "Customer Name", "Phone Number", "Invoice No", "Invoice Date",
            "Sales Amount", "Paid Amount", "Credit / Receivable",
            "Due Date / Delivery Date", "Status",
        ])
        receivable_row = [
            row for row in receivables["rows"]
            if row[2] == split_sale.invoice_number
        ][0]
        self.assertEqual(receivable_row[5], D("15.000"))
        self.assertEqual(receivable_row[6], D("6.000"))

        product_data = product_sales(self.business_a, filters)
        self.assertEqual(product_data["columns"], [
            "Product Name", "SKU", "Category", "Qty Sold", "Sales Amount",
            "Discount", "VAT", "Cost", "Gross Profit",
        ])
        product_row = [
            row for row in product_data["rows"] if row[0] == self.product_a.name
        ][0]
        self.assertEqual(product_row[1], "WID-A")

        vat_data = tax_report(self.business_a, filters)
        self.assertEqual(vat_data["columns"], [
            "VAT Rate", "Taxable Amount", "VAT Amount",
        ])

    def test_export_requires_permission(self):
        from apps.accounts.models import Membership, Role, User

        viewer = User.objects.create_user(email="noexport@example.com",
                                          password="x" * 10, full_name="NoExp")
        role = Role.objects.for_business(self.business_a).get(
            name="Read-Only Viewer")
        Membership.objects.create(business=self.business_a, user=viewer, role=role)
        self.client.force_login(viewer)
        response = self.client.get(
            reverse("reports:view", args=["sales_detailed"]) + "?export=csv"
        )
        self.assertEqual(response.status_code, 403)

    def test_new_financial_reports_render_and_export(self):
        for key in ("profit_loss", "cash_flow", "expense_analysis",
                    "customer_sales"):
            response = self.client.get(reverse("reports:view", args=[key]))
            self.assertEqual(response.status_code, 200, key)
            response = self.client.get(
                reverse("reports:view", args=[key]) + "?export=csv")
            self.assertEqual(response.status_code, 200, key)

    def test_profit_loss_net_matches_components(self):
        from decimal import Decimal as D

        response = self.client.get(reverse("reports:view", args=["profit_loss"]))
        rows = {r[0]: r[1] for r in response.context["data"]["rows"] if r[0]}
        self.assertEqual(rows["GROSS PROFIT"], D("12.000"))
        self.assertEqual(rows["ESTIMATED NET PROFIT"], D("12.000"))

    def test_dashboard_renders_with_real_data(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["kpis"]["invoices"], 1)
        self.assertEqual(response.context["kpis"]["period_sales"], D("21.000"))

    def test_dashboard_owner_kpis_separate_sales_income_credit_and_returns(self):
        from apps.customers.models import Customer
        from apps.sales import services as sales
        from apps.sales.models import PaymentMethod, SaleReturn

        bank = PaymentMethod.objects.for_business(self.business_a).get(kind="bank")
        customer = Customer.objects.create(
            business=self.business_a, code="DASH-CR",
            full_name="Dashboard Credit Customer", mobile="+96890000002",
            credit_limit=D("500.000"),
        )
        credit_sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )
        split_sale = self.make_sale(
            customer=customer,
            payments=[
                {"method": self.cash_a, "amount": D("5.000")},
                {"method": self.card_a, "amount": D("7.000")},
                {"method": bank, "amount": D("3.000")},
                {"method": self.credit_a, "amount": D("6.000")},
            ],
        )
        self.product_a.reorder_level = D("200.000")
        self.product_a.save(update_fields=["reorder_level"])
        sales.process_return(
            sale=self.sale,
            items=[{"sale_item": self.sale.items.get(), "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        kpis = response.context["kpis"]
        self.assertEqual(kpis["today_sales"], D("63.000"))
        self.assertEqual(kpis["today_income"], D("36.000"))
        self.assertEqual(kpis["today_receivable"], D("27.000"))
        self.assertEqual(kpis["today_returns"], D("10.500"))
        self.assertEqual(kpis["today_net_sales"], D("52.500"))
        self.assertEqual(kpis["cash"], D("26.000"))
        self.assertEqual(kpis["card"], D("7.000"))
        self.assertEqual(kpis["bank"], D("3.000"))
        self.assertGreaterEqual(kpis["low_stock"], 1)

        payment_chart = response.context["chart_methods"]
        self.assertEqual(payment_chart["labels"], [
            "Cash", "Card", "Bank Transfer", "Customer Credit",
        ])
        self.assertIn(credit_sale.invoice_number, {
            sale.invoice_number for sale in response.context["widgets"]["recent_sales"]
        })
        self.assertIn(split_sale.invoice_number, {
            sale.invoice_number for sale in response.context["widgets"]["recent_sales"]
        })
