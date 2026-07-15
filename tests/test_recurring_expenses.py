"""Focused coverage for fixed / recurring monthly expenses."""
from datetime import date
from decimal import Decimal as D
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch
from apps.expenses.models import (
    Expense,
    ExpenseCategory,
    RecurringExpenseTemplate,
)
from apps.expenses.services import (
    RecurringExpenseGenerationError,
    ensure_recurring_expenses_for_month,
)
from apps.reports.queries import expense_analysis, expenses_report
from tests.base import TenantTestCase


class RecurringExpenseTestMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.category_a = ExpenseCategory.objects.create(
            business=cls.business_a,
            name="Rent",
        )
        cls.category_a_2 = ExpenseCategory.objects.create(
            business=cls.business_a,
            name="Utilities",
        )
        cls.category_b = ExpenseCategory.objects.create(
            business=cls.business_b,
            name="Rent",
        )

    def make_template(self, *, business=None, category=None, **overrides):
        business = business or self.business_a
        if category is None:
            category = (
                self.category_a if business == self.business_a else self.category_b
            )
        values = {
            "business": business,
            "name": "Workshop Rent",
            "category": category,
            "default_amount": D("250.000"),
            "due_day": 5,
            "start_date": date(2026, 1, 15),
            "notes": "Monthly workshop rent",
            "is_active": True,
        }
        values.update(overrides)
        return RecurringExpenseTemplate.objects.create(**values)

    def make_manual_expense(self, *, business=None, category=None, **overrides):
        business = business or self.business_a
        branch = self.branch_a if business == self.business_a else self.branch_b
        if category is None:
            category = (
                self.category_a if business == self.business_a else self.category_b
            )
        sequence = Expense.objects.for_business(business).count() + 1
        values = {
            "business": business,
            "expense_number": f"MAN-{sequence:06d}",
            "expense_date": date(2026, 7, 10),
            "branch": branch,
            "category": category,
            "payee": "Manual supplier",
            "amount": D("10.000"),
            "status": Expense.Status.APPROVED,
        }
        values.update(overrides)
        return Expense.objects.create(**values)

    @staticmethod
    def report_filters(**overrides):
        values = {
            "date_from": None,
            "date_to": None,
            "branch_id": None,
            "warehouse_id": None,
        }
        values.update(overrides)
        return values


