"""Focused coverage for Nexa WMS Phase 1 shared platform foundation."""

from datetime import time

from django import forms
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.audit.models import AuditLog
from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product
from apps.customers.models import Customer
from apps.expenses.models import ExpenseCategory
from apps.platformadmin.views import PLAN_MODULE_FIELDS, PlanForm
from apps.registers.models import CashRegister
from apps.sales.models import PaymentMethod
from apps.subscriptions.access import calculate_effective_modules
from apps.subscriptions.feature_registry import FEATURE_REGISTRY
from apps.subscriptions.models import Plan
from apps.tenants.services import provision_business
from apps.wms_core import selectors, services
from apps.wms_core.access import evaluate_wms_access
from apps.wms_core.models import WmsLocation, WmsRole, WmsSettings, WmsUserAccess
from apps.wms_core.permissions import (
    WMS_PERMISSION_CODES,
    WMS_SYSTEM_ROLE_TEMPLATES,
)

PASSWORD = "StrongPass123!"


def make_owner(email):
    return User.objects.create_user(
        email=email,
        password=PASSWORD,
        full_name=email.split("@")[0].replace("-", " ").title(),
    )


def make_plan(name, *, wms=False, pos=False):
    return Plan.objects.create(
        name=name,
        allow_trial=False,
        feature_wms=wms,
        feature_sales=pos,
        feature_inventory=False,
        feature_suppliers=False,
        feature_purchases=False,
        feature_expenses=False,
        feature_transfers=False,
        feature_tailoring_module=False,
        feature_customer_credit=False,
        feature_advanced_reports=False,
        feature_audit_logs=False,
        feature_barcode_printing=False,
        feature_custom_roles=False,
        feature_api_access=False,
    )


def plan_form_payload(plan):
    data = {}
    form = PlanForm(instance=plan)
    for name, field in form.fields.items():
        value = getattr(plan, name)
        if isinstance(field, forms.BooleanField):
            if value:
                data[name] = "on"
        else:
            data[name] = str(value)
    return data


class WmsEntitlementTests(TestCase):
    def plan_payload(self, **overrides):
        data = {
            "name": "WMS Form Plan",
            "description": "",
            "support_level": Plan.SupportLevel.STANDARD,
            "sort_order": "0",
            "is_active": "on",
            "monthly_price": "0",
            "annual_price": "0",
            "setup_fee": "0",
            "currency_code": "USD",
            "trial_days": "0",
            "max_branches": "0",
            "max_users": "0",
            "max_warehouses": "0",
            "max_products": "0",
            "max_customers": "0",
            "max_monthly_invoices": "0",
            "storage_limit_mb": "0",
            "max_employees": "0",
            "max_suppliers": "0",
            "max_active_orders": "0",
            "max_api_calls": "0",
            "max_branch_managers": "0",
            "max_cashiers": "0",
            "max_logged_in_devices": "0",
            "max_pos_terminals": "0",
        }
        data.update(overrides)
        return data

    def test_wms_registry_is_independent_and_new_field_defaults_false(self):
        definition = FEATURE_REGISTRY["wms"]

        self.assertEqual(definition.label, "Workshop Management System")
        self.assertEqual(definition.plan_field, "feature_wms")
        self.assertEqual(definition.dependencies, ())
        self.assertFalse(Plan._meta.get_field("feature_wms").default)
        self.assertFalse(Plan.objects.create(name="Legacy Default Plan").feature_wms)

    def test_pos_only_wms_only_and_combined_module_resolution_are_valid(self):
        pos_only = make_plan("POS Only", pos=True)
        wms_only = make_plan("WMS Only", wms=True)
        combined = make_plan("Combined", pos=True, wms=True)

        self.assertIn("pos_core", calculate_effective_modules(pos_only).effective_modules)
        self.assertNotIn("wms", calculate_effective_modules(pos_only).effective_modules)
        self.assertEqual(
            calculate_effective_modules(wms_only).effective_modules,
            frozenset({"wms"}),
        )
        combined_modules = calculate_effective_modules(combined).effective_modules
        self.assertIn("pos_core", combined_modules)
        self.assertIn("wms", combined_modules)

    def test_platform_plan_form_accepts_wms_without_pos(self):
        form = PlanForm(self.plan_payload(feature_wms="on"))

        self.assertTrue(form.is_valid(), form.errors)
        plan = form.save()
        self.assertTrue(plan.feature_wms)
        self.assertFalse(plan.feature_sales)
        self.assertIn("feature_wms", PLAN_MODULE_FIELDS)

    def test_existing_pos_dependency_validation_remains_unchanged(self):
        form = PlanForm(
            self.plan_payload(
                feature_purchases="on",
                feature_inventory="on",
                feature_suppliers="on",
            )
        )

        self.assertFalse(form.is_valid())
        self.assertIn("feature_purchases", form.errors)


