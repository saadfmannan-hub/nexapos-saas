"""Focused regressions for tenant employee/account integrity boundaries."""

from unittest.mock import patch

from django.db import IntegrityError
from django.urls import reverse

from apps.accounts.forms import EmployeeForm
from apps.accounts.models import Membership, Role, User

from .base import TenantTestCase


class AccountIdentityBoundaryTests(TenantTestCase):
    password = "StrongPass123!"

    def setUp(self):
        plan = self.business_a.subscription.plan
        plan.max_users = 20
        plan.save(update_fields=["max_users"])

        self.admin = User.objects.create_user(
            email="business-admin@example.com",
            password=self.password,
            full_name="Business Admin",
        )
        self.admin_membership = Membership.objects.create(
            business=self.business_a,
            user=self.admin,
            role=Role.objects.for_business(self.business_a).get(
                name="Business Administrator"
            ),
        )
        self.cashier_role_a = Role.objects.for_business(self.business_a).get(
            name="Cashier"
        )
        self.client.force_login(self.admin)

    def create_payload(self, *, email, role=None, **overrides):
        payload = {
            "full_name": "New Employee",
            "email": email,
            "phone": "12345678",
            "password": self.password,
            "role": (role or self.cashier_role_a).pk,
            "branches": [],
            "is_active": "on",
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def edit_payload(membership, **overrides):
        payload = {
            "full_name": membership.user.full_name,
            "email": membership.user.email,
            "phone": membership.user.phone,
            "password": "",
            "role": membership.role_id,
            "branches": list(
                membership.branches.values_list("pk", flat=True)
            ),
            "is_active": "on" if membership.is_active else "",
        }
        payload.update(overrides)
        return payload

    def test_malformed_role_is_a_controlled_form_error(self):
        payload = self.create_payload(email="malformed-role@example.com")
        payload["role"] = "not-an-integer"
        response = self.client.post(
            reverse("accounts:user_create"),
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("role", response.context["form"].errors)
        self.assertFalse(
            User.objects.filter(email="malformed-role@example.com").exists()
        )

    def test_malformed_role_edit_is_controlled_and_writes_nothing(self):
        original_name = self.cashier_a.full_name
        original_role = self.cashier_membership.role
        payload = self.edit_payload(
            self.cashier_membership,
            full_name="Must Not Persist",
        )
        payload["role"] = "not-an-integer"

        response = self.client.post(
            reverse(
                "accounts:user_edit",
                args=[self.cashier_membership.public_id],
            ),
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("role", response.context["form"].errors)
        self.cashier_a.refresh_from_db()
        self.cashier_membership.refresh_from_db()
        self.assertEqual(self.cashier_a.full_name, original_name)
        self.assertEqual(self.cashier_membership.role, original_role)

    def test_employee_create_rejects_an_existing_global_user(self):
        original_password = self.owner_b.password

        response = self.client.post(
            reverse("accounts:user_create"),
            self.create_payload(email=f"  {self.owner_b.email.upper()}  "),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertFalse(
            Membership.objects.filter(
                business=self.business_a,
                user=self.owner_b,
            ).exists()
        )
        self.owner_b.refresh_from_db()
        self.assertEqual(self.owner_b.password, original_password)

    def test_non_self_admin_cannot_change_owner_identity_or_password(self):
        owner_membership = Membership.objects.get(
            business=self.business_a,
            user=self.owner_a,
        )
        original = {
            "full_name": self.owner_a.full_name,
            "email": self.owner_a.email,
            "phone": self.owner_a.phone,
            "password": self.owner_a.password,
            "branches": set(owner_membership.branches.values_list("pk", flat=True)),
        }

        response = self.client.post(
            reverse("accounts:user_edit", args=[owner_membership.public_id]),
            self.edit_payload(
                owner_membership,
                full_name="Compromised Owner",
                email="compromised-owner@example.com",
                phone="99999999",
                password="AttackerChosen123!",
                branches=[self.branch_a.pk],
            ),
        )

        self.assertEqual(response.status_code, 200)
        for field in ("full_name", "email", "phone", "password"):
            self.assertIn(field, response.context["form"].errors)
        self.owner_a.refresh_from_db()
        owner_membership.refresh_from_db()
        self.assertEqual(self.owner_a.full_name, original["full_name"])
        self.assertEqual(self.owner_a.email, original["email"])
        self.assertEqual(self.owner_a.phone, original["phone"])
        self.assertEqual(self.owner_a.password, original["password"])
        self.assertEqual(
            set(owner_membership.branches.values_list("pk", flat=True)),
            original["branches"],
        )

    def test_shared_identity_is_protected_but_membership_fields_can_change(self):
        shared_user = User.objects.create_user(
            email="shared-worker@example.com",
            password=self.password,
            full_name="Shared Worker",
            phone="11111111",
        )
        membership_a = Membership.objects.create(
            business=self.business_a,
            user=shared_user,
            role=self.cashier_role_a,
        )
        Membership.objects.create(
            business=self.business_b,
            user=shared_user,
            role=Role.objects.for_business(self.business_b).get(name="Cashier"),
        )
        salesperson = Role.objects.for_business(self.business_a).get(
            name="Salesperson"
        )
        original_password = shared_user.password

        rejected = self.client.post(
            reverse("accounts:user_edit", args=[membership_a.public_id]),
            self.edit_payload(
                membership_a,
                full_name="Hijacked Shared Worker",
                email="hijacked-shared@example.com",
                password="AttackerChosen123!",
                role=salesperson.pk,
                branches=[self.branch_a.pk],
                is_active="",
            ),
        )

        self.assertEqual(rejected.status_code, 200)
        shared_user.refresh_from_db()
        membership_a.refresh_from_db()
        self.assertEqual(shared_user.full_name, "Shared Worker")
        self.assertEqual(shared_user.email, "shared-worker@example.com")
        self.assertEqual(shared_user.password, original_password)
        self.assertEqual(membership_a.role, self.cashier_role_a)
        self.assertTrue(membership_a.is_active)
        self.assertFalse(membership_a.branches.exists())

        allowed = self.client.post(
            reverse("accounts:user_edit", args=[membership_a.public_id]),
            self.edit_payload(
                membership_a,
                role=salesperson.pk,
                branches=[self.branch_a.pk],
                is_active="",
            ),
        )

        self.assertRedirects(allowed, reverse("accounts:user_list"))
        shared_user.refresh_from_db()
        membership_a.refresh_from_db()
        self.assertEqual(shared_user.full_name, "Shared Worker")
        self.assertEqual(shared_user.email, "shared-worker@example.com")
        self.assertEqual(shared_user.password, original_password)
        self.assertEqual(membership_a.role, salesperson)
        self.assertFalse(membership_a.is_active)
        self.assertEqual(
            set(membership_a.branches.values_list("pk", flat=True)),
            {self.branch_a.pk},
        )

    def test_confirmed_email_integrity_race_becomes_a_form_error(self):
        conflicting_user = User.objects.create_user(
            email="raced-email@example.com",
            password=self.password,
            full_name="Concurrent Account",
        )

        with patch.object(
            EmployeeForm,
            "clean_email",
            return_value=conflicting_user.email,
        ):
            response = self.client.post(
                reverse("accounts:user_create"),
                self.create_payload(email=conflicting_user.email),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertFalse(
            Membership.objects.filter(
                business=self.business_a,
                user=conflicting_user,
            ).exists()
        )

    def test_unrelated_user_integrity_error_is_reraised(self):
        with patch(
            "apps.accounts.views.User.objects.create_user",
            side_effect=IntegrityError("unrelated user constraint"),
        ):
            with self.assertRaises(IntegrityError):
                self.client.post(
                    reverse("accounts:user_create"),
                    self.create_payload(email="unrelated-user-error@example.com"),
                )

    def test_membership_integrity_error_is_not_misclassified(self):
        with patch(
            "apps.accounts.views.Membership.objects.create",
            side_effect=IntegrityError("unrelated membership constraint"),
        ):
            with self.assertRaises(IntegrityError):
                self.client.post(
                    reverse("accounts:user_create"),
                    self.create_payload(email="membership-error@example.com"),
                )
        self.assertFalse(
            User.objects.filter(email="membership-error@example.com").exists()
        )

    def test_employee_email_edit_race_is_controlled_without_membership_writes(self):
        conflicting_user = User.objects.create_user(
            email="edit-race@example.com",
            password=self.password,
            full_name="Concurrent Edit Winner",
        )
        original_name = self.cashier_a.full_name
        original_role = self.cashier_membership.role

        with patch.object(
            EmployeeForm,
            "clean_email",
            return_value=conflicting_user.email,
        ):
            response = self.client.post(
                reverse(
                    "accounts:user_edit",
                    args=[self.cashier_membership.public_id],
                ),
                self.edit_payload(
                    self.cashier_membership,
                    full_name="Must Roll Back",
                    email=conflicting_user.email,
                    role=Role.objects.for_business(self.business_a)
                    .get(name="Salesperson")
                    .pk,
                    is_active="",
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.cashier_a.refresh_from_db()
        self.cashier_membership.refresh_from_db()
        self.assertEqual(self.cashier_a.full_name, original_name)
        self.assertEqual(self.cashier_a.email, "cashier-a@example.com")
        self.assertEqual(self.cashier_membership.role, original_role)
        self.assertTrue(self.cashier_membership.is_active)

    def test_unrelated_employee_edit_integrity_error_is_reraised(self):
        with (
            patch.object(
                User,
                "save",
                side_effect=IntegrityError("unrelated edit constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated edit constraint",
            ),
        ):
            self.client.post(
                reverse(
                    "accounts:user_edit",
                    args=[self.cashier_membership.public_id],
                ),
                self.edit_payload(
                    self.cashier_membership,
                    full_name="Unique Edit",
                ),
            )