class RecurringExpenseServiceTests(RecurringExpenseTestMixin, TenantTestCase):
    def test_active_applicable_template_generates_complete_expense(self):
        template = self.make_template(due_day=20)

        result = ensure_recurring_expenses_for_month(
            self.business_a, date(2026, 7, 1)
        )

        expense = Expense.objects.get(recurring_template=template)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.existing, 0)
        self.assertEqual(expense.business, self.business_a)
        self.assertEqual(expense.expense_number, f"REC-202607-{template.pk}")
        self.assertEqual(expense.expense_date, date(2026, 7, 20))
        self.assertEqual(expense.generated_for_month, date(2026, 7, 1))
        self.assertEqual(expense.branch, self.branch_a)
        self.assertEqual(expense.category, self.category_a)
        self.assertEqual(expense.payee, template.name)
        self.assertEqual(expense.amount, D("250.000"))
        self.assertEqual(expense.description, template.notes)
        self.assertEqual(expense.status, Expense.Status.APPROVED)
        self.assertEqual(expense.source, "recurring")
        self.assertEqual(expense.source_display, "Fixed")

    def test_generation_is_idempotent(self):
        template = self.make_template()

        first = ensure_recurring_expenses_for_month(
            self.business_a, date(2026, 7, 18)
        )
        second = ensure_recurring_expenses_for_month(
            self.business_a, date(2026, 7, 31)
        )

        self.assertEqual((first.created, first.existing), (1, 0))
        self.assertEqual((second.created, second.existing), (0, 1))
        self.assertEqual(template.generated_expenses.count(), 1)

    def test_database_constraint_blocks_duplicate_template_month(self):
        template = self.make_template()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))

        with self.assertRaises(IntegrityError), transaction.atomic():
            Expense.objects.create(
                business=self.business_a,
                expense_number="DUP-RECURRING",
                expense_date=date(2026, 7, 5),
                branch=self.branch_a,
                category=self.category_a,
                payee="Duplicate",
                amount=D("250.000"),
                recurring_template=template,
                generated_for_month=date(2026, 7, 1),
            )

    def test_inactive_template_does_not_generate(self):
        self.make_template(is_active=False)
        result = ensure_recurring_expenses_for_month(
            self.business_a, date(2026, 7, 1)
        )
        self.assertEqual((result.created, result.existing), (0, 0))
        self.assertFalse(Expense.objects.for_business(self.business_a).exists())

    def test_month_before_start_month_does_not_generate(self):
        self.make_template(start_date=date(2026, 8, 1))
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        self.assertFalse(Expense.objects.for_business(self.business_a).exists())

    def test_month_after_end_month_does_not_generate(self):
        self.make_template(end_date=date(2026, 6, 30))
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        self.assertFalse(Expense.objects.for_business(self.business_a).exists())

    def test_start_month_is_applicable_even_before_exact_start_day(self):
        template = self.make_template(start_date=date(2026, 7, 25), due_day=5)
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        self.assertEqual(
            template.generated_expenses.get().expense_date,
            date(2026, 7, 5),
        )

    def test_end_month_is_applicable_after_exact_end_day(self):
        template = self.make_template(end_date=date(2026, 7, 2), due_day=20)
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 31))
        self.assertEqual(
            template.generated_expenses.get().expense_date,
            date(2026, 7, 20),
        )

    def test_due_day_31_clamps_to_april_30(self):
        template = self.make_template(due_day=31)
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 4, 1))
        self.assertEqual(template.generated_expenses.get().expense_date, date(2026, 4, 30))

    def test_due_day_31_clamps_to_non_leap_february(self):
        template = self.make_template(due_day=31)
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 2, 1))
        self.assertEqual(template.generated_expenses.get().expense_date, date(2026, 2, 28))

    def test_due_day_31_clamps_to_leap_year_february(self):
        template = self.make_template(
            due_day=31,
            start_date=date(2024, 1, 1),
        )
        ensure_recurring_expenses_for_month(self.business_a, date(2024, 2, 1))
        self.assertEqual(template.generated_expenses.get().expense_date, date(2024, 2, 29))

    def test_due_day_30_clamps_to_february(self):
        template = self.make_template(due_day=30)
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 2, 1))
        self.assertEqual(template.generated_expenses.get().expense_date, date(2026, 2, 28))

    def test_template_edit_changes_future_only_and_preserves_history(self):
        template = self.make_template(due_day=31)
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 1, 1))
        historical = template.generated_expenses.get()
        historical.status = Expense.Status.PAID
        historical.save(update_fields=["status", "updated_at"])

        template.default_amount = D("300.000")
        template.category = self.category_a_2
        template.due_day = 15
        template.notes = "Updated notes"
        template.save()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 1, 1))
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 2, 1))

        historical.refresh_from_db()
        future = template.generated_expenses.get(generated_for_month=date(2026, 2, 1))
        self.assertEqual(historical.amount, D("250.000"))
        self.assertEqual(historical.category, self.category_a)
        self.assertEqual(historical.expense_date, date(2026, 1, 31))
        self.assertEqual(historical.description, "Monthly workshop rent")
        self.assertEqual(historical.status, Expense.Status.PAID)
        self.assertEqual(future.amount, D("300.000"))
        self.assertEqual(future.category, self.category_a_2)
        self.assertEqual(future.expense_date, date(2026, 2, 15))
        self.assertEqual(future.description, "Updated notes")

    def test_archive_then_restore_only_affects_requested_future_months(self):
        template = self.make_template()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 1, 1))
        template.is_active = False
        template.save(update_fields=["is_active", "updated_at"])
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 2, 1))
        template.is_active = True
        template.save(update_fields=["is_active", "updated_at"])
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 3, 1))

        months = list(
            template.generated_expenses.order_by("generated_for_month")
            .values_list("generated_for_month", flat=True)
        )
        self.assertEqual(months, [date(2026, 1, 1), date(2026, 3, 1)])

    def test_generation_falls_back_to_an_active_non_head_office_branch(self):
        self.branch_a.is_active = False
        self.branch_a.save(update_fields=["is_active", "updated_at"])
        fallback = Branch.objects.create(
            business=self.business_a,
            name="Fallback",
            code="FALLBACK",
            is_active=True,
        )
        template = self.make_template()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        self.assertEqual(template.generated_expenses.get().branch, fallback)

    def test_generation_requires_an_active_branch(self):
        Branch.objects.for_business(self.business_a).update(is_active=False)
        self.make_template()
        with self.assertRaisesMessage(
            RecurringExpenseGenerationError,
            "no active branch",
        ):
            ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))

    def test_generation_rejects_inactive_business(self):
        self.business_a.is_active = False
        self.business_a.save(update_fields=["is_active", "updated_at"])
        self.make_template()
        with self.assertRaisesMessage(
            RecurringExpenseGenerationError,
            "active business",
        ):
            ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))

    def test_generation_rejects_plan_without_expenses(self):
        plan = self.business_a.subscription.plan
        plan.feature_expenses = False
        plan.save(update_fields=["feature_expenses", "updated_at"])
        self.make_template()
        with self.assertRaisesMessage(
            RecurringExpenseGenerationError,
            "does not include expenses",
        ):
            ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))

    def test_template_model_rejects_cross_tenant_category(self):
        template = self.make_template(category=self.category_b)
        with self.assertRaises(ValidationError) as raised:
            template.full_clean()
        self.assertIn("category", raised.exception.message_dict)

    def test_generated_expense_requires_paired_provenance(self):
        expense = self.make_manual_expense()
        expense.generated_for_month = date(2026, 7, 1)
        with self.assertRaises(ValidationError) as raised:
            expense.full_clean()
        self.assertIn("generated_for_month", raised.exception.message_dict)

    def test_template_with_history_is_database_protected_from_delete(self):
        template = self.make_template()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        with self.assertRaises(ProtectedError):
            template.delete()


