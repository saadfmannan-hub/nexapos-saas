"""View mixins enforcing authentication, tenancy and permissions.

Every business-facing view must inherit BusinessRequiredMixin (or use
the function-view decorators in apps.core.decorators). Object lookups
go through `get_tenant_object`, which 404s on cross-tenant access.
"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect


class BusinessRequiredMixin(LoginRequiredMixin):
    """Requires an authenticated user with an active business membership."""

    permission_required: str | None = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if request.business is None or request.membership is None:
            return redirect("tenants:no_business")
        if self.permission_required and not request.membership.has_perm(
            self.permission_required
        ):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class PermissionRequiredMixin(BusinessRequiredMixin):
    """Alias kept for readability at call sites."""


def get_tenant_object(model, business, **lookup):
    """Fetch a tenant-owned object or raise 404.

    Cross-tenant primary keys / UUIDs must be indistinguishable from
    nonexistent ones, so this always raises Http404 (never 403).
    """
    try:
        return model.objects.for_business(business).get(**lookup)
    except model.DoesNotExist:
        raise Http404


class TenantListMixin(BusinessRequiredMixin):
    """ListView base that automatically scopes the queryset."""

    paginate_by = 25

    def get_queryset(self):
        return self.model.objects.for_business(self.request.business)
