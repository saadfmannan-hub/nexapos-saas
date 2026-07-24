from django.db import migrations

ORDER_FINISH_ROLES = ("owner_admin", "workshop_manager")
ORDER_FINISH_PERMISSION = "wms.orders.finish"


def add_order_finish_permission(apps, schema_editor):
    WmsRole = apps.get_model("wms_core", "WmsRole")
    for role in WmsRole.objects.filter(
        code__in=ORDER_FINISH_ROLES,
        is_system=True,
    ):
        permissions = list(role.permissions or [])
        if ORDER_FINISH_PERMISSION not in permissions:
            permissions.append(ORDER_FINISH_PERMISSION)
            role.permissions = permissions
            role.save(update_fields=["permissions"])


def remove_order_finish_permission(apps, schema_editor):
    WmsRole = apps.get_model("wms_core", "WmsRole")
    for role in WmsRole.objects.filter(
        code__in=ORDER_FINISH_ROLES,
        is_system=True,
    ):
        role.permissions = [
            permission
            for permission in (role.permissions or [])
            if permission != ORDER_FINISH_PERMISSION
        ]
        role.save(update_fields=["permissions"])

class Migration(migrations.Migration):
    dependencies = [
        ("wms_core", "0002_phase2_category_role_permissions"),
    ]

    operations = [
        migrations.RunPython(
            add_order_finish_permission,
            remove_order_finish_permission,
        ),
    ]