class RecurringExpenseViewTests(RecurringExpenseTestMixin, TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def template_payload(self, **overrides):
        values = {
            "name": "Internet",
            "category": str(self.category_a.pk),
            "default_amount": "35.500",
            "due_day": "12",
            "start_date": "2026-07-01",
            "end_date": "",
            "notes": "Office internet",
            "is_active": "on",
        }
        values.update(overrides)
        return values

    def expense_payload(self, **overrides):
        values = {
            "expense_date": "2026-07-10",
            "branch": str(self.branch_a.pk),
            "category": str(self.category_a.pk),
            "payee": "Local vendor",
            "supplier": "",
            "amount": "25.000",
            "tax_amount": "0.000",
            "payment_method": "",
            "reference": "MANUAL",
            "description": "Variable expense",
        }
        values.update(overrides)
        return values

    def test_authorized_user_can_create_tenant_template(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            self.template_payload(),
        )
        self.assertRedirects(
            response,
            reverse("expenses:list") + "#fixed-expenses",
        )
        template = RecurringExpenseTemplate.objects.get(name="Internet")
        self.assertEqual(template.business, self.business_a)
        self.assertEqual(template.default_amount, D("35.500"))

    def test_zero_default_amount_is_allowed(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            self.template_payload(default_amount="0.000"),
        )
        self.assertRedirects(
            response,
            reverse("expenses:list") + "#fixed-expenses",
        )
        self.assertEqual(
            RecurringExpenseTemplate.objects.get(name="Internet").default_amount,
            D("0.000"),
        )

    def test_missing_expense_name_is_rejected(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            self.template_payload(name="   "),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)

    def test_missing_default_amount_is_rejected(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            self.template_payload(default_amount=""),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("default_amount", response.context["form"].errors)

    def test_negative_default_amount_is_rejected(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            self.template_payload(default_amount="-0.001"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("default_amount", response.context["form"].errors)

    def test_due_day_below_one_is_rejected(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            self.template_payload(due_day="0"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("due_day", response.context["form"].errors)

    def test_due_day_above_31_is_rejected(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            self.template_payload(due_day="32"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("due_day", response.context["form"].errors)

    def test_end_date_before_start_date_is_rejected(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            self.template_payload(end_date="2026-06-30"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("end_date", response.context["form"].errors)

    def test_cross_tenant_category_is_rejected(self):
        response = self.client.post(
            reverse("expenses:recurring_create"),
            self.template_payload(category=str(self.category_b.pk)),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("category", response.context["form"].errors)
        self.assertFalse(RecurringExpenseTemplate.objects.filter(name="Internet").exists())

    def test_unauthorized_user_cannot_mutate_templates(self):
        template = self.make_template()
        self.client.force_login(self.cashier_a)
        requests = [
            (reverse("expenses:recurring_create"), self.template_payload()),
            (
                reverse("expenses:recurring_edit", args=[template.public_id]),
                self.template_payload(name="Changed"),
            ),
            (
                reverse(
                    "expenses:recurring_action",
                    args=[template.public_id, "archive"],
                ),
                {},
            ),
            (
                reverse("expenses:recurring_delete", args=[template.public_id]),
                {},
            ),
        ]
        for url, data in requests:
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, data).status_code, 403)
        template.refresh_from_db()
        self.assertTrue(template.is_active)

    def test_view_only_user_sees_templates_without_management_actions(self):
        template = self.make_template()
        user = User.objects.create_user(
            email="expense-viewer@example.com",
            password="StrongPass123!",
            full_name="Expense Viewer",
        )
        role = Role.objects.create(
            business=self.business_a,
            name="Expense Viewer",
            permissions=["expenses.view"],
        )
        Membership.objects.create(
            business=self.business_a,
            user=user,
            role=role,
        )
        self.client.force_login(user)
        response = self.client.get(reverse("expenses:list"))
        self.assertContains(response, template.name)
        self.assertNotContains(response, "Add Fixed Expense")
        legacy_response = self.client.get(reverse("expenses:recurring_list"))
        self.assertRedirects(
            legacy_response,
            reverse("expenses:list") + "#fixed-expenses",
        )
        self.assertEqual(
            self.client.get(reverse("expenses:recurring_create")).status_code,
            403,
        )

    def test_template_list_and_actions_are_tenant_isolated(self):
        template_a = self.make_template(name="Tenant A Rent")
        template_b = self.make_template(
            business=self.business_b,
            name="Tenant B Rent",
        )
        response = self.client.get(reverse("expenses:list"))
        self.assertContains(response, template_a.name)
        self.assertNotContains(response, template_b.name)
        self.assertEqual(
            self.client.get(
                reverse("expenses:recurring_edit", args=[template_b.public_id])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "expenses:recurring_action",
                    args=[template_b.public_id, "archive"],
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("expenses:recurring_delete", args=[template_b.public_id])
            ).status_code,
            404,
        )

    def test_archive_and_restore_are_post_only_and_audited(self):
        template = self.make_template()
        archive_url = reverse(
            "expenses:recurring_action",
            args=[template.public_id, "archive"],
        )
        self.assertEqual(self.client.get(archive_url).status_code, 405)
        self.client.post(archive_url)
        template.refresh_from_db()
        self.assertFalse(template.is_active)
        self.client.post(
            reverse(
                "expenses:recurring_action",
                args=[template.public_id, "restore"],
            )
        )
        template.refresh_from_db()
        self.assertTrue(template.is_active)
        actions = list(
            self.business_a.audit_logs.filter(
                action__startswith="recurring_expense_template."
            ).values_list("action", flat=True)
        )
        self.assertIn("recurring_expense_template.archived", actions)
        self.assertIn("recurring_expense_template.restored", actions)

    def test_template_without_history_can_be_permanently_deleted(self):
        template = self.make_template()
        url = reverse("expenses:recurring_delete", args=[template.public_id])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url)
        self.assertRedirects(
            response,
            reverse("expenses:list") + "#fixed-expenses",
        )
        self.assertFalse(
            RecurringExpenseTemplate.objects.filter(pk=template.pk).exists()
        )

    def test_template_with_history_cannot_be_permanently_deleted(self):
        template = self.make_template()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        response = self.client.post(
            reverse("expenses:recurring_delete", args=[template.public_id]),
            follow=True,
        )
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(
            RecurringExpenseTemplate.objects.filter(pk=template.pk).exists()
        )
        self.assertEqual(template.generated_expenses.count(), 1)

    def test_delete_action_is_hidden_when_template_has_history(self):
        template = self.make_template()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        response = self.client.get(reverse("expenses:list"))
        delete_url = reverse("expenses:recurring_delete", args=[template.public_id])
        self.assertNotContains(response, delete_url)
        self.assertContains(response, template.name)

    def test_recurring_list_shows_required_fields(self):
        template = self.make_template(end_date=date(2026, 12, 31), due_day=28)
        response = self.client.get(reverse("expenses:list"))
        for label in (
            "Expense Name", "Category", "Monthly Amount", "Due Day",
            "Start Date", "End Date", "Status", "Actions",
        ):
            self.assertContains(response, label)
        self.assertContains(response, template.name)
        self.assertContains(response, "Active")

    def test_manual_variable_expense_creation_remains_unchanged(self):
        response = self.client.post(
            reverse("expenses:create"),
            self.expense_payload(),
        )
        self.assertRedirects(response, reverse("expenses:list"))
        expense = Expense.objects.get(reference="MANUAL")
        self.assertIsNone(expense.recurring_template_id)
        self.assertIsNone(expense.generated_for_month)
        self.assertEqual(expense.source_display, "Current")
        self.assertEqual(expense.amount, D("25.000"))
        self.assertEqual(expense.status, Expense.Status.APPROVED)

    def test_manual_draft_expense_edit_remains_unchanged(self):
        expense = self.make_manual_expense(status=Expense.Status.DRAFT)
        response = self.client.post(
            reverse("expenses:edit", args=[expense.public_id]),
            self.expense_payload(amount="30.000", reference="EDITED"),
        )
        self.assertRedirects(response, reverse("expenses:list"))
        expense.refresh_from_db()
        self.assertEqual(expense.amount, D("30.000"))
        self.assertEqual(expense.reference, "EDITED")
        self.assertIsNone(expense.recurring_template_id)

    def test_manual_approved_expense_edit_stays_blocked(self):
        expense = self.make_manual_expense(status=Expense.Status.APPROVED)
        response = self.client.get(
            reverse("expenses:edit", args=[expense.public_id]),
            follow=True,
        )
        self.assertContains(response, "Approved or paid expenses cannot be edited")

    def test_generated_approved_expense_can_be_edited_without_template_change(self):
        template = self.make_template()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        expense = template.generated_expenses.get()
        response = self.client.post(
            reverse("expenses:edit", args=[expense.public_id]),
            self.expense_payload(
                expense_date="2026-07-06",
                amount="275.000",
                payee="Adjusted rent",
                reference="REC-ADJUSTED",
            ),
        )
        self.assertRedirects(response, reverse("expenses:list"))
        expense.refresh_from_db()
        template.refresh_from_db()
        self.assertEqual(expense.amount, D("275.000"))
        self.assertEqual(expense.payee, "Adjusted rent")
        self.assertEqual(expense.recurring_template, template)
        self.assertEqual(expense.generated_for_month, date(2026, 7, 1))
        self.assertEqual(template.default_amount, D("250.000"))
        self.assertEqual(template.name, "Workshop Rent")

    def test_current_expense_list_and_existing_filters_remain_correct(self):
        manual = self.make_manual_expense()
        template = self.make_template()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))

        list_response = self.client.get(reverse("expenses:list"))
        self.assertEqual(list(list_response.context["page_obj"]), [manual])
        self.assertEqual(list(list_response.context["fixed_templates"]), [template])
        filtered_response = self.client.get(
            reverse("expenses:list")
            + f"?category={self.category_a.pk}&status=approved"
            + "&from=2026-07-01&to=2026-07-31"
        )
        self.assertEqual(
            list(filtered_response.context["page_obj"].object_list),
            [manual],
        )


class RecurringExpenseReportTests(RecurringExpenseTestMixin, TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def test_expense_report_includes_variable_and_recurring_once(self):
        self.make_manual_expense(amount=D("10.000"))
        self.make_template(default_amount=D("20.000"))
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))

        data = expenses_report(self.business_a, self.report_filters())

        self.assertEqual(data["columns"], [
            "Number", "Date", "Category", "Source", "Payee", "Branch",
            "Amount", "Status",
        ])
        self.assertEqual({row[3] for row in data["rows"]}, {"Current", "Fixed"})
        self.assertEqual(data["totals"][6], D("30.000"))
        self.assertEqual(len(data["rows"]), 2)

    def test_expense_report_date_and_branch_filters_apply_to_generated_rows(self):
        template = self.make_template()
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        included = expenses_report(
            self.business_a,
            self.report_filters(
                date_from="2026-07-01",
                date_to="2026-07-31",
                branch_id=self.branch_a.pk,
            ),
        )
        excluded = expenses_report(
            self.business_a,
            self.report_filters(
                date_from="2026-08-01",
                date_to="2026-08-31",
                branch_id=self.branch_a.pk,
            ),
        )
        self.assertEqual(len(included["rows"]), 1)
        self.assertEqual(included["rows"][0][0], template.generated_expenses.get().expense_number)
        self.assertEqual(excluded["rows"], [])

    def test_expense_analysis_counts_each_generated_row_once(self):
        self.make_manual_expense(amount=D("10.000"))
        self.make_template(default_amount=D("20.000"))
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        data = expense_analysis(self.business_a, self.report_filters())
        self.assertEqual(data["totals"][1], 2)
        self.assertEqual(data["totals"][2], D("30.000"))

    def test_expense_csv_xlsx_and_pdf_exports_keep_source_and_totals(self):
        today = timezone.localdate()
        self.make_manual_expense(expense_date=today, amount=D("10.000"))
        self.make_template(
            start_date=today.replace(day=1),
            default_amount=D("20.000"),
        )
        ensure_recurring_expenses_for_month(self.business_a, today)
        base_url = (
            reverse("reports:view", args=["expenses"])
            + f"?from={today:%Y-%m-01}&to={today:%Y-%m-%d}"
        )

        csv_response = self.client.get(base_url + "&export=csv")
        csv_body = csv_response.content.decode("utf-8-sig")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("Number,Date,Category,Source,Payee,Branch,Amount,Status", csv_body)
        self.assertIn("Current", csv_body)
        self.assertIn("Fixed", csv_body)
        self.assertIn("30.000", csv_body)

        xlsx_response = self.client.get(base_url + "&export=xlsx")
        self.assertEqual(xlsx_response.status_code, 200)
        self.assertIn("spreadsheetml", xlsx_response["Content-Type"])

        pdf_response = self.client.get(base_url + "&export=pdf")
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")

    def test_expense_report_and_export_are_tenant_isolated(self):
        template_a = self.make_template(name="A Rent")
        template_b = self.make_template(
            business=self.business_b,
            name="B Rent",
        )
        ensure_recurring_expenses_for_month(self.business_a, date(2026, 7, 1))
        ensure_recurring_expenses_for_month(self.business_b, date(2026, 7, 1))
        data = expenses_report(self.business_a, self.report_filters())
        numbers = {row[0] for row in data["rows"]}
        self.assertIn(template_a.generated_expenses.get().expense_number, numbers)
        self.assertNotIn(template_b.generated_expenses.get().expense_number, numbers)


class RecurringExpenseCommandTests(RecurringExpenseTestMixin, TenantTestCase):
    def test_command_generates_for_all_eligible_businesses_with_tenant_isolation(self):
        template_a = self.make_template(name="A Rent")
        template_b = self.make_template(
            business=self.business_b,
            name="B Rent",
        )
        output = StringIO()
        call_command(
            "generate_recurring_expenses",
            month="2026-07",
            stdout=output,
        )
        expense_a = template_a.generated_expenses.get()
        expense_b = template_b.generated_expenses.get()
        self.assertEqual(expense_a.business, self.business_a)
        self.assertEqual(expense_b.business, self.business_b)
        self.assertEqual(expense_a.category, self.category_a)
        self.assertEqual(expense_b.category, self.category_b)
        self.assertIn("created=2", output.getvalue())

    def test_command_is_safe_to_rerun(self):
        template = self.make_template()
        call_command("generate_recurring_expenses", month="2026-07")
        output = StringIO()
        call_command(
            "generate_recurring_expenses",
            month="2026-07",
            stdout=output,
        )
        self.assertEqual(template.generated_expenses.count(), 1)
        self.assertIn("created=0", output.getvalue())
        self.assertIn("existing=1", output.getvalue())

    def test_command_can_safely_target_one_business_by_public_uuid(self):
        template_a = self.make_template(name="A Rent")
        template_b = self.make_template(
            business=self.business_b,
            name="B Rent",
        )
        call_command(
            "generate_recurring_expenses",
            month="2026-07",
            business_public_id=str(self.business_a.public_id),
        )
        self.assertEqual(template_a.generated_expenses.count(), 1)
        self.assertEqual(template_b.generated_expenses.count(), 0)

    def test_command_skips_inactive_business(self):
        template = self.make_template(business=self.business_b)
        self.business_b.is_active = False
        self.business_b.save(update_fields=["is_active", "updated_at"])
        call_command("generate_recurring_expenses", month="2026-07")
        self.assertEqual(template.generated_expenses.count(), 0)

    def test_command_skips_business_without_active_branch(self):
        template = self.make_template(business=self.business_b)
        Branch.objects.for_business(self.business_b).update(is_active=False)
        output = StringIO()
        call_command(
            "generate_recurring_expenses",
            month="2026-07",
            stdout=output,
        )
        self.assertEqual(template.generated_expenses.count(), 0)
        self.assertIn("no active branch", output.getvalue())

    def test_command_defaults_to_current_month(self):
        current_month = timezone.localdate().replace(day=1)
        template = self.make_template(start_date=current_month)
        call_command("generate_recurring_expenses")
        self.assertEqual(
            template.generated_expenses.get().generated_for_month,
            current_month,
        )

    def test_command_rejects_invalid_month_and_business(self):
        with self.assertRaises(CommandError):
            call_command("generate_recurring_expenses", month="2026-7")
        with self.assertRaises(CommandError):
            call_command(
                "generate_recurring_expenses",
                month="2026-07",
                business_public_id="not-a-uuid",
            )
