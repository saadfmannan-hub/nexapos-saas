"""Tests for platform admin enhancements: reactivation, status system,
SaaS metrics, login-as-owner, expiry-mode config, audit."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.audit.models import AuditLog
from apps.platformadmin.models import PlatformConfig
from apps.platformadmin.views import (
    PLAN_FORM_FIELDS,
    PLAN_MODULE_FIELDS,
    PLAN_MODULE_LABELS,
    PlanForm,
)
from apps.subscriptions.models import Plan, Subscription, SubscriptionPayment
from apps.tenants.models import Business

from .base import TenantTestCase

D = Decimal


class PlatformBaseTest(TenantTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="platform@nexapos.com", password="StrongPass123!",
            full_name="Platform Admin", is_superuser=True, is_staff=True,
            is_platform_admin=True)
        self.client.force_login(self.admin)
        self.sub_a = self.business_a.subscription


class ReactivationTests(PlatformBaseTest):
    def test_suspend_records_who_and_why(self):
        self.client.post(
            reverse("platformadmin:business_action",
                    args=[self.business_a.public_id, "suspend"]),
            {"reason": "non-payment"})
        self.business_a.refresh_from_db()
        self.assertFalse(self.business_a.is_active)
        self.assertEqual(self.business_a.suspended_by, self.admin)
        self.assertIsNotNone(self.business_a.suspended_at)
        self.assertEqual(self.business_a.suspension_reason, "non-payment")
        self.assertTrue(AuditLog.objects.filter(
            action="platform.business_suspended",
            business=self.business_a).exists())

    def test_reactivate_records_who_and_when_and_audits(self):
        self.business_a.is_active = False
        self.business_a.suspended_at = timezone.now()
        self.business_a.suspended_by = self.admin
        self.business_a.save()
        self.client.post(
            reverse("platformadmin:business_action",
                    args=[self.business_a.public_id, "activate"]))
        self.business_a.refresh_from_db()
        self.assertTrue(self.business_a.is_active)
        self.assertEqual(self.business_a.reactivated_by, self.admin)
        self.assertIsNotNone(self.business_a.reactivated_at)
        self.assertTrue(AuditLog.objects.filter(
            action="platform.business_reactivated",
            business=self.business_a).exists())

    def test_suspension_blocks_business_login(self):
        self.business_a.is_active = False
        self.business_a.save()
        self.client.logout()
        # Owner can authenticate but has no active workspace
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("no-business", response.url)

    def test_reactivation_restores_access(self):
        self.business_a.is_active = False
        self.business_a.save()
        self.business_a.is_active = True
        self.business_a.save()
        self.client.logout()
        self.client.force_login(self.owner_a)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)


class SubscriptionStatusTests(PlatformBaseTest):
    def test_expiring_soon_within_7_days(self):
        self.sub_a.status = Subscription.Status.ACTIVE
        self.sub_a.current_period_end = timezone.now() + timedelta(days=3)
        self.sub_a.save()
        self.assertTrue(self.sub_a.is_expiring_soon)
        self.assertEqual(self.sub_a.display_status, "expiring_soon")

    def test_active_not_expiring_when_far_out(self):
        self.sub_a.status = Subscription.Status.ACTIVE
        self.sub_a.current_period_end = timezone.now() + timedelta(days=60)
        self.sub_a.save()
        self.assertFalse(self.sub_a.is_expiring_soon)
        self.assertEqual(self.sub_a.display_status, "active")

    def test_suspended_business_status(self):
        self.business_a.is_active = False
        self.business_a.save()
        self.sub_a.refresh_from_db()
        self.assertEqual(self.sub_a.display_status, "suspended")
        self.assertFalse(self.sub_a.is_operational)

    def test_expired_status(self):
        self.sub_a.status = Subscription.Status.ACTIVE
        self.sub_a.current_period_end = timezone.now() - timedelta(days=60)
        self.sub_a.grace_days = 7
        self.sub_a.save()
        self.assertEqual(self.sub_a.display_status, "expired")

    def test_badge_renders_in_business_list(self):
        self.sub_a.status = Subscription.Status.ACTIVE
        self.sub_a.current_period_end = timezone.now() + timedelta(days=2)
        self.sub_a.save()
        r = self.client.get(reverse("platformadmin:business_list"))
        self.assertContains(r, "sub-expiring")


class SaaSMetricsTests(PlatformBaseTest):
    def test_dashboard_shows_metrics(self):
        r = self.client.get(reverse("platformadmin:dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("mrr", r.context)
        self.assertIn("revenue_total", r.context)
        self.assertIn("biz", r.context)
        self.assertIn("user_metrics", r.context)
        self.assertIn("chart_plans", r.context)
        self.assertEqual(r.context["biz"]["total"], 2)  # business A + B

    def test_mrr_counts_active_paid_only(self):
        # Make business A a paid active sub on a priced plan
        plan = self.sub_a.plan
        plan.monthly_price = D("30.000")
        plan.save()
        self.sub_a.status = Subscription.Status.ACTIVE
        self.sub_a.current_period_end = timezone.now() + timedelta(days=20)
        self.sub_a.save()
        r = self.client.get(reverse("platformadmin:dashboard"))
        self.assertGreaterEqual(r.context["mrr"], D("30.000"))


class LoginAsOwnerTests(PlatformBaseTest):
    def _start(self, reason="support check"):
        return self.client.post(
            reverse("platformadmin:login_as", args=[self.business_a.public_id]),
            {"reason": reason})

    def test_login_as_owner_enters_support_mode(self):
        r = self._start()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, reverse("dashboard"))
        # Now the admin's requests act as the owner with a banner
        dash = self.client.get(reverse("dashboard"))
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.context["request"].user, self.owner_a)
        self.assertEqual(dash.context["request"].support_admin, self.admin)
        self.assertContains(dash, "Support session active")

    def test_login_as_requires_reason(self):
        r = self.client.post(
            reverse("platformadmin:login_as", args=[self.business_a.public_id]),
            {"reason": ""})
        self.assertEqual(r.status_code, 302)
        self.client.get(reverse("platformadmin:business_detail",
                                args=[self.business_a.public_id]))
        # no support session established
        dash = self.client.get(reverse("platformadmin:dashboard"))
        self.assertEqual(dash.context["request"].user, self.admin)

    def test_login_as_is_audited(self):
        self._start("debugging invoice")
        self.assertTrue(AuditLog.objects.filter(
            action="platform.login_as_owner",
            business=self.business_a).exists())

    def test_exit_returns_to_platform_and_audits(self):
        self._start()
        r = self.client.post(reverse("platformadmin:support_exit"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, reverse("platformadmin:dashboard"))
        # Back to admin identity
        self.assertEqual(
            self.client.get(reverse("platformadmin:dashboard")).status_code, 200)
        self.assertTrue(AuditLog.objects.filter(
            action="platform.support_session_ended").exists())

    def test_non_platform_user_cannot_login_as(self):
        self.client.logout()
        self.client.force_login(self.owner_a)
        r = self.client.post(
            reverse("platformadmin:login_as", args=[self.business_a.public_id]),
            {"reason": "x"})
        self.assertEqual(r.status_code, 403)


class CreateBusinessTests(PlatformBaseTest):
    def setUp(self):
        super().setUp()
        self.plan = Plan.objects.filter(is_active=True).first()

    def _payload(self, **overrides):
        data = {
            "business_name": "Gamma Stores",
            "country": "Oman",
            "currency": "OMR",
            "business_category": "Grocery",
            "owner_name": "Gamma Owner",
            "owner_email": "gamma-owner@example.com",
            "phone": "+96890000000",
            "password": "",
            "plan": self.plan.pk,
            "subscription_mode": "trial",
            "days": "",
            "amount": "",
            "reference": "",
        }
        data.update(overrides)
        return data

    def test_create_business_creates_owner_membership_and_lists(self):
        r = self.client.post(reverse("platformadmin:business_create"),
                             self._payload())
        self.assertEqual(r.status_code, 302)
        business = Business.objects.get(name="Gamma Stores")
        self.assertEqual(business.owner.email, "gamma-owner@example.com")
        # Owner membership created with the owner role
        membership = Membership.objects.get(business=business,
                                            user=business.owner)
        self.assertTrue(membership.role.is_owner)
        # Default provisioning ran (branch + trial subscription)
        self.assertTrue(hasattr(business, "subscription"))
        # Appears in the businesses list
        listing = self.client.get(reverse("platformadmin:business_list"))
        self.assertContains(listing, "Gamma Stores")

    def test_password_provided_lets_owner_log_in(self):
        self.client.post(reverse("platformadmin:business_create"),
                         self._payload(password="OwnerPass123!"))
        owner = User.objects.get(email="gamma-owner@example.com")
        self.assertTrue(owner.check_password("OwnerPass123!"))

    def test_blank_password_is_auto_generated_and_shown_once(self):
        r = self.client.post(reverse("platformadmin:business_create"),
                             self._payload(), follow=True)
        owner = User.objects.get(email="gamma-owner@example.com")
        self.assertTrue(owner.has_usable_password())
        self.assertContains(r, "password:")

    def test_active_mode_sets_period_and_records_payment(self):
        self.client.post(reverse("platformadmin:business_create"),
                         self._payload(subscription_mode="active", days="30",
                                       amount="25.000", reference="bank-001"))
        sub = Business.objects.get(name="Gamma Stores").subscription
        self.assertEqual(sub.status, Subscription.Status.ACTIVE)
        self.assertIsNotNone(sub.current_period_end)
        self.assertTrue(sub.current_period_end > timezone.now())
        self.assertTrue(SubscriptionPayment.objects.filter(
            subscription=sub, reference="bank-001").exists())

    def test_trial_mode_with_explicit_days(self):
        self.client.post(reverse("platformadmin:business_create"),
                         self._payload(subscription_mode="trial", days="21"))
        sub = Business.objects.get(name="Gamma Stores").subscription
        self.assertEqual(sub.status, Subscription.Status.TRIAL)
        self.assertIsNotNone(sub.trial_ends_at)
        delta = (sub.trial_ends_at - timezone.now()).days
        self.assertGreaterEqual(delta, 19)
        self.assertLessEqual(delta, 21)

    def test_trial_mode_rejected_when_plan_disallows_trial(self):
        self.plan.allow_trial = False
        self.plan.save()
        before = Business.objects.count()

        r = self.client.post(reverse("platformadmin:business_create"),
                             self._payload(subscription_mode="trial"))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "does not allow trial subscriptions")
        self.assertEqual(Business.objects.count(), before)

    def test_active_mode_still_works_when_plan_disallows_trial(self):
        self.plan.allow_trial = False
        self.plan.save()

        r = self.client.post(reverse("platformadmin:business_create"),
                             self._payload(subscription_mode="active", days="30"))

        self.assertEqual(r.status_code, 302)
        sub = Business.objects.get(name="Gamma Stores").subscription
        self.assertEqual(sub.status, Subscription.Status.ACTIVE)

    def test_create_is_audited(self):
        self.client.post(reverse("platformadmin:business_create"),
                         self._payload())
        business = Business.objects.get(name="Gamma Stores")
        self.assertTrue(AuditLog.objects.filter(
            action="platform.business_created", business=business).exists())

    def test_duplicate_email_rejected(self):
        before = Business.objects.count()
        r = self.client.post(reverse("platformadmin:business_create"),
                             self._payload(owner_email="owner-a@example.com"))
        self.assertEqual(r.status_code, 200)  # re-renders form with errors
        self.assertContains(r, "already exists")
        self.assertEqual(Business.objects.count(), before)

    def test_non_platform_user_cannot_create(self):
        self.client.logout()
        self.client.force_login(self.owner_a)
        r = self.client.get(reverse("platformadmin:business_create"))
        self.assertEqual(r.status_code, 403)
        r = self.client.post(reverse("platformadmin:business_create"),
                             self._payload())
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Business.objects.filter(name="Gamma Stores").exists())


class PlanAdminTests(PlatformBaseTest):
    hidden_feature_fields = [
        "feature_customers",
        "feature_employees",
        "feature_returns",
        "feature_executive_dashboard",
        "feature_attendance",
        "feature_payroll",
        "feature_manufacturing",
        "feature_crm",
        "feature_loyalty_program",
        "feature_gift_cards",
        "feature_kitchen_display",
        "feature_multi_currency",
        "feature_offline_mode",
        "feature_mobile_app",
        "feature_owner_dashboard_app",
        "feature_whatsapp_integration",
        "feature_ai_reports",
        "feature_ai_forecast",
        "feature_ai_sales_prediction",
        "feature_ai_assistant",
        "feature_daily_backup",
        "feature_weekly_backup",
        "feature_priority_restore",
        "feature_email_integration",
        "feature_sms_integration",
        "feature_payment_gateway",
        "feature_white_label",
        "feature_custom_domain",
        "feature_logo_replacement",
        "feature_email_branding",
        "feature_receipt_branding",
        "feature_invoice_branding",
    ]
    expected_module_labels = [
        "POS Core",
        "Inventory Management",
        "Suppliers",
        "Purchasing",
        "Expenses",
        "Stock Transfers",
        "Tailoring Operations",
        "Customer Credit",
        "Advanced Reports",
        "Audit Logs",
        "Barcode Printing",
        "Custom Roles",
        "API Access",
    ]
    included_pos_core_capabilities = [
        "Products and Categories",
        "Customers",
        "Users and Staff",
        "Registers and Shifts",
        "Branches and Operational Warehouses",
        "Invoices and Receipts",
        "Held Sales",
        "Basic Sales Returns",
    ]

    def _payload(self, name="QA Commercial Plan", **overrides):
        data = {
            "name": name,
            "description": "Commercial modules test plan",
            "support_level": Plan.SupportLevel.STANDARD,
            "sort_order": "25",
            "is_active": "on",
            "monthly_price": "29.000",
            "annual_price": "290.000",
            "setup_fee": "5.000",
            "currency_code": "OMR",
            "trial_days": "14",
            "allow_trial": "on",
            "max_branches": "2",
            "max_users": "10",
            "max_warehouses": "3",
            "max_products": "1000",
            "max_customers": "500",
            "max_monthly_invoices": "2500",
            "storage_limit_mb": "2048",
            "max_employees": "20",
            "max_suppliers": "50",
            "max_active_orders": "100",
            "max_api_calls": "5000",
            "max_branch_managers": "3",
            "max_cashiers": "8",
            "max_logged_in_devices": "15",
            "max_pos_terminals": "6",
        }
        data.update({field: "on" for field in PLAN_MODULE_FIELDS})
        data.update(overrides)
        return data

    def test_form_uses_exact_module_whitelist_and_commercial_labels(self):
        form = PlanForm()

        self.assertEqual(list(form.fields), PLAN_FORM_FIELDS)
        self.assertEqual(len(PLAN_MODULE_FIELDS), 13)
        self.assertEqual(
            [form.fields[field].label for field in PLAN_MODULE_FIELDS],
            self.expected_module_labels,
        )
        self.assertEqual(
            [PLAN_MODULE_LABELS[field] for field in PLAN_MODULE_FIELDS],
            self.expected_module_labels,
        )
        self.assertEqual(form.fields["description"].widget.attrs["rows"], 2)
        for field in PLAN_MODULE_FIELDS:
            self.assertNotIn("Feature", form.fields[field].label)

    def test_plan_form_renders_required_sections_modules_and_help(self):
        r = self.client.get(reverse("platformadmin:plan_create"))

        self.assertEqual(r.status_code, 200)
        for heading in [
            "Basic Plan Information",
            "Pricing and Billing",
            "Limits",
            "NexaPOS Modules",
            "Included with POS Core",
        ]:
            self.assertContains(r, heading)
        for field, label in zip(
            PLAN_MODULE_FIELDS, self.expected_module_labels, strict=True
        ):
            self.assertContains(r, f'name="{field}"')
            self.assertContains(r, label)
        for text in [
            "Purchasing requires Inventory Management and Suppliers.",
            "Stock Transfers require Inventory Management.",
            "Tailoring Operations require POS Core and Inventory Management.",
            "Customer Credit requires POS Core.",
            "Barcode Printing requires POS Core.",
            "Custom Roles require POS Core Users &amp; Staff.",
            "API Access does not automatically enable any business module.",
        ]:
            self.assertContains(r, text, html=False)
        self.assertContains(r, "Use 0 for unlimited.")
        for capability in self.included_pos_core_capabilities:
            self.assertContains(r, capability)

    def test_compatibility_future_integration_and_branding_fields_are_hidden(self):
        r = self.client.get(reverse("platformadmin:plan_create"))

        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        for field in self.hidden_feature_fields:
            self.assertNotIn(field, form.fields)
            self.assertNotContains(r, f'name="{field}"')

    def test_edit_preserves_every_hidden_feature_value(self):
        plan = Plan.objects.create(name="Hidden Preservation Plan")
        expected = {}
        for index, field in enumerate(self.hidden_feature_fields):
            value = index % 2 == 0
            setattr(plan, field, value)
            expected[field] = value
        plan.save(update_fields=self.hidden_feature_fields)

        r = self.client.post(
            reverse("platformadmin:plan_edit", args=[plan.pk]),
            self._payload(name=plan.name, description="Visible edit only"),
        )

        self.assertRedirects(r, reverse("platformadmin:plan_list"))
        plan.refresh_from_db()
        self.assertEqual(plan.description, "Visible edit only")
        self.assertEqual(
            {field: getattr(plan, field) for field in self.hidden_feature_fields},
            expected,
        )

    def test_create_ignores_forged_hidden_fields_and_uses_model_defaults(self):
        payload = self._payload(name="Safe Create Plan")
        payload.update({field: "on" for field in self.hidden_feature_fields})

        r = self.client.post(reverse("platformadmin:plan_create"), payload)

        self.assertRedirects(r, reverse("platformadmin:plan_list"))
        plan = Plan.objects.get(name="Safe Create Plan")
        defaults = {
            field: Plan._meta.get_field(field).default
            for field in self.hidden_feature_fields
        }
        self.assertEqual(
            {field: getattr(plan, field) for field in self.hidden_feature_fields},
            defaults,
        )

    def test_create_and_edit_visible_plan_workflow(self):
        create_payload = self._payload(name="Plan Workflow Test")
        create_payload.pop("feature_transfers")

        create_response = self.client.post(
            reverse("platformadmin:plan_create"), create_payload
        )

        self.assertRedirects(create_response, reverse("platformadmin:plan_list"))
        plan = Plan.objects.get(name="Plan Workflow Test")
        self.assertEqual(plan.monthly_price, D("29.000"))
        self.assertTrue(plan.feature_sales)
        self.assertFalse(plan.feature_transfers)

        edit_payload = self._payload(
            name=plan.name,
            monthly_price="39.000",
            description="Updated through Platform Admin",
        )
        edit_payload.pop("feature_inventory")
        edit_response = self.client.post(
            reverse("platformadmin:plan_edit", args=[plan.pk]), edit_payload
        )

        self.assertRedirects(edit_response, reverse("platformadmin:plan_list"))
        plan.refresh_from_db()
        self.assertEqual(plan.monthly_price, D("39.000"))
        self.assertEqual(plan.description, "Updated through Platform Admin")
        self.assertFalse(plan.feature_inventory)

    def test_plan_validation_rejects_invalid_limits_and_duplicate_names(self):
        invalid_limit = self.client.post(
            reverse("platformadmin:plan_create"),
            self._payload(name="Invalid Limits Plan", max_branches="-1"),
        )

        self.assertEqual(invalid_limit.status_code, 200)
        self.assertFormError(
            invalid_limit.context["form"],
            "max_branches",
            "Ensure this value is greater than or equal to 0.",
        )
        self.assertFalse(Plan.objects.filter(name="Invalid Limits Plan").exists())

        Plan.objects.create(name="Duplicate Plan Name")
        duplicate = self.client.post(
            reverse("platformadmin:plan_create"),
            self._payload(name="Duplicate Plan Name"),
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertIn("name", duplicate.context["form"].errors)

    def test_non_platform_user_cannot_view_or_change_plans(self):
        plan = Plan.objects.create(name="Protected Plan")
        self.client.logout()
        self.client.force_login(self.owner_a)

        requests = [
            self.client.get(reverse("platformadmin:plan_list")),
            self.client.get(reverse("platformadmin:plan_create")),
            self.client.get(reverse("platformadmin:plan_edit", args=[plan.pk])),
            self.client.post(
                reverse("platformadmin:plan_create"),
                self._payload(name="Unauthorized Create"),
            ),
            self.client.post(
                reverse("platformadmin:plan_edit", args=[plan.pk]),
                self._payload(name=plan.name, description="Unauthorized edit"),
            ),
        ]

        self.assertTrue(all(response.status_code == 403 for response in requests))
        plan.refresh_from_db()
        self.assertEqual(plan.description, "")
        self.assertFalse(Plan.objects.filter(name="Unauthorized Create").exists())

    def test_coupons_hidden_from_platform_sidebar(self):
        r = self.client.get(reverse("platformadmin:plan_list"))

        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Coupons")


class SubscriptionManagementTests(PlatformBaseTest):
    def _second_plan(self):
        return Plan.objects.create(
            name="Growth",
            monthly_price=D("49.000"),
            annual_price=D("499.000"),
            currency_code="USD",
            max_branches=0,
            max_users=0,
            max_warehouses=0,
        )

    def test_renewal_from_active_subscription_extends_from_current_end(self):
        plan = self.sub_a.plan
        current_end = timezone.now() + timedelta(days=10)
        self.sub_a.status = Subscription.Status.ACTIVE
        self.sub_a.current_period_start = timezone.now()
        self.sub_a.current_period_end = current_end
        self.sub_a.save()

        r = self.client.post(
            reverse("platformadmin:business_action",
                    args=[self.business_a.public_id, "renew"]),
            {
                "renew-plan": plan.pk,
                "renew-renewal_type": "monthly",
                "renew-start_date": "",
                "renew-end_date": "",
                "renew-payment_amount": "30.000",
                "renew-payment_method": "manual",
                "renew-payment_reference": "REN-ACTIVE",
                "renew-notes": "monthly renewal",
            },
        )

        self.assertEqual(r.status_code, 302)
        self.sub_a.refresh_from_db()
        self.assertEqual(self.sub_a.status, Subscription.Status.ACTIVE)
        self.assertEqual(
            self.sub_a.current_period_end.date(),
            current_end.date() + timedelta(days=30),
        )
        payment = SubscriptionPayment.objects.get(reference="REN-ACTIVE")
        self.assertEqual(payment.business, self.business_a)
        self.assertEqual(payment.amount, D("30.000"))
        self.assertTrue(AuditLog.objects.filter(
            action="platform.subscription_renewed",
            business=self.business_a,
        ).exists())

    def test_renewal_from_expired_subscription_starts_today(self):
        plan = self.sub_a.plan
        self.sub_a.status = Subscription.Status.ACTIVE
        self.sub_a.current_period_end = timezone.now() - timedelta(days=20)
        self.sub_a.grace_days = 1
        self.sub_a.save()

        r = self.client.post(
            reverse("platformadmin:business_action",
                    args=[self.business_a.public_id, "renew"]),
            {
                "renew-plan": plan.pk,
                "renew-renewal_type": "annual",
                "renew-start_date": "",
                "renew-end_date": "",
                "renew-payment_amount": "300.000",
                "renew-payment_method": "bank_transfer",
                "renew-payment_reference": "REN-EXPIRED",
                "renew-notes": "",
            },
        )

        self.assertEqual(r.status_code, 302)
        self.sub_a.refresh_from_db()
        today = timezone.localdate()
        self.assertEqual(self.sub_a.current_period_start.date(), today)
        self.assertEqual(self.sub_a.current_period_end.date(), today + timedelta(days=365))
        self.assertEqual(self.sub_a.status, Subscription.Status.ACTIVE)

    def test_plan_change_records_optional_payment(self):
        new_plan = self._second_plan()

        r = self.client.post(
            reverse("platformadmin:business_action",
                    args=[self.business_a.public_id, "change_plan"]),
            {
                "plan-new_plan": new_plan.pk,
                "plan-effective_date": str(timezone.localdate()),
                "plan-notes": "upgrade",
                "plan-payment_amount": "15.000",
                "plan-payment_reference": "UP-001",
                "plan-payment_method": "manual",
            },
        )

        self.assertEqual(r.status_code, 302)
        self.sub_a.refresh_from_db()
        self.assertEqual(self.sub_a.plan, new_plan)
        self.assertTrue(SubscriptionPayment.objects.filter(
            business=self.business_a,
            subscription=self.sub_a,
            reference="UP-001",
        ).exists())
        self.assertTrue(AuditLog.objects.filter(
            action="platform.subscription_plan_changed",
            business=self.business_a,
        ).exists())

    def test_record_payment_from_business_manage_page(self):
        r = self.client.post(
            reverse("platformadmin:business_action",
                    args=[self.business_a.public_id, "record_payment"]),
            {
                "payment-amount": "12.500",
                "payment-payment_method": "gateway",
                "payment-payment_date": str(timezone.localdate()),
                "payment-payment_reference": "PAY-001",
                "payment-notes": "gateway receipt",
            },
        )

        self.assertEqual(r.status_code, 302)
        payment = SubscriptionPayment.objects.get(reference="PAY-001")
        self.assertEqual(payment.business, self.business_a)
        self.assertEqual(payment.method, "gateway")
        self.assertEqual(payment.recorded_by, self.admin)
        self.assertTrue(AuditLog.objects.filter(
            action="platform.subscription_payment_recorded",
            business=self.business_a,
        ).exists())

    def test_business_manage_page_renders_subscription_management(self):
        r = self.client.get(reverse("platformadmin:business_detail",
                                    args=[self.business_a.public_id]))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Business overview")
        self.assertContains(r, "Renew subscription")
        self.assertContains(r, "Change plan")
        self.assertContains(r, "Record payment")
        self.assertContains(r, "Subscription history")


class ExpiryModeTests(PlatformBaseTest):
    def setUp(self):
        super().setUp()
        # Expire business A's subscription
        self.sub_a.status = Subscription.Status.ACTIVE
        self.sub_a.current_period_end = timezone.now() - timedelta(days=60)
        self.sub_a.grace_days = 7
        self.sub_a.save()
        self.client.logout()
        self.client.force_login(self.owner_a)

    def test_read_only_mode_allows_view_blocks_writes(self):
        config = PlatformConfig.get_solo()
        config.expiry_mode = PlatformConfig.ExpiryMode.READ_ONLY
        config.save()
        # View works
        self.assertEqual(self.client.get(reverse("customers:list")).status_code, 200)
        # Write blocked
        r = self.client.post(reverse("catalog:product_create"), {
            "name": "Blocked", "product_type": "standard",
            "purchase_price": "1", "sale_price": "2"})
        self.assertEqual(r.status_code, 403)

    def test_suspend_mode_blocks_all_access(self):
        config = PlatformConfig.get_solo()
        config.expiry_mode = PlatformConfig.ExpiryMode.SUSPEND
        config.save()
        # Even a GET to a centrally guarded business page is denied.
        r = self.client.get(reverse("customers:list"))
        self.assertEqual(r.status_code, 403)

    def test_settings_page_changes_mode_and_audits(self):
        self.client.logout()
        self.client.force_login(self.admin)
        r = self.client.post(reverse("platformadmin:settings"),
                             {"expiry_mode": "suspend"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PlatformConfig.get_solo().expiry_mode, "suspend")
        self.assertTrue(AuditLog.objects.filter(
            action="platform.settings_changed").exists())
