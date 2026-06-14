"""Attaches the subscription to the request and enforces read-only mode.

When a subscription is expired/suspended/cancelled, existing data stays
readable but write requests (POST/PUT/PATCH/DELETE) to business
endpoints are blocked, with an allowlist for account/session/billing
actions the user still needs.
"""
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import resolve
from django.utils.deprecation import MiddlewareMixin

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# URL namespaces that must keep working in read-only mode
ALLOWED_NAMESPACES = {"accounts", "subscriptions", "platformadmin", "admin"}
ALLOWED_URL_NAMES = {"switch_business", "logout"}


class SubscriptionMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.subscription = None
        business = getattr(request, "business", None)
        if business is None:
            return
        request.subscription = getattr(business, "subscription", None)

    def process_view(self, request, view_func, view_args, view_kwargs):
        sub = getattr(request, "subscription", None)
        if sub is None or sub.is_operational:
            return None

        try:
            match = resolve(request.path_info)
        except Exception:
            return None
        if match.namespace in ALLOWED_NAMESPACES or match.url_name in ALLOWED_URL_NAMES:
            return None

        # Platform-configurable behaviour on expiry:
        #   read_only (default) — block writes only, data stays viewable
        #   suspend             — block all access until renewed
        from apps.platformadmin.models import PlatformConfig

        full_suspend = (
            PlatformConfig.get_solo().expiry_mode == PlatformConfig.ExpiryMode.SUSPEND
        )
        if not full_suspend and request.method not in WRITE_METHODS:
            return None

        messages.error(
            request,
            "Subscription expired. Please contact support."
            if full_suspend else
            "Your subscription is not active. Data is read-only until the "
            "subscription is renewed.",
        )
        return redirect("subscriptions:status")
