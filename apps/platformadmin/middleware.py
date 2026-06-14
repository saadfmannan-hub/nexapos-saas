"""Support-mode impersonation ("Login As Owner").

When a platform admin starts a support session, the session stores the
impersonation context. This middleware (which must run AFTER
AuthenticationMiddleware and BEFORE BusinessMiddleware) swaps
request.user to the target business owner for the duration, so the
tenant workspace resolves exactly as the owner sees it. The original
admin is kept on request.support_admin and a banner + audit trail make
the session visible and accountable.
"""
from django.utils.deprecation import MiddlewareMixin

SESSION_KEY = "support_session"


class SupportSessionMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.support_admin = None
        request.support_session = None

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return
        data = request.session.get(SESSION_KEY)
        if not data:
            return

        from apps.accounts.models import User

        # The logged-in account must be the platform admin who started the
        # session; otherwise discard the stale impersonation context.
        if user.pk != data.get("admin_id") or not user.is_platform_staff:
            request.session.pop(SESSION_KEY, None)
            return

        owner = User.objects.filter(
            pk=data.get("owner_id"), is_active=True
        ).first()
        if owner is None:
            request.session.pop(SESSION_KEY, None)
            return

        request.support_admin = user      # the real platform admin
        request.user = owner               # act as the business owner
        request.support_session = data     # {business_id, business_name, reason, started}
