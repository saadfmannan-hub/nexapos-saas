"""Regression coverage for the confirmed NexaPOS UAT polish batch."""
from decimal import Decimal

from django import forms
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.branches.models import Warehouse
from apps.catalog.models import Product, ProductVariant
from apps.customers import services as customer_services
from apps.customers.models import Customer
from apps.inventory import services as inventory_services
from apps.subscriptions import services as subscription_services
from apps.tenants.forms import BusinessSettingsForm

from .base import TenantTestCase


class UserSectionsAndSeatLimitTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.cashier_role = self.business_a.roles.get(name="Cashier")

    def make_inactive_member(self, *, email="inactive@example.com", name="Inactive User"):
        user = User.objects.create_user(
            email=email,
            password="StrongPass123!",
            full_name=name,
        )
        return Membership.objects.create(
            business=self.business_a,
            user=user,
            role=self.cashier_role,
            is_active=False,
        )

    def test_active_and_inactive_members_are_separate_ordered_and_tenant_scoped(self):
        inactive = self.make_inactive_member()

        response = self.client.get(reverse("accounts:user_list"))

        self.assertEqual(response.status_code, 200)
        active = list(response.context["active_memberships"])
        inactive_memberships = list(response.context["inactive_memberships"])
        self.assertIn(self.membership_a(), active)
        self.assertIn(self.cashier_membership, active)
        self.assertNotIn(inactive, active)
        self.assertEqual(inactive_memberships, [inactive])
        self.assertNotIn(self.business_b.memberships.get(user=self.owner_b), active)
        self.assertNotIn(
            self.business_b.memberships.get(user=self.owner_b),
            inactive_memberships,
        )
        self.assertContains(response, "Active Team Members (2)")
        self.assertContains(response, "Inactive Team Members (1)")
        self.assertContains(
            response,
            reverse("accounts:user_edit", args=[inactive.public_id]),
        )

    def test_empty_state_copy_is_rendered_by_shared_table(self):
        active_html = render_to_string(
            "accounts/_membership_table.html",
            {"memberships": [], "empty_message": "No active team members."},
        )
        inactive_html = render_to_string(
            "accounts/_membership_table.html",
            {"memberships": [], "empty_message": "No inactive team members."},
        )

        self.assertIn("No active team members.", active_html)
        self.assertIn("No inactive team members.", inactive_html)

    def test_inactive_members_do_not_use_plan_seats_and_can_be_created_at_limit(self):
        plan = self.business_a.subscription.plan
        plan.max_users = 2
        plan.save(update_fields=["max_users"])

        response = self.client.post(
            reverse("accounts:user_create"),
            {
                "full_name": "Inactive At Limit",
                "email": "inactive-at-limit@example.com",
                "phone": "",
                "password": "StrongPass123!",
                "role": self.cashier_role.pk,
            },
        )

        self.assertRedirects(response, reverse("accounts:user_list"))
        membership = Membership.objects.get(
            business=self.business_a,
            user__email="inactive-at-limit@example.com",
        )
        self.assertFalse(membership.is_active)
        current, limit, allowed = subscription_services.limit_state(
            self.business_a, "users"
        )
        self.assertEqual((current, limit, allowed), (2, 2, False))

    def test_reactivation_cannot_bypass_plan_user_limit(self):
        inactive = self.make_inactive_member(email="reactivate@example.com")
        plan = self.business_a.subscription.plan
        plan.max_users = 2
        plan.save(update_fields=["max_users"])

        response = self.client.post(
            reverse("accounts:user_edit", args=[inactive.public_id]),
            {
                "full_name": inactive.user.full_name,
                "email": inactive.user.email,
                "phone": "",
                "password": "",
                "role": self.cashier_role.pk,
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Users limit reached")
        inactive.refresh_from_db()
        self.assertFalse(inactive.is_active)


class CurrentStockActiveItemTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.stock_url = reverse("inventory:stock_list")
        self.stock_context = {
            "branch": self.branch_a.pk,
            "warehouse": self.warehouse_a.pk,
        }

    def stock_response(self, **filters):
        return self.client.get(self.stock_url, {**self.stock_context, **filters})

    def make_variant_stock(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Variant Stock Product",
            sku="VAR-STOCK",
            product_type=Product.Type.VARIANT,
            purchase_price=Decimal("3.000"),
            average_cost=Decimal("3.000"),
        )
        active = ProductVariant.objects.create(
            business=self.business_a,
            product=product,
            name="Active Color",
            sku="VAR-ACTIVE",
            purchase_price=Decimal("3.000"),
            average_cost=Decimal("3.000"),
        )
        inactive = ProductVariant.objects.create(
            business=self.business_a,
            product=product,
            name="Dormant Color",
            sku="VAR-DORMANT",
            purchase_price=Decimal("5.000"),
            average_cost=Decimal("5.000"),
            is_active=False,
        )
        for variant in (active, inactive):
            inventory_services.set_opening_stock(
                business=self.business_a,
                warehouse=self.warehouse_a,
                product=product,
                variant=variant,
                quantity=Decimal("10"),
                unit_cost=variant.purchase_price,
                user=self.owner_a,
            )
        return product, active, inactive

    def test_inactive_product_is_hidden_from_list_search_and_operational_value(self):
        self.product_a.is_active = False
        self.product_a.save(update_fields=["is_active"])

        response = self.stock_response(q="Widget A")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["page_obj"]), [])
        self.assertEqual(response.context["total_value"], Decimal("0.00"))
        self.assertEqual(
            self.client.get(
                reverse("inventory:item_search"), {"q": self.product_a.sku}
            ).json()["results"],
            [],
        )
        export_data = inventory_services.inventory_export_dataset(
            self.business_a,
            {"warehouse_id": self.warehouse_a.pk},
            allowed_warehouse_ids=[self.warehouse_a.pk],
        )
        self.assertEqual(export_data["rows"], [])
        self.assertEqual(
            inventory_services.stock_value(
                self.business_a,
                warehouse=self.warehouse_a,
                active_only=False,
            ),
            Decimal("400.00"),
        )

    def test_inactive_variant_is_hidden_but_active_and_reactivated_items_show(self):
        _product, active, inactive = self.make_variant_stock()

        response = self.stock_response()
        self.assertContains(response, active.name)
        self.assertNotContains(response, inactive.name)
        self.assertEqual(response.context["total_value"], Decimal("430.00"))
        self.assertNotContains(self.stock_response(q=inactive.sku), inactive.name)
        self.assertEqual(
            self.client.get(
                reverse("inventory:item_search"), {"q": inactive.sku}
            ).json()["results"],
            [],
        )

        inactive.is_active = True
        inactive.save(update_fields=["is_active"])

        self.assertContains(self.stock_response(q=inactive.sku), inactive.name)

    def test_reactivated_product_returns_without_losing_movement_history(self):
        self.product_a.is_active = False
        self.product_a.is_archived = True
        self.product_a.save(update_fields=["is_active", "is_archived"])

        movement_response = self.client.get(
            reverse("inventory:movement_list"), {"q": self.product_a.name}
        )
        self.assertContains(movement_response, self.product_a.name)
        self.assertNotContains(self.stock_response(), self.product_a.name)

        self.product_a.is_active = True
        self.product_a.is_archived = False
        self.product_a.save(update_fields=["is_active", "is_archived"])

        response = self.stock_response()
        self.assertContains(response, self.product_a.name)
        self.assertEqual(response.context["total_value"], Decimal("400.00"))
        self.assertNotContains(response, self.product_b.name)


class ProductDeleteRedirectTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def test_blocked_delete_redirects_to_scoped_detail_with_message(self):
        response = self.client.post(
            reverse("catalog:product_delete", args=[self.product_a.public_id]),
            {
                "branch": self.branch_a.pk,
                "warehouse": self.warehouse_a.pk,
            },
            follow=True,
        )

        expected = (
            reverse("catalog:product_detail", args=[self.product_a.public_id])
            + f"?branch={self.branch_a.pk}&warehouse={self.warehouse_a.pk}"
        )
        self.assertRedirects(response, expected)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Product.objects.filter(pk=self.product_a.pk).exists())

    def test_blocked_delete_falls_back_to_product_list_without_warehouse_context(self):
        Warehouse.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            name="Second Warehouse",
            code="SECOND",
        )

        response = self.client.post(
            reverse("catalog:product_delete", args=[self.product_a.public_id])
        )

        self.assertRedirects(
            response,
            reverse("catalog:product_list") + f"?branch={self.branch_a.pk}",
        )

    def test_unused_product_deletion_and_cross_tenant_protection_are_unchanged(self):
        unused = Product.objects.create(
            business=self.business_a,
            name="Unused UAT Product",
            sku="UNUSED-UAT",
            track_inventory=False,
            product_type=Product.Type.NON_STOCK,
        )

        response = self.client.post(
            reverse("catalog:product_delete", args=[unused.public_id]),
            {
                "branch": self.branch_a.pk,
                "warehouse": self.warehouse_a.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Product.objects.filter(pk=unused.pk).exists())

        response = self.client.post(
            reverse("catalog:product_delete", args=[self.product_b.public_id]),
            {
                "branch": self.branch_a.pk,
                "warehouse": self.warehouse_a.pk,
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Product.objects.filter(pk=self.product_b.pk).exists())


class OnboardingBannerDismissalTests(TenantTestCase):
    def setUp(self):
        self.business_a.onboarding_completed = False
        self.business_a.onboarding_banner_dismissed = False
        self.business_a.save(
            update_fields=[
                "onboarding_completed",
                "onboarding_banner_dismissed",
            ]
        )

    def test_authorized_dismissal_persists_without_completing_onboarding(self):
        self.client.force_login(self.owner_a)
        before = self.client.get(reverse("dashboard"))
        self.assertContains(before, "Finish setting up your business")

        response = self.client.post(reverse("tenants:dismiss_onboarding_banner"))
        self.assertRedirects(response, reverse("dashboard"))
        self.business_a.refresh_from_db()
        self.assertTrue(self.business_a.onboarding_banner_dismissed)
        self.assertFalse(self.business_a.onboarding_completed)
        self.business_b.refresh_from_db()
        self.assertFalse(self.business_b.onboarding_banner_dismissed)

        after = self.client.get(reverse("dashboard"))
        self.assertNotContains(after, "Finish setting up your business")
        self.assertEqual(
            self.client.get(reverse("tenants:onboarding")).status_code,
            200,
        )

    def test_unauthorized_member_cannot_dismiss_business_banner(self):
        self.client.force_login(self.cashier_a)

        response = self.client.post(reverse("tenants:dismiss_onboarding_banner"))

        self.assertEqual(response.status_code, 403)
        self.business_a.refresh_from_db()
        self.assertFalse(self.business_a.onboarding_banner_dismissed)

    def test_completed_onboarding_hides_banner_without_dismissal(self):
        self.business_a.onboarding_completed = True
        self.business_a.save(update_fields=["onboarding_completed"])
        self.client.force_login(self.owner_a)

        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, "Finish setting up your business")


class CustomCustomerFieldExpansionTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def settings_post_data(self, settings_obj):
        form = BusinessSettingsForm(instance=settings_obj)
        data = {}
        for name, field in form.fields.items():
            value = getattr(settings_obj, name)
            if isinstance(field, forms.BooleanField):
                if value:
                    data[name] = "on"
            elif hasattr(value, "pk"):
                data[name] = value.pk
            else:
                data[name] = "" if value is None else str(value)
        return data

    def configure_expanded_labels(self):
        settings_obj = self.business_a.settings
        settings_obj.more_option_label_1 = "Original Field"
        settings_obj.more_option_label_16 = "Field Sixteen"
        settings_obj.more_option_label_20 = "Field Twenty"
        settings_obj.save(
            update_fields=[
                "more_option_label_1",
                "more_option_label_16",
                "more_option_label_20",
            ]
        )
        return settings_obj

    def test_settings_layout_renders_twenty_fields_and_saves_new_labels(self):
        settings_obj = self.business_a.settings
        for index in range(1, 16):
            setattr(settings_obj, f"more_option_label_{index}", f"Existing {index}")
        settings_obj.save(
            update_fields=[
                f"more_option_label_{index}" for index in range(1, 16)
            ]
        )
        response = self.client.get(reverse("tenants:settings"))
        self.assertContains(response, "Custom Customer Fields")
        self.assertNotContains(response, "More Options Fields")
        for index in range(1, 21):
            self.assertContains(
                response,
                f'name="more_option_label_{index}"',
            )
        html = response.content.decode()
        self.assertLess(
            html.index("Stock &amp; sales policies"),
            html.index("Approvals &amp; alerts"),
        )
        self.assertLess(
            html.index("Approvals &amp; alerts"),
            html.index("Custom Customer Fields"),
        )

        data = self.settings_post_data(settings_obj)
        for index in range(16, 21):
            data[f"more_option_label_{index}"] = f"New {index}"
        response = self.client.post(reverse("tenants:settings"), data)

        self.assertRedirects(response, reverse("tenants:settings"))
        settings_obj.refresh_from_db()
        for index in range(1, 16):
            self.assertEqual(
                getattr(settings_obj, f"more_option_label_{index}"),
                f"Existing {index}",
            )
        for index in range(16, 21):
            self.assertEqual(
                getattr(settings_obj, f"more_option_label_{index}"),
                f"New {index}",
            )
        self.business_b.settings.refresh_from_db()
        self.assertEqual(self.business_b.settings.more_option_label_20, "")

    def test_customer_create_edit_detail_pos_and_job_card_support_field_twenty(self):
        self.configure_expanded_labels()
        response = self.client.post(
            reverse("customers:create"),
            {
                "home_branch": self.branch_a.pk,
                "full_name": "Expanded Fields Customer",
                "code": "EXP-20",
                "mobile": "99002020",
                "whatsapp": "",
                "email": "",
                "address": "",
                "city": "",
                "country": "",
                "group": "",
                "tax_number": "",
                "credit_limit": "0",
                "notes": "",
                "is_active": "on",
                "more_option_1": "Original Value",
                "more_option_16": "Sixteen Value",
                "more_option_20": "Twenty Value",
            },
        )
        customer = Customer.objects.get(
            business=self.business_a, code="EXP-20"
        )
        self.assertRedirects(
            response,
            reverse("customers:detail", args=[customer.public_id]),
        )
        self.assertEqual(
            customer.more_options,
            {
                "1": "Original Value",
                "16": "Sixteen Value",
                "20": "Twenty Value",
            },
        )

        detail = self.client.get(
            reverse("customers:detail", args=[customer.public_id])
        )
        self.assertContains(detail, "Field Twenty")
        self.assertContains(detail, "Twenty Value")

        pos_result = self.client.get(
            reverse("sales:pos_customers"),
            {"branch_id": self.branch_a.pk, "q": "EXP-20"},
        ).json()["results"][0]
        self.assertIn(
            {"label": "Field Twenty", "value": "Twenty Value"},
            pos_result["more_options"],
        )

        response = self.client.post(
            reverse("customers:edit", args=[customer.public_id]),
            {
                "home_branch": self.branch_a.pk,
                "full_name": customer.full_name,
                "code": customer.code,
                "mobile": customer.mobile,
                "whatsapp": "",
                "email": "",
                "address": "",
                "city": "",
                "country": "",
                "group": "",
                "tax_number": "",
                "credit_limit": "0",
                "notes": "",
                "is_active": "on",
                "more_option_1": "Original Value",
                "more_option_16": "Sixteen Value",
                "more_option_20": "Edited Twenty",
            },
        )
        self.assertRedirects(
            response,
            reverse("customers:detail", args=[customer.public_id]),
        )
        customer.refresh_from_db()
        self.assertEqual(customer.more_options["20"], "Edited Twenty")

        self.allow_no_shift()
        sale = self.make_sale(customer=customer)
        request = RequestFactory().get("/")
        request.business = self.business_a
        from apps.sales.views import _job_card_data

        card = _job_card_data(sale, request, list(sale.items.all()))
        self.assertIn(
            {"label": "Field Twenty", "value": "Edited Twenty"},
            card["more_options"],
        )
        job_card_html = render_to_string(
            "invoices/workshop_job_card.html",
            {"job_cards": [card]},
        )
        self.assertIn("Field Twenty", job_card_html)
        self.assertIn("Edited Twenty", job_card_html)

    def test_import_export_supports_new_fields_and_legacy_files(self):
        self.configure_expanded_labels()
        rows = [
            {
                "Branch Code": self.branch_a.code,
                "Branch Name": self.branch_a.name,
                "Customer Code": "IMP-20",
                "Customer Name": "Imported Twenty",
                "Field Sixteen": "Imported 16",
                "Field Twenty": "Imported 20",
            },
            {
                "Branch Code": self.branch_a.code,
                "Branch Name": self.branch_a.name,
                "Customer Code": "IMP-LEGACY",
                "Customer Name": "Legacy Import",
            },
        ]

        summary, errors = customer_services.import_customers(
            business=self.business_a,
            branch=self.branch_a,
            rows=rows,
            mode="skip",
            user=self.owner_a,
            membership=self.membership_a(),
        )

        self.assertEqual(errors, [])
        self.assertEqual(summary["imported"], 2)
        imported = Customer.objects.get(
            business=self.business_a, code="IMP-20"
        )
        legacy = Customer.objects.get(
            business=self.business_a, code="IMP-LEGACY"
        )
        self.assertEqual(
            imported.more_options,
            {"16": "Imported 16", "20": "Imported 20"},
        )
        self.assertEqual(legacy.more_options, {})

        dataset = customer_services.export_dataset(
            self.business_a,
            Customer.objects.for_business(self.business_a).filter(
                code__in=["IMP-20", "IMP-LEGACY"]
            ),
        )
        self.assertIn("Field Sixteen", dataset["columns"])
        self.assertIn("Field Twenty", dataset["columns"])
        self.assertNotIn("", dataset["columns"])
        code_index = dataset["columns"].index("Customer Code")
        field_twenty_index = dataset["columns"].index("Field Twenty")
        rows_by_code = {row[code_index]: row for row in dataset["rows"]}
        self.assertEqual(
            rows_by_code["IMP-20"][field_twenty_index],
            "Imported 20",
        )
        self.assertEqual(rows_by_code["IMP-LEGACY"][field_twenty_index], "")
