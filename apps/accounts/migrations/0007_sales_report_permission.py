"""Grant the sales-report permission only to intended built-in roles.

Owners receive all registered permissions implicitly. Custom roles are left
unchanged so a business owner must opt them in explicitly.
"""
from django.db import migrations


PERMISSION = "reports.sales"
BUILT_IN_ROLES = {
    "Business Administrator",
    "Branch Manager",
    "Workshop Manager",
    "Accountant",
    "Auditor",
}


def grant_to_builtin_roles(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(is_system=True, name__in=BUILT_IN_ROLES):
        permissions = list(role.permissions or [])
        if PERMISSION not in permissions:
            permissions.append(PERMISSION)
            role.permissions = permissions
            role.save(update_fields=["permissions"])


def revoke_from_builtin_roles(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(is_system=True, name__in=BUILT_IN_ROLES):
        permissions = [
            code for code in (role.permissions or []) if code != PERMISSION
        ]
        role.permissions = permissions
        role.save(update_fields=["permissions"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_workshop_fabric_permission")]
    operations = [
        migrations.RunPython(grant_to_builtin_roles, revoke_from_builtin_roles),
    ]
