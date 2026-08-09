"""Platform-admin capabilities for Backup & Restore.

These capabilities are deliberately separate from tenant ``Role.permissions``.
Phase 1 exposes read-only metadata to existing platform staff. The remaining
capabilities are declared now so later operational phases can assign them
through Django's platform authorization without granting tenant permissions.
"""

from enum import StrEnum
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


class PlatformBackupCapability(StrEnum):
    VIEW_METADATA = "view_metadata"
    MANAGE_BACKUPS = "manage_backups"
    APPROVE_RESTORE = "approve_restore"
    CLEANUP_BACKUPS = "cleanup_backups"


CAPABILITY_PERMISSION_CODENAMES = {
    PlatformBackupCapability.VIEW_METADATA: "platform_view_metadata",
    PlatformBackupCapability.MANAGE_BACKUPS: "platform_manage_backups",
    PlatformBackupCapability.APPROVE_RESTORE: "platform_approve_restore",
    PlatformBackupCapability.CLEANUP_BACKUPS: "platform_cleanup_backups",
}


def has_platform_backup_capability(user, capability) -> bool:
    """Return a fail-closed platform capability decision.

    Existing platform staff may inspect operational metadata in Phase 1,
    matching the current platform-admin convention. Operational capabilities
    require either a superuser or an explicitly assigned Django permission.
    """

    try:
        capability = PlatformBackupCapability(capability)
    except (TypeError, ValueError):
        return False
    if not user or not user.is_authenticated or not user.is_platform_staff:
        return False
    if capability == PlatformBackupCapability.VIEW_METADATA:
        return True
    if user.is_superuser:
        return True
    codename = CAPABILITY_PERMISSION_CODENAMES[capability]
    return user.has_perm(f"backups.{codename}")


def platform_backup_capability_required(capability):
    """Require a declared platform capability without consulting tenant RBAC."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            request_user = getattr(request, "user", None)
            support_admin = getattr(request, "support_admin", None)
            if not request_user or not request_user.is_authenticated:
                return redirect("accounts:login")
            # During support-mode impersonation request.user is deliberately the
            # tenant owner. Platform controls remain confined to this namespace
            # and authenticate against the separately retained support admin.
            platform_actor = support_admin or request_user
            if not has_platform_backup_capability(platform_actor, capability):
                raise PermissionDenied
            request.platform_actor = platform_actor
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
