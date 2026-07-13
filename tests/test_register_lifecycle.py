"""Register management lifecycle, tenancy, and history-safety tests."""
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.audit.models import AuditLog
from apps.branches.models import Branch
from apps.registers import services
from apps.registers.models import CashRegister, Shift

from .base import TenantTestCase


class RegisterLifecycleTests(TenantTestCase):
    password = "StrongPass123!"

    def setUp(self):
        self.client.force_login(self.owner_a)

    def register_url(self, name, register=None):
        register = register or self.register_a
        return reverse(f"registers:{name}", args=[register.public_id])

    def edit_payload(self, register=None, **overrides):
        register = register or self.register_a
        payload = {
            "name": register.name,
            "code": register.code,
            "branch": register.branch_id,
            "receipt_printer": register.receipt_printer,
        }
        payload.update(overrides)
        return payload

    def make_register(self, *, code="UNUSED", branch=None, active=True):
        return CashRegister.objects.create(
            business=self.business_a,
            branch=branch or self.branch_a,
            name=f"Register {code}",
            code=code,
            is_active=active,
        )

    def make_manager(self, *, branches=None, permissions=None):
        role = Role.objects.create(
            business=self.business_a,
            name=f"Register Manager {Role.objects.count()}",
            permissions=permissions or ["registers.manage"],
        )
        user = User.objects.create_user(
            email=f"register-manager-{User.objects.count()}@example.com",
            password=self.password,
            full_name="Register Manager",
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=user,
            role=role,
        )
        if branches is not None:
            membership.branches.set(branches)
        return user, membership

    def test_owner_can_edit_register_fields(self):
        branch = Branch.objects.create(
            business=self.business_a, name="Second Branch", code="SECOND"
        )
        response = self.client.post(
            self.register_url("register_edit"),
            self.edit_payload(
                name="Front Counter",
                code="front-1",
                branch=branch.id,
                receipt_printer="58mm",
            ),
        )
        self.assertRedirects(response, reverse("registers:shift_list"))
        self.register_a.refresh_from_db()
        self.assertEqual(self.register_a.name, "Front Counter")
        self.assertEqual(self.register_a.code, "FRONT-1")
        self.assertEqual(self.register_a.branch_id, branch.id)
        self.assertEqual(self.register_a.receipt_printer, "58mm")

    def test_edit_requires_nonblank_name(self):
        original_name = self.register_a.name
        response = self.client.post(
            self.register_url("register_edit"),
            self.edit_payload(name="   "),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.register_a.refresh_from_db()
        self.assertEqual(self.register_a.name, original_name)

    def test_edit_rejects_case_insensitive_duplicate_code(self):
        other = self.make_register(code="COUNTER2")
        response = self.client.post(
            self.register_url("register_edit"),
            self.edit_payload(code=other.code.lower()),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "register code is already in use")

    def test_branch_choices_are_tenant_and_membership_scoped(self):
        second_branch = Branch.objects.create(
            business=self.business_a, name="Second Branch", code="SECOND"
        )
        manager, _membership = self.make_manager(branches=[self.branch_a])
        self.client.force_login(manager)
        response = self.client.get(reverse("registers:register_create"))
        choices = response.context["form"].fields["branch"].queryset
        self.assertEqual(list(choices), [self.branch_a])
        self.assertNotIn(second_branch, choices)
        self.assertNotIn(self.branch_b, choices)

    def test_create_preserves_legacy_branch_id_payload(self):
        response = self.client.post(reverse("registers:register_create"), {
            "name": "Legacy Counter",
            "code": "legacy",
            "branch_id": self.branch_a.id,
            "receipt_printer": "a4",
        })
        self.assertRedirects(response, reverse("registers:shift_list"))
        register = CashRegister.objects.get(business=self.business_a, code="LEGACY")
        self.assertEqual(register.branch_id, self.branch_a.id)
        self.assertEqual(register.receipt_printer, "a4")

    def test_create_honors_pos_terminal_plan_limit(self):
        plan = self.business_a.subscription.plan
        plan.max_pos_terminals = 1
        plan.save(update_fields=["max_pos_terminals"])
        response = self.client.post(reverse("registers:register_create"), {
            "name": "Over Limit",
            "code": "OVER",
            "branch": self.branch_a.id,
            "receipt_printer": "80mm",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at most 1 pos terminals")
        self.assertFalse(CashRegister.objects.filter(code="OVER").exists())

    def test_cashier_cannot_access_register_actions(self):
        self.client.force_login(self.cashier_a)
        for route in ("register_edit", "register_archive", "register_delete"):
            response = self.client.get(self.register_url(route))
            self.assertEqual(response.status_code, 403)
        response = self.client.post(self.register_url("register_reactivate"))
        self.assertEqual(response.status_code, 403)

    def test_cashier_does_not_see_management_actions(self):
        self.client.force_login(self.cashier_a)
        response = self.client.get(reverse("registers:shift_list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Register management")
        self.assertNotContains(response, reverse("registers:register_create"))

    def test_register_only_manager_can_open_management_list(self):
        manager, _membership = self.make_manager()
        self.client.force_login(manager)
        response = self.client.get(reverse("registers:shift_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Register management")
        self.assertNotContains(response, "Open a shift")

    def test_register_only_manager_login_routes_to_management_list(self):
        manager, _membership = self.make_manager()
        self.client.logout()
        response = self.client.post(reverse("accounts:login"), {
            "email": manager.email,
            "password": self.password,
        })
        self.assertRedirects(
            response,
            reverse("registers:shift_list"),
            fetch_redirect_response=False,
        )

    def test_branch_limited_manager_cannot_manage_other_branch(self):
        other_branch = Branch.objects.create(
            business=self.business_a, name="Other Branch", code="OTHER"
        )
        other_register = self.make_register(code="OTHER-REG", branch=other_branch)
        manager, _membership = self.make_manager(branches=[self.branch_a])
        self.client.force_login(manager)
        for route in ("register_edit", "register_archive", "register_delete"):
            response = self.client.get(self.register_url(route, other_register))
            self.assertEqual(response.status_code, 404)
        response = self.client.post(
            self.register_url("register_reactivate", other_register)
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_register_ids_return_404(self):
        register = CashRegister.objects.for_business(self.business_b).first()
        for route in ("register_edit", "register_archive", "register_delete"):
            response = self.client.get(self.register_url(route, register))
            self.assertEqual(response.status_code, 404)
        response = self.client.post(self.register_url("register_reactivate", register))
        self.assertEqual(response.status_code, 404)

    def test_archive_confirmation_get_does_not_mutate(self):
        response = self.client.get(self.register_url("register_archive"))
        self.assertEqual(response.status_code, 200)
        self.register_a.refresh_from_db()
        self.assertTrue(self.register_a.is_active)

    def test_open_shift_blocks_archive(self):
        shift = services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=Decimal("0"),
        )
        response = self.client.post(self.register_url("register_archive"), follow=True)
        self.assertContains(response, "Close the register&#x27;s open shift")
        self.register_a.refresh_from_db()
        shift.refresh_from_db()
        self.assertTrue(self.register_a.is_active)
        self.assertEqual(shift.status, Shift.Status.OPEN)

    def test_open_shift_blocks_branch_reassignment(self):
        branch = Branch.objects.create(
            business=self.business_a, name="Move Target", code="MOVE"
        )
        services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=Decimal("0"),
        )
        response = self.client.post(
            self.register_url("register_edit"),
            self.edit_payload(branch=branch.id),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "open shift before changing its branch")
        self.register_a.refresh_from_db()
        self.assertEqual(self.register_a.branch_id, self.branch_a.id)

    def test_closed_shift_register_can_be_archived_without_losing_history(self):
        shift = services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=Decimal("25"),
        )
        services.close_shift(shift=shift, actual_cash=Decimal("25"))
        response = self.client.post(self.register_url("register_archive"))
        self.assertRedirects(response, reverse("registers:shift_list"))
        self.register_a.refresh_from_db()
        shift.refresh_from_db()
        self.assertFalse(self.register_a.is_active)
        self.assertEqual(shift.register_id, self.register_a.id)

    def test_archived_register_is_excluded_from_shift_dropdown(self):
        self.register_a.is_active = False
        self.register_a.save(update_fields=["is_active"])
        response = self.client.get(reverse("registers:shift_list"))
        self.assertNotIn(self.register_a, list(response.context["registers"]))
        self.assertContains(response, "Archived")

    def test_manipulated_post_cannot_open_archived_register(self):
        self.register_a.is_active = False
        self.register_a.save(update_fields=["is_active"])
        response = self.client.post(reverse("registers:shift_open"), {
            "register_id": self.register_a.id,
            "opening_cash": "0",
        }, follow=True)
        self.assertContains(response, "register is archived")
        self.assertFalse(Shift.objects.filter(register=self.register_a).exists())

    def test_service_cannot_open_register_on_inactive_branch(self):
        self.branch_a.is_active = False
        self.branch_a.save(update_fields=["is_active"])
        with self.assertRaisesMessage(services.ShiftError, "branch is inactive"):
            services.open_shift(
                business=self.business_a,
                register=self.register_a,
                cashier=self.owner_a,
                opening_cash=Decimal("0"),
            )

    def test_archived_register_shift_cannot_be_reopened(self):
        shift = services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=Decimal("0"),
        )
        services.close_shift(shift=shift, actual_cash=Decimal("0"))
        services.archive_register(register=self.register_a, user=self.owner_a)
        with self.assertRaisesMessage(services.ShiftError, "Archived registers"):
            services.reopen_shift(shift=shift, user=self.owner_a)

    def test_archived_shift_history_and_detail_remain_visible(self):
        shift = services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=Decimal("0"),
        )
        services.close_shift(shift=shift, actual_cash=Decimal("0"))
        services.archive_register(register=self.register_a, user=self.owner_a)
        list_response = self.client.get(reverse("registers:shift_list"))
        detail_response = self.client.get(
            reverse("registers:shift_detail", args=[shift.public_id])
        )
        self.assertContains(list_response, self.register_a.name)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.context["shift"].register_id, self.register_a.id)

    def test_archived_register_can_be_reactivated(self):
        services.archive_register(register=self.register_a, user=self.owner_a)
        response = self.client.post(self.register_url("register_reactivate"))
        self.assertRedirects(response, reverse("registers:shift_list"))
        self.register_a.refresh_from_db()
        self.assertTrue(self.register_a.is_active)

    def test_reactivate_is_post_only(self):
        self.register_a.is_active = False
        self.register_a.save(update_fields=["is_active"])
        response = self.client.get(self.register_url("register_reactivate"))
        self.assertEqual(response.status_code, 405)
        self.register_a.refresh_from_db()
        self.assertFalse(self.register_a.is_active)

    def test_inactive_branch_blocks_reactivation(self):
        self.register_a.is_active = False
        self.register_a.save(update_fields=["is_active"])
        self.branch_a.is_active = False
        self.branch_a.save(update_fields=["is_active"])
        response = self.client.post(self.register_url("register_reactivate"), follow=True)
        self.assertContains(response, "active branch before reactivating")
        self.register_a.refresh_from_db()
        self.assertFalse(self.register_a.is_active)

    def test_case_insensitive_active_code_conflict_blocks_reactivation(self):
        self.register_a.is_active = False
        self.register_a.save(update_fields=["is_active"])
        self.make_register(code=self.register_a.code.lower())
        response = self.client.post(self.register_url("register_reactivate"), follow=True)
        self.assertContains(response, "active register already uses this code")
        self.register_a.refresh_from_db()
        self.assertFalse(self.register_a.is_active)

    def test_plan_limit_blocks_reactivation(self):
        self.register_a.is_active = False
        self.register_a.save(update_fields=["is_active"])
        self.make_register(code="ACTIVE2")
        plan = self.business_a.subscription.plan
        plan.max_pos_terminals = 1
        plan.save(update_fields=["max_pos_terminals"])
        response = self.client.post(self.register_url("register_reactivate"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at most 1 pos terminals")
        self.register_a.refresh_from_db()
        self.assertFalse(self.register_a.is_active)

    def test_delete_confirmation_get_does_not_mutate(self):
        register = self.make_register()
        response = self.client.get(self.register_url("register_delete", register))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(CashRegister.objects.filter(pk=register.pk).exists())

    def test_unused_register_can_be_permanently_deleted(self):
        register = self.make_register()
        response = self.client.post(self.register_url("register_delete", register))
        self.assertRedirects(response, reverse("registers:shift_list"))
        self.assertFalse(CashRegister.objects.filter(pk=register.pk).exists())

    def test_shift_history_blocks_permanent_delete(self):
        shift = services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=Decimal("0"),
        )
        services.close_shift(shift=shift, actual_cash=Decimal("0"))
        response = self.client.post(self.register_url("register_delete"), follow=True)
        self.assertContains(
            response,
            "cannot be permanently deleted. Archive it instead",
        )
        self.assertTrue(CashRegister.objects.filter(pk=self.register_a.pk).exists())
        shift.refresh_from_db()
        self.assertEqual(shift.register_id, self.register_a.id)

    def test_sale_history_blocks_delete_and_preserves_register_fk(self):
        self.allow_no_shift()
        sale = self.make_sale(register=self.register_a)
        response = self.client.post(self.register_url("register_delete"), follow=True)
        self.assertContains(response, "cannot be permanently deleted")
        self.register_a.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(sale.register_id, self.register_a.id)

    def test_delete_action_is_hidden_when_history_exists(self):
        self.allow_no_shift()
        self.make_sale(register=self.register_a)
        response = self.client.get(reverse("registers:shift_list"))
        self.assertNotContains(
            response,
            reverse("registers:register_delete", args=[self.register_a.public_id]),
        )

    def test_deletion_assessment_reports_financial_dependencies(self):
        from apps.sales import services as sales
        from apps.sales.models import SaleReturn

        self.allow_no_shift()
        sale = self.make_sale(register=self.register_a)
        sales.process_return(
            sale=sale,
            items=[{"sale_item": sale.items.get(), "quantity": Decimal("1")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        assessment = services.assess_register_deletion(self.register_a)
        self.assertFalse(assessment.can_delete)
        self.assertIn("sale or invoice history", assessment.blockers)
        self.assertIn("sale payment history", assessment.blockers)
        self.assertIn("return or refund history", assessment.blockers)

    def test_deletion_assessment_reports_shift_linked_financial_history(self):
        from apps.customers.models import CustomerPayment
        from apps.expenses.models import Expense, ExpenseCategory

        shift = services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=Decimal("0"),
        )
        CustomerPayment.objects.create(
            business=self.business_a,
            receipt_number="CP-REG-1",
            customer=self.walk_in_a,
            branch=self.branch_a,
            amount=Decimal("1"),
            payment_method=self.cash_a,
            received_by=self.owner_a,
            shift=shift,
        )
        category = ExpenseCategory.objects.create(
            business=self.business_a,
            name="Register Test Expense",
        )
        Expense.objects.create(
            business=self.business_a,
            expense_number="EXP-REG-1",
            expense_date=timezone.localdate(),
            branch=self.branch_a,
            category=category,
            amount=Decimal("1"),
            payment_method=self.cash_a,
            shift=shift,
            created_by=self.owner_a,
        )
        assessment = services.assess_register_deletion(self.register_a)
        self.assertIn("customer payment history", assessment.blockers)
        self.assertIn("expense or cash movement history", assessment.blockers)

    def test_create_and_delete_audit_logs_survive_hard_delete(self):
        self.client.post(reverse("registers:register_create"), {
            "name": "Audited Register",
            "code": "AUDITREG",
            "branch": self.branch_a.id,
            "receipt_printer": "80mm",
        })
        register = CashRegister.objects.get(business=self.business_a, code="AUDITREG")
        object_id = str(register.public_id)
        assessment = services.assess_register_deletion(register)
        self.assertTrue(assessment.can_delete)
        self.assertTrue(assessment.audit_logs_preserved)
        self.client.post(self.register_url("register_delete", register))
        logs = AuditLog.objects.filter(
            business=self.business_a,
            object_type="CashRegister",
            object_id=object_id,
        )
        self.assertEqual(
            set(logs.values_list("action", flat=True)),
            {"register.created", "register.deleted"},
        )
        self.assertFalse(CashRegister.objects.filter(public_id=object_id).exists())

    def test_edit_archive_and_reactivate_are_audited(self):
        self.client.post(
            self.register_url("register_edit"),
            self.edit_payload(name="Audited Edit"),
        )
        self.client.post(self.register_url("register_archive"))
        self.client.post(self.register_url("register_reactivate"))
        actions = set(AuditLog.objects.filter(
            business=self.business_a,
            object_type="CashRegister",
            object_id=str(self.register_a.public_id),
        ).values_list("action", flat=True))
        self.assertTrue({
            "register.updated", "register.archived", "register.reactivated"
        }.issubset(actions))

    def test_archived_only_cashier_login_avoids_broken_shift_flow(self):
        self.register_a.is_active = False
        self.register_a.save(update_fields=["is_active"])
        role = Role.objects.create(
            business=self.business_a,
            name="Archived-only cashier",
            permissions=["sales.create", "sales.view", "shifts.open"],
        )
        user = User.objects.create_user(
            email="archived-only@example.com",
            password=self.password,
            full_name="Archived Only",
        )
        Membership.objects.create(business=self.business_a, user=user, role=role)
        self.client.logout()
        response = self.client.post(reverse("accounts:login"), {
            "email": user.email,
            "password": self.password,
        })
        self.assertRedirects(
            response,
            reverse("sales:list"),
            fetch_redirect_response=False,
        )
