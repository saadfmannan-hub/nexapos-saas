from django.db import migrations

PERMISSIONS = {
    "owner_admin": ("wms.salary.revise",),
    "workshop_manager": ("wms.salary.view", "wms.salary.calculate"),
}


def update_system_roles(apps, schema_editor, *, add):
    roles = apps.get_model("wms_core", "WmsRole").objects.using(
        schema_editor.connection.alias
    )
    for role in roles.filter(code__in=PERMISSIONS, is_system=True).iterator():
        codes = PERMISSIONS[role.code]
        existing = role.permissions or []
        role.permissions = (
            list(dict.fromkeys([*existing, *codes])) if add
            else [code for code in existing if code not in codes]
        )
        role.save(update_fields=["permissions"])


def grant_permissions(apps, schema_editor):
    update_system_roles(apps, schema_editor, add=True)


def remove_permissions(apps, schema_editor):
    update_system_roles(apps, schema_editor, add=False)


class Migration(migrations.Migration):
    dependencies = [("wms_core", "0004_phase6_alteration_complete_permission")]
    operations = [migrations.RunPython(grant_permissions, remove_permissions)]
