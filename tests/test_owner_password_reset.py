"""Platform owner reset and temporary-password login boundaries."""

import logging
from unittest.mock import patch

from django.test import Client
from django.urls import reverse
from rest_framework.authtoken.models import Token

from apps.accounts.forms import StyledSetPasswordForm
from apps.accounts.models import User
from apps.audit.models import AuditLog

from .base import TenantTestCase


class OwnerPasswordResetTests(TenantTestCase):
    old_password = "StrongPass123!"
    permanent_password = "PermanentPass789!"

    def setUp(self):
        self.platform_admin = User.objects.create_superuser(
            email="platform-reset@example.com",
            password="PlatformPass123!",
            full_name="Platform Admin",
        )
        self.reset_url = reverse(
            "platformadmin:owner_password_reset", args=[self.business_a.public_id]
        )
        self.client.force_login(self.platform_admin)

    def reset_owner(self):
        response = self.client.post(self.reset_url)
        self.assertEqual(response.status_code, 200)
        return response, response.context["temporary_password"]

    def test_confirmation_is_read_only_and_shows_owner_login(self):
        original_hash = self.owner_a.password
        response = self.client.get(self.reset_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.owner_a.full_name)
        self.assertContains(response, self.owner_a.email)
        self.assertContains(response, "cannot be viewed or recovered")
        self.assertNotContains(response, original_hash)
        self.assertNotIn("temporary_password", response.context)
        business_page = self.client.get(reverse(
            "platformadmin:business_detail", args=[self.business_a.public_id]
        ))
        self.assertContains(business_page, self.reset_url)
        self.owner_a.refresh_from_db()
        self.assertEqual(self.owner_a.password, original_hash)
        self.assertFalse(self.owner_a.must_change_password)

    def test_reset_hashes_password_and_shows_it_once(self):
        old_hash = self.owner_a.password
        response, temporary_password = self.reset_owner()
        self.owner_a.refresh_from_db()
        self.assertNotEqual(self.owner_a.password, old_hash)
        self.assertNotEqual(self.owner_a.password, temporary_password)
        self.assertTrue(self.owner_a.check_password(temporary_password))
        self.assertFalse(self.owner_a.check_password(self.old_password))
        self.assertTrue(self.owner_a.must_change_password)
        self.assertContains(response, temporary_password)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertContains(response, f'action="{reverse("accounts:logout")}"')
        self.assertNotIn(temporary_password, str(dict(self.client.session)))
        self.assertNotContains(self.client.get(self.reset_url), temporary_password)

    def test_reset_audit_identifies_actor_target_and_business_without_secret(self):
        with patch.object(logging.Logger, "_log") as write_log:
            _, temporary_password = self.reset_owner()
        entry = AuditLog.objects.get(action="platform.owner_password_reset")
        self.assertEqual(entry.business, self.business_a)
        self.assertEqual(entry.user, self.platform_admin)
        self.assertEqual(entry.object_id, str(self.owner_a.public_id))
        self.assertIn(self.owner_a.email, entry.description)
        self.assertNotIn(temporary_password, str(entry.__dict__))
        self.assertNotIn(temporary_password, str(write_log.call_args_list))

    def test_temporary_login_is_forced_to_change_and_tenant_pages_are_blocked(self):
        _, temporary_password = self.reset_owner()
        self.client.logout()
        response = self.client.post(reverse("accounts:login"), {
            "email": self.owner_a.email,
            "password": temporary_password,
        }, QUERY_STRING="next=/dashboard/")
        self.assertRedirects(
            response, reverse("accounts:change_password"), fetch_redirect_response=False
        )
        self.assertRedirects(
            self.client.get(reverse("dashboard")),
            reverse("accounts:change_password"), fetch_redirect_response=False,
        )
        self.assertEqual(self.client.get(reverse("accounts:change_password")).status_code, 200)
        api_response = self.client.get(reverse("api:me"))
        self.assertEqual(api_response.status_code, 403)
        self.assertEqual(api_response.json()["code"], "password_change_required")
        self.assertEqual(self.client.get(reverse("api:health")).status_code, 200)
        self.assertEqual(
            self.client.post(reverse("accounts:logout")).url,
            reverse("accounts:login"),
        )

    def test_permanent_change_clears_flag_and_invalidates_temporary_password(self):
        _, temporary_password = self.reset_owner()
        self.client.logout()
        self.assertFalse(self.client.login(
            email=self.owner_a.email, password=self.old_password
        ))
        self.assertTrue(self.client.login(
            email=self.owner_a.email, password=temporary_password
        ))
        response = self.client.post(reverse("accounts:change_password"), {
            "old_password": temporary_password,
            "new_password1": self.permanent_password,
            "new_password2": self.permanent_password,
        })
        self.assertRedirects(
            response, reverse("accounts:profile"), fetch_redirect_response=False
        )
        self.owner_a.refresh_from_db()
        self.assertFalse(self.owner_a.must_change_password)
        self.assertTrue(self.owner_a.check_password(self.permanent_password))
        self.assertFalse(self.owner_a.check_password(temporary_password))
        self.assertNotEqual(self.client.get(reverse("dashboard")).status_code, 302)
        self.client.logout()
        self.assertFalse(self.client.login(
            email=self.owner_a.email, password=temporary_password
        ))
        self.assertTrue(self.client.login(
            email=self.owner_a.email, password=self.permanent_password
        ))

    def test_existing_api_token_cannot_bypass_required_password_change(self):
        token = Token.objects.create(user=self.owner_a)
        self.reset_owner()
        self.assertFalse(Token.objects.filter(pk=token.pk).exists())
        token = Token.objects.create(user=self.owner_a)
        response = Client().get(
            reverse("api:me"),
            HTTP_AUTHORIZATION=f"Token {token.key}",
            HTTP_X_BUSINESS_ID=str(self.business_a.public_id),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "password_change_required")

    def test_tenant_user_cannot_open_confirmation_or_reset(self):
        old_hash = self.owner_a.password
        self.client.force_login(self.cashier_a)
        self.assertEqual(self.client.get(self.reset_url).status_code, 403)
        self.assertEqual(self.client.post(self.reset_url).status_code, 403)
        self.owner_a.refresh_from_db()
        self.assertEqual(self.owner_a.password, old_hash)

    def test_anonymous_user_cannot_open_confirmation_or_reset(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.reset_url).status_code, 302)
        self.assertEqual(self.client.post(self.reset_url).status_code, 302)

    def test_cross_business_target_spoof_is_rejected(self):
        owner_a_hash = self.owner_a.password
        owner_b_hash = self.owner_b.password
        response = self.client.post(self.reset_url, {
            "owner_id": str(self.owner_b.pk),
        })
        self.assertEqual(response.status_code, 400)
        self.owner_a.refresh_from_db()
        self.owner_b.refresh_from_db()
        self.assertEqual(self.owner_a.password, owner_a_hash)
        self.assertEqual(self.owner_b.password, owner_b_hash)
        self.assertFalse(AuditLog.objects.filter(
            action="platform.owner_password_reset"
        ).exists())

    def test_reset_mutation_is_post_only_and_requires_csrf(self):
        old_hash = self.owner_a.password
        self.assertEqual(self.client.put(self.reset_url).status_code, 405)
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.platform_admin)
        confirmation = strict_client.get(self.reset_url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertEqual(strict_client.post(self.reset_url).status_code, 403)
        token = strict_client.cookies["csrftoken"].value
        self.assertEqual(strict_client.post(
            self.reset_url, HTTP_X_CSRFTOKEN=token
        ).status_code, 200)
        self.owner_a.refresh_from_db()
        self.assertNotEqual(self.owner_a.password, old_hash)

    def test_support_impersonation_keeps_real_owner_change_requirement(self):
        _, temporary_password = self.reset_owner()
        login_as_url = reverse(
            "platformadmin:login_as", args=[self.business_a.public_id]
        )
        response = self.client.post(login_as_url, {"reason": "Owner support"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("accounts:change_password")).status_code, 403
        )
        self.owner_a.refresh_from_db()
        self.assertTrue(self.owner_a.must_change_password)
        self.client.get(reverse("platformadmin:support_exit"))
        self.client.logout()
        response = self.client.post(reverse("accounts:login"), {
            "email": self.owner_a.email, "password": temporary_password,
        })
        self.assertEqual(response.url, reverse("accounts:change_password"))

    def test_inactive_owner_stays_inactive_after_reset(self):
        self.owner_a.is_active = False
        self.owner_a.save(update_fields=["is_active"])
        _, temporary_password = self.reset_owner()
        self.owner_a.refresh_from_db()
        self.assertFalse(self.owner_a.is_active)
        self.client.logout()
        self.assertFalse(self.client.login(
            email=self.owner_a.email, password=temporary_password
        ))

    def test_owner_email_reset_also_clears_temporary_password_flag(self):
        self.reset_owner()
        token = Token.objects.create(user=self.owner_a)
        form = StyledSetPasswordForm(self.owner_a, data={
            "new_password1": self.permanent_password,
            "new_password2": self.permanent_password,
        })
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.owner_a.refresh_from_db()
        self.assertFalse(self.owner_a.must_change_password)
        self.assertTrue(self.owner_a.check_password(self.permanent_password))
        self.assertFalse(Token.objects.filter(pk=token.pk).exists())
