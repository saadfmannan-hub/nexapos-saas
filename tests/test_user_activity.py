"""Tenant-scoped activity tracking and platform presence display."""

from datetime import UTC, datetime, timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.accounts.activity import format_last_activity
from apps.accounts.models import Membership, Role, User
from apps.core.middleware import SESSION_BUSINESS_KEY
from apps.subscriptions.models import Subscription

from .base import TenantTestCase


class ActivityTrackingTests(TenantTestCase):
    def setUp(self):
        self.membership = self.business_a.memberships.get(user=self.owner_a)

    def test_authenticated_business_request_records_activity(self):
        self.client.force_login(self.owner_a)
        before = timezone.now()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.membership.refresh_from_db()
        self.assertGreaterEqual(self.membership.last_activity_at, before)
        self.assertLessEqual(self.membership.last_activity_at, timezone.now())

    def test_anonymous_request_does_not_record_activity(self):
        self.client.get(reverse("accounts:login"))
        self.membership.refresh_from_db()
        self.assertIsNone(self.membership.last_activity_at)

    def test_static_asset_request_does_not_record_activity(self):
        self.client.force_login(self.owner_a)
        self.client.get("/static/css/app.css")
        self.membership.refresh_from_db()
        self.assertIsNone(self.membership.last_activity_at)

    def test_platform_admin_request_does_not_record_tenant_activity(self):
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(reverse("platformadmin:dashboard")).status_code,
            403,
        )
        self.membership.refresh_from_db()
        self.assertIsNone(self.membership.last_activity_at)

    def test_requests_within_one_minute_do_not_issue_membership_updates(self):
        self.client.force_login(self.owner_a)
        self.client.get(reverse("dashboard"))
        self.membership.refresh_from_db()
        first_activity = self.membership.last_activity_at

        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("dashboard"))
        membership_updates = [
            query["sql"] for query in queries
            if query["sql"].lstrip().upper().startswith("UPDATE ")
            and "accounts_membership" in query["sql"].lower()
        ]
        self.assertEqual(membership_updates, [])
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.last_activity_at, first_activity)

    def test_activity_updates_after_one_minute(self):
        old = timezone.now() - timedelta(seconds=61)
        Membership.objects.filter(pk=self.membership.pk).update(last_activity_at=old)
        self.client.force_login(self.owner_a)
        self.client.get(reverse("dashboard"))
        self.membership.refresh_from_db()
        self.assertGreater(self.membership.last_activity_at, old)

    def test_activity_is_scoped_to_selected_membership(self):
        other_role = Role.objects.for_business(self.business_b).get(
            name="Business Administrator"
        )
        other_membership = Membership.objects.create(
            business=self.business_b, user=self.owner_a, role=other_role
        )
        self.client.force_login(self.owner_a)
        session = self.client.session
        session[SESSION_BUSINESS_KEY] = self.business_a.pk
        session.save()
        self.client.get(reverse("dashboard"))
        self.membership.refresh_from_db()
        other_membership.refresh_from_db()
        first_activity = self.membership.last_activity_at
        self.assertIsNotNone(first_activity)
        self.assertIsNone(other_membership.last_activity_at)

        session = self.client.session
        session[SESSION_BUSINESS_KEY] = self.business_b.pk
        session.save()
        self.client.get(reverse("dashboard"))
        self.membership.refresh_from_db()
        other_membership.refresh_from_db()
        self.assertEqual(self.membership.last_activity_at, first_activity)
        self.assertIsNotNone(other_membership.last_activity_at)

    def test_inactive_membership_and_suspended_business_are_not_recorded(self):
        self.client.force_login(self.owner_a)
        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])
        self.client.get(reverse("dashboard"))
        self.membership.refresh_from_db()
        self.assertIsNone(self.membership.last_activity_at)

        self.membership.is_active = True
        self.membership.save(update_fields=["is_active"])
        self.business_a.is_active = False
        self.business_a.save(update_fields=["is_active"])
        self.client.get(reverse("dashboard"))
        self.membership.refresh_from_db()
        self.assertIsNone(self.membership.last_activity_at)

    def test_suspended_subscription_keeps_access_behavior(self):
        self.business_a.subscription.status = Subscription.Status.SUSPENDED
        self.business_a.subscription.save(update_fields=["status"])
        self.client.force_login(self.owner_a)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 403)

    def test_inactive_user_does_not_record_activity(self):
        self.client.force_login(self.owner_a)
        self.owner_a.is_active = False
        self.owner_a.save(update_fields=["is_active"])
        self.client.get(reverse("dashboard"))
        self.membership.refresh_from_db()
        self.assertIsNone(self.membership.last_activity_at)

    def test_token_api_activity_requires_valid_business_context(self):
        plan = self.business_a.subscription.plan
        plan.feature_api_access = True
        plan.save(update_fields=["feature_api_access"])
        token = Token.objects.create(user=self.owner_a)
        api_client = APIClient()
        api_client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        url = reverse("api:product-list")

        self.assertEqual(
            api_client.get(
                url, HTTP_X_BUSINESS_ID=str(self.business_b.public_id)
            ).status_code,
            403,
        )
        self.membership.refresh_from_db()
        self.assertIsNone(self.membership.last_activity_at)

        self.assertEqual(
            api_client.get(
                url, HTTP_X_BUSINESS_ID=str(self.business_a.public_id)
            ).status_code,
            200,
        )
        self.membership.refresh_from_db()
        self.assertIsNotNone(self.membership.last_activity_at)

    def test_support_impersonation_does_not_mark_owner_online(self):
        from apps.accounts.middleware import ActivityMiddleware

        class Request:
            user = self.owner_a
            membership = self.membership
            support_admin = object()
            path_info = "/dashboard/"

        ActivityMiddleware.record_activity(Request())
        self.membership.refresh_from_db()
        self.assertIsNone(self.membership.last_activity_at)


