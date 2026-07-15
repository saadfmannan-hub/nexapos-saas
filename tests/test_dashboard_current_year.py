from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product
from apps.customers.models import Customer
from apps.expenses.models import Expense, ExpenseCategory
from apps.inventory import services as inventory
from apps.reports.queries import current_year_financial_summary, profit_summary
from apps.sales import services as sales
from apps.sales.models import PaymentMethod, Sale, SalePayment, SaleReturn

from .base import TenantTestCase


D = Decimal


class CurrentYearDashboardTests(TenantTestCase):
    BASELINE_QUERY_COUNT = 44
    MAX_ADDED_QUERIES = 6

    def setUp(self):
        self.allow_no_shift()
        self.today = timezone.localdate()
        self.previous_year_date = date(self.today.year - 1, 12, 31)
        self.client.force_login(self.owner_a)

    def summary(self, *, branch_id=None, membership=None, include_profit=True):
        return current_year_financial_summary(
            self.business_a,
            membership or self.membership_a(),
            branch_id=branch_id,
            today=self.today,
            include_profit=include_profit,
        )

    def set_sale_date(self, sale, value):
        timestamp = timezone.make_aware(
            datetime.combine(value, time(hour=12)),
            timezone.get_current_timezone(),
        )
        Sale.objects.filter(pk=sale.pk).update(sale_date=timestamp)
        sale.refresh_from_db()
        return sale

    def set_return_date(self, sale_return, value):
        timestamp = timezone.make_aware(
            datetime.combine(value, time(hour=12)),
            timezone.get_current_timezone(),
        )
        SaleReturn.objects.filter(pk=sale_return.pk).update(created_at=timestamp)

    def make_customer(self, code="CY-CREDIT"):
        return Customer.objects.create(
            business=self.business_a,
            code=code,
            full_name=f"Current Year Customer {code}",
            credit_limit=D("1000.000"),
        )

    def make_expense(self, amount, status, *, expense_date=None, label="Operating"):
        category, _created = ExpenseCategory.objects.get_or_create(
            business=self.business_a,
            name=label,
            parent=None,
        )
        number = Expense.objects.for_business(self.business_a).count() + 1
        return Expense.objects.create(
            business=self.business_a,
            expense_number=f"CY-EXP-{number:03d}",
            expense_date=expense_date or self.today,
            branch=self.branch_a,
            category=category,
            amount=D(str(amount)),
            status=status,
            created_by=self.owner_a,
        )

    def make_second_branch(self):
        branch = Branch.objects.create(
            business=self.business_a,
            name="Current Year Branch",
            code="CYB",
        )
        warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=branch,
            name="Current Year Warehouse",
            code="CYB-WH",
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=warehouse,
            product=self.product_a,
            quantity=D("20.000"),
            unit_cost=D("4.000"),
            user=self.owner_a,
        )
        return branch, warehouse

    def make_branch_sale(self, branch, warehouse):
        return sales.complete_sale(
            business=self.business_a,
            branch=branch,
            warehouse=warehouse,
            cashier=self.owner_a,
            customer=self.walk_in_a,
            membership=self.membership_a(),
            items=[{
                "product": self.product_a,
                "quantity": D("1.000"),
                "unit_price": D("10.000"),
            }],
            payments=[{"method": self.cash_a, "amount": D("10.500")}],
        )

    def test_dates_and_heading_use_dynamic_application_local_date(self):
        local_today = date(2032, 5, 6)
        with patch("apps.reports.views.business_localdate", return_value=local_today):
            response = self.client.get(reverse("dashboard"))

        current_year = response.context["current_year"]
        self.assertEqual(current_year["year"], 2032)
        self.assertEqual(current_year["start_date"], date(2032, 1, 1))
        self.assertEqual(current_year["end_date"], local_today)
        self.assertContains(response, "This Year Overview &mdash; 2032", html=True)
        self.assertContains(response, "January 1, 2032 to Today")

    def test_current_sales_are_included_and_previous_or_voided_sales_are_excluded(self):
        current = self.make_sale()
        previous = self.set_sale_date(self.make_sale(), self.previous_year_date)
        voided = self.make_sale()
        Sale.objects.filter(pk=voided.pk).update(status=Sale.Status.VOIDED)

        result = self.summary()

        self.assertEqual(current.total, D("21.000"))
        self.assertEqual(previous.total, D("21.000"))
        self.assertEqual(result["total_sales"], D("21.000"))
        self.assertEqual(result["total_income"], D("42.000"))

    def test_existing_date_filters_do_not_change_current_year_values(self):
        self.make_sale()
        prior = str(self.previous_year_date)
        filtered = self.client.get(reverse("dashboard"), {"from": prior, "to": prior})
        today = self.client.get(
            reverse("dashboard"),
            {"from": str(self.today), "to": str(self.today)},
        )

        self.assertEqual(
            filtered.context["current_year"],
            today.context["current_year"],
        )

    def test_income_uses_payment_date_and_includes_payment_for_older_invoice(self):
        older_invoice = self.set_sale_date(self.make_sale(), self.previous_year_date)
        current_invoice = self.make_sale()
        SalePayment.objects.filter(sale=current_invoice).update(
            payment_date=self.previous_year_date
        )

        result = self.summary()

        self.assertEqual(older_invoice.total, D("21.000"))
        self.assertEqual(result["total_sales"], D("21.000"))
        self.assertEqual(result["total_income"], D("21.000"))

    def test_income_includes_online_and_other_and_excludes_customer_credit(self):
        online = PaymentMethod.objects.create(
            business=self.business_a,
            name="Mobile Payment",
            kind=PaymentMethod.Kind.ONLINE,
        )
        other = PaymentMethod.objects.create(
            business=self.business_a,
            name="Approved Other Payment",
            kind=PaymentMethod.Kind.OTHER,
        )
        customer = self.make_customer()
        self.make_sale(payments=[{"method": online, "amount": D("21.000")}])
        self.make_sale(payments=[{"method": other, "amount": D("21.000")}])
        self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )

        self.assertEqual(self.summary()["total_income"], D("42.000"))

    def test_receivable_matches_existing_current_invoice_balance_logic(self):
        customer = self.make_customer()
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )
        sales.process_return(
            sale=sale,
            items=[{"sale_item": sale.items.get(), "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CUSTOMER_ACCOUNT,
            user=self.owner_a,
        )

        self.assertEqual(self.summary()["total_receivable"], D("10.500"))

    def test_expenses_include_only_posted_current_year_transactions(self):
        self.make_expense("10.000", Expense.Status.APPROVED)
        self.make_expense("20.000", Expense.Status.PAID)
        self.make_expense("30.000", Expense.Status.DRAFT)
        self.make_expense("40.000", Expense.Status.SUBMITTED)
        self.make_expense("50.000", Expense.Status.REJECTED)
        self.make_expense("60.000", Expense.Status.CANCELLED)
        self.make_expense(
            "70.000",
            Expense.Status.APPROVED,
            expense_date=self.previous_year_date,
        )

        self.assertEqual(self.summary()["total_expenses"], D("30.000"))

    def test_posted_fixed_and_recurring_entries_count_but_unposted_template_does_not(self):
        self.make_expense("12.000", Expense.Status.APPROVED, label="Fixed Rent")
        self.make_expense("8.000", Expense.Status.PAID, label="Recurring Utilities")
        self.make_expense("99.000", Expense.Status.DRAFT, label="Recurring Template")

        self.assertEqual(self.summary()["total_expenses"], D("20.000"))

    def test_returns_use_return_date_and_net_sales_is_sales_minus_returns(self):
        previous_sale = self.set_sale_date(self.make_sale(), self.previous_year_date)
        current_sale = self.make_sale()
        current_return = sales.process_return(
            sale=previous_sale,
            items=[{"sale_item": previous_sale.items.get(), "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        previous_return = sales.process_return(
            sale=current_sale,
            items=[{"sale_item": current_sale.items.get(), "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        self.set_return_date(current_return, self.today)
        self.set_return_date(previous_return, self.previous_year_date)

        result = self.summary()

        self.assertEqual(result["total_sales"], D("21.000"))
        self.assertEqual(result["total_returns"], D("10.500"))
        self.assertEqual(result["net_sales"], D("10.500"))

    def test_gross_profit_matches_existing_return_adjusted_report_logic(self):
        sale = self.make_sale()
        sales.process_return(
            sale=sale,
            items=[{"sale_item": sale.items.get(), "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        report = profit_summary(
            self.business_a,
            {"date_from": date(self.today.year, 1, 1), "date_to": self.today},
        )
        report_values = dict(report["rows"])

        self.assertEqual(self.summary()["gross_profit"], report_values["Gross profit"])
        self.assertEqual(self.summary()["gross_profit"], D("6.000"))

    def test_estimated_net_profit_uses_gross_profit_minus_expenses_and_can_be_negative(self):
        self.make_sale()
        self.make_expense("20.000", Expense.Status.APPROVED)

        result = self.summary()
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(result["gross_profit"], D("12.000"))
        self.assertEqual(result["estimated_net_profit"], D("-8.000"))
        self.assertContains(response, "Estimated Net Profit")
        self.assertContains(response, "text-danger")

    def test_branch_filter_and_all_branches_use_the_same_year_range(self):
        self.make_sale()
        branch, warehouse = self.make_second_branch()
        self.make_branch_sale(branch, warehouse)

        all_branches = self.summary()
        selected_branch = self.summary(branch_id=branch.id)

        self.assertEqual(all_branches["total_sales"], D("31.500"))
        self.assertEqual(selected_branch["total_sales"], D("10.500"))

    def test_restricted_membership_excludes_unauthorized_branch_for_all_and_selected(self):
        self.make_sale()
        branch, warehouse = self.make_second_branch()
        self.make_branch_sale(branch, warehouse)
        membership = self.membership_a()
        membership.branches.set([self.branch_a])

        self.assertEqual(self.summary(membership=membership)["total_sales"], D("21.000"))
        self.assertEqual(
            self.summary(branch_id=branch.id, membership=membership)["total_sales"],
            D("0.000"),
        )
        response = self.client.get(reverse("dashboard"))
        self.assertNotIn(branch, list(response.context["branches"]))

    def test_tenant_isolation_excludes_other_business_transactions(self):
        self.allow_no_shift(self.business_b)
        cash_b = PaymentMethod.objects.for_business(self.business_b).get(kind="cash")
        sales.complete_sale(
            business=self.business_b,
            branch=self.branch_b,
            warehouse=self.warehouse_b,
            cashier=self.owner_b,
            customer=self.walk_in_b,
            membership=self.business_b.memberships.get(user=self.owner_b),
            items=[{
                "product": self.product_b,
                "quantity": D("1.000"),
                "unit_price": D("5.000"),
            }],
            payments=[{"method": cash_b, "amount": D("5.000")}],
        )

        result = self.summary()
        self.assertEqual(result["total_sales"], D("0.000"))
        self.assertEqual(result["total_income"], D("0.000"))

    def test_empty_state_uses_zero_and_configured_omr_precision(self):
        self.business_a.currency_code = "OMR"
        self.business_a.currency_precision = 3
        self.business_a.save(update_fields=["currency_code", "currency_precision"])

        response = self.client.get(reverse("dashboard"))

        for key, value in response.context["current_year"].items():
            if key not in {"year", "start_date", "end_date"}:
                self.assertIn(value, (D("0.000"), None), key)
        self.assertContains(response, "0.000")
        self.assertNotContains(response, "None")
        self.assertNotContains(response, "NaN")

    def test_profit_values_follow_existing_profit_permission(self):
        user = User.objects.create_user(
            email="dashboard-no-profit@example.com",
            password="StrongPass123!",
            full_name="Dashboard Without Profit",
        )
        role = Role.objects.create(
            business=self.business_a,
            name="Dashboard Without Profit",
            permissions=["dashboard.view"],
        )
        Membership.objects.create(
            business=self.business_a,
            user=user,
            role=role,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["current_year"]["gross_profit"])
        self.assertNotContains(response, "Gross Profit &mdash; This Year", html=True)
        self.assertNotContains(response, "Estimated Net Profit &mdash; This Year", html=True)

    def test_dashboard_query_growth_is_bounded(self):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(captured),
            self.BASELINE_QUERY_COUNT + self.MAX_ADDED_QUERIES,
        )

    def test_original_period_cards_still_follow_selected_dates(self):
        self.make_sale()
        self.set_sale_date(self.make_sale(), self.previous_year_date)

        response = self.client.get(
            reverse("dashboard"),
            {
                "from": str(self.previous_year_date),
                "to": str(self.previous_year_date),
            },
        )

        self.assertEqual(response.context["kpis"]["period_sales"], D("21.000"))
        self.assertEqual(response.context["current_year"]["total_sales"], D("21.000"))
