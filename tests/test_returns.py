"""Sales return / refund tests."""
from decimal import Decimal

from django.urls import reverse

from apps.catalog.models import Product, ProductVariant
from apps.inventory import services as inventory
from apps.sales import services as sales
from apps.sales.models import Sale, SaleReturn
from apps.sales.services import SaleError

from .base import TenantTestCase

D = Decimal


class ReturnTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.sale = self.make_sale(items=[{
            "product": self.product_a, "quantity": D("4"),
            "unit_price": D("10.000"),
        }])  # total 42.000 (40 + 5% tax)

    def variant_sale(self, variant_names):
        product = Product.objects.create(
            business=self.business_a,
            name="Saad Fabrics",
            sku="RET-SAAD",
            product_type=Product.Type.VARIANT,
            track_inventory=False,
            sale_price=D("10.000"),
            tax_rate=self.tax_a,
        )
        variants = [
            ProductVariant.objects.create(
                business=self.business_a,
                product=product,
                name=name,
                sku=f"RET-SAAD-{name}",
                sale_price=D("10.000"),
            )
            for name in variant_names
        ]
        sale = self.make_sale(items=[
            {
                "product": product,
                "variant": variant,
                "quantity": D("1.000"),
                "unit_price": D("10.000"),
            }
            for variant in variants
        ])
        return sale

    def test_return_list_displays_single_returned_item_name(self):
        sale = self.variant_sale(["1"])
        item = sale.items.get()
        sales.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        self.client.force_login(self.owner_a)

        response = self.client.get(reverse("sales:return_list"))

        self.assertContains(response, "Returned Item(s)")
        self.assertContains(response, "Saad Fabrics \N{EM DASH} 1")

    def test_return_list_displays_multiple_returned_item_names_once(self):
        sale = self.variant_sale(["1", "2"])
        items = list(sale.items.order_by("pk"))
        sale_return = sales.process_return(
            sale=sale,
            items=[
                {"sale_item": item, "quantity": D("1.000")}
                for item in items
            ],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        self.client.force_login(self.owner_a)

        response = self.client.get(reverse("sales:return_list"))

        self.assertContains(response, "Saad Fabrics \N{EM DASH} 1")
        self.assertContains(response, "Saad Fabrics \N{EM DASH} 2")
        self.assertContains(response, sale_return.return_number, count=1)
        self.assertEqual(len(response.context["page_obj"].object_list), 1)

    def test_sale_detail_return_section_displays_exact_returned_item(self):
        sale = self.variant_sale(["2"])
        item = sale.items.get()
        sales.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        self.client.force_login(self.owner_a)

        response = self.client.get(reverse("sales:detail", args=[sale.public_id]))

        self.assertContains(response, "Returns against this invoice")
        self.assertContains(response, "Returned Item(s)")
        self.assertContains(response, "Saad Fabrics \N{EM DASH} 2")

    def discounted_credit_sale(self, product=None):
        from apps.customers.models import Customer

        product = product or self.product_a
        customer = Customer.objects.create(
            business=self.business_a, code="RET-NET", full_name="Return Net",
            credit_limit=D("500.000"),
        )
        return self.make_sale(
            customer=customer,
            items=[{"product": product, "quantity": D("4.000"),
                    "unit_price": D("25.000")}],
            payments=[
                {"method": self.cash_a, "amount": D("84.500")},
                {"method": self.credit_a, "amount": D("10.000")},
            ],
            invoice_discount=D("10.000"),
        )

    def test_partial_return_restores_stock_and_refund(self):
        item = self.sale.items.get()
        ret = sales.process_return(
            sale=self.sale, items=[{"sale_item": item, "quantity": D("1")}],
            refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
        )
        self.assertEqual(ret.refund_amount, D("10.500"))
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("97"),
        )
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.status, Sale.Status.PART_RETURNED)

    def test_full_return(self):
        item = self.sale.items.get()
        ret = sales.process_return(
            sale=self.sale, items=[{"sale_item": item, "quantity": D("4")}],
            refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
        )
        self.assertEqual(ret.refund_amount, D("42.000"))
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.status, Sale.Status.RETURNED)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("100"),
        )

    def test_cannot_return_more_than_sold(self):
        item = self.sale.items.get()
        with self.assertRaises(SaleError):
            sales.process_return(
                sale=self.sale, items=[{"sale_item": item, "quantity": D("5")}],
                refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
            )
        # And across multiple returns
        sales.process_return(
            sale=self.sale, items=[{"sale_item": item, "quantity": D("3")}],
            refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
        )
        item.refresh_from_db()
        with self.assertRaises(SaleError):
            sales.process_return(
                sale=self.sale, items=[{"sale_item": item, "quantity": D("2")}],
                refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
            )

    def test_store_credit_refund(self):
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="RC", full_name="Returner",
        )
        sale = self.make_sale(customer=customer)
        item = sale.items.get()
        sales.process_return(
            sale=sale, items=[{"sale_item": item, "quantity": D("2")}],
            refund_method=SaleReturn.RefundMethod.STORE_CREDIT, user=self.owner_a,
        )
        customer.refresh_from_db()
        self.assertEqual(customer.store_credit, D("21.000"))

    def test_customer_account_refund_reduces_balance(self):
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="CB", full_name="Credit Buyer",
            credit_limit=D("100"),
        )
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("21.000"))
        item = sale.items.get()
        sales.process_return(
            sale=sale, items=[{"sale_item": item, "quantity": D("2")}],
            refund_method=SaleReturn.RefundMethod.CUSTOMER_ACCOUNT,
            user=self.owner_a,
        )
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("0.000"))

        from apps.customers.views import _statement_entries

        entries, balance = _statement_entries(self.business_a, customer)
        return_entries = [e for e in entries if e["type"] == "Return credited"]
        self.assertEqual(return_entries[0]["credit"], D("21.000"))
        self.assertEqual(balance, D("0.000"))

    def test_partial_cash_return_updates_net_total_paid_and_balance(self):
        sale = self.discounted_credit_sale()
        item = sale.items.get()

        ret = sales.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )

        sale.refresh_from_db()
        self.assertEqual(ret.refund_amount, D("23.625"))
        self.assertEqual(sale.total, D("94.500"))
        self.assertEqual(sale.returned_amount, D("23.625"))
        self.assertEqual(sale.net_total, D("70.875"))
        self.assertEqual(sale.amount_paid, D("84.500"))
        self.assertEqual(sale.refunded_amount, D("23.625"))
        self.assertEqual(sale.net_amount_paid, D("60.875"))
        self.assertEqual(sale.balance, D("10.000"))
        self.assertEqual(sale.payment_state, "Partially Paid")

    def test_returned_invoice_detail_receipt_and_a4_show_net_summary(self):
        sale = self.discounted_credit_sale()
        item = sale.items.get()
        sales.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        self.client.force_login(self.owner_a)

        detail = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assertContains(detail, "Original Total")
        self.assertContains(detail, "Returned/Refunded")
        self.assertContains(detail, "Net Total")
        self.assertContains(detail, "70.88")
        self.assertContains(detail, "Net Paid")
        self.assertContains(detail, "60.88")
        self.assertContains(detail, "10.00")

        receipt = self.client.get(reverse("sales:receipt", args=[sale.public_id]))
        self.assertContains(receipt, "NET TOTAL")
        self.assertContains(receipt, "70.875")
        self.assertContains(receipt, "NET PAID")
        self.assertContains(receipt, "60.875")

        invoice = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        self.assertContains(invoice, "NET TOTAL")
        self.assertContains(invoice, "70.875")
        self.assertContains(invoice, "Net Paid")
        self.assertContains(invoice, "60.875")

    def test_reports_use_net_sales_and_net_returned_quantity(self):
        from apps.catalog.models import Product
        from apps.reports.queries import customer_sales, product_sales, sales_summary

        product = Product.objects.create(
            business=self.business_a, name="Returned Report Product",
            sku="RET-REPORT", sale_price=D("25.000"), tax_rate=self.tax_a,
            track_inventory=False, product_type="non_stock",
        )
        sale = self.discounted_credit_sale(product=product)
        item = sale.items.get()
        sales.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        filters = {"date_from": None, "date_to": None, "branch_id": None}

        product_row = [
            row for row in product_sales(self.business_a, filters)["rows"]
            if row[0] == "Returned Report Product"
        ][0]
        self.assertEqual(product_row[1], "RET-REPORT")
        self.assertEqual(product_row[3], D("3.000"))
        self.assertEqual(product_row[4], D("70.875"))
        self.assertEqual(product_row[6], D("3.375"))
        self.assertEqual(product_row[8], D("67.500"))

        summary_data = sales_summary(self.business_a, filters)
        summary = [
            row for row in summary_data["rows"]
            if row[1] == sale.invoice_number
        ][0]
        self.assertEqual(summary[2], D("70.875"))
        self.assertEqual(summary[8], D("3.375"))
        self.assertEqual(summary[9], D("67.500"))
        self.assertEqual(summary_data["totals"][2], D("112.875"))
        self.assertEqual(summary_data["totals"][8], D("5.375"))
        self.assertEqual(summary_data["totals"][9], D("91.500"))

        customer = customer_sales(self.business_a, filters)["rows"][0]
        self.assertEqual(customer[3], D("70.875"))
        self.assertEqual(customer[4], D("60.875"))
        self.assertEqual(customer[5], D("10.000"))

        from apps.reports.queries import returns_report

        returns = returns_report(self.business_a, filters)
        self.assertEqual(returns["columns"], [
            "Return Date", "Return No", "Invoice No", "Customer",
            "Phone Number", "Product", "SKU", "Returned Qty", "Unit Price",
            "Returned Amount", "Refund Method", "Reason", "Processed By",
        ])
        return_row = [
            row for row in returns["rows"] if row[2] == sale.invoice_number
        ][0]
        self.assertEqual(return_row[5], "Returned Report Product")
        self.assertEqual(return_row[6], "RET-REPORT")
        self.assertEqual(return_row[7], D("1.000"))
        self.assertEqual(return_row[9], D("23.625"))

    def test_non_restock_return_keeps_stock_out(self):
        item = self.sale.items.get()
        sales.process_return(
            sale=self.sale,
            items=[{"sale_item": item, "quantity": D("1"), "restock": False}],
            refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
            restock=False,
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("96"),
        )

    def test_voided_sale_cannot_be_returned(self):
        sales.void_sale(sale=self.sale, user=self.owner_a, reason="mistake")
        item = self.sale.items.get()
        with self.assertRaises(SaleError):
            sales.process_return(
                sale=self.sale, items=[{"sale_item": item, "quantity": D("1")}],
                refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
            )
