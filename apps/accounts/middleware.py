"""Throttled activity tracking for authenticated business memberships."""

from django.conf import settings

from .activity import record_membership_activity


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
