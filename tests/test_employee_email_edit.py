"""Regression tests for employee login-email editing."""

from django.urls import reverse

from apps.accounts.models import Membership, Role

from .base import TenantTestCase


class EmployeeEmailEditTests(TenantTestCase):
    password = "EmployeePass123!"

    def setUp(self):
        plan = self.business_a.subscription.plan
        plan.max_users = 10
        plan.save(update_fields=["max_users"])
        self.client.force_login(self.owner_a)
        self.role = Role.objects.for_business(self.business_a).get(name="Cashier")
        response = self.client.post(
            reverse("accounts:user_create"),
            {
                "full_name": "Osama",
                "email": "osama@nexa.com",
                "phone": "12345678",
                "password": self.password,
                "role": self.role.pk,
                "branches": [self.branch_a.pk],
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.membership = Membership.objects.get(
            business=self.business_a,
            user__email="osama@nexa.com",
        )
        self.user = self.membership.user

    def edit_payload(self, **overrides):
        payload = {
            "full_name": self.user.full_name,
            "email": self.user.email,
            "phone": self.user.phone,
            "password": "",
            "role": self.membership.role_id,
            "branches": list(self.membership.branches.values_list("pk", flat=True)),
            "is_active": "on",
        }
        payload.update(overrides)
        return payload

    def test_email_edit_updates_login_and_preserves_linked_account_data(self):
        original_pk = self.user.pk
        original_public_id = self.user.public_id
        original_password = self.user.password
        original_user_count = type(self.user).objects.count()
        original_membership_pk = self.membership.pk
        original_role_id = self.membership.role_id
        original_branch_ids = set(
            self.membership.branches.values_list("pk", flat=True)
        )

        response = self.client.post(
            reverse("accounts:user_edit", args=[self.membership.public_id]),
            self.edit_payload(email="  OSAMA@SHAMOUKH.COM  "),
        )

        self.assertRedirects(response, reverse("accounts:user_list"))
        self.user.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual(self.user.email, "osama@shamoukh.com")
        self.assertEqual(self.user.pk, original_pk)
        self.assertEqual(self.user.public_id, original_public_id)
        self.assertEqual(self.user.password, original_password)
        self.assertEqual(type(self.user).objects.count(), original_user_count)
        self.assertEqual(self.membership.pk, original_membership_pk)
        self.assertEqual(self.membership.user_id, original_pk)
        self.assertEqual(self.membership.role_id, original_role_id)
        self.assertEqual(
            set(self.membership.branches.values_list("pk", flat=True)),
            original_branch_ids,
        )
        self.assertTrue(self.membership.is_active)

        edit_page = self.client.get(
            reverse("accounts:user_edit", args=[self.membership.public_id])
        )
        self.assertEqual(edit_page.status_code, 200)
        self.assertEqual(
            edit_page.context["form"]["email"].value(),
            "osama@shamoukh.com",
        )
        self.assertContains(edit_page, "osama@shamoukh.com")

        self.client.logout()
        old_login = self.client.post(
            reverse("accounts:login"),
            {"email": "osama@nexa.com", "password": self.password},
        )
        self.assertEqual(old_login.status_code, 200)
        self.assertContains(old_login, "Invalid email or password.")
        self.assertNotIn("_auth_user_id", self.client.session)

        new_login = self.client.post(
            reverse("accounts:login"),
            {"email": "osama@shamoukh.com", "password": self.password},
        )
        self.assertEqual(new_login.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), original_pk)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, original_password)

    def test_duplicate_email_is_case_insensitive_and_update_is_atomic(self):
        original_email = self.user.email
        original_name = self.user.full_name
        original_phone = self.user.phone
        original_password = self.user.password
        original_role_id = self.membership.role_id
        original_branch_ids = set(
            self.membership.branches.values_list("pk", flat=True)
        )

        response = self.client.post(
            reverse("accounts:user_edit", args=[self.membership.public_id]),
            self.edit_payload(
                email=self.owner_b.email.upper(),
                full_name="Should Not Persist",
                phone="99999999",
                role=Role.objects.for_business(self.business_a)
                .get(name="Salesperson")
                .pk,
                branches=[],
                is_active="",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "An account with this email already exists.")
        self.assertNotContains(response, self.business_b.name)
        self.user.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual(self.user.email, original_email)
        self.assertEqual(self.user.full_name, original_name)
        self.assertEqual(self.user.phone, original_phone)
        self.assertEqual(self.user.password, original_password)
        self.assertEqual(self.membership.role_id, original_role_id)
        self.assertEqual(
            set(self.membership.branches.values_list("pk", flat=True)),
            original_branch_ids,
        )
        self.assertTrue(self.membership.is_active)

    def test_other_business_owner_cannot_edit_employee(self):
        original_email = self.user.email
        original_password = self.user.password
        self.client.force_login(self.owner_b)

        response = self.client.post(
            reverse("accounts:user_edit", args=[self.membership.public_id]),
            self.edit_payload(email="intruder@example.com"),
        )

        self.assertEqual(response.status_code, 404)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, original_email)
        self.assertEqual(self.user.password, original_password)
