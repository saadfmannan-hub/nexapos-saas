"""Regression: platform super-admin access for users with no workspace.

A Django superuser (is_superuser=True) must reach /platform/ and
/django-admin/ even when they belong to no business — and must NOT be
bounced to /no-business/. Business users still require membership.
"""
from django.urls import reverse

from apps.accounts.models import User

from .base import TenantTestCase


class PlatformSuperuserAccessTests(TenantTestCase):
    def setUp(self):
        # A bare superuser created without an explicit platform flag and
        # with no business membership — exactly the bug scenario.
        self.superuser = User.objects.create_user(
            email="admin@nexapos.com", password="StrongPass123!",
            full_name="Platform Admin",
        )
        self.superuser.is_superuser = True
        self.superuser.is_staff = True
        self.superuser.is_platform_admin = False
        self.superuser.save()

    def test_is_platform_staff_true_for_superuser(self):
        self.assertTrue(self.superuser.is_platform_staff)
        # ordinary cashier is not platform staff
        self.assertFalse(self.cashier_a.is_platform_staff)

    def test_login_redirects_superuser_to_platform_not_no_business(self):
        response = self.client.post(reverse("accounts:login"), {
            "email": "admin@nexapos.com", "password": "StrongPass123!",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("platformadmin:dashboard"))
        self.assertNotIn("no-business", response.url)

    def test_superuser_can_open_platform_dashboard(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("platformadmin:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_superuser_can_open_django_admin(self):
        self.client.force_login(self.superuser)
        response = self.client.get("/django-admin/")
        # 200 (index) or redirect within admin — never a permission error
        self.assertIn(response.status_code, (200, 302))
        if response.status_code == 302:
            self.assertNotIn("/accounts/login", response.url)

    def test_no_business_page_redirects_superuser_to_platform(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("tenants:no_business"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("platformadmin:dashboard"))

    def test_flagged_platform_admin_without_superuser_still_works(self):
        staff = User.objects.create_user(
            email="staff@nexapos.com", password="StrongPass123!",
            full_name="Support Staff", is_platform_admin=True)
        self.client.force_login(staff)
        self.assertTrue(staff.is_platform_staff)
        self.assertEqual(
            self.client.get(reverse("platformadmin:dashboard")).status_code, 200)

    # --- isolation / non-regression guards ---------------------------------
    def test_business_user_without_membership_still_blocked(self):
        loner = User.objects.create_user(
            email="loner2@example.com", password="StrongPass123!",
            full_name="No Business")
        response = self.client.post(reverse("accounts:login"), {
            "email": "loner2@example.com", "password": "StrongPass123!",
        })
        self.assertEqual(response.url, reverse("tenants:no_business"))

    def test_business_owner_login_still_goes_to_dashboard(self):
        response = self.client.post(reverse("accounts:login"), {
            "email": "owner-a@example.com", "password": "StrongPass123!",
        })
        self.assertEqual(response.url, reverse("dashboard"))

    def test_non_platform_user_cannot_access_platform(self):
        self.client.force_login(self.owner_a)  # business owner, not platform
        response = self.client.get(reverse("platformadmin:dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_superuser_cannot_see_business_data_without_membership(self):
        # Multi-tenant isolation unchanged: a superuser with no membership
        # has no active business, so business pages still bounce to platform
        # (not into another tenant's data).
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("catalog:product_list"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("/products/", response.url)
