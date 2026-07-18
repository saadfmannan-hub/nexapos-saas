"""Focused Phase 2A enforcement tests for the authoritative POS Core flag.

These tests intentionally change only transaction-scoped test data.  They do
not alter seeded plan defaults or production subscription records.
"""

import csv
import io
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch, Warehouse
from apps.catalog import services as catalog_services
from apps.catalog.models import Brand, Category, Product, TaxRate, Unit
from apps.customers import services as customer_services
from apps.customers.models import Customer, CustomerGroup
from apps.inventory import services as inventory_services
from apps.registers import services as register_services
from apps.registers.models import CashRegister, Shift
from apps.sales import services as sales_services
from apps.sales.models import HeldSale, Sale, SaleReturn
from apps.subscriptions.access import AccessAction, evaluate_actor_access
from apps.subscriptions.exceptions import DenialCode, ModuleAccessDenied
from apps.subscriptions.models import Plan, Subscription
from apps.suppliers.models import Supplier

from .base import TenantTestCase

D = Decimal


class PosCoreEnforcementTests(TenantTestCase):
    password = "StrongPass123!"

    def setUp(self):
        self.allow_no_shift()

    # ------------------------------------------------------------------
    # Test-only entitlement and actor helpers
    # ------------------------------------------------------------------
    def subscription(self):
        return Subscription.objects.select_related("plan").get(business=self.business_a)

    def set_plan(self, **fields):
        subscription = self.subscription()
        Plan.objects.filter(pk=subscription.plan_id).update(**fields)

    def set_pos_core(self, enabled):
        self.set_plan(feature_sales=enabled)

    def set_subscription_status(self, status):
        values = {
            "status": status,
            "trial_ends_at": None,
            "current_period_end": timezone.now() + timedelta(days=30),
        }
        Subscription.objects.filter(business=self.business_a).update(**values)

    def make_staff(self, permissions, *, email, branches=None):
        role = Role.objects.create(
            business=self.business_a,
            name=f"Phase 2A role {email}",
            permissions=list(permissions),
        )
        user = User.objects.create_user(
            email=email,
            password=self.password,
            full_name="Phase 2A Staff",
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=user,
            role=role,
        )
        if branches is not None:
            membership.branches.set(branches)
        return user, membership

    def login(self, user):
        return self.client.post(
            reverse("accounts:login"),
            {"email": user.email, "password": self.password},
        )

    def assert_service_denied(self, callback, code):
        with self.assertRaises(ModuleAccessDenied) as caught:
            callback()
        self.assertEqual(caught.exception.denial.code, code)

    def make_secondary_locations(self):
        branch = Branch.objects.create(
            business=self.business_a,
            name="Security Other Branch",
            code="SEC-B2",
        )
        warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=branch,
            name="Security Other Warehouse",
            code="SEC-W2",
        )
        central = Warehouse.objects.create(
            business=self.business_a,
            branch=None,
            name="Security Central Warehouse",
            code="SEC-CENTRAL",
        )
        return branch, warehouse, central

    def product_export_row(self, response, sku="WID-A"):
        self.assertEqual(response.status_code, 200)
        records = csv.DictReader(
            io.StringIO(response.content.decode("utf-8"), newline="")
        )
        return next(row for row in records if row["SKU"] == sku)

    @staticmethod
    def direct_read_routes():
        return (
            "sales:list",
            "catalog:product_list",
            "customers:list",
            "registers:shift_list",
            "branches:list",
            "accounts:user_list",
        )

    @staticmethod
    def direct_write_routes():
        return (
            "sales:pos_checkout",
            "catalog:product_create",
            "customers:create",
            "registers:shift_open",
            "branches:branch_create",
            "accounts:user_create",
        )

    # ------------------------------------------------------------------
    # Browser route matrix: owner, staff, permissions, and plan state
    # ------------------------------------------------------------------
    def test_enabled_owner_can_open_direct_pos_core_routes(self):
        self.set_pos_core(True)
        self.client.force_login(self.owner_a)

        for route_name in self.direct_read_routes():
            with self.subTest(route=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("sales:pos")).status_code, 200)

    def test_enabled_non_owner_with_permissions_can_open_direct_routes(self):
        permissions = {
            "sales.view",
            "sales.create",
            "products.view",
            "customers.view",
            "shifts.open",
            "registers.manage",
            "branches.manage",
            "users.manage",
        }
        user, _membership = self.make_staff(
            permissions,
            email="phase2a-enabled@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)
        self.client.force_login(user)

        for route_name in self.direct_read_routes():
            with self.subTest(route=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("sales:pos")).status_code, 200)

    def test_disabled_pos_core_denies_owner_without_bypass(self):
        self.set_pos_core(False)
        self.client.force_login(self.owner_a)

        for route_name in (*self.direct_read_routes(), "sales:pos"):
            with self.subTest(route=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 403)

    def test_disabled_pos_core_denies_non_owner_with_permissions(self):
        permissions = {
            "sales.view",
            "sales.create",
            "products.view",
            "customers.view",
            "shifts.open",
            "registers.manage",
            "branches.manage",
            "users.manage",
        }
        user, _membership = self.make_staff(
            permissions,
            email="phase2a-disabled@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(False)
        self.client.force_login(user)

        for route_name in (*self.direct_read_routes(), "sales:pos"):
            with self.subTest(route=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 403)

    def test_disabled_pos_core_denies_direct_detail_and_output_urls(self):
        sale = self.make_sale()
        shift = register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=D("0"),
            membership=self.membership_a(),
        )
        self.set_pos_core(False)
        self.client.force_login(self.owner_a)

        urls = (
            reverse("sales:detail", args=[sale.public_id]),
            reverse("sales:invoice", args=[sale.public_id]),
            reverse("sales:receipt", args=[sale.public_id]),
            reverse("sales:invoice_pdf", args=[sale.public_id]),
            reverse("catalog:product_detail", args=[self.product_a.public_id]),
            reverse("customers:detail", args=[self.walk_in_a.public_id]),
            reverse("registers:register_edit", args=[self.register_a.public_id]),
            reverse("registers:shift_detail", args=[shift.public_id]),
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_enabled_module_does_not_replace_role_permissions(self):
        user, _membership = self.make_staff([], email="phase2a-no-perms@example.com")
        self.set_pos_core(True)
        self.client.force_login(user)

        permission_specific_routes = (
            "sales:list",
            "catalog:product_list",
            "customers:list",
            "registers:register_create",
            "branches:list",
            "accounts:user_list",
        )
        for route_name in permission_specific_routes:
            with self.subTest(route=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 403)

    def test_disabled_module_blocks_direct_write_urls_before_form_or_payload_handling(self):
        self.set_pos_core(False)
        self.client.force_login(self.owner_a)

        for route_name in self.direct_write_routes():
            with self.subTest(route=route_name):
                response = self.client.post(reverse(route_name), data={})
                self.assertEqual(response.status_code, 403)

    # ------------------------------------------------------------------
    # Subscription state matrix and read-only output behavior
    # ------------------------------------------------------------------
    def test_active_and_grace_states_allow_pos_reads_and_writes(self):
        self.set_pos_core(True)
        self.client.force_login(self.owner_a)

        for status in (Subscription.Status.ACTIVE, Subscription.Status.GRACE):
            with self.subTest(status=status):
                self.set_subscription_status(status)
                self.assertEqual(self.client.get(reverse("sales:pos")).status_code, 200)
                before = Sale.objects.for_business(self.business_a).count()
                self.make_sale()
                self.assertEqual(Sale.objects.for_business(self.business_a).count(), before + 1)

    def test_past_due_and_expired_allow_history_but_deny_write_routes(self):
        sale = self.make_sale()
        self.client.force_login(self.owner_a)

        for status in (Subscription.Status.PAST_DUE, Subscription.Status.EXPIRED):
            with self.subTest(status=status):
                self.set_subscription_status(status)
                self.assertEqual(self.client.get(reverse("sales:list")).status_code, 200)
                self.assertEqual(
                    self.client.get(reverse("sales:detail", args=[sale.public_id])).status_code,
                    200,
                )
                self.assertEqual(self.client.get(reverse("sales:pos")).status_code, 403)
                for route_name in self.direct_write_routes():
                    response = self.client.post(reverse(route_name), data={})
                    self.assertEqual(response.status_code, 403, route_name)

    def test_read_only_catalog_setup_post_is_a_stable_403_without_mutation(self):
        self.set_pos_core(True)
        self.set_subscription_status(Subscription.Status.PAST_DUE)
        self.client.force_login(self.owner_a)
        before = Category.objects.for_business(self.business_a).count()

        response = self.client.post(
            reverse("catalog:category_list"),
            {"name": "Must Not Save", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Category.objects.for_business(self.business_a).count(), before)

    def test_suspended_subscription_denies_historical_pos_core_gets(self):
        self.set_subscription_status(Subscription.Status.SUSPENDED)
        self.client.force_login(self.owner_a)

        for route_name in self.direct_read_routes():
            with self.subTest(route=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 403)

    def test_missing_subscription_fails_closed(self):
        Subscription.objects.filter(business=self.business_a).delete()
        self.client.force_login(self.owner_a)

        self.assertEqual(self.client.get(reverse("sales:list")).status_code, 403)

    def test_inactive_assigned_plan_fails_closed(self):
        self.set_plan(is_active=False)
        self.client.force_login(self.owner_a)

        self.assertEqual(self.client.get(reverse("sales:list")).status_code, 403)

    def test_valid_trial_is_full_and_cancelled_is_read_only(self):
        self.set_pos_core(True)
        self.set_plan(allow_trial=True)
        Subscription.objects.filter(business=self.business_a).update(
            status=Subscription.Status.TRIAL,
            trial_ends_at=timezone.now() + timedelta(days=7),
            current_period_end=None,
        )
        self.client.force_login(self.owner_a)

        self.assertEqual(self.client.get(reverse("sales:list")).status_code, 200)
        trial_sale = self.make_sale()
        self.assertIsNotNone(trial_sale.pk)

        self.set_subscription_status(Subscription.Status.CANCELLED)
        self.assertEqual(self.client.get(reverse("sales:list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("sales:pos")).status_code, 403)
        self.assert_service_denied(
            lambda: self.make_sale(),
            DenialCode.SUBSCRIPTION_READ_ONLY,
        )

    def test_invalid_membership_and_inactive_business_fail_closed(self):
        self.set_pos_core(True)
        foreign_membership = self.business_b.memberships.get(user=self.owner_b)

        invalid_membership = evaluate_actor_access(
            self.owner_a,
            self.business_a,
            "pos_core",
            action=AccessAction.READ,
            membership=foreign_membership,
        )
        self.assertFalse(invalid_membership.allowed)
        self.assertEqual(invalid_membership.denial.code, DenialCode.MEMBERSHIP_REQUIRED)

        self.business_a.is_active = False
        self.business_a.save(update_fields=["is_active", "updated_at"])
        inactive_business = evaluate_actor_access(
            self.owner_a,
            self.business_a,
            "pos_core",
            action=AccessAction.READ,
            membership=self.membership_a(),
        )
        self.assertFalse(inactive_business.allowed)
        self.assertEqual(inactive_business.denial.code, DenialCode.BUSINESS_INACTIVE)

    def test_read_only_invoice_receipt_and_pdf_are_safe_gets_without_reprint_mutation(self):
        sale = self.make_sale()
        Sale.objects.filter(pk=sale.pk).update(reprint_count=3)
        self.set_subscription_status(Subscription.Status.PAST_DUE)
        self.client.force_login(self.owner_a)

        with mock.patch("apps.reports.pdf.render_pdf", return_value=b"%PDF-1.4\n"):
            routes = (
                "sales:invoice",
                "sales:receipt",
                "sales:invoice_pdf",
            )
            for route_name in routes:
                with self.subTest(route=route_name):
                    response = self.client.get(reverse(route_name, args=[sale.public_id]))
                    self.assertEqual(response.status_code, 200)

        sale.refresh_from_db()
        self.assertEqual(sale.reprint_count, 3)

    def test_read_only_direct_service_write_is_denied_before_mutation(self):
        self.set_subscription_status(Subscription.Status.PAST_DUE)
        before = Sale.objects.for_business(self.business_a).count()

        self.assert_service_denied(
            lambda: self.make_sale(),
            DenialCode.SUBSCRIPTION_READ_ONLY,
        )
        self.assertEqual(Sale.objects.for_business(self.business_a).count(), before)

    def test_read_only_blocks_all_critical_service_families_before_mutation(self):
        sale = self.make_sale()
        sale_item = sale.items.get()
        shift = register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=D("0"),
            membership=self.membership_a(),
        )
        unsaved_product = Product(
            business=self.business_a,
            name="Read-only Product",
            sku="READ-ONLY-PRODUCT",
            sale_price=D("1.000"),
        )
        unsaved_customer = Customer(
            business=self.business_a,
            code="READ-ONLY-CUSTOMER",
            full_name="Read-only Customer",
        )
        payment_count = sale.payments.count()
        return_count = sale.returns.count()
        self.set_subscription_status(Subscription.Status.PAST_DUE)

        cases = (
            (
                "sale payment",
                lambda: sales_services.add_sale_payment(
                    sale=sale,
                    amount=D("1.000"),
                    method=self.cash_a,
                    user=self.owner_a,
                ),
            ),
            (
                "sale void",
                lambda: sales_services.void_sale(
                    sale=sale,
                    user=self.owner_a,
                    reason="read-only denial",
                ),
            ),
            (
                "sale return",
                lambda: sales_services.process_return(
                    sale=sale,
                    items=[{"sale_item": sale_item, "quantity": D("1.000")}],
                    refund_method=SaleReturn.RefundMethod.CASH,
                    user=self.owner_a,
                ),
            ),
            (
                "product save",
                lambda: catalog_services.save_product(
                    product=unsaved_product,
                    business=self.business_a,
                    user=self.owner_a,
                ),
            ),
            (
                "customer save",
                lambda: customer_services.save_customer(
                    customer=unsaved_customer,
                    business=self.business_a,
                    user=self.owner_a,
                ),
            ),
            (
                "shift open",
                lambda: register_services.open_shift(
                    business=self.business_a,
                    register=self.register_a,
                    cashier=self.owner_a,
                    opening_cash=D("0"),
                ),
            ),
            (
                "shift close",
                lambda: register_services.close_shift(
                    shift=shift,
                    actual_cash=D("0"),
                    user=self.owner_a,
                ),
            ),
            (
                "register archive",
                lambda: register_services.archive_register(
                    register=self.register_a,
                    user=self.owner_a,
                ),
            ),
        )
        for label, callback in cases:
            with self.subTest(service=label):
                self.assert_service_denied(callback, DenialCode.SUBSCRIPTION_READ_ONLY)

        sale.refresh_from_db()
        shift.refresh_from_db()
        self.register_a.refresh_from_db()
        self.assertEqual(sale.payments.count(), payment_count)
        self.assertEqual(sale.returns.count(), return_count)
        self.assertEqual(sale.status, Sale.Status.COMPLETED)
        self.assertEqual(shift.status, Shift.Status.OPEN)
        self.assertIsNone(unsaved_product.pk)
        self.assertIsNone(unsaved_customer.pk)
        self.assertTrue(self.register_a.is_active)

    # ------------------------------------------------------------------
    # Critical non-HTTP mutation entry points
    # ------------------------------------------------------------------
    def test_disabled_module_blocks_critical_direct_services_without_mutation(self):
        sale = self.make_sale()
        sale_item = sale.items.get()
        unsaved_product = Product(
            business=self.business_a,
            name="Denied Product",
            sku="DENIED-PRODUCT",
            sale_price=D("1.000"),
        )
        unsaved_customer = Customer(
            business=self.business_a,
            code="DENIED-CUSTOMER",
            full_name="Denied Customer",
        )
        sale_count = Sale.objects.for_business(self.business_a).count()
        payment_count = sale.payments.count()
        return_count = sale.returns.count()
        stock_before = self.product_a.stock_levels.get(warehouse=self.warehouse_a).quantity
        shift = register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=D("0"),
            membership=self.membership_a(),
        )
        self.set_pos_core(False)

        cases = (
            (
                "sale completion",
                lambda: self.make_sale(),
            ),
            (
                "later sale payment",
                lambda: sales_services.add_sale_payment(
                    sale=sale,
                    amount=D("1.000"),
                    method=self.cash_a,
                    user=self.owner_a,
                ),
            ),
            (
                "sale void",
                lambda: sales_services.void_sale(
                    sale=sale,
                    user=self.owner_a,
                    reason="must be denied",
                ),
            ),
            (
                "sale return",
                lambda: sales_services.process_return(
                    sale=sale,
                    items=[{"sale_item": sale_item, "quantity": D("1.000")}],
                    refund_method=SaleReturn.RefundMethod.CASH,
                    user=self.owner_a,
                ),
            ),
            (
                "product save",
                lambda: catalog_services.save_product(
                    product=unsaved_product,
                    business=self.business_a,
                    user=self.owner_a,
                ),
            ),
            (
                "product import",
                lambda: catalog_services.import_products(
                    business=self.business_a,
                    rows=[{"product name": "Denied Import", "sku": "DENIED-IMPORT"}],
                    match_by="sku",
                    user=self.owner_a,
                ),
            ),
            (
                "customer save",
                lambda: customer_services.save_customer(
                    customer=unsaved_customer,
                    business=self.business_a,
                    user=self.owner_a,
                ),
            ),
            (
                "customer import",
                lambda: customer_services.import_customers(
                    business=self.business_a,
                    rows=[{"customer name": "Denied Import", "customer code": "DENIED-I"}],
                    mode="skip",
                    user=self.owner_a,
                ),
            ),
            (
                "shift open",
                lambda: register_services.open_shift(
                    business=self.business_a,
                    register=self.register_a,
                    cashier=self.owner_a,
                    opening_cash=D("0"),
                ),
            ),
            (
                "shift close",
                lambda: register_services.close_shift(
                    shift=shift,
                    actual_cash=D("0"),
                    user=self.owner_a,
                ),
            ),
            (
                "register archive",
                lambda: register_services.archive_register(
                    register=self.register_a,
                    user=self.owner_a,
                ),
            ),
        )

        for label, callback in cases:
            with self.subTest(service=label):
                self.assert_service_denied(callback, DenialCode.MODULE_DISABLED)

        sale.refresh_from_db()
        self.product_a.refresh_from_db()
        self.register_a.refresh_from_db()
        shift.refresh_from_db()
        self.assertEqual(Sale.objects.for_business(self.business_a).count(), sale_count)
        self.assertEqual(sale.payments.count(), payment_count)
        self.assertEqual(sale.returns.count(), return_count)
        self.assertEqual(
            self.product_a.stock_levels.get(warehouse=self.warehouse_a).quantity,
            stock_before,
        )
        self.assertEqual(sale.status, Sale.Status.COMPLETED)
        self.assertIsNone(unsaved_product.pk)
        self.assertIsNone(unsaved_customer.pk)
        self.assertTrue(self.register_a.is_active)
        self.assertEqual(shift.status, Shift.Status.OPEN)

    def test_service_permission_layer_still_denies_non_owner(self):
        user, membership = self.make_staff(
            ["sales.view"],
            email="phase2a-service-no-create@example.com",
        )
        self.set_pos_core(True)
        before = Sale.objects.for_business(self.business_a).count()

        self.assert_service_denied(
            lambda: sales_services.complete_sale(
                business=self.business_a,
                branch=self.branch_a,
                warehouse=self.warehouse_a,
                cashier=user,
                customer=self.walk_in_a,
                items=[
                    {
                        "product": self.product_a,
                        "quantity": D("1.000"),
                        "unit_price": D("10.000"),
                    }
                ],
                payments=[{"method": self.cash_a, "amount": D("10.500")}],
                membership=membership,
            ),
            DenialCode.PERMISSION_DENIED,
        )
        self.assertEqual(Sale.objects.for_business(self.business_a).count(), before)

    def test_service_request_actor_cannot_authorize_a_different_actor(self):
        user, membership = self.make_staff([], email="phase2a-request-actor-mismatch@example.com")
        self.set_pos_core(True)
        request = SimpleNamespace(
            user=self.owner_a,
            business=self.business_a,
            membership=self.membership_a(),
            method="POST",
        )

        decision = evaluate_actor_access(
            user,
            self.business_a,
            "pos_core",
            permission_code="sales.create",
            action=AccessAction.WRITE,
            membership=membership,
            request=request,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.denial.code, DenialCode.PERMISSION_DENIED)

    def test_direct_shift_close_requires_cashier_or_approver(self):
        user, membership = self.make_staff(
            ["shifts.close"],
            email="phase2a-close-only@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)
        shift = register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.cashier_a,
            opening_cash=D("0"),
            membership=self.cashier_membership,
        )

        with self.assertRaisesMessage(
            register_services.ShiftError,
            "Only the shift's cashier or a manager can close it.",
        ):
            register_services.close_shift(
                shift=shift,
                actual_cash=D("0"),
                user=user,
                membership=membership,
            )

        shift.refresh_from_db()
        self.assertEqual(shift.status, Shift.Status.OPEN)

    def test_product_and_customer_services_reject_foreign_related_objects(self):
        self.set_pos_core(True)
        foreign_relations = {
            "category": Category.objects.create(business=self.business_b, name="Foreign Category"),
            "brand": Brand.objects.create(business=self.business_b, name="Foreign Brand"),
            "unit": Unit.objects.create(
                business=self.business_b,
                name="Foreign Unit",
                abbreviation="FU",
            ),
            "tax_rate": TaxRate.objects.create(
                business=self.business_b,
                name="Foreign Tax",
                rate=D("5"),
            ),
            "preferred_supplier": Supplier.objects.create(
                business=self.business_b,
                code="FOREIGN-SUPPLIER",
                name="Foreign Supplier",
            ),
        }

        for index, (field, related_object) in enumerate(foreign_relations.items()):
            product = Product(
                business=self.business_a,
                name=f"Scoped Product {index}",
                sku=f"SCOPED-{index}",
                sale_price=D("1.000"),
                **{field: related_object},
            )
            with self.subTest(field=field):
                self.assert_service_denied(
                    lambda product=product: catalog_services.save_product(
                        product=product,
                        business=self.business_a,
                        user=self.owner_a,
                    ),
                    DenialCode.SCOPE_DENIED,
                )
                self.assertIsNone(product.pk)

        foreign_group = CustomerGroup.objects.create(
            business=self.business_b,
            name="Foreign Customer Group",
        )
        customer = Customer(
            business=self.business_a,
            code="SCOPED-CUSTOMER",
            full_name="Scoped Customer",
            group=foreign_group,
        )
        self.assert_service_denied(
            lambda: customer_services.save_customer(
                customer=customer,
                business=self.business_a,
                user=self.owner_a,
            ),
            DenialCode.SCOPE_DENIED,
        )
        self.assertIsNone(customer.pk)

    def test_save_product_rejects_persisted_foreign_instance_with_forged_business(self):
        self.set_pos_core(True)
        foreign_product = Product.objects.create(
            business=self.business_b,
            name="Foreign Persisted Product",
            sku="FOREIGN-PERSISTED-PRODUCT",
            sale_price=D("3.000"),
        )
        foreign_product.business = self.business_a
        foreign_product.name = "Forged Product Update"

        self.assert_service_denied(
            lambda: catalog_services.save_product(
                product=foreign_product,
                business=self.business_a,
                user=self.owner_a,
            ),
            DenialCode.SCOPE_DENIED,
        )

        persisted = Product.objects.get(pk=foreign_product.pk)
        self.assertEqual(persisted.business_id, self.business_b.id)
        self.assertEqual(persisted.name, "Foreign Persisted Product")
        self.assertEqual(persisted.sku, "FOREIGN-PERSISTED-PRODUCT")

    def test_save_customer_rejects_persisted_foreign_instance_with_forged_business(self):
        self.set_pos_core(True)
        foreign_customer = Customer.objects.create(
            business=self.business_b,
            code="FOREIGN-PERSISTED-CUSTOMER",
            full_name="Foreign Persisted Customer",
        )
        foreign_customer.business = self.business_a
        foreign_customer.full_name = "Forged Customer Update"

        self.assert_service_denied(
            lambda: customer_services.save_customer(
                customer=foreign_customer,
                business=self.business_a,
                user=self.owner_a,
            ),
            DenialCode.SCOPE_DENIED,
        )

        persisted = Customer.objects.get(pk=foreign_customer.pk)
        self.assertEqual(persisted.business_id, self.business_b.id)
        self.assertEqual(persisted.full_name, "Foreign Persisted Customer")
        self.assertEqual(persisted.code, "FOREIGN-PERSISTED-CUSTOMER")

    def test_save_product_rejects_foreign_relation_with_forged_business(self):
        self.set_pos_core(True)
        foreign_category = Category.objects.create(
            business=self.business_b,
            name="Foreign Persisted Category",
        )
        foreign_category.business = self.business_a
        product = Product(
            business=self.business_a,
            name="Forged Relation Product",
            sku="FORGED-RELATION-PRODUCT",
            sale_price=D("3.000"),
            category=foreign_category,
        )

        self.assert_service_denied(
            lambda: catalog_services.save_product(
                product=product,
                business=self.business_a,
                user=self.owner_a,
            ),
            DenialCode.SCOPE_DENIED,
        )

        persisted_category = Category.objects.get(pk=foreign_category.pk)
        self.assertEqual(persisted_category.business_id, self.business_b.id)
        self.assertEqual(persisted_category.name, "Foreign Persisted Category")
        self.assertIsNone(product.pk)
        self.assertFalse(Product.objects.filter(sku="FORGED-RELATION-PRODUCT").exists())

    def test_register_writes_reject_foreign_instance_with_forged_tenant(self):
        self.set_pos_core(True)
        foreign_register = CashRegister.objects.for_business(self.business_b).get(code="REG1")
        original_name = foreign_register.name
        original_branch_id = foreign_register.branch_id
        foreign_register.business = self.business_a
        foreign_register.branch = self.branch_a
        foreign_register.name = "Forged Register Update"

        cases = (
            lambda: register_services.archive_register(
                register=foreign_register,
                user=self.owner_a,
                membership=self.membership_a(),
            ),
            lambda: register_services.delete_register_if_safe(
                register=foreign_register,
                user=self.owner_a,
                membership=self.membership_a(),
            ),
            lambda: register_services.save_register(
                register=foreign_register,
                business=self.business_a,
                user=self.owner_a,
                membership=self.membership_a(),
            ),
        )
        for callback in cases:
            self.assert_service_denied(callback, DenialCode.SCOPE_DENIED)

        persisted = CashRegister.objects.get(pk=foreign_register.pk)
        self.assertEqual(persisted.business_id, self.business_b.id)
        self.assertEqual(persisted.branch_id, original_branch_id)
        self.assertEqual(persisted.name, original_name)
        self.assertTrue(persisted.is_active)

    def test_register_and_shift_writes_recheck_canonical_branch(self):
        self.set_pos_core(True)
        restricted_branch = Branch.objects.create(
            business=self.business_a,
            name="Restricted Branch",
            code="RESTRICTED",
        )
        restricted_register = CashRegister.objects.create(
            business=self.business_a,
            branch=restricted_branch,
            name="Restricted Register",
            code="RESTRICTED",
        )
        user, membership = self.make_staff(
            ["shifts.open", "shifts.close"],
            email="phase2a-forged-shift@example.com",
            branches=[self.branch_a],
        )

        forged_register = CashRegister.objects.get(pk=restricted_register.pk)
        forged_register.branch = self.branch_a
        self.assert_service_denied(
            lambda: register_services.open_shift(
                business=self.business_a,
                register=forged_register,
                cashier=user,
                opening_cash=D("0"),
                membership=membership,
            ),
            DenialCode.SCOPE_DENIED,
        )
        self.assertFalse(Shift.objects.filter(register=restricted_register).exists())

        shift = register_services.open_shift(
            business=self.business_a,
            register=restricted_register,
            cashier=self.owner_a,
            opening_cash=D("0"),
            membership=self.membership_a(),
        )
        forged_shift = Shift.objects.get(pk=shift.pk)
        forged_shift.branch = self.branch_a
        self.assert_service_denied(
            lambda: register_services.close_shift(
                shift=forged_shift,
                actual_cash=D("0"),
                user=user,
                membership=membership,
            ),
            DenialCode.SCOPE_DENIED,
        )
        shift.refresh_from_db()
        self.assertEqual(shift.status, Shift.Status.OPEN)

    def test_sale_and_held_sale_writes_recheck_canonical_scope(self):
        self.set_pos_core(True)
        restricted_branch = Branch.objects.create(
            business=self.business_a,
            name="Sale Restricted Branch",
            code="SALE-RESTRICTED",
        )
        restricted_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=restricted_branch,
            name="Sale Restricted Warehouse",
            code="SALE-RESTRICTED",
        )
        non_stock = Product.objects.create(
            business=self.business_a,
            name="Scoped Non-stock Item",
            sku="SCOPED-NON-STOCK",
            product_type=Product.Type.NON_STOCK,
            track_inventory=False,
            sale_price=D("10.000"),
        )
        sale = sales_services.complete_sale(
            business=self.business_a,
            branch=restricted_branch,
            warehouse=restricted_warehouse,
            cashier=self.owner_a,
            customer=self.walk_in_a,
            items=[{"product": non_stock, "quantity": D("1")}],
            payments=[{"method": self.cash_a, "amount": D("10.000")}],
            membership=self.membership_a(),
        )
        user, membership = self.make_staff(
            ["sales.create"],
            email="phase2a-forged-sale@example.com",
            branches=[self.branch_a],
        )

        forged_sale = Sale.objects.get(pk=sale.pk)
        forged_sale.branch = self.branch_a
        self.assert_service_denied(
            lambda: sales_services.set_delivery_status(
                sale=forged_sale,
                status=Sale.DeliveryStatus.DELIVERED,
                user=user,
                membership=membership,
            ),
            DenialCode.SCOPE_DENIED,
        )
        sale.refresh_from_db()
        self.assertEqual(sale.delivery_status, "")

        foreign_branch = Branch.objects.get(pk=self.branch_b.pk)
        foreign_branch.business = self.business_a
        held_before = HeldSale.objects.for_business(self.business_a).count()
        self.assert_service_denied(
            lambda: sales_services.hold_sale(
                business=self.business_a,
                branch=foreign_branch,
                cashier=self.owner_a,
                cart={"items": []},
                membership=self.membership_a(),
            ),
            DenialCode.SCOPE_DENIED,
        )
        self.assertEqual(HeldSale.objects.for_business(self.business_a).count(), held_before)

    def test_complete_sale_rejects_foreign_salesperson_without_mutation(self):
        self.set_pos_core(True)
        sale_count = Sale.objects.for_business(self.business_a).count()
        stock_before = self.product_a.stock_levels.get(warehouse=self.warehouse_a).quantity

        self.assert_service_denied(
            lambda: self.make_sale(salesperson=self.owner_b),
            DenialCode.SCOPE_DENIED,
        )

        self.assertEqual(Sale.objects.for_business(self.business_a).count(), sale_count)
        self.assertEqual(
            self.product_a.stock_levels.get(warehouse=self.warehouse_a).quantity,
            stock_before,
        )

    # ------------------------------------------------------------------
    # Branch and warehouse security regressions
    # ------------------------------------------------------------------
    def test_restricted_branch_admin_lists_and_object_routes_are_scoped(self):
        other_branch, other_warehouse, central = self.make_secondary_locations()
        user, _membership = self.make_staff(
            ["branches.manage"],
            email="phase2a-branch-scope@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)
        self.client.force_login(user)

        response = self.client.get(reverse("branches:list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {branch.pk for branch in response.context["branches"]},
            {self.branch_a.pk},
        )
        self.assertEqual(
            {warehouse.pk for warehouse in response.context["warehouses"]},
            {self.warehouse_a.pk, central.pk},
        )
        self.assertNotContains(response, other_branch.name)
        self.assertNotContains(response, other_warehouse.name)
        self.assertContains(response, central.name)

        self.assertEqual(
            self.client.get(
                reverse("branches:branch_edit", args=[self.branch_a.public_id])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("branches:warehouse_edit", args=[self.warehouse_a.public_id])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("branches:warehouse_edit", args=[central.public_id])
            ).status_code,
            200,
        )

        branch_url = reverse("branches:branch_edit", args=[other_branch.public_id])
        warehouse_url = reverse(
            "branches:warehouse_edit", args=[other_warehouse.public_id]
        )
        self.assertEqual(self.client.get(branch_url).status_code, 404)
        self.assertEqual(
            self.client.post(
                branch_url,
                {"name": "Forbidden Branch Change", "code": other_branch.code},
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get(warehouse_url).status_code, 404)
        self.assertEqual(
            self.client.post(
                warehouse_url,
                {"name": "Forbidden Warehouse Change", "code": other_warehouse.code},
            ).status_code,
            404,
        )
        other_branch.refresh_from_db()
        other_warehouse.refresh_from_db()
        self.assertEqual(other_branch.name, "Security Other Branch")
        self.assertEqual(other_warehouse.name, "Security Other Warehouse")

    def test_restricted_warehouse_edit_cannot_reassign_or_mutate_other_defaults(self):
        other_branch, other_warehouse, _central = self.make_secondary_locations()
        user, _membership = self.make_staff(
            ["branches.manage"],
            email="phase2a-warehouse-scope@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)
        self.client.force_login(user)
        warehouse_url = reverse(
            "branches:warehouse_edit", args=[self.warehouse_a.public_id]
        )

        response = self.client.get(warehouse_url)
        branch_choices = response.context["form"].fields["branch"].queryset
        self.assertIn(self.branch_a, branch_choices)
        self.assertNotIn(other_branch, branch_choices)

        response = self.client.post(
            warehouse_url,
            {
                "name": self.warehouse_a.name,
                "code": self.warehouse_a.code,
                "branch": other_branch.pk,
                "address": self.warehouse_a.address,
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("branch", response.context["form"].errors)
        self.warehouse_a.refresh_from_db()
        self.assertEqual(self.warehouse_a.branch_id, self.branch_a.pk)

        Warehouse.objects.filter(pk=self.warehouse_a.pk).update(is_default=False)
        Warehouse.objects.filter(pk=other_warehouse.pk).update(is_default=True)
        response = self.client.post(
            warehouse_url,
            {
                "name": self.warehouse_a.name,
                "code": self.warehouse_a.code,
                "branch": self.branch_a.pk,
                "address": self.warehouse_a.address,
                "is_default": "on",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.warehouse_a.refresh_from_db()
        other_warehouse.refresh_from_db()
        self.assertTrue(self.warehouse_a.is_default)
        self.assertTrue(other_warehouse.is_default)

    def test_owner_and_unrestricted_branch_admin_retain_tenant_wide_scope(self):
        other_branch, other_warehouse, _central = self.make_secondary_locations()
        unrestricted_user, _membership = self.make_staff(
            ["branches.manage"],
            email="phase2a-unrestricted-branch-admin@example.com",
        )
        self.set_pos_core(True)
        expected_branches = set(
            Branch.objects.for_business(self.business_a).values_list("pk", flat=True)
        )
        expected_warehouses = set(
            Warehouse.objects.for_business(self.business_a).values_list("pk", flat=True)
        )

        for label, user in (
            ("owner", self.owner_a),
            ("unrestricted staff", unrestricted_user),
        ):
            with self.subTest(actor=label):
                self.client.force_login(user)
                response = self.client.get(reverse("branches:list"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    {branch.pk for branch in response.context["branches"]},
                    expected_branches,
                )
                self.assertEqual(
                    {warehouse.pk for warehouse in response.context["warehouses"]},
                    expected_warehouses,
                )
                self.assertEqual(
                    self.client.get(
                        reverse("branches:branch_edit", args=[other_branch.public_id])
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    self.client.get(
                        reverse(
                            "branches:warehouse_edit",
                            args=[other_warehouse.public_id],
                        )
                    ).status_code,
                    200,
                )

    # ------------------------------------------------------------------
    # Product import and export security regressions
    # ------------------------------------------------------------------
    def test_restricted_product_export_intersects_all_location_filters(self):
        other_branch, other_warehouse, central = self.make_secondary_locations()
        inventory_services.set_opening_stock(
            business=self.business_a,
            warehouse=other_warehouse,
            product=self.product_a,
            quantity=D("37"),
            unit_cost=D("4"),
            user=self.owner_a,
        )
        inventory_services.set_opening_stock(
            business=self.business_a,
            warehouse=central,
            product=self.product_a,
            quantity=D("11"),
            unit_cost=D("4"),
            user=self.owner_a,
        )
        user, _membership = self.make_staff(
            ["products.export"],
            email="phase2a-export-scope@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)
        self.client.force_login(user)
        export_url = reverse("catalog:product_export")

        unfiltered = self.product_export_row(self.client.get(export_url))
        self.assertEqual(D(unfiltered["Current Stock"]), D("111"))
        self.assertNotEqual(D(unfiltered["Current Stock"]), D("148"))

        allowed_warehouse = self.product_export_row(
            self.client.get(export_url, {"warehouse": self.warehouse_a.pk})
        )
        self.assertEqual(D(allowed_warehouse["Current Stock"]), D("100"))
        self.assertEqual(allowed_warehouse["Warehouse"], self.warehouse_a.name)
        self.assertEqual(allowed_warehouse["Branch"], self.branch_a.name)

        allowed_branch = self.product_export_row(
            self.client.get(export_url, {"branch": self.branch_a.pk})
        )
        self.assertEqual(D(allowed_branch["Current Stock"]), D("100"))
        self.assertEqual(allowed_branch["Branch"], self.branch_a.name)

        central_only = self.product_export_row(
            self.client.get(export_url, {"warehouse": central.pk})
        )
        self.assertEqual(D(central_only["Current Stock"]), D("11"))
        self.assertEqual(central_only["Warehouse"], central.name)
        self.assertEqual(central_only["Branch"], "All")

    def test_restricted_product_export_rejects_forged_filters_before_generation(self):
        other_branch, other_warehouse, _central = self.make_secondary_locations()
        user, _membership = self.make_staff(
            ["products.export"],
            email="phase2a-export-forged@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)
        self.client.force_login(user)
        export_url = reverse("catalog:product_export")
        forged_filters = (
            {"branch": other_branch.pk},
            {"warehouse": other_warehouse.pk},
            {"branch": self.branch_b.pk},
            {"warehouse": self.warehouse_b.pk},
        )

        for params in forged_filters:
            with self.subTest(params=params):
                with mock.patch(
                    "apps.catalog.services.product_export_dataset"
                ) as dataset:
                    response = self.client.get(export_url, params)
                self.assertEqual(response.status_code, 404)
                dataset.assert_not_called()

    def test_owner_product_export_can_use_any_same_business_warehouse(self):
        other_branch, other_warehouse, _central = self.make_secondary_locations()
        inventory_services.set_opening_stock(
            business=self.business_a,
            warehouse=other_warehouse,
            product=self.product_a,
            quantity=D("37"),
            unit_cost=D("4"),
            user=self.owner_a,
        )
        self.set_pos_core(True)
        self.client.force_login(self.owner_a)
        export_url = reverse("catalog:product_export")

        row = self.product_export_row(
            self.client.get(export_url, {"warehouse": other_warehouse.pk})
        )
        self.assertEqual(D(row["Current Stock"]), D("37"))
        self.assertEqual(row["Warehouse"], other_warehouse.name)
        self.assertEqual(row["Branch"], other_branch.name)
        self.assertEqual(
            self.client.get(
                export_url, {"warehouse": self.warehouse_b.pk}
            ).status_code,
            404,
        )

    def test_import_products_derives_canonical_scope_when_ids_are_omitted(self):
        other_branch, other_warehouse, central = self.make_secondary_locations()
        user, _membership = self.make_staff(
            ["products.import"],
            email="phase2a-import-derived-scope@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)
        rows = [
            {
                "Product Name": "Allowed Scoped Import",
                "SKU": "SEC-IMPORT-ALLOWED",
                "Product Type": "standard",
                "Purchase Price": "2",
                "Sale Price": "4",
                "Opening Stock": "5",
                "Branch": self.branch_a.name,
                "Warehouse": self.warehouse_a.name,
            },
            {
                "Product Name": "Central Scoped Import",
                "SKU": "SEC-IMPORT-CENTRAL",
                "Product Type": "standard",
                "Purchase Price": "2",
                "Sale Price": "4",
                "Opening Stock": "7",
                "Warehouse": central.name,
            },
            {
                "Product Name": "Forbidden Scoped Import",
                "SKU": "SEC-IMPORT-FORBIDDEN",
                "Product Type": "standard",
                "Purchase Price": "2",
                "Sale Price": "4",
                "Opening Stock": "13",
                "Branch": other_branch.name,
                "Warehouse": other_warehouse.name,
            },
        ]

        summary, errors = catalog_services.import_products(
            business=self.business_a,
            rows=rows,
            match_by="sku",
            user=user,
        )

        self.assertEqual(summary["created"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(errors), 1)
        allowed_product = Product.objects.for_business(self.business_a).get(
            sku="SEC-IMPORT-ALLOWED"
        )
        central_product = Product.objects.for_business(self.business_a).get(
            sku="SEC-IMPORT-CENTRAL"
        )
        self.assertEqual(
            allowed_product.stock_levels.get(warehouse=self.warehouse_a).quantity,
            D("5"),
        )
        self.assertEqual(
            central_product.stock_levels.get(warehouse=central).quantity,
            D("7"),
        )
        self.assertFalse(
            Product.objects.for_business(self.business_a).filter(
                sku="SEC-IMPORT-FORBIDDEN"
            ).exists()
        )

    def test_import_products_explicit_ids_cannot_widen_canonical_scope(self):
        other_branch, other_warehouse, _central = self.make_secondary_locations()
        user, membership = self.make_staff(
            ["products.import"],
            email="phase2a-import-explicit-scope@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)

        summary, errors = catalog_services.import_products(
            business=self.business_a,
            rows=[
                {
                    "Product Name": "Explicit Forbidden Import",
                    "SKU": "SEC-IMPORT-EXPLICIT-FORBIDDEN",
                    "Product Type": "standard",
                    "Purchase Price": "2",
                    "Sale Price": "4",
                    "Opening Stock": "9",
                    "Branch": other_branch.name,
                    "Warehouse": other_warehouse.name,
                }
            ],
            match_by="sku",
            user=user,
            membership=membership,
            allowed_warehouse_ids=[other_warehouse.pk],
        )

        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(errors), 1)
        self.assertFalse(
            Product.objects.for_business(self.business_a).filter(
                sku="SEC-IMPORT-EXPLICIT-FORBIDDEN"
            ).exists()
        )

    def test_owner_import_without_explicit_scope_can_use_other_branch_warehouse(self):
        other_branch, other_warehouse, _central = self.make_secondary_locations()
        self.set_pos_core(True)

        summary, errors = catalog_services.import_products(
            business=self.business_a,
            rows=[
                {
                    "Product Name": "Owner Other Branch Import",
                    "SKU": "SEC-IMPORT-OWNER",
                    "Product Type": "standard",
                    "Purchase Price": "2",
                    "Sale Price": "4",
                    "Opening Stock": "17",
                    "Branch": other_branch.name,
                    "Warehouse": other_warehouse.name,
                }
            ],
            match_by="sku",
            user=self.owner_a,
        )

        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(errors, [])
        product = Product.objects.for_business(self.business_a).get(
            sku="SEC-IMPORT-OWNER"
        )
        self.assertEqual(
            product.stock_levels.get(warehouse=other_warehouse).quantity,
            D("17"),
        )

    # ------------------------------------------------------------------
    # Tenant scope, navigation, login destinations, and phase boundaries
    # ------------------------------------------------------------------
    def test_cross_tenant_pos_core_object_urls_preserve_404(self):
        sale = self.make_sale()
        self.set_pos_core(True)
        self.client.force_login(self.owner_b)

        foreign_urls = (
            reverse("sales:detail", args=[sale.public_id]),
            reverse("catalog:product_detail", args=[self.product_a.public_id]),
            reverse("customers:detail", args=[self.walk_in_a.public_id]),
            reverse("registers:register_edit", args=[self.register_a.public_id]),
            reverse("branches:branch_edit", args=[self.branch_a.public_id]),
            reverse("branches:warehouse_edit", args=[self.warehouse_a.public_id]),
            reverse("accounts:user_edit", args=[self.membership_a().public_id]),
        )
        for url in foreign_urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_cross_tenant_invoice_receipt_and_pdf_outputs_preserve_404(self):
        sale = self.make_sale()
        self.set_pos_core(True)
        self.client.force_login(self.owner_b)

        urls = (
            reverse("sales:invoice", args=[sale.public_id]),
            reverse("sales:receipt", args=[sale.public_id]),
            reverse("sales:invoice_pdf", args=[sale.public_id]),
        )
        with mock.patch("apps.reports.pdf.render_pdf", return_value=b"%PDF-1.4\n"):
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 404)

    def test_disabled_nav_hides_pos_core_and_dashboard_shortcuts_but_keeps_inventory(self):
        sale = self.make_sale()
        self.set_plan(feature_sales=False, feature_inventory=True)
        self.client.force_login(self.owner_a)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        hidden_routes = (
            reverse("sales:pos"),
            reverse("sales:list"),
            reverse("customers:list"),
            reverse("catalog:product_list"),
            reverse("registers:shift_list"),
            reverse("branches:list"),
            reverse("accounts:user_list"),
            reverse("sales:detail", args=[sale.public_id]),
        )
        for url in hidden_routes:
            with self.subTest(url=url):
                self.assertNotContains(response, f'href="{url}"')
        self.assertContains(response, f'href="{reverse("inventory:stock_list")}"')
        self.assertNotContains(response, "Recent Sales")
        self.assertEqual(self.client.get(reverse("inventory:stock_list")).status_code, 200)

    def test_disabled_pos_routes_are_skipped_by_login_redirect(self):
        user, _membership = self.make_staff(
            ["sales.view", "inventory.view"],
            email="phase2a-disabled-redirect@example.com",
        )
        self.set_pos_core(False)

        response = self.login(user)
        self.assertRedirects(
            response,
            reverse("inventory:stock_list"),
            fetch_redirect_response=False,
        )

    def test_read_only_user_with_sales_view_lands_on_sales_history(self):
        user, _membership = self.make_staff(
            ["sales.view"], email="phase2a-read-only-redirect@example.com"
        )
        self.set_pos_core(True)
        self.set_subscription_status(Subscription.Status.PAST_DUE)

        response = self.login(user)

        self.assertRedirects(
            response,
            reverse("sales:list"),
            fetch_redirect_response=False,
        )

    def test_enabled_salesperson_with_open_shift_lands_on_pos(self):
        user, membership = self.make_staff(
            ["sales.create", "shifts.open"],
            email="phase2a-open-shift@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)
        settings_obj = self.business_a.settings
        settings_obj.allow_sale_without_shift = False
        settings_obj.save(update_fields=["allow_sale_without_shift"])
        register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=user,
            opening_cash=D("0"),
            membership=membership,
        )

        response = self.login(user)
        self.assertRedirects(response, reverse("sales:pos"), fetch_redirect_response=False)

    def test_enabled_salesperson_without_shift_lands_on_registers(self):
        user, _membership = self.make_staff(
            ["sales.create", "shifts.open"],
            email="phase2a-no-shift@example.com",
            branches=[self.branch_a],
        )
        self.set_pos_core(True)
        settings_obj = self.business_a.settings
        settings_obj.allow_sale_without_shift = False
        settings_obj.save(update_fields=["allow_sale_without_shift"])

        response = self.login(user)
        self.assertRedirects(
            response,
            reverse("registers:shift_list"),
            fetch_redirect_response=False,
        )

    def test_inventory_customer_credit_barcode_tailoring_and_api_boundaries_are_unchanged(self):
        sale = self.make_sale()
        sale_item = sale.items.get()
        sale_item.tailoring_details = {"customer_notes": "Boundary job"}
        sale_item.save(update_fields=["tailoring_details", "updated_at"])
        self.set_plan(
            feature_sales=False,
            feature_inventory=True,
            feature_api_access=True,
            feature_custom_roles=True,
        )
        self.client.force_login(self.owner_a)

        # Inventory stays outside this phase.
        self.assertEqual(self.client.get(reverse("inventory:stock_list")).status_code, 200)

        # Custom-role enforcement remains on its existing feature and
        # permission guards rather than being absorbed into POS Core.
        self.assertEqual(self.client.get(reverse("accounts:role_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:role_create")).status_code, 200)

        # Customer-credit collection and statements stay on their legacy guards.
        statement_url = reverse("customers:statement", args=[self.walk_in_a.public_id])
        payment_url = reverse("customers:payment", args=[self.walk_in_a.public_id])
        self.assertEqual(self.client.get(statement_url).status_code, 200)
        payment_response = self.client.get(payment_url)
        self.assertEqual(payment_response.status_code, 302)
        self.assertEqual(
            payment_response.url,
            reverse("customers:detail", args=[self.walk_in_a.public_id]),
        )

        # Normal barcode lookup and premium label printing are deliberately
        # not absorbed into POS Core during Phase 2A.
        barcode = self.client.get(
            reverse("catalog:product_barcode", args=[self.product_a.public_id])
        )
        labels = self.client.get(reverse("catalog:product_labels", args=[self.product_a.public_id]))
        self.assertEqual(barcode.status_code, 200)
        self.assertEqual(barcode["Content-Type"], "image/svg+xml")
        self.assertEqual(labels.status_code, 200)

        # Workshop job-card outputs remain governed by tailoring permissions.
        with mock.patch("apps.reports.pdf.render_pdf", return_value=b"%PDF-1.4\n"):
            bulk = self.client.get(reverse("sales:workshop_job_card_pdf", args=[sale.public_id]))
            single = self.client.get(
                reverse(
                    "sales:sale_item_workshop_job_card_pdf",
                    args=[sale.public_id, sale_item.id],
                )
            )
        self.assertEqual(bulk.status_code, 200)
        self.assertEqual(single.status_code, 200)

        # Existing read-only API behavior continues to depend on API Access,
        # not on the browser POS Core rollout in this phase.
        api_response = self.client.get(reverse("api:product-list"))
        self.assertEqual(api_response.status_code, 200)
        self.assertContains(api_response, str(self.product_a.public_id))
