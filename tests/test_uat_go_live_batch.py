"""Focused regressions for the seven go-live UAT fixes."""
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.template.loader import render_to_string
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch
from apps.core.date_ranges import business_localtime
from apps.expenses.models import Expense, ExpenseCategory, RecurringExpenseTemplate
from apps.expenses.services import (
    RecurringExpenseGenerationError,
    ensure_recurring_expenses_for_month,
)
from apps.reports.queries import sales_summary
from apps.sales.models import SalePayment
from apps.subscriptions.models import Subscription, SubscriptionPayment

from .base import TenantTestCase

D = Decimal


class SubscriptionRenewalUATTests(TenantTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="uat-platform@example.com",
            password="StrongPass123!",
            full_name="UAT Platform Admin",
            is_superuser=True,
            is_staff=True,
            is_platform_admin=True,
        )
        self.business_a.timezone = "Asia/Muscat"
        self.business_a.save(update_fields=["timezone", "updated_at"])
        self.client.force_login(self.admin)

    def renew(self, *, renewal_type="monthly", start="2026-07-21", end=""):
        return self.client.post(
            reverse(
                "platformadmin:business_action",
                args=[self.business_a.public_id, "renew"],
            ),
            {
                "renew-plan": self.business_a.subscription.plan_id,
                "renew-renewal_type": renewal_type,
                "renew-start_date": start,
                "renew-end_date": end,
                "renew-payment_amount": "25.000",
                "renew-payment_method": "manual",
                "renew-payment_reference": f"UAT-{renewal_type}-{end}",
                "renew-notes": "UAT renewal",
            },
        )

    def test_explicit_multi_month_end_date_is_preserved_independently(self):
        response = self.renew(end="2027-02-20")
        self.assertEqual(response.status_code, 302)
        subscription = Subscription.objects.get(business=self.business_a)
        self.assertEqual(subscription.billing_cycle, "monthly")
        self.assertEqual(subscription.current_period_start_on, date(2026, 7, 21))
        self.assertEqual(subscription.current_period_end_on, date(2027, 2, 20))

        payment = SubscriptionPayment.objects.get(
            reference="UAT-monthly-2027-02-20"
        )
        self.assertEqual(
            business_localtime(
                self.business_a, value=payment.period_end
            ).date(),
            date(2027, 2, 20),
        )

        admin_page = self.client.get(
            reverse(
                "platformadmin:business_detail",
                args=[self.business_a.public_id],
            )
        )
        self.assertContains(admin_page, "2027-02-20")

        self.client.force_login(self.owner_a)
        customer_page = self.client.get(reverse("subscriptions:status"))
        self.assertContains(customer_page, "2027-02-20")

        now = datetime(2026, 7, 21, 8, tzinfo=UTC)
        with patch("django.utils.timezone.now", return_value=now):
            subscription.refresh_from_db()
            self.assertEqual(
                subscription.days_remaining,
                (date(2027, 2, 20) - date(2026, 7, 21)).days,
            )
            self.assertTrue(subscription.can_access_app)
            self.assertFalse(subscription.should_suspend)

    def test_standard_monthly_renewal_still_defaults_to_thirty_days(self):
        self.renew(start="2026-07-21")
        subscription = Subscription.objects.get(business=self.business_a)
        self.assertEqual(subscription.current_period_end_on, date(2026, 8, 20))

    def test_renewal_rolls_back_if_payment_history_cannot_be_recorded(self):
        subscription = Subscription.objects.get(business=self.business_a)
        old_end = subscription.current_period_end
        with patch(
            "apps.platformadmin.views.record_subscription_payment",
            side_effect=RuntimeError("payment ledger unavailable"),
        ):
            with self.assertRaisesMessage(RuntimeError, "payment ledger unavailable"):
                self.renew(end="2027-02-20")
        subscription.refresh_from_db()
        self.assertEqual(subscription.current_period_end, old_end)


class ExpenseBranchUATTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.branch_two = Branch.objects.create(
            business=self.business_a,
            name="Mabelah",
            code="MAB-UAT",
        )
        self.category = ExpenseCategory.objects.for_business(self.business_a).first()

    def make_expense(self, branch, number, amount):
        return Expense.objects.create(
            business=self.business_a,
            expense_number=number,
            expense_date=date(2026, 7, 21),
            branch=branch,
            category=self.category,
            payee=number,
            amount=D(amount),
        )

    def make_template(self, branch, name="Branch Rent"):
        return RecurringExpenseTemplate.objects.create(
            business=self.business_a,
            branch=branch,
            name=name,
            category=self.category,
            default_amount=D("50.000"),
            due_day=1,
            start_date=date(2026, 7, 1),
        )

    def test_current_expense_branch_filter_controls_rows_and_total(self):
        self.make_expense(self.branch_a, "EXP-UAT-A", "10.000")
        self.make_expense(self.branch_two, "EXP-UAT-B", "25.000")
        response = self.client.get(
            reverse("expenses:list"),
            {
                "branch": self.branch_two.pk,
                "from": "2026-07-01",
                "to": "2026-07-31",
            },
        )
        self.assertNotContains(response, "EXP-UAT-A")
        self.assertContains(response, "EXP-UAT-B")
        self.assertEqual(response.context["total"], D("25.000"))

    def test_fixed_generation_inherits_template_branch(self):
        template = self.make_template(self.branch_two)
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        self.assertEqual(template.generated_expenses.get().branch, self.branch_two)

    def test_ambiguous_legacy_template_never_chooses_an_arbitrary_branch(self):
        template = self.make_template(None, "Legacy Ambiguous Rent")
        with self.assertRaisesMessage(
            RecurringExpenseGenerationError,
            "Choose a branch for legacy fixed expense",
        ):
            ensure_recurring_expenses_for_month(
                self.business_a, date(2026, 7, 1)
            )
        self.assertFalse(template.generated_expenses.exists())

    def test_branch_user_cannot_list_or_open_other_branch_template(self):
        hidden = self.make_template(self.branch_two, "Hidden Mabelah Rent")
        visible = self.make_template(self.branch_a, "Visible HO Rent")
        role = Role.objects.create(
            business=self.business_a,
            name="UAT Expense Branch",
            permissions=["expenses.view", "expenses.manage"],
        )
        user = User.objects.create_user(
            email="uat-expense-branch@example.com",
            password="StrongPass123!",
            full_name="UAT Expense Branch",
        )
        membership = Membership.objects.create(
            business=self.business_a, user=user, role=role
        )
        membership.branches.add(self.branch_a)
        self.client.force_login(user)

        response = self.client.get(reverse("expenses:list"))
        self.assertContains(response, visible.name)
        self.assertNotContains(response, hidden.name)
        self.assertEqual(
            self.client.get(
                reverse("expenses:recurring_edit", args=[hidden.public_id])
            ).status_code,
            404,
        )
        form = self.client.get(reverse("expenses:recurring_create")).context["form"]
        self.assertTrue(form.fields["branch"].disabled)
        self.assertEqual(form.initial["branch"], self.branch_a.pk)

    def test_multi_branch_owner_must_choose_fixed_expense_branch(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            {
                "name": "No branch",
                "branch": "",
                "category": self.category.pk,
                "default_amount": "12.000",
                "due_day": 5,
                "start_date": "2026-07-01",
                "end_date": "",
                "notes": "",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("branch", response.context["form"].errors)


class SalesReportPermissionUATTests(TenantTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="uat-report-viewer@example.com",
            password="StrongPass123!",
            full_name="UAT Report Viewer",
        )
        self.role = Role.objects.create(
            business=self.business_a,
            name="UAT Report Viewer",
            permissions=["reports.view"],
        )
        Membership.objects.create(
            business=self.business_a, user=self.user, role=self.role
        )
        self.client.force_login(self.user)

    def test_sales_report_links_and_direct_access_need_specific_permission(self):
        index = self.client.get(reverse("reports:index"))
        self.assertNotContains(index, "Daily Sales Report")
        self.assertEqual(
            self.client.get(
                reverse("reports:view", args=["sales_summary"])
            ).status_code,
            403,
        )
        # An unrelated report remains available with generic View reports.
        self.assertEqual(
            self.client.get(
                reverse("reports:view", args=["current_stock"])
            ).status_code,
            200,
        )

        self.role.permissions = ["reports.view", "reports.sales"]
        self.role.save(update_fields=["permissions", "updated_at"])
        self.assertEqual(
            self.client.get(
                reverse("reports:view", args=["sales_summary"])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("reports:view", args=["sales_summary"]),
                {"export": "csv"},
            ).status_code,
            403,
        )

        self.role.permissions.append("reports.export")
        self.role.save(update_fields=["permissions", "updated_at"])
        export = self.client.get(
            reverse("reports:view", args=["sales_summary"]),
            {"export": "csv"},
        )
        self.assertEqual(export.status_code, 200)

    def test_owner_keeps_sales_report_access(self):
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(
                reverse("reports:view", args=["sales_summary"])
            ).status_code,
            200,
        )


class PrintedHeaderAndTimezoneUATTests(TenantTestCase):
    def setUp(self):
        self.business_a.phone = "BUSINESS-PHONE"
        self.business_a.email = "private-header@example.com"
        self.business_a.commercial_registration = "CR-80000000"
        self.business_a.tax_registration_number = "TAX-123456789"
        self.business_a.timezone = "Asia/Muscat"
        self.business_a.save(update_fields=[
            "phone",
            "email",
            "commercial_registration",
            "tax_registration_number",
            "timezone",
            "updated_at",
        ])
        self.branch_a.name = "Al Hail"
        self.branch_a.address = "Al Hail South"
        self.branch_a.phone = "BRANCH-12345678"
        self.branch_a.save(update_fields=["name", "address", "phone", "updated_at"])
        self.allow_no_shift()
        self.client.force_login(self.owner_a)
        self.sale = self.make_sale()

    def assert_commercial_header(self, html):
        values = [
            self.business_a.name,
            self.branch_a.name,
            self.branch_a.address,
            "Tel: BRANCH-12345678",
            "CR: CR-80000000",
            "Tax RN: TAX-123456789",
        ]
        positions = [html.index(value) for value in values]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("BUSINESS-PHONE", html)
        self.assertNotIn("private-header@example.com", html)

    def test_receipt_a4_pdf_and_reprint_share_approved_header(self):
        receipt = self.client.get(
            reverse("sales:receipt", args=[self.sale.public_id])
        ).content.decode()
        invoice = self.client.get(
            reverse("sales:invoice", args=[self.sale.public_id])
        ).content.decode()
        self.assert_commercial_header(receipt)
        self.assert_commercial_header(invoice)

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF UAT") as render:
            response = self.client.get(
                reverse("sales:invoice_pdf", args=[self.sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        template, context = render.call_args.args
        self.assert_commercial_header(render_to_string(template, context))

        self.sale.reprint_count = 1
        self.sale.save(update_fields=["reprint_count", "updated_at"])
        reprint = self.client.get(
            reverse("sales:receipt", args=[self.sale.public_id])
        ).content.decode()
        self.assert_commercial_header(reprint)
        self.assertIn("DUPLICATE COPY", reprint)

    def test_branch_phone_falls_back_to_business_phone_only_when_blank(self):
        self.branch_a.phone = ""
        self.branch_a.save(update_fields=["phone", "updated_at"])
        receipt = self.client.get(
            reverse("sales:receipt", args=[self.sale.public_id])
        )
        self.assertContains(receipt, "Tel: BUSINESS-PHONE")

    def test_cr_and_tax_rn_are_independently_conditional(self):
        cases = (
            ("CR-ONLY", "", True, False),
            ("", "TAX-ONLY", False, True),
            ("", "", False, False),
        )
        for cr, tax, has_cr, has_tax in cases:
            with self.subTest(cr=cr, tax=tax):
                self.business_a.commercial_registration = cr
                self.business_a.tax_registration_number = tax
                self.business_a.save(update_fields=[
                    "commercial_registration", "tax_registration_number",
                ])
                html = self.client.get(
                    reverse("sales:invoice", args=[self.sale.public_id])
                ).content.decode()
                self.assertEqual("CR:" in html, has_cr)
                self.assertEqual("Tax RN:" in html, has_tax)

    def test_job_card_uses_branch_phone_without_legal_tax_lines(self):
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item", "updated_at"])
        sale = self.make_sale(items=[{
            "product": self.product_a,
            "quantity": D("1.000"),
            "unit_price": D("10.000"),
            "garment_classification": "adult",
        }], delivery_date=date(2026, 7, 30))
        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF JOB") as render:
            response = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        template, context = render.call_args.args
        html = render_to_string(template, context)
        self.assertIn("Phone: BRANCH-12345678", html)
        self.assertNotIn("BUSINESS-PHONE", html)
        self.assertNotIn("CR:", html)
        self.assertNotIn("Tax RN:", html)

    def test_utc_previous_day_renders_and_filters_as_muscat_next_day(self):
        boundary = datetime(2026, 7, 20, 22, 17, tzinfo=UTC)
        type(self.sale).objects.filter(pk=self.sale.pk).update(sale_date=boundary)
        payment = SalePayment.objects.filter(sale=self.sale).first()
        SalePayment.objects.filter(pk=payment.pk).update(
            payment_date=None, created_at=boundary
        )
        self.sale.refresh_from_db()

        for route in ("sales:receipt", "sales:invoice", "sales:detail"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[self.sale.public_id]))
                self.assertContains(response, "2026-07-21")
        sale_list = self.client.get(
            reverse("sales:list"),
            {"from": "2026-07-21", "to": "2026-07-21"},
        )
        self.assertContains(sale_list, self.sale.invoice_number)
        previous_day = self.client.get(
            reverse("sales:list"),
            {"from": "2026-07-20", "to": "2026-07-20"},
        )
        self.assertNotContains(previous_day, self.sale.invoice_number)

        data = sales_summary(self.business_a, {
            "date_from": date(2026, 7, 21),
            "date_to": date(2026, 7, 21),
            "allowed_branch_ids": None,
        })
        self.assertEqual(data["rows"][0][0], date(2026, 7, 21))
        self.assertEqual(
            sales_summary(self.business_a, {
                "date_from": date(2026, 7, 20),
                "date_to": date(2026, 7, 20),
                "allowed_branch_ids": None,
            })["rows"],
            [],
        )

        # The same UTC instant resolves independently for another tenant zone.
        self.business_b.timezone = "America/New_York"
        self.business_b.save(update_fields=["timezone", "updated_at"])
        self.assertEqual(
            business_localtime(self.business_b, value=boundary).date(),
            date(2026, 7, 20),
        )
