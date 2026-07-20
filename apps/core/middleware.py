"""Tenant resolution middleware.

Attaches to every request:
  request.business    — the active Business for the authenticated user
  request.membership  — the Membership linking user to that business

The active business is remembered in the session; if missing or no
longer valid (membership revoked / business inactive) it falls back to
the user's first active membership. Platform staff have no business by
default.
"""
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

from apps.core.date_ranges import business_timezone

SESSION_BUSINESS_KEY = "active_business_id"


class BusinessMiddleware(MiddlewareMixin):
    def process_request(self, request):
        # Never let a previous request's tenant timezone leak into this one.
        timezone.deactivate()
        request.business = None
        request.membership = None

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return

        memberships = user.memberships.select_related("business", "role").filter(
            is_active=True, business__is_active=True
        )

        wanted = request.session.get(SESSION_BUSINESS_KEY)
        membership = None
        if wanted:
            membership = next(
                (m for m in memberships if m.business_id == wanted), None
            )
        if membership is None:
            membership = memberships.first()
            if membership:
                request.session[SESSION_BUSINESS_KEY] = membership.business_id

        if membership:
            request.membership = membership
            request.business = membership.business
            timezone.activate(business_timezone(membership.business))

    def process_response(self, request, response):
        timezone.deactivate()
        return response

    def process_exception(self, request, exception):
        timezone.deactivate()
