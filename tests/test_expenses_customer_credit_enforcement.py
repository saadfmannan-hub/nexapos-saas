"""Focused Phase 2D/E enforcement tests for Expenses and Customer Credit."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch
from apps.core.date_ranges import business_localdate
from apps.customers import services as customer_services
from apps.customers.models import Customer
from apps.expenses.models import Expense, ExpenseCategory, RecurringExpenseTemplate
from apps.expenses.services import ensure_recurring_expenses_for_month
from apps.subscriptions.exceptions import DenialCode, ModuleAccessDenied
from apps.subscriptions.models import Plan, Subscription

from .base import TenantTestCase

D = Decimal


class ExpensesCustomerCreditEnforcementTests(TenantTestCase):
    password = "StrongPass123!"

    def setUp(self):
        self.set_plan(feature_expenses=True, feature_customer_credit=True)

    def subscription(self):
        return Subscription.objects.select_related("plan").get(
            business=self.business_a
        )

    def set_plan(self, **fields):
        subscription = self.subscription()
        Plan.objects.filter(pk=subscription.plan_id).update(
            feature_sales=True,
            **fields,
        )

    def set_status(self, status):
        Subscription.objects.filter(business=self.business_a).update(status=status)

    def make_customer(self, *, code="CREDIT-1", balance=None):
        balance = D("20.000") if balance is None else balance
        return Customer.objects.create(
            business=self.business_a,
            code=code,
            full_name=f"Customer {code}",
            balance=balance,
            credit_limit=D("100.000"),
        )

    def make_expense(self, *, branch=None, number="EXP-PHASE2-1"):
        category = ExpenseCategory.objects.for_business(self.business_a).first()
        return Expense.objects.create(
            business=self.business_a,
            expense_number=number,
            expense_date=business_localdate(self.business_a),
            branch=branch or self.branch_a,
            category=category,
            payee=number,
            amount=D("10.000"),
            status=Expense.Status.SUBMITTED,
            created_by=self.owner_a,
        )

    def make_staff(self, permissions, *, branch=None):
        role = Role.objects.create(
            business=self.business_a,
            name=f"Phase D/E {len(permissions)}",
            permissions=list(permissions),
        )
        user = User.objects.create_user(
            email=f"phase-de-{User.objects.count()}@example.com",
            password=self.password,
            full_name="Phase D/E staff",
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=user,
            role=role,
        )
        if branch is not None:
            membership.branches.set([branch])
        return user, membership

    def test_every_expense_url_uses_the_central_module_guard(self):
        from apps.expenses.urls import urlpatterns

        self.assertEqual(len(urlpatterns), 10)
        for pattern in urlpatterns:
            with self.subTest(route=pattern.name):
                self.assertTrue(
                    getattr(pattern.callback, "_subscription_module_guarded", False)
                )

    def test_expenses_off_denies_owner_and_list_get_never_posts_recurring(self):
        category = ExpenseCategory.objects.for_business(self.business_a).first()
        RecurringExpenseTemplate.objects.create(
            business=self.business_a,
            name="Monthly rent",
            category=category,
            default_amount=D("100.000"),
            due_day=1,
            start_date=date.today().replace(day=1),
        )
        self.set_plan(feature_expenses=False, feature_customer_credit=True)
        self.client.force_login(self.owner_a)

        self.assertEqual(self.client.get(reverse("expenses:list")).status_code, 403)
        self.assertFalse(Expense.objects.filter(recurring_template__isnull=False).exists())

    def test_expense_list_is_safe_get_and_explicit_generation_remains_available(self):
        category = ExpenseCategory.objects.for_business(self.business_a).first()
        RecurringExpenseTemplate.objects.create(
            business=self.business_a,
            name="Monthly internet",
            category=category,
            default_amount=D("25.000"),
            due_day=1,
            start_date=date.today().replace(day=1),
        )
        self.client.force_login(self.owner_a)

        self.assertEqual(self.client.get(reverse("expenses:list")).status_code, 200)
        self.assertFalse(Expense.objects.filter(recurring_template__isnull=False).exists())
        result = ensure_recurring_expenses_for_month(self.business_a, date.today())
        self.assertEqual(result.created, 1)

    def test_expenses_read_only_allows_history_and_blocks_mutations(self):
        expense = self.make_expense()
        self.set_status(Subscription.Status.PAST_DUE)
        self.client.force_login(self.owner_a)

        self.assertEqual(self.client.get(reverse("expenses:list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("expenses:create")).status_code, 403)
        response = self.client.post(
            reverse("expenses:action", args=[expense.public_id, "approve"])
        )
        self.assertEqual(response.status_code, 403)
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.SUBMITTED)

    def test_expenses_are_branch_scoped_for_list_and_object_urls(self):
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Other expense branch",
            code="EXP-B2",
        )
        visible = self.make_expense(number="EXP-VISIBLE")
        hidden = self.make_expense(branch=other_branch, number="EXP-HIDDEN")
        user, _membership = self.make_staff(
            {"expenses.view", "expenses.manage", "expenses.approve"},
            branch=self.branch_a,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("expenses:list"))
        self.assertContains(response, visible.expense_number)
        self.assertNotContains(response, hidden.expense_number)
        self.assertEqual(
            self.client.get(
                reverse("expenses:edit", args=[hidden.public_id])
            ).status_code,
            404,
        )

    def test_customer_credit_off_preserves_customers_and_denies_credit_urls(self):
        customer = self.make_customer()
        self.set_plan(feature_expenses=True, feature_customer_credit=False)
        self.client.force_login(self.owner_a)

        list_response = self.client.get(reverse("customers:list"))
        detail_response = self.client.get(
            reverse("customers:detail", args=[customer.public_id])
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertNotContains(list_response, "Outstanding receivables")
        self.assertNotContains(detail_response, "Statement")
        self.assertEqual(
            self.client.get(
                reverse("customers:statement", args=[customer.public_id])
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("customers:payment", args=[customer.public_id]),
                {"amount": "1.000", "payment_method": self.cash_a.pk},
            ).status_code,
            403,
        )

    def test_credit_tender_service_cannot_bypass_disabled_module(self):
        customer = self.make_customer(balance=D("0"))
        self.allow_no_shift()
        self.set_plan(feature_expenses=True, feature_customer_credit=False)

        with self.assertRaises(ModuleAccessDenied) as caught:
            self.make_sale(
                customer=customer,
                payments=[{"method": self.credit_a, "amount": D("21.000")}],
            )
        self.assertEqual(caught.exception.denial.code, DenialCode.MODULE_DISABLED)
        self.assertFalse(
            customer.sales.exclude(status="draft").exists()
        )

    def test_credit_checkout_replay_reevaluates_current_module_access(self):
        customer = self.make_customer(balance=D("0"))
        self.allow_no_shift()
        checkout_token = uuid4()
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
            checkout_token=checkout_token,
        )
        self.set_plan(feature_expenses=True, feature_customer_credit=False)

        with self.assertRaises(ModuleAccessDenied) as caught:
            self.make_sale(
                customer=customer,
                payments=[{"method": self.credit_a, "amount": D("21.000")}],
                checkout_token=checkout_token,
            )

        self.assertEqual(caught.exception.denial.code, DenialCode.MODULE_DISABLED)
        self.assertEqual(customer.sales.exclude(status="draft").count(), 1)
        self.assertEqual(sale.checkout_token, str(checkout_token))

    def test_disabled_credit_hides_credit_owned_sale_outputs(self):
        customer = self.make_customer(balance=D("0"))
        self.allow_no_shift()
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )
        self.set_plan(feature_expenses=True, feature_customer_credit=False)
        self.client.force_login(self.owner_a)

        for route in ("sales:detail", "sales:invoice", "sales:receipt"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[sale.public_id]))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, self.credit_a.name)
                self.assertNotContains(response, "BALANCE DUE")

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF-1.4\n") as render:
            response = self.client.get(
                reverse("sales:invoice_pdf", args=[sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        invoice_context = render.call_args.args[1]
        self.assertFalse(invoice_context["show_credit"])
        self.assertEqual(invoice_context["payments"], [])

    def test_credit_enabled_collection_and_statement_work(self):
        customer = self.make_customer()
        self.client.force_login(self.owner_a)

        response = self.client.post(
            reverse("customers:payment", args=[customer.public_id]),
            {"amount": "5.000", "payment_method": self.cash_a.pk},
        )
        self.assertEqual(response.status_code, 302)
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("15.000"))
        self.assertEqual(
            self.client.get(
                reverse("customers:statement", args=[customer.public_id])
            ).status_code,
            200,
        )

    def test_credit_read_only_allows_statement_but_blocks_collection(self):
        customer = self.make_customer()
        self.set_status(Subscription.Status.PAST_DUE)
        self.client.force_login(self.owner_a)

        statement = reverse("customers:statement", args=[customer.public_id])
        payment = reverse("customers:payment", args=[customer.public_id])
        self.assertEqual(self.client.get(statement).status_code, 200)
        self.assertEqual(
            self.client.post(
                payment,
                {"amount": "5.000", "payment_method": self.cash_a.pk},
            ).status_code,
            403,
        )
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("20.000"))

    def test_disabled_credit_hides_fields_and_blocks_direct_credit_limit_save(self):
        customer = self.make_customer(balance=D("0"))
        self.set_plan(feature_expenses=True, feature_customer_credit=False)
        self.client.force_login(self.owner_a)

        form_response = self.client.get(
            reverse("customers:edit", args=[customer.public_id])
        )
        export_response = self.client.get(
            reverse("customers:export"), {"branch": self.branch_a.id}
        )
        self.assertNotContains(form_response, "Credit limit")
        self.assertNotIn(b"Outstanding Balance", export_response.content)

        customer.credit_limit = D("500.000")
        with self.assertRaises(ModuleAccessDenied):
            customer_services.save_customer(
                customer=customer,
                business=self.business_a,
                user=self.owner_a,
                membership=self.membership_a(),
            )

    def test_branch_restricted_customer_export_includes_branch_credit_balances(self):
        self.make_customer()
        user, _membership = self.make_staff(
            {"customers.view", "customers.export"},
            branch=self.branch_a,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("customers:export"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Outstanding Balance", response.content)
        self.assertIn(b"Store Credit", response.content)
