"""Subscription limits, trial expiry, suspension behaviour."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.sales.services import SaleError
from apps.subscriptions import services as subscriptions
from apps.subscriptions.exceptions import ModuleAccessDenied
from apps.subscriptions.models import Subscription

from .base import TenantTestCase


class LimitTests(TenantTestCase):
    def setUp(self):
        self.plan = self.business_a.subscription.plan

    def test_branch_limit_enforced(self):
        self.plan.max_branches = 1
        self.plan.save()
        with self.assertRaises(subscriptions.LimitExceeded):
            subscriptions.check_limit(self.business_a, "branches")

    def test_user_limit_enforced(self):
        self.plan.max_users = 2  # owner + cashier already exist
        self.plan.save()
        with self.assertRaises(subscriptions.LimitExceeded):
            subscriptions.check_limit(self.business_a, "users")

    def test_unlimited_when_zero(self):
        self.plan.max_products = 0
        self.plan.save()
        subscriptions.check_limit(self.business_a, "products")  # no raise

    def test_new_commercial_limit_fields_use_zero_as_unlimited(self):
        self.plan.max_suppliers = 0
        self.plan.max_cashiers = 0
        self.plan.max_pos_terminals = 0
        self.plan.save()

        for resource in ("suppliers", "cashiers", "pos_terminals"):
            current, limit, allowed = subscriptions.limit_state(
                self.business_a, resource)
            self.assertEqual(limit, 0)
            self.assertGreaterEqual(current, 0)
            self.assertTrue(allowed)

    def test_cashier_limit_can_be_enforced(self):
        self.plan.max_cashiers = 1
        self.plan.save()

        with self.assertRaises(subscriptions.LimitExceeded):
            subscriptions.check_limit(self.business_a, "cashiers")

    def test_branch_create_view_shows_upgrade_page(self):
        self.plan.max_branches = 1
        self.plan.save()
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("branches:branch_create"))
        self.assertContains(response, "Branches limit reached")
        self.assertContains(response, "View plans")

    def test_branch_create_post_blocked_when_over_limit(self):
        """Bug #5 — limits must BLOCK creation, not just highlight red."""
        from apps.branches.models import Branch

        self.plan.max_branches = 1
        self.plan.save()
        self.client.force_login(self.owner_a)
        before = Branch.objects.for_business(self.business_a).count()
        response = self.client.post(reverse("branches:branch_create"), {
            "name": "Sneaky Branch", "code": "SNK", "address": "", "phone": "",
            "email": "", "invoice_prefix": "", "receipt_footer": "",
            "is_active": "on",
        })
        self.assertContains(response, "limit reached")
        self.assertEqual(Branch.objects.for_business(self.business_a).count(), before)

    def test_user_create_post_blocked_when_over_limit(self):
        from apps.accounts.models import Membership

        self.plan.max_users = 2  # owner + cashier already exist
        self.plan.save()
        self.client.force_login(self.owner_a)
        before = Membership.objects.for_business(self.business_a).count()
        role = self.business_a.roles.get(name="Cashier")
        response = self.client.post(reverse("accounts:user_create"), {
            "full_name": "Extra", "email": "extra@example.com", "phone": "",
            "password": "StrongPass123!", "role": role.id, "is_active": "on",
        })
        self.assertContains(response, "Users limit reached")
        self.assertEqual(
            Membership.objects.for_business(self.business_a).count(), before)

    def test_warehouse_create_blocked_when_over_limit(self):
        from apps.branches.models import Warehouse

        self.plan.max_warehouses = 1
        self.plan.save()
        self.client.force_login(self.owner_a)
        response = self.client.post(reverse("branches:warehouse_create"), {
            "name": "Extra WH", "code": "XWH", "branch": self.branch_a.id,
        })
        self.assertContains(response, "Warehouses limit reached")
        self.assertFalse(Warehouse.objects.for_business(self.business_a).filter(
            code="XWH").exists())

    def test_monthly_invoice_limit(self):
        self.allow_no_shift()
        self.make_sale()
        self.plan.max_monthly_invoices = 1
        self.plan.save()
        with self.assertRaises(subscriptions.LimitExceeded):
            self.make_sale()