class ActivityLabelTests(TenantTestCase):
    def test_online_minutes_hours_days_and_exact_date(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        cases = (
            (timedelta(minutes=5), "Online now", True),
            (timedelta(minutes=5, seconds=1), "6 min ago", False),
            (timedelta(minutes=18), "18 min ago", False),
            (timedelta(minutes=59, seconds=59), "59 min ago", False),
            (timedelta(hours=1), "1 hour ago", False),
            (timedelta(hours=2), "2 hours ago", False),
            (timedelta(hours=23), "23 hours ago", False),
            (timedelta(days=1), "1 day ago", False),
            (timedelta(days=29), "29 days ago", False),
            (timedelta(days=30), "Sep 12, 2026", False),
        )
        for age, expected, online in cases:
            with self.subTest(age=age):
                self.assertEqual(
                    format_last_activity(now - age, business=self.business_a, now=now),
                    (expected, online),
                )

    def test_never_active(self):
        self.assertEqual(format_last_activity(None), ("Never active", False))


class PlatformBusinessActivityTests(TenantTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="platform-presence@example.com",
            password="StrongPass123!",
            full_name="Platform Presence Admin",
            is_platform_admin=True,
        )
        self.detail_url = reverse(
            "platformadmin:business_detail", args=[self.business_a.public_id]
        )

    def test_manage_users_shows_activity_role_branch_and_only_selected_business(self):
        member = self.business_a.memberships.get(user=self.owner_a)
        member.branches.add(self.branch_a)
        member.last_activity_at = timezone.now() - timedelta(minutes=18)
        member.save(update_fields=["last_activity_at"])
        self.client.force_login(self.admin)

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Last Activity")
        self.assertContains(response, self.owner_a.email)
        self.assertContains(response, member.role.name)
        self.assertContains(response, self.branch_a.name)
        self.assertContains(response, "18 min ago")
        self.assertNotContains(response, self.owner_b.email)
        self.assertNotContains(response, "Online now")

    def test_online_indicator_and_never_active_are_only_on_manage_page(self):
        member = self.business_a.memberships.get(user=self.owner_a)
        member.last_activity_at = timezone.now()
        member.save(update_fields=["last_activity_at"])
        self.client.force_login(self.admin)
        detail = self.client.get(self.detail_url)
        self.assertContains(detail, "Online now")
        self.assertContains(detail, "Never active")  # Cashier has no activity.
        self.assertContains(detail, 'class="badge badge-status bs-active"')

        listing = self.client.get(reverse("platformadmin:business_list"))
        self.assertNotContains(listing, "Last Activity")
        self.assertNotContains(listing, "Online now")

    def test_non_platform_user_cannot_read_business_presence(self):
        self.client.force_login(self.owner_a)
        self.assertEqual(self.client.get(self.detail_url).status_code, 403)

    def test_shared_user_activity_from_another_business_is_not_displayed(self):
        other_role = Role.objects.for_business(self.business_b).get(
            name="Business Administrator"
        )
        Membership.objects.create(
            business=self.business_b,
            user=self.owner_a,
            role=other_role,
            last_activity_at=timezone.now(),
        )
        self.client.force_login(self.admin)

        response = self.client.get(self.detail_url)
        self.assertContains(response, self.owner_a.email)
        self.assertContains(response, "Never active")
        self.assertNotContains(response, "Online now")
