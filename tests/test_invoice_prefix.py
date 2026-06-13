"""Regression tests for the invoice-prefix bug.

Root cause: the per-branch `invoice_prefix` shadowed the Business
Settings prefix, so the configured value was never used. The fix makes
BusinessSettings.invoice_prefix authoritative, with an opt-in
per-branch-code scheme.
"""
from decimal import Decimal

from django.urls import reverse

from apps.sales.models import Sale

from .base import TenantTestCase

D = Decimal


class InvoicePrefixTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        # branch_a was provisioned with code/prefix "HO" — it must NOT
        # shadow the business setting any more.
        self.settings = self.business_a.settings

    def _set_prefix(self, prefix, include_branch=False):
        self.settings.invoice_prefix = prefix
        self.settings.invoice_include_branch_code = include_branch
        self.settings.save()

    def test_business_prefix_is_used_not_branch_prefix(self):
        self._set_prefix("INV")
        sale = self.make_sale()
        self.assertTrue(sale.invoice_number.startswith("INV-"),
                        sale.invoice_number)
        self.assertNotIn("HO", sale.invoice_number)

    def test_changing_prefix_affects_only_new_sales(self):
        self._set_prefix("INV")
        old = self.make_sale()
        old_number = old.invoice_number
        self._set_prefix("BILL")
        new = self.make_sale()
        self.assertTrue(new.invoice_number.startswith("BILL-"))
        # Historical number untouched
        old.refresh_from_db()
        self.assertEqual(old.invoice_number, old_number)
        self.assertTrue(old_number.startswith("INV-"))

    def test_global_numbering_is_sequential_across_branches(self):
        from apps.branches.models import Branch, Warehouse
        from apps.sales import services as sales

        self._set_prefix("INV")  # global (default)
        branch2 = Branch.objects.create(
            business=self.business_a, name="Second", code="HK",
            invoice_prefix="HK")
        Warehouse.objects.create(business=self.business_a, name="W2",
                                 code="W2HK", branch=branch2)
        s1 = self.make_sale()
        s2 = sales.complete_sale(
            business=self.business_a, branch=branch2,
            warehouse=self.warehouse_a, cashier=self.owner_a,
            customer=self.walk_in_a,
            items=[{"product": self.product_a, "quantity": D("1"),
                    "unit_price": D("10.000")}],
            payments=[{"method": self.cash_a, "amount": D("10.50")}],
            membership=self.membership_a())
        n1 = int(s1.invoice_number.rsplit("-", 1)[1])
        n2 = int(s2.invoice_number.rsplit("-", 1)[1])
        self.assertEqual(n2, n1 + 1)  # one shared global counter
        self.assertTrue(s2.invoice_number.startswith("INV-"))

    def test_per_branch_scheme_includes_branch_code(self):
        self._set_prefix("INV", include_branch=True)
        sale = self.make_sale()  # branch_a invoice_prefix "HO"
        # base + branch segment + year + seq
        self.assertTrue(sale.invoice_number.startswith("INV-HO-"),
                        sale.invoice_number)

    def test_per_branch_scheme_numbers_branches_independently(self):
        from apps.branches.models import Branch
        from apps.sales import services as sales

        self._set_prefix("INV", include_branch=True)
        branch2 = Branch.objects.create(
            business=self.business_a, name="Khoud", code="KHD",
            invoice_prefix="KHD")
        s1 = self.make_sale()  # HO branch
        s2 = sales.complete_sale(
            business=self.business_a, branch=branch2,
            warehouse=self.warehouse_a, cashier=self.owner_a,
            customer=self.walk_in_a,
            items=[{"product": self.product_a, "quantity": D("1"),
                    "unit_price": D("10.000")}],
            payments=[{"method": self.cash_a, "amount": D("10.50")}],
            membership=self.membership_a())
        self.assertIn("-HO-", s1.invoice_number)
        self.assertIn("-KHD-", s2.invoice_number)
        # each branch starts its own counter at 1
        self.assertTrue(s1.invoice_number.endswith("000001"))
        self.assertTrue(s2.invoice_number.endswith("000001"))

    def test_receipt_invoice_and_statement_show_configured_number(self):
        from apps.customers.models import Customer

        self._set_prefix("INV")
        customer = Customer.objects.create(
            business=self.business_a, code="PFX", full_name="Prefix Cust",
            credit_limit=D("500"))
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}])
        self.assertTrue(sale.invoice_number.startswith("INV-"))
        self.client.force_login(self.owner_a)
        for name in ("sales:detail", "sales:invoice", "sales:receipt"):
            r = self.client.get(reverse(name, args=[sale.public_id]))
            self.assertContains(r, sale.invoice_number, msg_prefix=name)
        # PDF invoice
        r = self.client.get(reverse("sales:invoice_pdf", args=[sale.public_id]))
        self.assertTrue(r.content.startswith(b"%PDF"))
        # Customer statement references the same invoice number
        r = self.client.get(
            reverse("customers:statement", args=[customer.public_id]))
        self.assertContains(r, sale.invoice_number)

    def test_blank_prefix_falls_back_to_inv(self):
        self._set_prefix("")
        sale = self.make_sale()
        self.assertTrue(sale.invoice_number.startswith("INV-"))

    def test_invoice_numbers_remain_unique(self):
        self._set_prefix("INV")
        numbers = {self.make_sale().invoice_number for _ in range(5)}
        self.assertEqual(len(numbers), 5)
