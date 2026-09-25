"""Logout stays a same-origin, CSRF-protected POST in every account state."""

from django.test import Client
from django.urls import reverse

from apps.accounts.models import User

from .base import TenantTestCase


class LogoutCSRFFlowTests(TenantTestCase):
    def _client_with_logout_form(self, user, page_url):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        response = client.get(page_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, f'action="{reverse("accounts:logout")}"'
        )
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertIn("csrftoken", client.cookies)
        return client

    def _assert_same_origin_logout(self, client):
        response = client.post(
            reverse("accounts:logout"),
            HTTP_ORIGIN="http://testserver",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", client.session)

    def test_platform_admin_logout_from_platform_page(self):
        admin = User.objects.create_superuser(
            email="csrf-platform@example.com",
            password="StrongPass123!",
            full_name="Platform Admin",
        )
        client = self._client_with_logout_form(
            admin, reverse("platformadmin:dashboard")
        )
        self._assert_same_origin_logout(client)

    def test_platform_admin_logout_from_reset_success_page(self):
        admin = User.objects.create_superuser(
            email="csrf-reset-result@example.com",
            password="StrongPass123!",
            full_name="Platform Admin",
        )
        reset_url = reverse(
            "platformadmin:owner_password_reset", args=[self.business_a.public_id]
        )
        client = self._client_with_logout_form(admin, reset_url)
        response = client.post(
            reset_url,
            HTTP_ORIGIN="http://testserver",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self._assert_same_origin_logout(client)

    def test_tenant_owner_logout_from_tenant_page(self):
        client = self._client_with_logout_form(
            self.owner_a, reverse("dashboard")
        )
        self._assert_same_origin_logout(client)

    def test_forced_password_change_user_can_logout(self):
        self.owner_a.must_change_password = True
        self.owner_a.save(update_fields=["must_change_password"])
        client = self._client_with_logout_form(
            self.owner_a, reverse("accounts:change_password")
        )
        self._assert_same_origin_logout(client)

    def test_logout_rejects_get_missing_token_and_null_origin(self):
        admin = User.objects.create_superuser(
            email="csrf-enforcement@example.com",
            password="StrongPass123!",
            full_name="Platform Admin",
        )
        client = self._client_with_logout_form(
            admin, reverse("platformadmin:dashboard")
        )
        logout_url = reverse("accounts:logout")
        token = client.cookies["csrftoken"].value
        self.assertEqual(client.get(logout_url).status_code, 405)
        self.assertEqual(client.post(
            logout_url, HTTP_ORIGIN="http://testserver"
        ).status_code, 403)
        self.assertEqual(client.post(
            logout_url, HTTP_ORIGIN="null", HTTP_X_CSRFTOKEN=token
        ).status_code, 403)
        self.assertEqual(client.post(
            logout_url, HTTP_ORIGIN="https://untrusted.example",
            HTTP_X_CSRFTOKEN=token,
        ).status_code, 403)
        self.assertIn("_auth_user_id", client.session)
        self._assert_same_origin_logout(client)
