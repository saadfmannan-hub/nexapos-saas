"""Regression tests for the production-ready upgrade sprint."""
from decimal import Decimal

from django.urls import reverse

from apps.catalog.models import Product
from apps.sales.models import Sale

from .base import TenantTestCase

D = Decimal


class PaymentPrecisionTests(TenantTestCase):
    """Bug #1 — 'Payments do not cover the total' when Remaining = 0.00.

    Sales totals are stored at 3 dp but a 2-dp business displays/tenders
    2-dp amounts. The grand total must be rounded to the business
    precision so an exact on-screen payment always validates.
    """

    def setUp(self):
        self.allow_no_shift()
        # 29.90 + 5% tax = 31.395 raw — displays as 31.40 in a 2-dp business
        self.tricky = Product.objects.create(
            business=self.business_a, name="Earbuds", sku="EARB-1",
            purchase_price=D("12.000"), sale_price=D("29.900"),
            tax_rate=self.tax_a, track_inventory=False,
            product_type="non_stock",
        )

    def test_exact_display_amount_card_payment_succeeds(self):
        # business_a has currency_precision=2; tendering 31.40 (the shown
        # total) must NOT raise under/over-payment errors.
        sale = self.make_sale(
            items=[{"product": self.tricky, "quantity": D("1"),
                    "unit_price": D("29.900")}],
            payments=[{"method": self.card_a, "amount": D("31.40")}],
        )
        self.assertEqual(sale.total, D("31.400"))
        self.assertEqual(sale.rounding, D("0.005"))
        self.assertEqual(sale.status, Sale.Status.COMPLETED)
        self.assertEqual(sale.balance, D("0.000"))

    def test_exact_cash_payment_no_phantom_change(self):
        sale = self.make_sale(
            items=[{"product": self.tricky, "quantity": D("1"),
                    "unit_price": D("29.900")}],
            payments=[{"method": self.cash_a, "amount": D("31.40")}],
        )
        self.assertEqual(sale.change_due, D("0.000"))
        self.assertEqual(sale.amount_paid, D("31.400"))

    def test_three_dp_business_unaffected(self):
        # business_b is OMR-style 3 dp: totals keep all three decimals.
        from apps.sales import services as sales
        from apps.sales.models import PaymentMethod

        self.allow_no_shift(self.business_b)
        cash_b = PaymentMethod.objects.for_business(self.business_b).get(kind="cash")
        sale = sales.complete_sale(
            business=self.business_b, branch=self.branch_b,
            warehouse=self.warehouse_b, cashier=self.owner_b,
            customer=self.walk_in_b,
            items=[{"product": self.product_b, "quantity": D("3"),
                    "unit_price": D("1.115")}],
            payments=[{"method": cash_b, "amount": D("3.345")}],
            membership=self.business_b.memberships.get(user=self.owner_b),
        )
        self.assertEqual(sale.total, D("3.345"))
        self.assertEqual(sale.rounding, D("0.000"))

    def test_rounding_disabled_keeps_raw_total(self):
        settings_obj = self.business_a.settings
        settings_obj.price_rounding = "none"
        settings_obj.save()
        sale = self.make_sale(
            items=[{"product": self.tricky, "quantity": D("1"),
                    "unit_price": D("29.900")}],
            payments=[{"method": self.cash_a, "amount": D("31.395")}],
        )
        self.assertEqual(sale.total, D("31.395"))


