"""Throttled activity tracking for authenticated business memberships."""

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin

from .activity import record_membership_activity


class RequiredPasswordChangeMiddleware(MiddlewareMixin):
    """Keep a temporary-password login out of tenant application views."""

    ALLOWED_ACCOUNT_VIEWS = {
        "change_password", "logout", "login", "password_reset",
        "password_reset_done", "password_reset_confirm",
        "password_reset_complete",
    }

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated or not getattr(user, "must_change_password", False):
            return None
        # Support sessions use the admin's session while request.user is the
        # owner. They must leave the owner's flag intact for the real login.
        if getattr(request, "support_admin", None) is not None:
            return None

        path = request.path_info
        for asset_url in (settings.STATIC_URL, settings.MEDIA_URL):
            if path.startswith(asset_url.rstrip("/") + "/"):
                return None

        match = request.resolver_match
        if match.namespace in {"platformadmin", "admin"}:
            return None
        if match.namespace == "api" and match.url_name in {"health", "token"}:
            return None
        if match.namespace == "accounts" and match.url_name in self.ALLOWED_ACCOUNT_VIEWS:
            return None
        if path.startswith("/api/"):
            return JsonResponse(
                {"detail": "Password change required.", "code": "password_change_required"},
                status=403,
            )
        return redirect("accounts:change_password")


class ActivityMiddleware:
    """Record a tenant user's activity at most once per minute per business."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self.record_activity(request)
        return self.get_response(request)

    @staticmethod
    def record_activity(request):
        user = getattr(request, "user", None)
        membership = getattr(request, "membership", None)
        if (
            not user
            or not user.is_authenticated
            or not user.is_active
            or membership is None
            or not membership.is_active
            or getattr(request, "support_admin", None) is not None
        ):
            return

        path = request.path_info
        if path.startswith(("/platform/", "/django-admin/")):
            return
        for asset_url in (settings.STATIC_URL, settings.MEDIA_URL):
            prefix = asset_url.rstrip("/") + "/"
            if path.startswith(prefix):
                return

        record_membership_activity(membership, user=user)
