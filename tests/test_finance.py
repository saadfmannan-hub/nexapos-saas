"""Financial calculation tests: tax, discounts, balances, profit."""
from decimal import Decimal

from apps.catalog.models import Product
from apps.customers import services as customer_services
from apps.customers.models import Customer

from .base import TenantTestCase

D = Decimal


class TaxAndDiscountTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()

    def test_tax_exclusive_calculation(self):
        sale = self.make_sale(items=[{
            "product": self.product_a, "quantity": D("1"),
            "unit_price": D("100.000"),
        }], payments=[{"method": self.cash_a, "amount": D("105.000")}])
        self.assertEqual(sale.tax_amount, D("5.000"))
        self.assertEqual(sale.total, D("105.000"))

    def test_tax_inclusive_calculation(self):
        settings_obj = self.business_a.settings
        settings_obj.prices_include_tax = True
        settings_obj.save()
        sale = self.make_sale(items=[{
            "product": self.product_a, "quantity": D("1"),
            "unit_price": D("105.000"),
        }], payments=[{"method": self.cash_a, "amount": D("105.000")}])
        self.assertEqual(sale.total, D("105.000"))
        self.assertEqual(sale.tax_amount, D("5.000"))
        self.assertEqual(sale.subtotal, D("100.000"))

    def test_line_discount_reduces_tax_base(self):
        sale = self.make_sale(items=[{
            "product": self.product_a, "quantity": D("1"),
            "unit_price": D("100.000"), "discount_amount": D("20.000"),
        }], payments=[{"method": self.cash_a, "amount": D("84.000")}])
        self.assertEqual(sale.tax_amount, D("4.000"))
        self.assertEqual(sale.total, D("84.000"))

    def test_invoice_discount(self):
        sale = self.make_sale(
            items=[{"product": self.product_a, "quantity": D("2"),
                    "unit_price": D("10.000")}],
            payments=[{"method": self.cash_a, "amount": D("16.000")}],
            invoice_discount=D("5.000"),
        )
        self.assertEqual(sale.total, D("16.000"))

    def test_discount_cap_enforced(self):
        settings_obj = self.business_a.settings
        settings_obj.max_discount_percent = D("10")
        settings_obj.save()
        from apps.sales.services import SaleError

        with self.assertRaises(SaleError):
            self.make_sale(
                items=[{"product": self.product_a, "quantity": D("1"),
                        "unit_price": D("10.000"),
                        "discount_amount": D("5.000")}],
                payments=[{"method": self.cash_a, "amount": D("5.250")}],
            )

    def test_minimum_price_enforced_without_permission(self):
        product = Product.objects.create(
            business=self.business_a, name="MinPrice", sku="MIN-1",
            sale_price=D("10"), minimum_sale_price=D("8"),
            track_inventory=False, product_type="non_stock",
        )
        from apps.accounts.models import Membership
        from apps.sales import services as sales
        from apps.sales.services import SaleError

        cashier_membership = Membership.objects.get(
            business=self.business_a, user=self.cashier_a)
        with self.assertRaises(SaleError):
            sales.complete_sale(
                business=self.business_a, branch=self.branch_a,
                warehouse=self.warehouse_a, cashier=self.cashier_a,
                customer=self.walk_in_a,
                items=[{"product": product, "quantity": D("1"),
                        "unit_price": D("7.000")}],
                payments=[{"method": self.cash_a, "amount": D("7.000")}],
                membership=cashier_membership,
            )


class BalanceTests(TenantTestCase):
    def test_customer_payment_reduces_balance(self):
        customer = Customer.objects.create(
            business=self.business_a, code="BAL", full_name="Balance Test",
            balance=D("50.000"),
        )
        customer_services.apply_balance_change(customer.id, D("-20.000"))
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("30.000"))

    def test_customer_payment_view(self):
        from django.urls import reverse

        customer = Customer.objects.create(
            business=self.business_a, code="PAY", full_name="Payer",
            balance=D("40.000"),
        )
        self.client.force_login(self.owner_a)
        response = self.client.post(
            reverse("customers:payment", args=[customer.public_id]),
            {"amount": "15.000", "payment_method": self.cash_a.id,
             "reference": "", "notes": ""},
        )
        self.assertEqual(response.status_code, 302)
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("25.000"))

    def test_overcollection_blocked(self):
        from django.urls import reverse

        customer = Customer.objects.create(
            business=self.business_a, code="OVR", full_name="Over",
            balance=D("10.000"),
        )
        self.client.force_login(self.owner_a)
        self.client.post(
            reverse("customers:payment", args=[customer.public_id]),
            {"amount": "15.000", "payment_method": self.cash_a.id,
             "reference": "", "notes": ""},
        )
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("10.000"))

    def test_walk_in_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            self.walk_in_a.delete()


class ProfitReportTests(TenantTestCase):
    def test_profit_summary_includes_expenses(self):
        self.allow_no_shift()
        self.make_sale()  # gross profit 12.000
        from apps.expenses.models import Expense, ExpenseCategory

        category = ExpenseCategory.objects.for_business(self.business_a).first()
        Expense.objects.create(
            business=self.business_a, expense_number="EXP-000001",
            expense_date="2026-06-01", branch=self.branch_a,
            category=category, amount=D("3.000"), status="approved",
        )
        from apps.reports.queries import profit_summary

        data = profit_summary(self.business_a, {"date_from": None,
                                                "date_to": None,
                                                "branch_id": None})
        values = {row[0]: row[1] for row in data["rows"]}
        self.assertEqual(values["Gross profit"], D("12.000"))
        self.assertEqual(values["Operating expenses"], D("3.000"))
        self.assertEqual(values["Estimated net profit"], D("9.000"))
