import json
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.audit.models import AuditLog
from apps.branches.models import Branch
from apps.catalog.models import Product
from apps.customers import services as customer_services
from apps.customers.forms import CustomerForm
from apps.customers.models import Customer, CustomerFamilyMember
from apps.sales import services as sales_services
from apps.sales.models import SaleItem
from apps.sales.views import _job_card_data
from apps.subscriptions.exceptions import ModuleAccessDenied

from .base import TenantTestCase


class CustomerFamilyPhase2Tests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.client.force_login(self.owner_a)
        settings_obj = self.business_a.settings
        settings_obj.more_option_label_1 = "Chest"
        settings_obj.more_option_label_2 = "Length"
        settings_obj.save(
            update_fields=["more_option_label_1", "more_option_label_2"]
        )
        self.customer = Customer.objects.create(
            business=self.business_a,
            home_branch=self.branch_a,
            code="FAM-001",
            full_name="Main Customer",
            mobile="99001122",
            more_options={"1": "41", "2": "56"},
        )
        self.other_customer = Customer.objects.create(
            business=self.business_a,
            home_branch=self.branch_a,
            code="FAM-002",
            full_name="Other Customer",
        )
        self.customer_b = Customer.objects.create(
            business=self.business_b,
            home_branch=self.branch_b,
            code="FAM-B-001",
            full_name="Tenant B Customer",
        )
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item", "updated_at"])

    def create_family(
        self,
        *,
        customer=None,
        business=None,
        user=None,
        membership=None,
        name="Ahmed",
        relation=CustomerFamilyMember.Relation.SON,
        more_options=None,
    ):
        business = business or self.business_a
        user = user or self.owner_a
        membership = membership or business.memberships.get(user=user)
        return customer_services.create_customer_family_member(
            business=business,
            customer=customer or self.customer,
            name=name,
            relation=relation,
            more_options=(
                {"1": "30", "2": "48"}
                if more_options is None
                else more_options
            ),
            user=user,
            membership=membership,
        )

    def tailoring_line(self, family_member=None):
        line = {
            "product": self.product_a,
            "quantity": Decimal("1"),
            "unit_price": Decimal("10.000"),
            "garment_classification": "adult",
            "collection_type": "normal",
            "tailoring_details": {"design_type": "Daraz"},
        }
        if family_member is not None:
            line["family_member_id"] = str(family_member.public_id)
        return line

    def complete_tailoring_sale(self, lines, *, customer=None):
        return self.make_sale(
            items=lines,
            customer=customer or self.customer,
            delivery_date=timezone.localdate(),
        )

    def test_schema_is_minimal_relations_are_locked_and_duplicate_names_allowed(self):
        field_names = {field.name for field in CustomerFamilyMember._meta.fields}
        self.assertTrue(
            {"business", "public_id", "customer", "name", "relation", "more_options", "is_active"}
            .issubset(field_names)
        )
        self.assertFalse({"mobile", "phone", "balance", "credit_limit"} & field_names)
        self.assertEqual(
            {choice for choice, _label in CustomerFamilyMember.Relation.choices},
            {"son", "brother", "father"},
        )
        first = self.create_family(name="Same Name")
        second = self.create_family(name="Same Name")
        self.assertNotEqual(first.pk, second.pk)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CustomerFamilyMember.objects.create(
                    business=self.business_a,
                    customer=self.customer,
                    name="Invalid Relation",
                    relation="cousin",
                )

        disposable = Customer.objects.create(
            business=self.business_a,
            home_branch=self.branch_a,
            code="FAM-DROP",
            full_name="Disposable",
        )
        dependent = CustomerFamilyMember.objects.create(
            business=self.business_a,
            customer=disposable,
            name="Dependent",
            relation="son",
        )
        disposable.delete()
        self.assertFalse(
            CustomerFamilyMember.objects.filter(pk=dependent.pk).exists()
        )

    def test_normal_customer_create_and_edit_flow_remains_family_optional(self):
        create_response = self.client.post(
            reverse("customers:create"),
            {
                "home_branch": self.branch_a.id,
                "full_name": "Ordinary Customer",
                "code": "",
                "mobile": "99887766",
                "credit_limit": "0",
                "more_option_1": "40",
                "more_option_2": "55",
                "is_active": "on",
            },
        )
        ordinary = Customer.objects.get(
            business=self.business_a,
            full_name="Ordinary Customer",
        )
        self.assertRedirects(
            create_response,
            reverse("customers:detail", args=[ordinary.public_id]),
        )
        self.assertEqual(ordinary.mobile, "99887766")
        self.assertEqual(ordinary.more_options, {"1": "40", "2": "55"})
        self.assertFalse(ordinary.family_members.exists())

        edit_response = self.client.post(
            reverse("customers:edit", args=[ordinary.public_id]),
            {
                "home_branch": self.branch_a.id,
                "full_name": "Ordinary Customer Edited",
                "code": ordinary.code,
                "mobile": "99887766",
                "credit_limit": "0",
                "more_option_1": "42",
                "is_active": "on",
            },
        )
        self.assertEqual(edit_response.status_code, 302)
        ordinary.refresh_from_db()
        self.assertEqual(ordinary.full_name, "Ordinary Customer Edited")
        self.assertEqual(ordinary.mobile, "99887766")
        self.assertEqual(ordinary.more_options, {"1": "42"})
        self.assertFalse(ordinary.family_members.exists())

    def test_measurements_are_configured_flat_bounded_and_customer_form_is_unchanged(self):
        family = self.create_family(more_options={"1": 31, "2": " 49 "})
        self.assertEqual(family.more_options, {"1": "31", "2": "49"})
        self.assertNotIn("family_member", CustomerForm(self.business_a).fields)

        invalid_payloads = (
            {"99": "unexpected"},
            {"1": {"nested": "value"}},
            {"1": ["nested"]},
            {"1": "x" * 256},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.create_family(
                        name=f"Invalid {len(str(payload))}",
                        more_options=payload,
                    )

    def test_services_canonicalize_scope_and_write_audit_events(self):
        family = self.create_family()
        family = customer_services.update_customer_family_member(
            business=self.business_a,
            customer=self.customer,
            family_member=family,
            name="Ahmed Updated",
            relation="brother",
            more_options={"1": "33"},
            user=self.owner_a,
            membership=self.membership_a(),
        )
        customer_services.deactivate_customer_family_member(
            business=self.business_a,
            customer=self.customer,
            family_member=family,
            user=self.owner_a,
            membership=self.membership_a(),
        )
        self.assertEqual(family.name, "Ahmed Updated")
        self.assertFalse(
            CustomerFamilyMember.objects.get(pk=family.pk).is_active
        )
        self.assertEqual(
            set(
                AuditLog.objects.filter(
                    business=self.business_a,
                    object_id=str(family.public_id),
                )
                .values_list("action", flat=True)
            ),
            {
                "customer_family.created",
                "customer_family.updated",
                "customer_family.deactivated",
            },
        )

        family_b = CustomerFamilyMember.objects.create(
            business=self.business_b,
            customer=self.customer_b,
            name="Tenant B Son",
            relation="son",
        )
        with self.assertRaises(ModuleAccessDenied):
            customer_services.update_customer_family_member(
                business=self.business_a,
                customer=self.customer,
                family_member=family_b,
                name="Tampered",
                relation="son",
                more_options={},
                user=self.owner_a,
                membership=self.membership_a(),
            )

    def test_customer_pages_create_edit_list_and_deactivate_family_profiles(self):
        create_url = reverse(
            "customers:family_create", args=[self.customer.public_id]
        )
        response = self.client.post(
            create_url,
            {
                "name": "Yusuf",
                "relation": "father",
                "more_option_1": "44",
                "more_option_2": "58",
            },
        )
        self.assertRedirects(
            response,
            reverse("customers:detail", args=[self.customer.public_id]),
        )
        family = self.customer.family_members.get(name="Yusuf")
        detail = self.client.get(
            reverse("customers:detail", args=[self.customer.public_id])
        )
        self.assertContains(detail, "Family / Wearers")
        self.assertContains(detail, "Yusuf")
        self.assertContains(detail, "Chest: 44")

        edit = self.client.post(
            reverse(
                "customers:family_edit",
                args=[self.customer.public_id, family.public_id],
            ),
            {"name": "Yusuf Two", "relation": "brother", "more_option_1": "45"},
        )
        self.assertEqual(edit.status_code, 302)
        family.refresh_from_db()
        self.assertEqual(family.name, "Yusuf Two")
        self.assertEqual(family.more_options, {"1": "45"})

        deactivate = self.client.post(
            reverse(
                "customers:family_deactivate",
                args=[self.customer.public_id, family.public_id],
            )
        )
        self.assertEqual(deactivate.status_code, 302)
        family.refresh_from_db()
        self.assertFalse(family.is_active)

    def test_branch_and_tenant_scope_are_404_in_views_and_lookup(self):
        second_branch = Branch.objects.create(
            business=self.business_a,
            name="Second Family Branch",
            code="FAMILY-B2",
        )
        remote_customer = Customer.objects.create(
            business=self.business_a,
            home_branch=second_branch,
            code="FAM-REMOTE",
            full_name="Remote Customer",
        )
        remote_family = self.create_family(
            customer=remote_customer,
            name="Remote Son",
        )
        scoped_user = User.objects.create_user(
            email="family-scoped@example.com",
            password="StrongPass123!",
            full_name="Scoped Family User",
        )
        owner_role = Role.objects.for_business(self.business_a).get(is_owner=True)
        scoped_membership = Membership.objects.create(
            business=self.business_a,
            user=scoped_user,
            role=owner_role,
        )
        scoped_membership.branches.add(self.branch_a)
        self.client.force_login(scoped_user)

        urls = (
            reverse("customers:detail", args=[remote_customer.public_id]),
            reverse("customers:family_create", args=[remote_customer.public_id]),
            reverse(
                "customers:family_edit",
                args=[remote_customer.public_id, remote_family.public_id],
            ),
            reverse(
                "sales:pos_customer_family", args=[remote_customer.public_id]
            ),
            reverse(
                "sales:pos_customer_family", args=[self.customer_b.public_id]
            ),
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        with self.assertRaises(ModuleAccessDenied):
            customer_services.create_customer_family_member(
                business=self.business_a,
                customer=remote_customer,
                name="Blocked Remote Son",
                relation="son",
                more_options={},
                user=scoped_user,
                membership=scoped_membership,
            )

    def test_pos_lookup_returns_only_active_profiles_and_public_identifiers(self):
        active = self.create_family(name="Active Son")
        inactive = self.create_family(name="Inactive Brother", relation="brother")
        inactive.is_active = False
        inactive.save(update_fields=["is_active", "updated_at"])
        response = self.client.get(
            reverse("sales:pos_customer_family", args=[self.customer.public_id]),
            {"branch_id": self.branch_a.id},
        )
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["public_id"], str(active.public_id))
        self.assertNotIn("id", results[0])
        self.assertNotIn("more_options", results[0])

    def test_pos_checkout_passes_public_wearer_id_to_the_sale_item(self):
        family = self.create_family(name="POS Son")
        pos_page = self.client.get(reverse("sales:pos"))
        self.assertEqual(pos_page.status_code, 200)
        self.assertContains(pos_page, 'x-model="line.family_member_id"')
        self.assertContains(pos_page, "Main Customer")
        total = sales_services.compute_line(
            self.product_a,
            None,
            Decimal("1"),
            Decimal("10.000"),
            Decimal("0"),
            self.business_a.settings.prices_include_tax,
        )["total"]
        payload = {
            "branch_id": self.branch_a.id,
            "customer_id": self.customer.id,
            "items": [{
                "product_id": self.product_a.id,
                "variant_id": None,
                "quantity": "1",
                "unit_price": "10.000",
                "customer_supplied_fabric": False,
                "garment_classification": "adult",
                "collection_type": "normal",
                "tailoring_details": {"design_type": "Daraz"},
                "family_member_id": str(family.public_id),
            }],
            "payments": [{"method_id": self.cash_a.id, "amount": str(total)}],
            "invoice_discount": "0",
            "priority": "normal",
            "delivery_date": str(timezone.localdate()),
            "checkout_token": f"family-{uuid4().hex}",
        }
        response = self.client.post(
            reverse("sales:pos_checkout"),
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"], response.json())
        item = SaleItem.objects.get(
            sale__public_id=response.json()["sale"]["public_id"]
        )
        self.assertEqual(item.family_member_id, family.id)

    def test_family_management_requires_customers_manage_permission(self):
        family = self.create_family(name="Read Only Son")
        user = User.objects.create_user(
            email="family-viewer@example.com",
            password="StrongPass123!",
            full_name="Family Viewer",
        )
        role = Role.objects.create(
            business=self.business_a,
            name="Family Viewer",
            permissions=["customers.view", "sales.view", "sales.create"],
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=user,
            role=role,
        )
        self.client.force_login(user)
        detail = self.client.get(
            reverse("customers:detail", args=[self.customer.public_id])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, family.name)
        self.assertNotContains(detail, "+ Add")
        self.assertEqual(
            self.client.get(
                reverse("customers:family_create", args=[self.customer.public_id])
            ).status_code,
            403,
        )
        with self.assertRaises(ModuleAccessDenied):
            customer_services.create_customer_family_member(
                business=self.business_a,
                customer=self.customer,
                name="Blocked",
                relation="son",
                more_options={},
                user=user,
                membership=membership,
            )

    def test_each_tailoring_line_can_use_a_different_wearer_or_main_customer(self):
        first = self.create_family(name="First Son")
        second = self.create_family(name="Second Brother", relation="brother")
        sale = self.complete_tailoring_sale(
            [
                self.tailoring_line(first),
                self.tailoring_line(second),
                self.tailoring_line(),
            ]
        )
        items = list(sale.items.order_by("id"))
        self.assertEqual(
            [item.family_member_id for item in items],
            [first.id, second.id, None],
        )
        with self.assertRaises(ProtectedError):
            first.delete()
        first.is_active = False
        first.save(update_fields=["is_active", "updated_at"])
        items[0].refresh_from_db()
        self.assertEqual(items[0].family_member_id, first.id)
        sale_return = sales_services.process_return(
            sale=sale,
            items=[{"sale_item": items[0], "quantity": Decimal("1")}],
            refund_method="cash",
            user=self.owner_a,
            membership=self.membership_a(),
        )
        self.assertEqual(sale_return.customer_id, self.customer.id)

    def test_checkout_rejects_wrong_inactive_walkin_cross_tenant_and_non_tailoring_profiles(self):
        active = self.create_family()
        wrong_customer = self.create_family(
            customer=self.other_customer,
            name="Wrong Customer Son",
        )
        tenant_b = CustomerFamilyMember.objects.create(
            business=self.business_b,
            customer=self.customer_b,
            name="Tenant B Son",
            relation="son",
        )
        inactive = self.create_family(name="Inactive Son")
        inactive.is_active = False
        inactive.save(update_fields=["is_active", "updated_at"])

        invalid_lines = (
            (self.tailoring_line(wrong_customer), self.customer),
            (self.tailoring_line(tenant_b), self.customer),
            (self.tailoring_line(inactive), self.customer),
            (self.tailoring_line(active), self.walk_in_a),
            ({**self.tailoring_line(), "family_member_id": "not-a-uuid"}, self.customer),
        )
        for line, customer in invalid_lines:
            with self.subTest(family=line["family_member_id"], customer=customer):
                with self.assertRaises(sales_services.SaleError):
                    self.complete_tailoring_sale([line], customer=customer)

        retail = Product.objects.create(
            business=self.business_a,
            name="Family Retail Item",
            sku="FAM-RETAIL",
            purchase_price=Decimal("1"),
            sale_price=Decimal("2"),
            track_inventory=False,
        )
        with self.assertRaisesMessage(
            sales_services.SaleError, "tailoring garment"
        ):
            self.make_sale(
                customer=self.customer,
                items=[{
                    "product": retail,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("2"),
                    "family_member_id": str(active.public_id),
                }],
            )

    def test_job_card_uses_live_wearer_measurements_but_keeps_main_contact(self):
        family = self.create_family(
            name="Job Son",
            more_options={"1": "29", "2": "46"},
        )
        sale = self.complete_tailoring_sale([self.tailoring_line(family)])
        item = sale.items.select_related("family_member").get()
        family.more_options = {"1": "32", "2": "50"}
        family.is_active = False
        family.save(update_fields=["more_options", "is_active", "updated_at"])
        item.refresh_from_db()
        request = RequestFactory().get("/job-card")
        request.business = self.business_a
        card = _job_card_data(sale, request, [item], sale_item=item)

        self.assertEqual(card["wearer"].name, "Job Son")
        self.assertEqual(
            card["more_options"],
            [
                {"label": "Chest", "value": "32"},
                {"label": "Length", "value": "50"},
            ],
        )
        self.assertEqual(card["sale"].customer.full_name, "Main Customer")
        self.assertEqual(card["sale"].customer.mobile, "99001122")

        main_sale = self.complete_tailoring_sale([self.tailoring_line()])
        main_item = main_sale.items.get()
        main_card = _job_card_data(
            main_sale, request, [main_item], sale_item=main_item
        )
        self.assertIsNone(main_card["wearer"])
        self.assertEqual(
            main_card["more_options"],
            [
                {"label": "Chest", "value": "41"},
                {"label": "Length", "value": "56"},
            ],
        )

        detail = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assertContains(detail, "Wearer: Job Son")
