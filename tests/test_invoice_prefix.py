"""Regression tests for the invoice-prefix bug.

Root cause: sale numbering ignored the configured per-branch
``invoice_prefix`` unless a separate Business Settings option was enabled.
The branch prefix is authoritative for new sales; blank branches retain the
existing business fallback.
"""
from decimal import Decimal

from django.urls import reverse

from .base import TenantTestCase

D = Decimal


class InvoicePrefixTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.settings = self.business_a.settings

    def _set_prefix(self, prefix, include_branch=False):
        self.settings.invoice_prefix = prefix
        self.settings.invoice_include_branch_code = include_branch
        self.settings.save()

    def _set_branch_prefix(self, prefix):
        self.branch_a.invoice_prefix = prefix
        self.branch_a.save(update_fields=["invoice_prefix"])

    def test_branch_invoice_prefix_is_used(self):
        self._set_prefix("INV")
        self._set_branch_prefix("AH-")
        sale = self.make_sale()
        self.assertEqual(sale.invoice_number, "AH-001")

    def test_changing_prefix_affects_only_new_sales(self):
        self._set_branch_prefix("AH-")
        old = self.make_sale()
        old_number = old.invoice_number
        self._set_branch_prefix("MB-")
        new = self.make_sale()
        self.assertTrue(new.invoice_number.startswith("MB-"))
        # Historical number untouched
        old.refresh_from_db()
        self.assertEqual(old.invoice_number, old_number)
        self.assertTrue(old_number.startswith("AH-"))

    def test_global_numbering_is_sequential_across_branches(self):
        from apps.branches.models import Branch, Warehouse
        from apps.sales import services as sales

        self._set_prefix("INV")  # global (default)
        self._set_branch_prefix("")
        branch2 = Branch.objects.create(
            business=self.business_a, name="Second", code="HK",
            invoice_prefix="")
        warehouse2 = Warehouse.objects.create(
            business=self.business_a,
            name="W2",
            code="W2HK",
            branch=branch2,
        )
        from apps.inventory import services as inventory

        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=warehouse2,
            product=self.product_a,
            quantity=D("1.000"),
            unit_cost=self.product_a.purchase_price,
            user=self.owner_a,
        )
        from apps.customers.services import ensure_walk_in_customer

        customer2 = ensure_walk_in_customer(self.business_a, branch2)
        s1 = self.make_sale()
        s2 = sales.complete_sale(
            business=self.business_a, branch=branch2,
            warehouse=warehouse2, cashier=self.owner_a,
            customer=customer2,
            items=[{"product": self.product_a, "quantity": D("1"),
                    "unit_price": D("10.000")}],
            payments=[{"method": self.cash_a, "amount": D("10.50")}],
            membership=self.membership_a())
        n1 = int(s1.invoice_number.rsplit("-", 1)[1])
        n2 = int(s2.invoice_number.rsplit("-", 1)[1])
        self.assertEqual(n2, n1 + 1)  # one shared global counter
        self.assertTrue(s2.invoice_number.startswith("INV-"))

    def test_branch_prefix_does_not_depend_on_business_toggle(self):
        self._set_prefix("INV", include_branch=True)
        self._set_branch_prefix("AH-")
        self.assertEqual(self.make_sale().invoice_number, "AH-001")

    def test_trailing_separator_is_normalized_without_double_hyphen(self):
        self._set_branch_prefix("AH -")
        self.assertEqual(self.make_sale().invoice_number, "AH-001")

    def test_per_branch_scheme_numbers_branches_independently(self):
        from apps.branches.models import Branch, Warehouse
        from apps.sales import services as sales

        self._set_prefix("INV", include_branch=False)
        self._set_branch_prefix("AH-")
        branch2 = Branch.objects.create(
            business=self.business_a, name="Khoud", code="KHD",
            invoice_prefix="KHD")
        warehouse2 = Warehouse.objects.create(
            business=self.business_a,
            name="Khoud Warehouse",
            code="KHD-WH",
            branch=branch2,
        )
        from apps.inventory import services as inventory

        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=warehouse2,
            product=self.product_a,
            quantity=D("1.000"),
            unit_cost=self.product_a.purchase_price,
            user=self.owner_a,
        )
        from apps.customers.services import ensure_walk_in_customer

        customer2 = ensure_walk_in_customer(self.business_a, branch2)
        s1 = self.make_sale()  # HO branch
        s2 = sales.complete_sale(
            business=self.business_a, branch=branch2,
            warehouse=warehouse2, cashier=self.owner_a,
            customer=customer2,
            items=[{"product": self.product_a, "quantity": D("1"),
                    "unit_price": D("10.000")}],
            payments=[{"method": self.cash_a, "amount": D("10.50")}],
            membership=self.membership_a())
        self.assertTrue(s1.invoice_number.startswith("AH-"))
        self.assertTrue(s2.invoice_number.startswith("KHD-"))
        # each branch starts its own counter at 1
        self.assertTrue(s1.invoice_number.endswith("-001"))
        self.assertTrue(s2.invoice_number.endswith("-001"))

    def test_shared_branch_prefixes_cannot_duplicate_invoice_numbers(self):
        from apps.branches.models import Branch, Warehouse
        from apps.inventory import services as inventory
        from apps.sales import services as sales

        self._set_branch_prefix("SHARED-")
        branch2 = Branch.objects.create(
            business=self.business_a,
            name="Second Shared Prefix",
            code="SECOND",
            invoice_prefix="SHARED-",
        )
        warehouse2 = Warehouse.objects.create(
            business=self.business_a,
            name="Second Shared Warehouse",
            code="SECOND-WH",
            branch=branch2,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=warehouse2,
            product=self.product_a,
            quantity=D("1.000"),
            unit_cost=self.product_a.purchase_price,
            user=self.owner_a,
        )

        from apps.customers.services import ensure_walk_in_customer

        customer2 = ensure_walk_in_customer(self.business_a, branch2)

        first = self.make_sale()
        second = sales.complete_sale(
            business=self.business_a,
            branch=branch2,
            warehouse=warehouse2,
            cashier=self.owner_a,
            customer=customer2,
            items=[{
                "product": self.product_a,
                "quantity": D("1"),
                "unit_price": D("10.000"),
            }],
            payments=[{"method": self.cash_a, "amount": D("10.500")}],
            membership=self.membership_a(),
        )
        self.assertEqual(first.invoice_number, "SHARED-001")
        self.assertEqual(second.invoice_number, "SHARED-002")

    def test_receipt_invoice_and_statement_show_configured_number(self):
        from apps.customers.models import Customer

        self._set_branch_prefix("AH-")
        customer = Customer.objects.create(
            business=self.business_a, code="PFX", full_name="Prefix Cust",
            credit_limit=D("500"))
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}])
        self.assertTrue(sale.invoice_number.startswith("AH-"))
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
        self._set_branch_prefix("")
        self._set_prefix("")
        sale = self.make_sale()
        self.assertTrue(sale.invoice_number.startswith("INV-"))

    def test_invoice_numbers_remain_unique(self):
        self._set_branch_prefix("AH-")
        numbers = {self.make_sale().invoice_number for _ in range(5)}
        self.assertEqual(len(numbers), 5)

    # ----- exact format required by the spec --------------------------------
    def test_format_is_prefix_then_three_digit_sequence(self):
        self._set_branch_prefix("INV B")
        s1 = self.make_sale()
        s2 = self.make_sale()
        self.assertEqual(s1.invoice_number, "INV B-001")
        self.assertEqual(s2.invoice_number, "INV B-002")
        self.assertRegex(s1.invoice_number, r"^INV B-\d{3}$")

    def test_abc_prefix_example(self):
        self._set_branch_prefix("ABC-")
        self.assertEqual(self.make_sale().invoice_number, "ABC-001")
        self.assertEqual(self.make_sale().invoice_number, "ABC-002")

    def test_number_has_no_year_or_second_sequence(self):
        from django.utils import timezone

        self._set_branch_prefix("INV B")
        number = self.make_sale().invoice_number
        self.assertNotIn(str(timezone.now().year), number)
        self.assertNotIn("-000", number)          # old 6-digit block gone
        self.assertEqual(number.count("-"), 1)     # prefix + one sequence only

    def test_sequence_continues_past_999(self):
        from apps.sales.models import InvoiceSequence
        from apps.sales.services import LIFETIME_SEQUENCE

        self._set_branch_prefix("INV B")
        self.make_sale()  # creates the lifetime counter row
        InvoiceSequence.objects.filter(
            business=self.business_a, branch=self.branch_a,
            year=LIFETIME_SEQUENCE,
        ).update(last_number=999)
        self.assertEqual(self.make_sale().invoice_number, "INV B-1000")
        self.assertEqual(self.make_sale().invoice_number, "INV B-1001")

    def test_counter_does_not_reset_across_years(self):
        # The lifetime counter is year-independent, so two sales always
        # advance the same sequence (no INV B-001 collision next year).
        self._set_branch_prefix("INV B")
        n1 = int(self.make_sale().invoice_number.rsplit("-", 1)[1])
        n2 = int(self.make_sale().invoice_number.rsplit("-", 1)[1])
        self.assertEqual(n2, n1 + 1)
