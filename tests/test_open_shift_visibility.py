"""Live register and open-shift operational visibility."""
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch
from apps.expenses.models import Expense, ExpenseCategory
from apps.registers import services as register_services
from apps.registers.models import CashRegister, Shift
from apps.sales import services as sales_services
from apps.sales.models import PaymentMethod, SaleReturn

from .base import TenantTestCase

D = Decimal


class OpenShiftVisibilityTests(TenantTestCase):
    password = "StrongPass123!"

    def setUp(self):
        self.business_a.timezone = "Asia/Muscat"
        self.business_a.save(update_fields=["timezone"])
        self.client.force_login(self.owner_a)

    def open_shift(self, *, register=None, cashier=None, opening="10.000", notes=""):
        return register_services.open_shift(
            business=self.business_a,
            register=register or self.register_a,
            cashier=cashier or self.owner_a,
            opening_cash=D(opening),
            notes=notes,
        )

    def make_register(self, *, branch=None, code="SECOND"):
        return CashRegister.objects.create(
            business=self.business_a,
            branch=branch or self.branch_a,
            name=f"Register {code}",
            code=code,
        )

    def test_open_shift_section_and_register_shift_statuses(self):
        closed_register = self.make_register()
        shift = self.open_shift()
        self.make_sale(register=self.register_a, shift=shift)

        with patch.object(
            register_services,
            "shift_totals",
            wraps=register_services.shift_totals,
        ) as totals_mock:
            response = self.client.get(reverse("registers:shift_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(totals_mock.call_count, 1)
        self.assertEqual([item.pk for item in response.context["open_shifts"]], [shift.pk])
        expected_cash = D("31.000")
        current_open_shift = response.context["open_shifts"][0]
        history_shift = next(
            item for item in response.context["page_obj"] if item.pk == shift.pk
        )
        my_shift = response.context["my_shift"]
        self.assertEqual(current_open_shift.live_totals["expected_cash"], expected_cash)
        self.assertEqual(history_shift.history_expected_cash, expected_cash)
        self.assertEqual(my_shift.live_totals["expected_cash"], expected_cash)
        self.assertIsNone(history_shift.actual_cash)
        self.assertContains(response, '<td class="text-end">&mdash;</td>', count=2)
        self.assertContains(
            response,
            "<tr><th>Expected cash</th><td>31.00</td></tr>",
            html=True,
        )
        self.assertContains(response, "Currently Open Shifts")
        self.assertContains(response, "Open &mdash; Owner A")
        managed = {item.pk: item for item in response.context["managed_registers"]}
        self.assertEqual(managed[self.register_a.pk].current_open_shift.pk, shift.pk)
        self.assertIsNone(managed[closed_register.pk].current_open_shift)
        self.assertContains(
            response,
            '<span class="badge text-bg-secondary">Closed</span>',
            html=True,
        )

    def test_closed_shift_is_not_currently_open_but_remains_in_history(self):
        shift = self.open_shift(opening="25.000")
        register_services.close_shift(
            shift=shift,
            actual_cash=D("25.000"),
            user=self.owner_a,
        )

        response = self.client.get(reverse("registers:shift_list"))

        self.assertEqual(list(response.context["open_shifts"]), [])
        self.assertContains(response, "No registers are currently open.")
        self.assertIn(shift, list(response.context["page_obj"]))
        shift.refresh_from_db()
        history_shift = next(
            item for item in response.context["page_obj"] if item.pk == shift.pk
        )
        self.assertEqual(shift.expected_cash, D("25.000"))
        self.assertEqual(history_shift.history_expected_cash, shift.expected_cash)
        self.assertEqual(history_shift.actual_cash, D("25.000"))
        self.assertEqual(history_shift.difference, D("0.000"))
        self.assertEqual(shift.status, Shift.Status.CLOSED)

    def test_open_register_is_excluded_from_selector_and_duplicate_post_is_friendly(self):
        self.open_shift()
        self.client.force_login(self.cashier_a)

        response = self.client.get(reverse("registers:shift_list"))
        self.assertNotIn(self.register_a, list(response.context["registers"]))

        response = self.client.post(
            reverse("registers:shift_open"),
            {"register_id": self.register_a.pk, "opening_cash": "0.000"},
            follow=True,
        )
        self.assertContains(response, "This register already has an open shift.")
        self.assertEqual(
            Shift.objects.filter(register=self.register_a, status=Shift.Status.OPEN).count(),
            1,
        )

    def test_service_and_database_both_prevent_duplicate_open_register(self):
        self.open_shift()
        with self.assertRaisesMessage(
            register_services.ShiftError,
            "This register already has an open shift.",
        ):
            self.open_shift(cashier=self.cashier_a)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Shift.objects.create(
                business=self.business_a,
                register=self.register_a,
                branch=self.branch_a,
                cashier=self.cashier_a,
                opened_at=timezone.now(),
                opening_cash=D("0.000"),
            )

    def test_live_expected_cash_uses_only_canonical_physical_cash_movements(self):
        shift = self.open_shift(opening="10.001")
        cash_sale = self.make_sale(register=self.register_a, shift=shift)
        self.make_sale(
            register=self.register_a,
            shift=shift,
            payments=[{"method": self.card_a, "amount": D("21.000")}],
        )
        item = cash_sale.items.get()
        sales_services.process_return(
            sale=cash_sale,
            items=[{"sale_item": item, "quantity": D("1")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
            shift=shift,
        )
        category = ExpenseCategory.objects.create(
            business=self.business_a,
            name="Drawer supplies",
        )
        Expense.objects.create(
            business=self.business_a,
            expense_number="EXP-LIVE-CASH",
            expense_date=timezone.localdate(),
            branch=self.branch_a,
            category=category,
            amount=D("1.234"),
            payment_method=self.cash_a,
            shift=shift,
            created_by=self.owner_a,
        )
        Expense.objects.create(
            business=self.business_a,
            expense_number="EXP-LIVE-CARD",
            expense_date=timezone.localdate(),
            branch=self.branch_a,
            category=category,
            amount=D("9.999"),
            payment_method=self.card_a,
            created_by=self.owner_a,
        )

        totals = register_services.shift_totals(shift)

        self.assertEqual(totals["gross_cash_sales"], D("21.000"))
        self.assertEqual(totals["card_sales"], D("21.000"))
        self.assertEqual(totals["cash_refunds"], D("10.500"))
        self.assertEqual(totals["cash_expenses"], D("1.234"))
        self.assertEqual(totals["expected_cash"], D("19.267"))

        response = self.client.get(
            reverse("registers:shift_detail", args=[shift.public_id])
        )
        self.assertContains(response, "EXP-LIVE-CASH")
        self.assertNotContains(response, "EXP-LIVE-CARD")
        self.assertContains(response, "Drawer supplies")

    def test_omr_decimal_precision_is_preserved(self):
        register_b = CashRegister.objects.for_business(self.business_b).first()
        shift = register_services.open_shift(
            business=self.business_b,
            register=register_b,
            cashier=self.owner_b,
            opening_cash=D("1.001"),
        )
        category = ExpenseCategory.objects.create(
            business=self.business_b,
            name="OMR drawer expense",
        )
        cash_b = PaymentMethod.objects.for_business(self.business_b).get(kind="cash")
        Expense.objects.create(
            business=self.business_b,
            expense_number="EXP-OMR-001",
            expense_date=timezone.localdate(),
            branch=self.branch_b,
            category=category,
            amount=D("0.002"),
            payment_method=cash_b,
            shift=shift,
            created_by=self.owner_b,
        )

        self.assertEqual(
            register_services.shift_totals(shift)["expected_cash"],
            D("0.999"),
        )
        self.client.force_login(self.owner_b)
        list_response = self.client.get(reverse("registers:shift_list"))
        detail_response = self.client.get(
            reverse("registers:shift_detail", args=[shift.public_id])
        )
        self.assertContains(list_response, "1.001")
        self.assertContains(list_response, "0.999")
        self.assertContains(detail_response, "0.999")

    def test_tenant_and_branch_access_scope_current_shifts_and_detail(self):
        visible_shift = self.open_shift()
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Other Branch",
            code="OTHER-LIVE",
        )
        other_register = self.make_register(branch=other_branch, code="OTHER-LIVE")
        hidden_shift = self.open_shift(
            register=other_register,
            cashier=self.cashier_a,
        )
        register_b = CashRegister.objects.for_business(self.business_b).first()
        tenant_b_shift = register_services.open_shift(
            business=self.business_b,
            register=register_b,
            cashier=self.owner_b,
            opening_cash=D("5.000"),
        )

        role = Role.objects.create(
            business=self.business_a,
            name="Branch Shift Manager",
            permissions=["shifts.open", "shifts.approve"],
        )
        manager = User.objects.create_user(
            email="branch-shift-manager@example.com",
            password=self.password,
            full_name="Branch Shift Manager",
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=manager,
            role=role,
        )
        membership.branches.set([self.branch_a])
        self.client.force_login(manager)

        response = self.client.get(reverse("registers:shift_list"))
        visible_ids = {item.pk for item in response.context["open_shifts"]}
        self.assertEqual(visible_ids, {visible_shift.pk})
        self.assertNotIn(hidden_shift.pk, visible_ids)
        self.assertNotIn(tenant_b_shift.pk, visible_ids)
        self.assertEqual(
            self.client.get(
                reverse("registers:shift_detail", args=[hidden_shift.public_id])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("registers:shift_detail", args=[tenant_b_shift.public_id])
            ).status_code,
            404,
        )

    def test_cashier_visibility_is_not_broadened_to_other_cashiers(self):
        self.open_shift()
        self.client.force_login(self.cashier_a)

        response = self.client.get(reverse("registers:shift_list"))

        self.assertEqual(list(response.context["open_shifts"]), [])
        self.assertNotContains(response, "Owner A")

    def test_expense_activity_details_require_existing_expense_view_permission(self):
        shift = self.open_shift(cashier=self.cashier_a)
        category = ExpenseCategory.objects.create(
            business=self.business_a,
            name="Private expense category",
        )
        Expense.objects.create(
            business=self.business_a,
            expense_number="EXP-PRIVATE",
            expense_date=timezone.localdate(),
            branch=self.branch_a,
            category=category,
            amount=D("1.000"),
            payment_method=self.cash_a,
            shift=shift,
            created_by=self.cashier_a,
        )
        self.client.force_login(self.cashier_a)

        response = self.client.get(
            reverse("registers:shift_detail", args=[shift.public_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cash expenses")
        self.assertNotContains(response, "EXP-PRIVATE")
        self.assertNotContains(response, "Private expense category")

    def test_live_detail_has_operational_fields_notes_and_duration(self):
        shift = self.open_shift(notes="Opening till verified")
        shift.opened_at = datetime(2026, 8, 28, 4, 0, tzinfo=dt_timezone.utc)
        shift.save(update_fields=["opened_at"])

        label = register_services.shift_duration_label(
            shift,
            now=shift.opened_at + timedelta(hours=7, minutes=42, seconds=59),
        )
        response = self.client.get(
            reverse("registers:shift_detail", args=[shift.public_id])
        )

        self.assertEqual(label, "7h 42m")
        self.assertContains(response, "Opening till verified")
        self.assertContains(response, self.branch_a.name)
        self.assertContains(response, "Shift status")