class WmsProvisioningTests(TestCase):
    def test_wms_only_tenant_has_no_pos_operational_defaults(self):
        owner = make_owner("wms-only@example.com")
        plan = make_plan("WMS Only Provisioning", wms=True)

        business = provision_business(
            owner=owner,
            name="WMS Only Business",
            plan=plan,
        )

        self.assertEqual(business.memberships.count(), 1)
        self.assertEqual(business.roles.count(), 1)
        self.assertEqual(Branch.objects.for_business(business).count(), 0)
        self.assertEqual(Warehouse.objects.for_business(business).count(), 0)
        self.assertEqual(Product.objects.for_business(business).count(), 0)
        self.assertEqual(Customer.objects.for_business(business).count(), 0)
        self.assertEqual(CashRegister.objects.for_business(business).count(), 0)
        self.assertEqual(PaymentMethod.objects.for_business(business).count(), 0)
        self.assertEqual(ExpenseCategory.objects.for_business(business).count(), 0)
        self.assertEqual(
            WmsRole.objects.for_business(business).count(),
            len(WMS_SYSTEM_ROLE_TEMPLATES),
        )
        self.assertEqual(WmsSettings.objects.for_business(business).count(), 1)
        owner_access = WmsUserAccess.objects.for_business(business).get()
        self.assertEqual(owner_access.membership.user, owner)
        self.assertEqual(owner_access.role.code, "owner_admin")
        self.assertTrue(
            AuditLog.objects.filter(
                business=business,
                action="wms.enabled",
            ).exists()
        )

    def test_pos_only_tenant_keeps_existing_provisioning_and_no_wms_records(self):
        owner = make_owner("pos-only@example.com")
        plan = make_plan("POS Only Provisioning", pos=True)

        business = provision_business(
            owner=owner,
            name="POS Only Business",
            plan=plan,
        )

        self.assertTrue(Branch.objects.for_business(business).filter(code="HO").exists())
        self.assertTrue(
            Warehouse.objects.for_business(business).filter(code="MAIN").exists()
        )
        self.assertTrue(Customer.objects.for_business(business).filter(is_walk_in=True).exists())
        self.assertTrue(PaymentMethod.objects.for_business(business).exists())
        self.assertTrue(CashRegister.objects.for_business(business).exists())
        self.assertFalse(WmsSettings.objects.for_business(business).exists())
        self.assertFalse(WmsUserAccess.objects.for_business(business).exists())

    def test_combined_tenant_reuses_shared_records_and_adds_both_foundations(self):
        owner = make_owner("combined@example.com")
        plan = make_plan("Combined Provisioning", pos=True, wms=True)

        business = provision_business(
            owner=owner,
            name="Combined Business",
            plan=plan,
        )

        self.assertEqual(User.objects.filter(email=owner.email).count(), 1)
        self.assertEqual(business.memberships.count(), 1)
        self.assertEqual(Branch.objects.for_business(business).count(), 1)
        self.assertEqual(Warehouse.objects.for_business(business).count(), 1)
        self.assertEqual(WmsSettings.objects.for_business(business).count(), 1)
        self.assertEqual(WmsUserAccess.objects.for_business(business).count(), 1)

    def test_enabling_wms_is_idempotent_and_does_not_grant_pos_staff(self):
        owner = make_owner("enable-owner@example.com")
        plan = make_plan("Enable Later", pos=True)
        business = provision_business(owner=owner, name="Enable Later", plan=plan)
        cashier_role = business.roles.get(name="Cashier")
        staff = make_owner("enable-staff@example.com")
        staff_membership = Membership.objects.create(
            business=business,
            user=staff,
            role=cashier_role,
        )
        owner_membership = business.memberships.get(user=owner)

        first = services.provision_wms_foundation(
            business,
            owner_membership=owner_membership,
        )
        second = services.provision_wms_foundation(
            business,
            owner_membership=owner_membership,
        )

        self.assertEqual(first["settings"].pk, second["settings"].pk)
        self.assertEqual(
            WmsRole.objects.for_business(business).count(),
            len(WMS_SYSTEM_ROLE_TEMPLATES),
        )
        self.assertEqual(WmsUserAccess.objects.for_business(business).count(), 1)
        self.assertFalse(
            WmsUserAccess.objects.filter(membership=staff_membership).exists()
        )

    def test_disabling_entitlement_retains_all_wms_data(self):
        owner = make_owner("retain-owner@example.com")
        plan = make_plan("Retain WMS", wms=True)
        business = provision_business(owner=owner, name="Retain WMS", plan=plan)
        counts = (
            WmsRole.objects.for_business(business).count(),
            WmsSettings.objects.for_business(business).count(),
            WmsUserAccess.objects.for_business(business).count(),
        )

        services.sync_wms_entitlement(
            business,
            was_enabled=True,
            is_enabled=False,
            user=owner,
        )

        self.assertEqual(
            (
                WmsRole.objects.for_business(business).count(),
                WmsSettings.objects.for_business(business).count(),
                WmsUserAccess.objects.for_business(business).count(),
            ),
            counts,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                business=business,
                action="wms.disabled",
            ).exists()
        )

    def test_explicit_existing_branch_can_be_idempotently_wrapped(self):
        owner = make_owner("location-owner@example.com")
        plan = make_plan("Location WMS", wms=True)
        business = provision_business(owner=owner, name="Location WMS", plan=plan)
        branch = Branch.objects.create(
            business=business,
            name="Workshop One",
            code="WS1",
        )

        services.provision_wms_foundation(
            business,
            location_specs=[(branch, WmsLocation.LocationType.WORKSHOP)],
        )
        services.provision_wms_foundation(
            business,
            location_specs=[(branch, WmsLocation.LocationType.WORKSHOP)],
        )

        self.assertEqual(WmsLocation.objects.for_business(business).count(), 1)

    def test_platform_admin_can_reuse_an_existing_shared_owner_account(self):
        platform_admin = User.objects.create_superuser(
            email="wms-platform@example.com",
            password=PASSWORD,
            full_name="WMS Platform Admin",
        )
        shared_owner = make_owner("shared-wms-owner@example.com")
        plan = make_plan("Shared Owner WMS", wms=True)
        self.client.force_login(platform_admin)

        response = self.client.post(
            reverse("platformadmin:business_create"),
            {
                "business_name": "Shared Owner Business",
                "country": "Oman",
                "currency": "OMR",
                "business_category": "Tailoring",
                "owner_name": "Ignored Replacement Name",
                "owner_email": shared_owner.email,
                "phone": "",
                "password": "",
                "plan": plan.pk,
                "subscription_mode": "active",
                "days": "30",
                "amount": "",
                "reference": "",
            },
        )

        business = shared_owner.owned_businesses.get(name="Shared Owner Business")
        self.assertRedirects(
            response,
            reverse(
                "platformadmin:business_detail",
                args=[business.public_id],
            ),
        )
        self.assertEqual(User.objects.filter(email=shared_owner.email).count(), 1)
        self.assertEqual(business.memberships.get().user, shared_owner)
        self.assertTrue(
            WmsUserAccess.objects.for_business(business).filter(
                membership__user=shared_owner
            ).exists()
        )

    def test_platform_plan_toggle_provisions_then_retains_wms_foundation(self):
        platform_admin = User.objects.create_superuser(
            email="toggle-platform@example.com",
            password=PASSWORD,
            full_name="Toggle Platform Admin",
        )
        owner = make_owner("toggle-owner@example.com")
        plan = make_plan("Toggle WMS Plan", pos=True)
        business = provision_business(owner=owner, name="Toggle WMS", plan=plan)
        self.client.force_login(platform_admin)
        enable_payload = plan_form_payload(plan)
        enable_payload["feature_wms"] = "on"

        response = self.client.post(
            reverse("platformadmin:plan_edit", args=[plan.pk]),
            enable_payload,
        )

        self.assertRedirects(response, reverse("platformadmin:plan_list"))
        plan.refresh_from_db()
        self.assertTrue(plan.feature_wms)
        self.assertTrue(WmsSettings.objects.for_business(business).exists())
        self.assertTrue(WmsUserAccess.objects.for_business(business).exists())
        counts = (
            WmsRole.objects.for_business(business).count(),
            WmsSettings.objects.for_business(business).count(),
            WmsUserAccess.objects.for_business(business).count(),
        )

        disable_payload = plan_form_payload(plan)
        disable_payload.pop("feature_wms", None)
        response = self.client.post(
            reverse("platformadmin:plan_edit", args=[plan.pk]),
            disable_payload,
        )

        self.assertRedirects(response, reverse("platformadmin:plan_list"))
        plan.refresh_from_db()
        self.assertFalse(plan.feature_wms)
        self.assertEqual(
            (
                WmsRole.objects.for_business(business).count(),
                WmsSettings.objects.for_business(business).count(),
                WmsUserAccess.objects.for_business(business).count(),
            ),
            counts,
        )


