"""Grant Phase 2.1 import/export permissions to existing system roles.

Additive and idempotent: it only appends new permission codes to the
"Business Administrator" and "Branch Manager" system roles that already
exist. No data is removed; custom roles and the owner role (which
implicitly holds every permission) are untouched.
"""
from django.db import migrations

ADMIN_NEW = [
    "customers.export", "customers.import", "products.export",
    "inventory.export", "inventory.import",
]
MANAGER_NEW = ADMIN_NEW  # same set for branch managers


def grant(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(
        is_system=True, name__in=["Business Administrator", "Branch Manager"]
    ):
        perms = list(role.permissions or [])
        changed = False
        for code in (ADMIN_NEW if role.name == "Business Administrator" else MANAGER_NEW):
            if code not in perms:
                perms.append(code)
                changed = True
        if changed:
            role.permissions = perms
            role.save(update_fields=["permissions"])


def ungrant(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    revoke = set(ADMIN_NEW)
    for role in Role.objects.filter(
        is_system=True, name__in=["Business Administrator", "Branch Manager"]
    ):
        perms = [c for c in (role.permissions or []) if c not in revoke]
        role.permissions = perms
        role.save(update_fields=["permissions"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_initial")]
    operations = [migrations.RunPython(grant, ungrant)]
