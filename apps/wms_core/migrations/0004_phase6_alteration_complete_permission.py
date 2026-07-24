from django.db import migrations

ALTERATION_COMPLETE_ROLES = ("owner_admin", "workshop_manager")
ALTERATION_COMPLETE_PERMISSION = "wms.alterations.complete"


def add_alteration_complete_permission(apps, schema_editor):
    WmsRole = apps.get_model("wms_core", "WmsRole")
    for role in WmsRole.objects.filter(
        code__in=ALTERATION_COMPLETE_ROLES,
        is_system=True,
    ):
        permissions = list(role.permissions or [])
        if ALTERATION_COMPLETE_PERMISSION not in permissions:
            permissions.append(ALTERATION_COMPLETE_PERMISSION)
            role.permissions = permissions
            role.save(update_fields=["permissions"])


def remove_alteration_complete_permission(apps, schema_editor):
    WmsRole = apps.get_model("wms_core", "WmsRole")
    for role in WmsRole.objects.filter(
        code__in=ALTERATION_COMPLETE_ROLES,
        is_system=True,
    ):
        role.permissions = [
            permission
            for permission in (role.permissions or [])
            if permission != ALTERATION_COMPLETE_PERMISSION
        ]
        role.save(update_fields=["permissions"])


class Migration(migrations.Migration):
    dependencies = [
        ("wms_core", "0003_phase5_order_finish_permission"),
    ]

    operations = [
        migrations.RunPython(
            add_alteration_complete_permission,
            remove_alteration_complete_permission,
        ),
    ]