class WmsIsolationTests(TestCase):
    def setUp(self):
        self.plan = make_plan("Isolation WMS", wms=True)
        self.owner_a = make_owner("isolation-a@example.com")
        self.owner_b = make_owner("isolation-b@example.com")
        self.business_a = provision_business(
            owner=self.owner_a,
            name="Isolation A",
            plan=self.plan,
        )
        self.business_b = provision_business(
            owner=self.owner_b,
            name="Isolation B",
            plan=self.plan,
        )
        self.branch_a = Branch.objects.create(
            business=self.business_a,
            name="A Workshop",
            code="A-W",
        )
        self.branch_b = Branch.objects.create(
            business=self.business_b,
            name="B Workshop",
            code="B-W",
        )
        self.location_a = services.save_location(
            business=self.business_a,
            branch=self.branch_a,
            location_type=WmsLocation.LocationType.WORKSHOP,
            user=self.owner_a,
        )
        self.location_b = services.save_location(
            business=self.business_b,
            branch=self.branch_b,
            location_type=WmsLocation.LocationType.WORKSHOP,
            user=self.owner_b,
        )

    def make_membership(self, business, email):
        role = Role.objects.create(
            business=business,
            name=f"Shared {email}",
            permissions=[],
        )
        user = make_owner(email)
        return Membership.objects.create(
            business=business,
            user=user,
            role=role,
        )

    def test_cross_tenant_location_branch_is_rejected(self):
        with self.assertRaises(ValidationError):
            WmsLocation.objects.create(
                business=self.business_a,
                branch=self.branch_b,
                location_type=WmsLocation.LocationType.SOURCE,
            )

    def test_cross_tenant_default_workshop_is_rejected(self):
        settings_obj = WmsSettings.objects.for_business(self.business_a).get()
        settings_obj.default_workshop_location = self.location_b

        with self.assertRaises(ValidationError):
            settings_obj.save()

    def test_cross_tenant_membership_and_role_are_rejected(self):
        member_a = self.make_membership(
            self.business_a,
            "isolation-member-a@example.com",
        )
        member_b = self.make_membership(
            self.business_b,
            "isolation-member-b@example.com",
        )
        role_a = WmsRole.objects.for_business(self.business_a).get(
            code="production_entry"
        )
        role_b = WmsRole.objects.for_business(self.business_b).get(
            code="production_entry"
        )

        with self.assertRaises(ValidationError):
            WmsUserAccess.objects.create(
                business=self.business_a,
                membership=member_b,
                role=role_a,
            )
        with self.assertRaises(ValidationError):
            WmsUserAccess.objects.create(
                business=self.business_a,
                membership=member_a,
                role=role_b,
            )

    def test_cross_tenant_allowed_location_is_rejected(self):
        access = WmsUserAccess.objects.for_business(self.business_a).get(
            membership__user=self.owner_a
        )

        with self.assertRaises(ValidationError):
            access.allowed_locations.add(self.location_b)

    def test_cross_tenant_selector_returns_not_found(self):
        access = WmsUserAccess.objects.for_business(self.business_a).get(
            membership__user=self.owner_a
        )

        with self.assertRaises(Http404):
            selectors.get_location_for_access(access, self.location_b.public_id)


