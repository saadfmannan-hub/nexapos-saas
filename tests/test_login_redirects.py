"""Production-safe, permission-aware post-login routing tests."""
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch, Warehouse
from apps.registers import services as register_services
from apps.registers.models import CashRegister, Shift

from .base import TenantTestCase


class PermissionAwareLoginRedirectTests(TenantTestCase):
    password = "StrongPass123!"

    def make_staff(self, permissions, *, email="staff@example.com", branches=None):
        role = Role.objects.create(
            business=self.business_a,
            name=f"Role for {email}",
            permissions=list(permissions),
        )
        user = User.objects.create_user(
            email=email,
            password=self.password,
            full_name="Test Staff",
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=user,
            role=role,
        )
        if branches is not None:
            membership.branches.set(branches)
        return user, membership

    def login(self, user, *, next_url=None, follow=False):
        url = reverse("accounts:login")
        if next_url is not None:
            url = f"{url}?next={next_url}"
        return self.client.post(
            url,
            {"email": user.email, "password": self.password},
            follow=follow,
        )

    def test_owner_with_dashboard_permission_redirects_to_dashboard(self):
        response = self.login(self.owner_a)
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_staff_without_dashboard_permission_does_not_redirect_to_dashboard(self):
        user, _membership = self.make_staff(["sales.view"])
        response = self.login(user)
        self.assertRedirects(response, reverse("sales:list"), fetch_redirect_response=False)

    def test_staff_with_pos_permission_can_land_on_pos_without_required_shift(self):
        self.allow_no_shift()
        user, _membership = self.make_staff(["sales.create"])
        response = self.login(user)
        self.assertRedirects(response, reverse("sales:pos"), fetch_redirect_response=False)

    def test_cashier_with_open_shift_redirects_to_pos(self):
        user, _membership = self.make_staff(
            ["sales.create", "shifts.open"], branches=[self.branch_a]
        )
        register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=user,
            opening_cash=0,
        )
        response = self.login(user)
        self.assertRedirects(response, reverse("sales:pos"), fetch_redirect_response=False)

    def test_cashier_without_open_shift_redirects_to_open_shift_screen(self):
        user, _membership = self.make_staff(
            ["sales.create", "shifts.open"], branches=[self.branch_a]
        )
        response = self.login(user)
        self.assertRedirects(
            response, reverse("registers:shift_list"), fetch_redirect_response=False
        )

    def test_open_shift_on_unassigned_branch_does_not_unlock_pos(self):
        other_branch = Branch.objects.create(
            business=self.business_a, name="Other Branch", code="OTHER"
        )
        Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Other Warehouse",
            code="OTHER-WH",
            is_default=True,
        )
        other_register = CashRegister.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Other Register",
            code="OTHER-REG",
        )
        user, _membership = self.make_staff(
            ["sales.create", "shifts.open"], branches=[self.branch_a]
        )
        # Seed an intentionally out-of-scope historical row directly.  The
        # guarded service now correctly refuses to create this state.
        Shift.objects.create(
            business=self.business_a,
            register=other_register,
            cashier=user,
            branch=other_branch,
            opening_cash=0,
            opened_at=timezone.now(),
        )

        response = self.login(user)

        self.assertRedirects(
            response, reverse("registers:shift_list"), fetch_redirect_response=False
        )

        self.client.force_login(user)
        pos_response = self.client.get(reverse("sales:pos"))
        self.assertEqual(pos_response.status_code, 200)
        self.assertIsNone(pos_response.context["shift"])
        self.assertEqual(pos_response.context["branch"], self.branch_a)

    def test_customer_only_user_redirects_to_customers(self):
        user, _membership = self.make_staff(["customers.view"])
        response = self.login(user)
        self.assertRedirects(response, reverse("customers:list"), fetch_redirect_response=False)

    def test_user_without_usable_permissions_gets_no_access_page(self):
        user, _membership = self.make_staff([])
        response = self.login(user, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("accounts:no_access"))
        self.assertContains(response, "no module access has been assigned")
        self.assertContains(response, "Sign out")
        self.assertNotContains(response, "Role for staff@example.com")

    def test_inactive_user_is_denied(self):
        user, _membership = self.make_staff(["customers.view"])
        user.is_active = False
        user.save(update_fields=["is_active"])

        response = self.login(user)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(response, "Invalid email or password")

    def test_inactive_business_redirects_to_no_business_state(self):
        self.business_a.is_active = False
        self.business_a.save(update_fields=["is_active"])

        response = self.login(self.owner_a)

        self.assertRedirects(
            response, reverse("tenants:no_business"), fetch_redirect_response=False
        )

    def test_external_next_url_is_rejected(self):
        response = self.login(self.owner_a, next_url="https://evil.example/phish")
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_unauthorized_internal_next_url_is_rejected(self):
        user, _membership = self.make_staff(["customers.view"])
        response = self.login(user, next_url=reverse("dashboard"))
        self.assertRedirects(
            response, reverse("customers:list"), fetch_redirect_response=False
        )

    def test_authorized_internal_next_url_is_honored(self):
        response = self.login(
            self.owner_a, next_url=f"{reverse('customers:list')}?q=client"
        )
        self.assertRedirects(
            response, reverse("customers:list"), fetch_redirect_response=False
        )

    def test_cross_tenant_deep_next_url_is_rejected(self):
        cross_tenant_url = reverse(
            "catalog:product_detail", args=[self.product_b.public_id]
        )
        response = self.login(self.owner_a, next_url=cross_tenant_url)
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_direct_unauthorized_route_still_returns_403(self):
        user, _membership = self.make_staff(["customers.view"])
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_platform_superuser_login_behavior_is_unchanged(self):
        user = User.objects.create_superuser(
            email="platform@example.com",
            password=self.password,
            full_name="Platform Admin",
        )
        response = self.login(user)
        self.assertRedirects(
            response,
            reverse("platformadmin:dashboard"),
            fetch_redirect_response=False,
        )

    def test_root_and_authenticated_login_use_same_resolver(self):
        user, _membership = self.make_staff(["customers.view"])
        self.client.force_login(user)
        for route_name in ("home", "accounts:login"):
            response = self.client.get(reverse(route_name))
            self.assertRedirects(
                response, reverse("customers:list"), fetch_redirect_response=False
            )

    def test_no_access_fallback_has_no_redirect_loop(self):
        user, _membership = self.make_staff([])
        self.client.force_login(user)
        response = self.client.get(reverse("home"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("accounts:no_access"))
        self.assertLessEqual(len(response.redirect_chain), 2)

    def test_switch_business_uses_destination_for_selected_membership(self):
        user, membership_a = self.make_staff(["dashboard.view"])
        role_b = Role.objects.create(
            business=self.business_b,
            name="Customer Viewer",
            permissions=["customers.view"],
        )
        Membership.objects.create(
            business=self.business_b,
            user=user,
            role=role_b,
        )
        self.client.force_login(user)
        session = self.client.session
        session["active_business_id"] = membership_a.business_id
        session.save()

        response = self.client.post(
            reverse("tenants:switch_business"),
            {"business_id": self.business_b.id},
        )

        self.assertRedirects(
            response, reverse("customers:list"), fetch_redirect_response=False
        )