class StatusTests(TenantTestCase):
    def test_subscription_status_shows_official_contact_links(self):
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("subscriptions:status"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nexabusinesssolutions360@gmail.com")
        self.assertContains(
            response,
            'href="mailto:nexabusinesssolutions360@gmail.com"',
        )
        self.assertContains(response, 'href="https://wa.me/96890124734"')
        self.assertContains(response, "+968 90124734")
        self.assertContains(
            response,
            "Bank transfer and manual activation are supported.",
        )
        self.assertNotContains(response, "support@example.com")

    def test_trial_expiry_blocks_new_sales(self):
        self.allow_no_shift()
        sub = self.business_a.subscription
        sub.trial_ends_at = timezone.now() - timedelta(days=1)
        sub.save()
        self.assertEqual(sub.effective_status, Subscription.Status.EXPIRED)
        with self.assertRaises(
            (subscriptions.SubscriptionInactive, SaleError, ModuleAccessDenied)
        ):
            self.make_sale()

    def test_grace_period_still_operational(self):
        sub = self.business_a.subscription
        sub.status = Subscription.Status.ACTIVE
        sub.current_period_end = timezone.now() - timedelta(days=2)
        sub.grace_days = 7
        sub.save()
        self.assertEqual(sub.effective_status, Subscription.Status.GRACE)
        self.assertTrue(sub.is_operational)

    def test_expired_after_grace(self):
        sub = self.business_a.subscription
        sub.status = Subscription.Status.ACTIVE
        sub.current_period_end = timezone.now() - timedelta(days=30)
        sub.grace_days = 7
        sub.save()
        self.assertEqual(sub.effective_status, Subscription.Status.EXPIRED)

    def test_days_remaining_helper_uses_calendar_days(self):
        sub = self.business_a.subscription
        sub.status = Subscription.Status.ACTIVE
        sub.current_period_end = timezone.now() + timedelta(days=5)
        sub.save()
        sub.refresh_from_db()

        self.assertGreaterEqual(sub.days_remaining, 4)
        self.assertLessEqual(sub.days_remaining, 5)
        self.assertTrue(sub.is_active_subscription)
        self.assertFalse(sub.is_expired)
        self.assertTrue(sub.can_access_app)

    def test_suspended_business_denies_pos_core(self):
        self.allow_no_shift()
        sale = self.make_sale()  # data created while active
        sub = self.business_a.subscription
        sub.status = Subscription.Status.SUSPENDED
        sub.save()
        self.client.force_login(self.owner_a)
        # Reads still work — data is never deleted
        response = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assertEqual(response.status_code, 403)
        # Writes are blocked by middleware
        response = self.client.post(
            reverse("catalog:product_create"),
            {"name": "Blocked", "product_type": "standard",
             "purchase_price": "1", "sale_price": "2"},
        )
        self.assertEqual(response.status_code, 403)
        from apps.catalog.models import Product

        self.assertFalse(Product.objects.for_business(self.business_a).filter(
            name="Blocked").exists())

    def test_platform_suspension_locks_out_members(self):
        self.business_a.is_active = False
        self.business_a.save()
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)  # → no_business

    def test_feature_flag_gates_transfers(self):
        plan = self.business_a.subscription.plan
        plan.feature_transfers = False
        plan.save()
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("inventory:transfer_list"))
        self.assertContains(response, "not included in your plan")

    def test_new_feature_helpers_default_false_then_follow_plan(self):
        plan = self.business_a.subscription.plan
        plan.feature_tailoring_module = False
        plan.feature_executive_dashboard = False
        plan.save()

        self.business_a.subscription.refresh_from_db()
        self.assertFalse(subscriptions.has_tailoring_module(self.business_a))
        self.assertFalse(subscriptions.has_executive_dashboard(self.business_a))
        self.assertFalse(self.business_a.subscription.has_tailoring_module)
        self.assertFalse(self.business_a.subscription.has_executive_dashboard)

        plan.feature_tailoring_module = True
        plan.feature_executive_dashboard = True
        plan.save()

        self.business_a.subscription.refresh_from_db()
        self.assertTrue(subscriptions.has_tailoring_module(self.business_a))
        self.assertTrue(subscriptions.has_executive_dashboard(self.business_a))
        self.assertTrue(self.business_a.subscription.has_tailoring_module)
        self.assertTrue(self.business_a.subscription.has_executive_dashboard)
