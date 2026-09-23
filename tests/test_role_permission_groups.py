"""The role editor groups checkboxes without changing stored permission values."""

from collections import Counter
from html.parser import HTMLParser
from unittest.mock import patch

from django.urls import reverse
from django.utils.html import escape

from apps.accounts.forms import ROLE_PERMISSION_GROUPS
from apps.accounts.models import Role
from apps.core.permissions import PERMISSIONS
from tests.base import TenantTestCase


class PermissionMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.checkboxes = []
        self.label_targets = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "permissions":
            self.checkboxes.append(attrs)
        elif tag == "label" and attrs.get("for"):
            self.label_targets.add(attrs["for"])


class RolePermissionGroupTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def permission_markup(self, response):
        self.assertEqual(response.status_code, 200)
        parser = PermissionMarkupParser()
        parser.feed(response.content.decode())
        return parser

    def test_registry_codes_are_all_mapped_once(self):
        mapped = [code for _title, codes in ROLE_PERMISSION_GROUPS for code in codes]
        self.assertEqual(Counter(mapped), Counter(list(PERMISSIONS)))

    def test_create_and_edit_render_each_checkbox_once_in_grouped_cards(self):
        role = Role.objects.create(
            business=self.business_a,
            name="Grouped role",
            permissions=["sales.view"],
        )
        for url in (
            reverse("accounts:role_create"),
            reverse("accounts:role_edit", args=[role.public_id]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                markup = self.permission_markup(response)
                self.assertEqual(
                    Counter(checkbox["value"] for checkbox in markup.checkboxes),
                    Counter(list(PERMISSIONS)),
                )
                self.assertTrue(all(
                    checkbox.get("id") in markup.label_targets
                    for checkbox in markup.checkboxes
                ))
                self.assertContains(response, 'id="perm-grid" class="row g-3"')
                self.assertContains(response, 'class="col-12 col-md-6"', count=10)
                for title, _codes in ROLE_PERMISSION_GROUPS:
                    self.assertContains(response, escape(str(title)))

    def test_edit_keeps_selected_permissions_checked_and_saves_exact_values(self):
        role = Role.objects.create(
            business=self.business_a,
            name="Mixed permissions",
            permissions=["sales.view", "inventory.adjust"],
        )
        url = reverse("accounts:role_edit", args=[role.public_id])
        markup = self.permission_markup(self.client.get(url))
        checked = {
            checkbox["value"]
            for checkbox in markup.checkboxes
            if "checked" in checkbox
        }
        self.assertEqual(checked, {"sales.view", "inventory.adjust"})
        self.assertNotIn("checked", next(
            checkbox for checkbox in markup.checkboxes
            if checkbox["value"] == "sales.create"
        ))

        selected = ["sales.view", "customers.view", "backups.view"]
        response = self.client.post(url, {"name": role.name, "permissions": selected})
        self.assertEqual(response.status_code, 302)
        role.refresh_from_db()
        self.assertEqual(role.permissions, selected)

        membership = self.cashier_membership
        membership.role = role
        self.assertTrue(membership.has_perm("backups.view"))
        self.assertFalse(membership.has_perm("inventory.adjust"))

    def test_create_saves_selected_permissions(self):
        selected = ["workshop.fabric_actual", "products.view"]
        response = self.client.post(
            reverse("accounts:role_create"),
            {"name": "Workshop reader", "permissions": selected},
        )
        self.assertEqual(response.status_code, 302)
        role = Role.objects.get(business=self.business_a, name="Workshop reader")
        self.assertEqual(role.permissions, selected)

    def test_unmapped_registry_permission_appears_in_other_permissions(self):
        with patch.dict(PERMISSIONS, {"future.permission": "Future permission"}):
            response = self.client.get(reverse("accounts:role_create"))
        markup = self.permission_markup(response)
        self.assertContains(response, "Other Permissions")
        self.assertEqual(
            [checkbox["value"] for checkbox in markup.checkboxes].count("future.permission"),
            1,
        )
        self.assertContains(response, "Future permission")

    def test_existing_unregistered_permission_remains_selected_on_edit(self):
        role = Role.objects.create(
            business=self.business_a,
            name="Legacy permission",
            permissions=["sales.view", "legacy.permission"],
        )
        url = reverse("accounts:role_edit", args=[role.public_id])
        response = self.client.get(url)
        markup = self.permission_markup(response)
        self.assertContains(response, "Other Permissions")
        legacy_checkbox = next(
            checkbox for checkbox in markup.checkboxes
            if checkbox["value"] == "legacy.permission"
        )
        self.assertIn("checked", legacy_checkbox)

        response = self.client.post(
            url,
            {"name": role.name, "permissions": role.permissions},
        )
        self.assertEqual(response.status_code, 302)
        role.refresh_from_db()
        self.assertEqual(role.permissions, ["sales.view", "legacy.permission"])
