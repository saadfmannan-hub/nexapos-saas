"""Subscription limits, trial expiry, suspension behaviour."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.sales.services import SaleError
from apps.subscriptions import services as subscriptions
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

    def test_branch_create_view_respects_limit(self):
        self.plan.max_branches = 1
        self.plan.save()
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("branches:branch_create"), follow=True)
        self.assertContains(response, "Upgrade your plan")

    def test_monthly_invoice_limit(self):
        self.allow_no_shift()
        self.make_sale()
        self.plan.max_monthly_invoices = 1
        self.plan.save()
        with self.assertRaises(subscriptions.LimitExceeded):
            self.make_sale()


class StatusTests(TenantTestCase):
    def test_trial_expiry_blocks_new_sales(self):
        self.allow_no_shift()
        sub = self.business_a.subscription
        sub.trial_ends_at = timezone.now() - timedelta(days=1)
        sub.save()
        self.assertEqual(sub.effective_status, Subscription.Status.EXPIRED)
        with self.assertRaises(
            (subscriptions.SubscriptionInactive, SaleError)
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

    def test_suspended_business_read_only(self):
        self.allow_no_shift()
        sale = self.make_sale()  # data created while active
        sub = self.business_a.subscription
        sub.status = Subscription.Status.SUSPENDED
        sub.save()
        self.client.force_login(self.owner_a)
        # Reads still work — data is never deleted
        response = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assertEqual(response.status_code, 200)
        # Writes are blocked by middleware
        response = self.client.post(
            reverse("catalog:product_create"),
            {"name": "Blocked", "product_type": "standard",
             "purchase_price": "1", "sale_price": "2"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/subscription/", response.url)
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
