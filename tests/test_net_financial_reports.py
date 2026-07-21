"""Focused contracts for net sales, tender, and receivable reporting."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.branches.models import Branch, Warehouse
from apps.customers.models import Customer
from apps.inventory import services as inventory
from apps.registers import services as registers
from apps.reports.queries import (
    cash_flow,
    customer_sales,
    payment_methods_report,
    product_sales,
    profit_summary,
    returns_report,
    sales_summary,
)
from apps.sales import financials
from apps.sales import services as sales
from apps.sales.models import PaymentMethod, Sale, SalePayment, SaleReturn

from .base import TenantTestCase

D = Decimal
ZERO = D("0.000")


class NetFinancialReportTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        settings_obj = self.business_a.settings
        settings_obj.vat_enabled = False
        settings_obj.save(update_fields=["vat_enabled"])
        self.bank_a = PaymentMethod.objects.for_business(self.business_a).get(
            kind=PaymentMethod.Kind.BANK
        )
        self.customer = Customer.objects.create(
            business=self.business_a,
            home_branch=self.branch_a,
            code="NET-REPORT",
            full_name="Net Report Customer",
            credit_limit=D("1000.000"),
        )
        self.client.force_login(self.owner_a)

    def filters(self, **overrides):
        values = {
            "date_from": None,
            "date_to": None,
            "branch_id": None,
            "warehouse_id": None,
        }
        values.update(overrides)
        return values

    def make_hundred_sale(self, payments, **kwargs):
        return self.make_sale(
            customer=kwargs.pop("customer", self.customer),
            items=[{
                "product": self.product_a,
                "quantity": D("10.000"),
                "unit_price": D("10.000"),
            }],
            payments=payments,
            **kwargs,
        )

    def return_quantity(self, sale, quantity, method, **kwargs):
        return sales.process_return(
            sale=sale,
            items=[{
                "sale_item": sale.items.get(),
                "quantity": D(str(quantity)),
                "restock": False,
            }],
            refund_method=method,
            user=self.owner_a,
            restock=False,
            **kwargs,
        )

    def financial_sale(self, sale):
        sale = (
            Sale.objects.for_business(self.business_a)
            .prefetch_related("payments__method", "returns")
            .get(pk=sale.pk)
        )
        return financials.financial_summary_for_sale(sale)

    def test_full_mixed_tender_return_nets_every_financial_consumer(self):
        sale = self.make_hundred_sale([
            {"method": self.cash_a, "amount": D("25.000")},
            {"method": self.card_a, "amount": D("25.000")},
            {"method": self.bank_a, "amount": D("25.000")},
            {"method": self.credit_a, "amount": D("25.000")},
        ])
        for method in (
            SaleReturn.RefundMethod.CASH,
            SaleReturn.RefundMethod.CARD,
            SaleReturn.RefundMethod.BANK,
            SaleReturn.RefundMethod.CUSTOMER_ACCOUNT,
        ):
            self.return_quantity(sale, "2.500", method)

        summary = self.financial_sale(sale)
        self.assertEqual(summary.net_sales, ZERO)
        self.assertEqual(summary.net_paid, ZERO)
        self.assertEqual(summary.receivable, ZERO)
        for kind in (
            financials.CASH,
            financials.CARD,
            financials.BANK,
            financials.CUSTOMER_CREDIT,
        ):
            self.assertEqual(summary.tenders.amount(kind), ZERO)

        report_filters = self.filters()
        daily = sales_summary(self.business_a, report_filters)
        daily_row = next(row for row in daily["rows"] if row[1] == sale.invoice_number)
        self.assertEqual(daily_row[2:7], [ZERO, ZERO, ZERO, ZERO, ZERO])

        methods = payment_methods_report(self.business_a, report_filters)
        method_row = next(row for row in methods["rows"] if row[1] == sale.invoice_number)
        self.assertEqual(method_row[4:9], [ZERO, ZERO, ZERO, ZERO, ZERO])

        product_row = next(
            row for row in product_sales(self.business_a, report_filters)["rows"]
            if row[1] == self.product_a.sku
        )
        self.assertEqual(product_row[3], ZERO)
        self.assertEqual(product_row[4], ZERO)
        customer_row = customer_sales(self.business_a, report_filters)["rows"][0]
        self.assertEqual(customer_row[3:6], [ZERO, ZERO, ZERO])
        self.assertEqual(dict(profit_summary(
            self.business_a, report_filters
        )["rows"])["Gross profit"], ZERO)
        self.assertEqual(
            dict(cash_flow(self.business_a, report_filters)["rows"])[
                "NET CASH FLOW"
            ],
            ZERO,
        )

        returns = returns_report(self.business_a, report_filters)
        sale_returns = [row for row in returns["rows"] if row[2] == sale.invoice_number]
        self.assertEqual(len(sale_returns), 4)
        self.assertEqual(sum((row[9] for row in sale_returns), ZERO), D("100.000"))

        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.context["kpis"]["today_sales"], ZERO)
        self.assertEqual(dashboard.context["kpis"]["today_income"], ZERO)
        self.assertEqual(dashboard.context["kpis"]["today_receivable"], ZERO)

        sale_list = self.client.get(reverse("sales:list"))
        self.assertEqual(sale_list.context["totals"]["total"], ZERO)
        self.assertEqual(sale_list.context["totals"]["paid"], ZERO)
        customer_detail = self.client.get(
            reverse("customers:detail", args=[self.customer.public_id])
        )
        self.assertEqual(customer_detail.context["stats"]["total"], ZERO)
        self.assertEqual(customer_detail.context["stats"]["paid"], ZERO)

        csv_response = self.client.get(
            reverse("reports:view", args=["sales_summary"]),
            {"export": "csv"},
        )
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn(sale.invoice_number, csv_response.content.decode())
        self.assertIn("0.000", csv_response.content.decode())

    def test_partial_cash_return_preserves_other_tenders_and_receivable(self):
        sale = self.make_hundred_sale([
            {"method": self.cash_a, "amount": D("40.000")},
            {"method": self.card_a, "amount": D("30.000")},
            {"method": self.bank_a, "amount": D("20.000")},
            {"method": self.credit_a, "amount": D("10.000")},
        ])
        self.return_quantity(sale, "3.000", SaleReturn.RefundMethod.CASH)

        summary = self.financial_sale(sale)
        self.assertEqual(summary.net_sales, D("70.000"))
        self.assertEqual(summary.net_paid, D("60.000"))
        self.assertEqual(summary.receivable, D("10.000"))
        self.assertEqual(summary.tenders.amount(financials.CASH), D("10.000"))
        self.assertEqual(summary.tenders.amount(financials.CARD), D("30.000"))
        self.assertEqual(summary.tenders.amount(financials.BANK), D("20.000"))
        self.assertEqual(
            summary.tenders.amount(financials.CUSTOMER_CREDIT), D("10.000")
        )

        daily_row = sales_summary(self.business_a, self.filters())["rows"][0]
        self.assertEqual(
            daily_row[2:7],
            [D("70.000"), D("20.000"), D("30.000"), D("10.000"), D("10.000")],
        )
        method_row = payment_methods_report(
            self.business_a, self.filters()
        )["rows"][0]
        self.assertEqual(
            method_row[4:9],
            [D("10.000"), D("30.000"), D("20.000"), D("10.000"), D("60.000")],
        )

    def test_cash_refund_larger_than_cash_received_keeps_credit_outstanding(self):
        sale = self.make_hundred_sale([
            {"method": self.cash_a, "amount": D("40.000")},
            {"method": self.credit_a, "amount": D("60.000")},
        ])
        self.return_quantity(sale, "7.000", SaleReturn.RefundMethod.CASH)

        summary = self.financial_sale(sale)
        self.assertEqual(summary.net_sales, D("30.000"))
        self.assertEqual(summary.net_paid, D("-30.000"))
        self.assertEqual(summary.receivable, D("60.000"))
        self.assertEqual(summary.tenders.amount(financials.CASH), D("-30.000"))
        self.assertEqual(
            summary.tenders.amount(financials.CUSTOMER_CREDIT), D("60.000")
        )
        daily_row = sales_summary(self.business_a, self.filters())["rows"][0]
        self.assertEqual(daily_row[2], D("30.000"))
        self.assertEqual(daily_row[5], D("-30.000"))
        self.assertEqual(daily_row[6], D("60.000"))

    def test_one_fully_returned_invoice_does_not_hide_second_invoice(self):
        returned_sale = self.make_hundred_sale([
            {"method": self.cash_a, "amount": D("100.000")},
        ])
        kept_sale = self.make_hundred_sale([
            {"method": self.card_a, "amount": D("100.000")},
        ])
        self.return_quantity(
            returned_sale, "10.000", SaleReturn.RefundMethod.CASH
        )

        daily = sales_summary(self.business_a, self.filters())
        self.assertEqual(daily["totals"][2], D("100.000"))
        self.assertEqual(daily["totals"][4], D("100.000"))
        self.assertEqual(daily["totals"][5], ZERO)
        rows = {row[1]: row for row in daily["rows"]}
        self.assertEqual(rows[returned_sale.invoice_number][2], ZERO)
        self.assertEqual(rows[kept_sale.invoice_number][2], D("100.000"))

    def test_branch_filter_keeps_returns_with_their_originating_branch(self):
        branch_b = Branch.objects.create(
            business=self.business_a,
            name="Net Report Branch",
            code="NET-B",
        )
        warehouse_b = Warehouse.objects.create(
            business=self.business_a,
            branch=branch_b,
            name="Net Report Warehouse",
            code="NET-B-WH",
        )
        self.membership_a().branches.add(self.branch_a, branch_b)
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=warehouse_b,
            product=self.product_a,
            quantity=D("20.000"),
            unit_cost=D("4.000"),
            user=self.owner_a,
        )
        customer_b = Customer.objects.create(
            business=self.business_a,
            home_branch=branch_b,
            code="NET-B-CUSTOMER",
            full_name="Net Branch Customer",
        )
        branch_a_sale = self.make_hundred_sale([
            {"method": self.cash_a, "amount": D("100.000")},
        ])
        branch_b_sale = sales.complete_sale(
            business=self.business_a,
            branch=branch_b,
            warehouse=warehouse_b,
            cashier=self.owner_a,
            customer=customer_b,
            membership=self.membership_a(),
            items=[{
                "product": self.product_a,
                "quantity": D("10.000"),
                "unit_price": D("10.000"),
            }],
            payments=[{"method": self.cash_a, "amount": D("100.000")}],
        )
        self.return_quantity(
            branch_b_sale, "10.000", SaleReturn.RefundMethod.CASH
        )

        branch_a = sales_summary(
            self.business_a, self.filters(branch_id=self.branch_a.id)
        )
        branch_b_data = sales_summary(
            self.business_a, self.filters(branch_id=branch_b.id)
        )
        self.assertEqual(branch_a["totals"][2], D("100.000"))
        self.assertEqual(branch_b_data["totals"][2], ZERO)
        self.assertEqual(branch_a["rows"][0][1], branch_a_sale.invoice_number)
        self.assertEqual(branch_b_data["rows"][0][1], branch_b_sale.invoice_number)

    def test_dashboard_date_activity_books_refund_on_actual_return_date(self):
        sale = self.make_hundred_sale([
            {"method": self.cash_a, "amount": D("100.000")},
        ])
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        yesterday_at_noon = timezone.make_aware(
            datetime.combine(yesterday, time(hour=12)),
            timezone.get_current_timezone(),
        )
        Sale.objects.filter(pk=sale.pk).update(sale_date=yesterday_at_noon)
        SalePayment.objects.filter(sale=sale).update(payment_date=yesterday)
        self.return_quantity(sale, "10.000", SaleReturn.RefundMethod.CASH)

        yesterday_response = self.client.get(
            reverse("dashboard"),
            {"from": str(yesterday), "to": str(yesterday)},
        )
        today_response = self.client.get(
            reverse("dashboard"),
            {"from": str(today), "to": str(today)},
        )
        self.assertEqual(
            yesterday_response.context["kpis"]["period_sales"], D("100.000")
        )
        self.assertEqual(
            yesterday_response.context["kpis"]["period_income"], D("100.000")
        )
        self.assertEqual(
            today_response.context["kpis"]["period_sales"], D("-100.000")
        )
        self.assertEqual(
            today_response.context["kpis"]["period_income"], D("-100.000")
        )
        yesterday_cash_flow = dict(cash_flow(
            self.business_a,
            self.filters(date_from=yesterday, date_to=yesterday),
        )["rows"])
        today_cash_flow = dict(cash_flow(
            self.business_a,
            self.filters(date_from=today, date_to=today),
        )["rows"])
        self.assertEqual(yesterday_cash_flow["NET CASH FLOW"], D("100.000"))
        self.assertEqual(today_cash_flow["NET CASH FLOW"], D("-100.000"))

    def test_register_totals_net_each_actual_refund_method_once(self):
        shift = registers.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=D("50.000"),
        )
        sale = self.make_hundred_sale(
            [
                {"method": self.cash_a, "amount": D("25.000")},
                {"method": self.card_a, "amount": D("25.000")},
                {"method": self.bank_a, "amount": D("25.000")},
                {"method": self.credit_a, "amount": D("25.000")},
            ],
            register=self.register_a,
            shift=shift,
        )
        for method in (
            SaleReturn.RefundMethod.CASH,
            SaleReturn.RefundMethod.CARD,
            SaleReturn.RefundMethod.BANK,
            SaleReturn.RefundMethod.CUSTOMER_ACCOUNT,
        ):
            self.return_quantity(sale, "2.500", method, shift=shift)

        totals = registers.shift_totals(shift)
        self.assertEqual(totals["gross_cash_sales"], D("25.000"))
        self.assertEqual(totals["cash_refunds"], D("25.000"))
        self.assertEqual(totals["cash_sales"], ZERO)
        self.assertEqual(totals["card_sales"], ZERO)
        self.assertEqual(totals["bank_sales"], ZERO)
        self.assertEqual(totals["credit_sales"], ZERO)
        self.assertEqual(totals["expected_cash"], D("50.000"))
