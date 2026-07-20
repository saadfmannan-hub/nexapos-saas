"""Authentication and registration tests."""
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.subscriptions.models import Subscription
from apps.tenants.models import Business

from .base import TenantTestCase


class RegistrationTests(TestCase):
    def test_business_registration_provisions_everything(self):
        response = self.client.post(reverse("tenants:register"), {
            "business_name": "New Shop",
            "owner_name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "99999999",
            "country": "Testland",
            "timezone_name": "UTC",
            "currency": "USD",
            "business_category": "Clothing",
            "expected_branches": 1,
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
            "accept_terms": "on",
        })
        self.assertEqual(response.status_code, 302)
        business = Business.objects.get(name="New Shop")
        self.assertTrue(business.subscription.status == Subscription.Status.TRIAL)
        self.assertTrue(business.branches_branch_set.filter(is_head_office=True).exists())
        self.assertTrue(business.roles.filter(is_owner=True).exists())
        self.assertTrue(business.customers_customer_set.filter(is_walk_in=True).exists())
        self.assertTrue(business.sales_paymentmethod_set.filter(kind="cash").exists())

    def test_duplicate_email_rejected(self):
        User.objects.create_user(email="dup@example.com", password="x" * 10,
                                 full_name="Dup")
        response = self.client.post(reverse("tenants:register"), {
            "business_name": "Another", "owner_name": "X",
            "email": "dup@example.com", "phone": "1", "timezone_name": "UTC",
            "currency": "USD", "business_category": "Other",
            "expected_branches": 1, "password": "StrongPass123!",
            "confirm_password": "StrongPass123!", "accept_terms": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")


class LoginTests(TenantTestCase):
    def test_login_uses_official_nexa_branding(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "NexaPOS")
        self.assertContains(response, "Point of Sale &amp; Business Management")
        self.assertContains(
            response,
            "images/02_Nexa_Logo_Only-removebg-preview.png",
        )
        self.assertContains(response, 'class="auth-brand-logo"')
        self.assertIsNotNone(
            finders.find("images/02_Nexa_Logo_Only-removebg-preview.png")
        )

    def test_login_has_no_public_registration_or_reset_links(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Forgot password?")
        self.assertNotContains(response, "Register your business")
        self.assertNotContains(response, reverse("accounts:password_reset"))
        self.assertNotContains(response, reverse("tenants:register"))

    def test_login_success(self):
        response = self.client.post(reverse("accounts:login"), {
            "email": "owner-a@example.com", "password": "StrongPass123!",
        })
        self.assertEqual(response.status_code, 302)

    def test_login_invalid_password(self):
        response = self.client.post(reverse("accounts:login"), {
            "email": "owner-a@example.com", "password": "wrong",
        })
        self.assertContains(response, "Invalid email or password")

    def test_lockout_after_failed_attempts(self):
        for _ in range(5):
            self.client.post(reverse("accounts:login"), {
                "email": "owner-a@example.com", "password": "wrong",
            })
        self.owner_a.refresh_from_db()
        self.assertTrue(self.owner_a.is_locked)
        response = self.client.post(reverse("accounts:login"), {
            "email": "owner-a@example.com", "password": "StrongPass123!",
        })
        self.assertContains(response, "temporarily locked")

    def test_disabled_account_cannot_login(self):
        self.owner_a.is_active = False
        self.owner_a.save()
        response = self.client.post(reverse("accounts:login"), {
            "email": "owner-a@example.com", "password": "StrongPass123!",
        })
        self.assertContains(response, "Invalid email or password")

    def test_logout(self):
        self.client.force_login(self.owner_a)
        response = self.client.post(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 302)

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_password_reset_flow_renders(self):
        response = self.client.get(reverse("accounts:password_reset"))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("accounts:password_reset"),
                                    {"email": "owner-a@example.com"})
        self.assertEqual(response.status_code, 302)
