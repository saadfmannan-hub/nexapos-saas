from django.db import migrations

ROLE_PERMISSION_ADDITIONS = {
    "owner_admin": (
        "wms.categories.view",
        "wms.categories.manage",
    ),
    "workshop_manager": (
        "wms.categories.view",
        "wms.categories.manage",
    ),
    "production_entry": (
        "wms.categories.view",
    ),
    "report_viewer": (
        "wms.categories.view",
    ),
}


def add_phase2_permissions(apps, schema_editor):
    WmsRole = apps.get_model("wms_core", "WmsRole")
    for code, additions in ROLE_PERMISSION_ADDITIONS.items():
        for role in WmsRole.objects.filter(code=code, is_system=True):
            permissions = list(role.permissions or [])
            changed = False
            for permission in additions:
                if permission not in permissions:
                    permissions.append(permission)
                    changed = True
            if changed:
                role.permissions = permissions
                role.save(update_fields=["permissions"])


def remove_phase2_permissions(apps, schema_editor):
    WmsRole = apps.get_model("wms_core", "WmsRole")
    for code, additions in ROLE_PERMISSION_ADDITIONS.items():
        additions = set(additions)
        for role in WmsRole.objects.filter(code=code, is_system=True):
            permissions = [
                permission
                for permission in (role.permissions or [])
                if permission not in additions
            ]
            role.permissions = permissions
            role.save(update_fields=["permissions"])


class Migration(migrations.Migration):

    dependencies = [
        ("wms_core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            add_phase2_permissions,
            remove_phase2_permissions,
        ),
    ]
