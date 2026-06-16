"""Temporarily seed a Render demo platform admin.

This migration is intentionally additive and idempotent. It only creates
admin@nexapos.com when that user is missing, and rollback does not delete
or change any user data.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, migrations


ADMIN_EMAIL = "admin@nexapos.com"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Admin@2026"
ADMIN_FULL_NAME = "Render Demo Admin"


def _model_has_field(model, field_name):
    return any(field.name == field_name for field in model._meta.get_fields())


def seed_render_admin(apps, schema_editor):
    User = get_user_model()
    db_alias = schema_editor.connection.alias
    manager = User._default_manager.db_manager(db_alias)

    email_field = getattr(User, "EMAIL_FIELD", "email")
    normalize_email = getattr(manager, "normalize_email", None)
    email = normalize_email(ADMIN_EMAIL) if normalize_email else ADMIN_EMAIL

    if manager.filter(**{email_field: email}).exists():
        return

    user_fields = {
        email_field: email,
        "is_staff": True,
        "is_superuser": True,
    }

    if _model_has_field(User, "username"):
        user_fields["username"] = ADMIN_USERNAME
    if _model_has_field(User, "full_name"):
        user_fields["full_name"] = ADMIN_FULL_NAME
    if _model_has_field(User, "is_active"):
        user_fields["is_active"] = True
    if _model_has_field(User, "is_platform_admin"):
        user_fields["is_platform_admin"] = True
    if _model_has_field(User, "email_verified"):
        user_fields["email_verified"] = True

    user = User(**user_fields)
    user.set_password(ADMIN_PASSWORD)

    try:
        user.save(using=db_alias)
    except IntegrityError:
        if manager.filter(**{email_field: email}).exists():
            return
        raise


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_phase21_role_permissions")]

    operations = [
        migrations.RunPython(seed_render_admin, migrations.RunPython.noop),
    ]