class PurchaseOrderDocumentTests(TenantTestCase):
    """Bug #3 — purchase order print / PDF / email / supplier share link."""

    def setUp(self):
        from apps.purchases import services as purchases
        from apps.suppliers.models import Supplier

        self.supplier = Supplier.objects.create(
            business=self.business_a, code="SUP-9", name="Doc Supplies",
            email="supplier@example.com", contact_person="Pat",
        )
        self.purchase = purchases.create_purchase(
            business=self.business_a, supplier=self.supplier,
            branch=self.branch_a, warehouse=self.warehouse_a,
            rows=[{"product": self.product_a, "variant": None,
                   "quantity": D("5"), "unit_cost": D("4.000")}],
            user=self.owner_a, purchase_date="2026-06-01",
        )
        self.client.force_login(self.owner_a)

    def test_print_view_renders_po(self):
        response = self.client.get(
            reverse("purchases:print", args=[self.purchase.public_id]))
        self.assertContains(response, "PURCHASE ORDER")
        self.assertContains(response, self.purchase.purchase_number)
        self.assertContains(response, "Doc Supplies")
        self.assertContains(response, "Accepted by supplier")

    def test_pdf_download(self):
        response = self.client.get(
            reverse("purchases:pdf", args=[self.purchase.public_id]))
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_share_link_opens_without_login(self):
        response = self.client.get(
            reverse("purchases:share", args=[self.purchase.public_id]))
        self.assertEqual(response.status_code, 200)
        share_url = response.context["share_url"]
        self.client.logout()
        response = self.client.get(share_url)
        self.assertContains(response, self.purchase.purchase_number)

    def test_tampered_share_token_is_404(self):
        self.client.logout()
        response = self.client.get("/purchases/shared/forged-token-value/")
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_po_documents_blocked(self):
        self.client.force_login(self.owner_b)
        for name in ("purchases:print", "purchases:pdf", "purchases:share"):
            response = self.client.get(
                reverse(name, args=[self.purchase.public_id]))
            self.assertEqual(response.status_code, 404, name)

    def test_email_po_sends_with_pdf_attachment(self):
        from django.core import mail

        response = self.client.post(
            reverse("purchases:email", args=[self.purchase.public_id]),
            {"email": "supplier@example.com"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn(self.purchase.purchase_number, message.subject)
        self.assertEqual(message.to, ["supplier@example.com"])
        filename, content, mimetype = message.attachments[0]
        self.assertTrue(filename.endswith(".pdf"))
        self.assertEqual(mimetype, "application/pdf")


class RegisterBranchDropdownTests(TenantTestCase):
    """Registers & Shifts: the 'Add a register' branch dropdown must list
    every active branch of the business — not just branches that already
    have a register."""

    def setUp(self):
        from apps.branches.models import Branch

        self.branch2 = Branch.objects.create(
            business=self.business_a, name="HK Road Branch", code="HK",
        )
        Branch.objects.create(
            business=self.business_a, name="Closed Branch", code="CLS",
            is_active=False,
        )

    def test_owner_sees_all_active_branches(self):
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("registers:shift_list"))
        branch_names = [b.name for b in response.context["branches"]]
        self.assertIn("Head Office", branch_names)
        self.assertIn("HK Road Branch", branch_names)
        self.assertNotIn("Closed Branch", branch_names)
        # Both options render in the dropdown even though only Head Office
        # has a register so far.
        self.assertContains(response, "HK Road Branch")

    def test_branch_limited_user_sees_only_assigned_branches(self):
        self.cashier_membership.branches.set([self.branch_a])
        self.client.force_login(self.cashier_a)
        response = self.client.get(reverse("registers:shift_list"))
        branch_ids = [b.id for b in response.context["branches"]]
        self.assertEqual(branch_ids, [self.branch_a.id])

    def test_register_create_blocked_for_unassigned_branch(self):
        from apps.accounts.models import Membership, Role, User
        from apps.registers.models import CashRegister

        manager_role = Role.objects.for_business(self.business_a).get(
            name="Branch Manager")
        manager = User.objects.create_user(
            email="ho-manager@example.com", password="StrongPass123!",
            full_name="HO Manager",
        )
        membership = Membership.objects.create(
            business=self.business_a, user=manager, role=manager_role)
        membership.branches.set([self.branch_a])  # NOT branch2
        self.client.force_login(manager)
        self.client.post(reverse("registers:register_create"), {
            "name": "Forbidden Register", "code": "FRB",
            "branch_id": self.branch2.id, "receipt_printer": "80mm",
        })
        self.assertFalse(
            CashRegister.objects.for_business(self.business_a)
            .filter(code="FRB").exists()
        )

    def test_register_create_works_for_owner_on_second_branch(self):
        from apps.registers.models import CashRegister

        self.client.force_login(self.owner_a)
        self.client.post(reverse("registers:register_create"), {
            "name": "HK Counter", "code": "HK1",
            "branch_id": self.branch2.id, "receipt_printer": "80mm",
        })
        register = CashRegister.objects.for_business(self.business_a).get(code="HK1")
        self.assertEqual(register.branch_id, self.branch2.id)


class DashboardTrendSeriesTests(TenantTestCase):
    """Revenue & Profit Trend must be a daily series over the selected
    range, with zero-filled days — not one point per day that had sales."""

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.sales.models import Sale

        self.allow_no_shift()
        self.today = timezone.localdate()
        s_old = self.make_sale()  # 21.000, profit 12.000
        Sale.objects.filter(pk=s_old.pk).update(
            sale_date=timezone.now() - timedelta(days=3))
        self.make_sale()  # today
        self.client.force_login(self.owner_a)
        self.d_from = self.today - timedelta(days=4)

    def dashboard(self, **params):
        query = {"from": str(self.d_from), "to": str(self.today), **params}
        return self.client.get(reverse("dashboard"), query)

    def test_every_day_in_range_present_with_zero_fill(self):
        response = self.dashboard()
        trend = response.context["chart_trend"]
        self.assertEqual(len(trend["labels"]), 5)        # 5-day range
        self.assertEqual(len(trend["sales"]), 5)
        self.assertEqual(len(trend["profit"]), 5)
        # Sales on day index 1 (today-3) and index 4 (today); zeros between
        self.assertEqual(trend["sales"][0], 0.0)
        self.assertAlmostEqual(trend["sales"][1], 21.0)
        self.assertEqual(trend["sales"][2], 0.0)
        self.assertEqual(trend["sales"][3], 0.0)
        self.assertAlmostEqual(trend["sales"][4], 21.0)
        self.assertAlmostEqual(trend["profit"][1], 12.0)
        # Labels are human-readable dates ("Jun 01" style)
        self.assertEqual(trend["labels"][4], self.today.strftime("%b %d"))

    def test_branch_filter_keeps_full_range(self):
        from apps.branches.models import Branch

        empty_branch = Branch.objects.create(
            business=self.business_a, name="Empty Branch", code="EMP")
        response = self.dashboard(branch=empty_branch.id)
        trend = response.context["chart_trend"]
        self.assertEqual(len(trend["labels"]), 5)
        self.assertEqual(sum(trend["sales"]), 0.0)

    def test_sparklines_align_with_zero_filled_series(self):
        response = self.dashboard()
        sparks = response.context["sparks"]
        self.assertEqual(len(sparks["sales"]), 5)
        self.assertEqual(len(sparks["expenses"]), 5)


class CustomerDetailRegressionTests(TenantTestCase):
    """Bug #2 — FieldError: Cannot compute Avg('total') on customer detail."""

    def test_detail_renders_for_brand_new_customer(self):
        self.client.force_login(self.owner_a)
        response = self.client.post(reverse("customers:create"), {
            "full_name": "Fresh Customer", "code": "", "mobile": "98765432",
            "whatsapp": "", "email": "", "address": "", "city": "",
            "country": "", "tax_number": "", "credit_limit": "0",
            "notes": "", "is_active": "on",
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fresh Customer")
        # Zero orders: stats must render without crashing
        self.assertEqual(response.context["stats"]["count"], 0)

    def test_detail_renders_with_sales_history(self):
        self.allow_no_shift()
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="HIST", full_name="History Buyer",
        )
        self.make_sale(customer=customer)
        self.make_sale(customer=customer)
        self.client.force_login(self.owner_a)
        response = self.client.get(
            reverse("customers:detail", args=[customer.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["count"], 2)
        self.assertIsNotNone(response.context["stats"]["avg"])
