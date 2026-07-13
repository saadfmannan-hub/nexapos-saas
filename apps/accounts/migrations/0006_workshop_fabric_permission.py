"""Provision the Phase 1 workshop fabric permission for existing tenants.

Owners receive new permissions implicitly. Existing business administrators
and Workshop Manager roles are granted the dedicated permission; tenants
without a Workshop Manager role receive the same system role used for new
business provisioning. Cashier roles are deliberately untouched.
"""
from django.db import migrations
from django.db.models import Q


PERMISSION = "workshop.fabric_actual"
WORKSHOP_PERMISSIONS = [
    "sales.view",
    "products.view",
    "customers.view",
    "reports.view",
    "reports.export",
    PERMISSION,
    "notifications.view",
]


def provision_workshop_permission(apps, schema_editor):
    Business = apps.get_model("tenants", "Business")
    Role = apps.get_model("accounts", "Role")

    for role in Role.objects.filter(
        Q(name="Business Administrator") | Q(name__iexact="Workshop Manager")
    ):
        permissions = list(role.permissions or [])
        if PERMISSION not in permissions:
            permissions.append(PERMISSION)
            role.permissions = permissions
            role.save(update_fields=["permissions"])

    for business_id in Business.objects.values_list("id", flat=True):
        if Role.objects.filter(
            business_id=business_id,
            name__iexact="Workshop Manager",
        ).exists():
            continue
        Role.objects.create(
            business_id=business_id,
            name="Workshop Manager",
            is_system=True,
            permissions=WORKSHOP_PERMISSIONS,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_seed_demo_tailoring"),
    ]

    operations = [
        migrations.RunPython(
            provision_workshop_permission,
            migrations.RunPython.noop,
        ),
    ]
