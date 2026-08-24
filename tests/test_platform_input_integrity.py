"""No-500 coverage for platform and tenant selector inputs."""

from unittest import mock

from django import forms
from django.db import IntegrityError
from django.urls import reverse

from apps.accounts.models import User
from apps.platformadmin.models import SupportAccessGrant
from apps.platformadmin.views import (
    PlanForm,
    _create_platform_owner,
    _OwnerEmailConflict,
)
from apps.subscriptions.models import Coupon, Plan, SubscriptionPayment
from apps.tenants.forms import RegistrationForm
from apps.tenants.models import Business

from .base import TenantTestCase


class TenantSwitchInputTests(TenantTestCase):
    def test_malformed_business_id_is_a_controlled_rejection(self):
        self.client.force_login(self.owner_a)

        response = self.client.post(
            reverse("tenants:switch_business"),
            {"business_id": "not-an-integer"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(
            self.client.session.get("active_business_id"),
            self.business_a.pk,
        )


class RegistrationEmailRaceTests(TenantTestCase):
    @staticmethod
    def registration_payload(email):
        return {
            "business_name": "Concurrent Registration",
            "owner_name": "Registration Owner",
            "email": email,
            "phone": "12345678",
            "country": "OM",
            "timezone_name": "Asia/Muscat",
            "currency": "OMR",
            "currency_other": "",
            "business_category": "Other",
            "expected_branches": "1",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
            "accept_terms": "on",
        }

    def test_registration_email_race_is_a_form_error_without_partial_tenant(self):
        conflicting_user = User.objects.create_user(
            email="registration-race@example.com",
            password="StrongPass123!",
            full_name="Concurrent Winner",
        )
        before_businesses = Business.objects.count()

        with mock.patch.object(
            RegistrationForm,
            "clean_email",
            return_value=conflicting_user.email,
        ):
            response = self.client.post(
                reverse("tenants:register"),
                self.registration_payload(conflicting_user.email),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertEqual(Business.objects.count(), before_businesses)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_registration_reraises_unrelated_user_integrity_error(self):
        with (
            mock.patch(
                "apps.tenants.views.User.objects.create_user",
                side_effect=IntegrityError("unrelated registration constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated registration constraint",
            ),
        ):
            self.client.post(
                reverse("tenants:register"),
                self.registration_payload("unique-registration@example.com"),
            )


class PlatformInputIntegrityTests(TenantTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="integrity-platform@example.com",
            password="StrongPass123!",
            full_name="Integrity Platform Admin",
            is_staff=True,
            is_superuser=True,
            is_platform_admin=True,
        )
        self.client.force_login(self.admin)

    @staticmethod
    def coupon_payload(code):
        return {
            "code": code,
            "description": "",
            "percent_off": "0",
            "amount_off": "0",
            "extra_trial_days": "0",
            "max_redemptions": "0",
            "valid_until": "",
            "is_active": "on",
        }

    @staticmethod
    def plan_payload(plan, *, name=None):
        payload = {}
        for field_name, field in PlanForm(instance=plan).fields.items():
            value = getattr(plan, field_name)
            if isinstance(field, forms.BooleanField):
                if value:
                    payload[field_name] = "on"
            else:
                payload[field_name] = str(value)
        if name is not None:
            payload["name"] = name
        return payload

    def test_malformed_coupon_edit_uuid_returns_404(self):
        response = self.client.get(
            reverse("platformadmin:coupon_list"),
            {"edit": "not-a-uuid"},
        )

        self.assertEqual(response.status_code, 404)

    def test_coupon_case_and_whitespace_duplicate_is_a_form_error(self):
        Coupon.objects.create(code="WELCOME")
        before = Coupon.objects.count()

        response = self.client.post(
            reverse("platformadmin:coupon_list"),
            self.coupon_payload("  welcome  "),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("code", response.context["form"].errors)
        self.assertEqual(Coupon.objects.count(), before)

    def test_coupon_race_is_controlled_but_unrelated_integrity_is_reraised(self):
        Coupon.objects.create(code="RACE")
        with (
            mock.patch(
                "apps.platformadmin.views.CouponForm.clean_code",
                return_value="RACE",
            ),
            mock.patch(
                "apps.platformadmin.views.CouponForm.validate_unique",
                return_value=None,
            ),
        ):
            response = self.client.post(
                reverse("platformadmin:coupon_list"),
                self.coupon_payload("RACE"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("code", response.context["form"].errors)

        with (
            mock.patch(
                "apps.platformadmin.views.Coupon.save",
                side_effect=IntegrityError("unrelated database failure"),
            ),
            self.assertRaises(IntegrityError),
        ):
            self.client.post(
                reverse("platformadmin:coupon_list"),
                self.coupon_payload("UNIQUE"),
            )

    def test_plan_race_is_controlled_but_unrelated_integrity_is_reraised(self):
        existing = self.business_a.subscription.plan
        payload = self.plan_payload(existing)
        before = Plan.objects.count()

        with (
            mock.patch.object(
                PlanForm,
                "clean_name",
                return_value=existing.name,
            ),
            mock.patch.object(PlanForm, "validate_unique", return_value=None),
        ):
            response = self.client.post(
                reverse("platformadmin:plan_create"),
                payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Plan.objects.count(), before)

        unrelated_payload = self.plan_payload(
            existing,
            name="Unique Unrelated Plan",
        )
        with (
            mock.patch.object(
                Plan,
                "save",
                side_effect=IntegrityError("unrelated plan constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated plan constraint",
            ),
        ):
            self.client.post(
                reverse("platformadmin:plan_create"),
                unrelated_payload,
            )

    def test_invalid_support_hours_do_not_create_a_grant(self):
        before = SupportAccessGrant.objects.count()
        for value in ("not-a-number", "0", "-1", "73"):
            with self.subTest(value=value):
                response = self.client.post(
                    reverse(
                        "platformadmin:support_access",
                        args=[self.business_a.public_id],
                    ),
                    {"reason": "Integrity check", "hours": value},
                )
                self.assertEqual(response.status_code, 302)
        self.assertEqual(SupportAccessGrant.objects.count(), before)

    def test_malformed_support_revoke_id_is_controlled(self):
        response = self.client.post(
            reverse(
                "platformadmin:support_access",
                args=[self.business_a.public_id],
            ),
            {"revoke_id": "not-an-integer"},
        )

        self.assertEqual(response.status_code, 302)

    def test_invalid_legacy_extension_inputs_do_not_mutate_subscription(self):
        subscription = self.business_a.subscription
        original_end = subscription.current_period_end
        payment_count = SubscriptionPayment.objects.count()

        for action, payload in (
            ("extend", {"days": "bad", "method": "manual"}),
            ("extend", {"days": "-1", "method": "manual"}),
            ("extend", {"days": str(10**100), "method": "manual"}),
            (
                "extend",
                {
                    "days": "30",
                    "amount": "NaN",
                    "method": "manual",
                },
            ),
            (
                "extend",
                {
                    "days": "30",
                    "plan_id": "bad",
                    "method": "not-a-method",
                },
            ),
            ("extend_trial", {"days": "bad"}),
            ("extend_trial", {"days": "0"}),
            ("extend_trial", {"days": str(10**100)}),
        ):
            with self.subTest(action=action, payload=payload):
                response = self.client.post(
                    reverse(
                        "platformadmin:business_action",
                        args=[self.business_a.public_id, action],
                    ),
                    payload,
                )
                self.assertEqual(response.status_code, 302)

        subscription.refresh_from_db()
        self.assertEqual(subscription.current_period_end, original_end)
        self.assertEqual(SubscriptionPayment.objects.count(), payment_count)

    def test_oversized_business_period_is_a_form_error_without_partial_creation(self):
        before_businesses = Business.objects.count()

        response = self.client.post(
            reverse("platformadmin:business_create"),
            {
                "business_name": "Must Not Be Created",
                "country": "OM",
                "currency": "OMR",
                "business_category": "Other",
                "owner_name": "Overflow Owner",
                "owner_email": "overflow-owner@example.com",
                "phone": "",
                "password": "StrongPass123!",
                "plan": self.business_a.subscription.plan_id,
                "subscription_mode": "active",
                "days": str(10**100),
                "amount": "",
                "reference": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("days", response.context["form"].errors)
        self.assertEqual(Business.objects.count(), before_businesses)
        self.assertFalse(
            User.objects.filter(email="overflow-owner@example.com").exists()
        )

    def test_platform_owner_email_race_is_controlled_and_narrow(self):
        before_businesses = Business.objects.count()
        payload = {
            "business_name": "Concurrent Platform Business",
            "country": "OM",
            "currency": "OMR",
            "business_category": "Other",
            "owner_name": "Concurrent Platform Owner",
            "owner_email": "platform-owner-race@example.com",
            "phone": "",
            "password": "StrongPass123!",
            "plan": self.business_a.subscription.plan_id,
            "subscription_mode": "active",
            "days": "30",
            "amount": "",
            "reference": "",
        }

        with mock.patch(
            "apps.platformadmin.views._create_platform_owner",
            side_effect=_OwnerEmailConflict,
        ):
            response = self.client.post(
                reverse("platformadmin:business_create"),
                payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("owner_email", response.context["form"].errors)
        self.assertEqual(Business.objects.count(), before_businesses)

        conflicting_user = User.objects.create_user(
            email=payload["owner_email"],
            password="StrongPass123!",
            full_name="Concurrent Winner",
        )
        with mock.patch(
            "apps.platformadmin.views.User.objects.create_user",
            side_effect=IntegrityError("email race"),
        ):
            with self.assertRaises(_OwnerEmailConflict):
                _create_platform_owner(
                    email=conflicting_user.email,
                    password="StrongPass123!",
                    full_name="Race Loser",
                    phone="",
                )

        with (
            mock.patch(
                "apps.platformadmin.views.User.objects.create_user",
                side_effect=IntegrityError("unrelated platform owner constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated platform owner constraint",
            ),
        ):
            _create_platform_owner(
                email="unique-platform-owner@example.com",
                password="StrongPass123!",
                full_name="Unique Owner",
                phone="",
            )