class WmsAccessTests(TestCase):
    def setUp(self):
        self.plan = make_plan("Access WMS", wms=True)
        self.owner = make_owner("wms-access-owner@example.com")
        self.business = provision_business(
            owner=self.owner,
            name="WMS Access Business",
            plan=self.plan,
        )
        self.membership = self.business.memberships.get(user=self.owner)
        self.access = WmsUserAccess.objects.for_business(self.business).get(
            membership=self.membership
        )
        self.branch_one = Branch.objects.create(
            business=self.business,
            name="Workshop One",
            code="W1",
        )
        self.branch_two = Branch.objects.create(
            business=self.business,
            name="Workshop Two",
            code="W2",
        )
        self.location_one = services.save_location(
            business=self.business,
            branch=self.branch_one,
            location_type=WmsLocation.LocationType.WORKSHOP,
            user=self.owner,
        )
        self.location_two = services.save_location(
            business=self.business,
            branch=self.branch_two,
            location_type=WmsLocation.LocationType.BOTH,
            user=self.owner,
        )

    def make_staff(self, email, role_code="production_entry"):
        core_role = Role.objects.create(
            business=self.business,
            name=f"Shared role {email}",
            permissions=[],
        )
        user = make_owner(email)
        membership = Membership.objects.create(
            business=self.business,
            user=user,
            role=core_role,
        )
        role = WmsRole.objects.for_business(self.business).get(code=role_code)
        access = services.save_user_access(
            business=self.business,
            membership=membership,
            role=role,
            user=self.owner,
        )
        return user, membership, access

    def login(self, user, *, next_url=None):
        url = reverse("accounts:login")
        if next_url:
            url = f"{url}?next={next_url}"
        return self.client.post(
            url,
            {"email": user.email, "password": PASSWORD},
        )

    def test_valid_owner_access_and_wms_only_login_landing(self):
        response = self.login(self.owner)

        self.assertRedirects(
            response,
            reverse("wms:dashboard"),
            fetch_redirect_response=False,
        )
        self.assertEqual(self.client.get(reverse("wms:dashboard")).status_code, 200)

    def test_authorized_wms_next_route_is_honored(self):
        response = self.login(
            self.owner,
            next_url=reverse("wms:settings"),
        )

        self.assertRedirects(
            response,
            reverse("wms:settings"),
            fetch_redirect_response=False,
        )

    def test_disabled_entitlement_denies_direct_access_and_hides_navigation(self):
        self.plan.feature_wms = False
        self.plan.save(update_fields=["feature_wms"])
        self.client.force_login(self.owner)

        self.assertEqual(self.client.get(reverse("wms:dashboard")).status_code, 403)
        profile = self.client.get(reverse("accounts:profile"))
        self.assertNotContains(profile, "Nexa WMS")

    def test_missing_and_inactive_explicit_access_are_denied(self):
        core_role = Role.objects.create(
            business=self.business,
            name="No WMS Access",
            permissions=[],
        )
        user = make_owner("no-wms-access@example.com")
        Membership.objects.create(
            business=self.business,
            user=user,
            role=core_role,
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("wms:dashboard")).status_code, 403)

        self.access.is_active = False
        self.access.save(update_fields=["is_active", "updated_at"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("wms:dashboard")).status_code, 403)

    def test_inactive_role_and_missing_permission_are_denied(self):
        self.access.role.is_active = False
        self.access.role.save(update_fields=["is_active", "updated_at"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("wms:dashboard")).status_code, 403)

        user, _membership, _access = self.make_staff(
            "production-user@example.com",
            role_code="production_entry",
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("wms:dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("wms:settings")).status_code, 403)

    def test_empty_location_scope_means_all_active_then_assignment_restricts(self):
        self.assertEqual(
            set(selectors.locations_for_access(self.access)),
            {self.location_one, self.location_two},
        )
        self.access.allowed_locations.set([self.location_one])
        request = RequestFactory().get("/wms/")
        request.user = self.owner
        request.business = self.business
        request.membership = self.membership

        self.assertTrue(
            evaluate_wms_access(request, location=self.location_one).allowed
        )
        self.assertFalse(
            evaluate_wms_access(request, location=self.location_two).allowed
        )
        self.assertEqual(
            list(selectors.locations_for_access(self.access)),
            [self.location_one],
        )

    def test_cross_tenant_public_id_is_not_disclosed(self):
        other_owner = make_owner("wms-other-owner@example.com")
        other_business = provision_business(
            owner=other_owner,
            name="Other WMS",
            plan=self.plan,
        )
        other_branch = Branch.objects.create(
            business=other_business,
            name="Other Workshop",
            code="OW",
        )
        other_location = services.save_location(
            business=other_business,
            branch=other_branch,
            location_type=WmsLocation.LocationType.WORKSHOP,
            user=other_owner,
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("wms:location_edit", args=[other_location.public_id])
        )

        self.assertEqual(response.status_code, 404)

    def test_combined_owner_can_access_pos_and_wms_without_duplicate_identity(self):
        combined_plan = make_plan("Access Combined", wms=True, pos=True)
        owner = make_owner("access-combined@example.com")
        business = provision_business(
            owner=owner,
            name="Access Combined",
            plan=combined_plan,
        )

        response = self.login(owner)

        self.assertRedirects(
            response,
            reverse("dashboard"),
            fetch_redirect_response=False,
        )
        self.assertEqual(self.client.get(reverse("wms:dashboard")).status_code, 200)
        self.assertEqual(business.memberships.filter(user=owner).count(), 1)
        self.assertEqual(User.objects.filter(email=owner.email).count(), 1)

    def test_settings_defaults_and_approved_role_permissions_are_exact(self):
        settings_obj = WmsSettings.objects.for_business(self.business).get()
        self.assertEqual(settings_obj.first_shift_start, time(10, 0))
        self.assertEqual(settings_obj.first_shift_end, time(13, 0))
        self.assertEqual(settings_obj.second_shift_start, time(16, 30))
        self.assertEqual(settings_obj.second_shift_end, time(22, 0))
        self.assertEqual(settings_obj.grace_period_minutes, 15)
        for role in WmsRole.objects.for_business(self.business):
            self.assertTrue(set(role.permissions).issubset(WMS_PERMISSION_CODES))

    def test_foundation_mutation_services_emit_required_audit_events(self):
        custom_role = services.save_role(
            business=self.business,
            name="Custom Foundation Role",
            code="custom_foundation",
            permissions=["wms.dashboard.view"],
            user=self.owner,
        )
        settings_obj = WmsSettings.objects.for_business(self.business).get()
        services.save_settings(
            settings_obj,
            {
                "grace_period_minutes": 20,
            },
            user=self.owner,
        )
        services.save_location(
            business=self.business,
            branch=self.branch_two,
            location_type=WmsLocation.LocationType.BOTH,
            is_active=False,
            instance=self.location_two,
            user=self.owner,
        )
        user, membership, access = self.make_staff("audit-access@example.com")
        services.save_user_access(
            business=self.business,
            membership=membership,
            role=custom_role,
            is_active=False,
            instance=access,
            user=user,
        )

        actions = set(
            AuditLog.objects.filter(business=self.business).values_list(
                "action",
                flat=True,
            )
        )
        self.assertTrue(
            {
                "wms.role_changed",
                "wms.settings_changed",
                "wms.location_deactivated",
                "wms.user_access_deactivated",
            }.issubset(actions)
        )
