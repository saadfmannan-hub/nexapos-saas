"""API v1 — tenant-aware, token or session authenticated, read-first.

Every queryset is filtered by the caller's active business membership.
API access additionally requires the subscription plan feature flag.
"""
from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Membership
from apps.subscriptions import services as subscriptions

from . import serializers


def _membership_for(request):
    return (
        Membership.objects.filter(
            user=request.user, is_active=True, business__is_active=True
        )
        .select_related("business", "role")
        .first()
    )


class HasBusinessAPIAccess(BasePermission):
    message = "API access requires an active business and an API-enabled plan."

    def has_permission(self, request, view):
        membership = _membership_for(request)
        if membership is None:
            return False
        if not subscriptions.has_feature(membership.business, "api_access"):
            raise PermissionDenied(
                "Your subscription plan does not include API access."
            )
        request.api_membership = membership
        request.api_business = membership.business
        return True


class TenantReadOnlyViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, HasBusinessAPIAccess]
    lookup_field = "public_id"
    required_perm = None

    def get_queryset(self):
        qs = self.base_queryset.for_business(self.request.api_business)
        if self.required_perm and not self.request.api_membership.has_perm(
            self.required_perm
        ):
            raise PermissionDenied("Missing permission: " + self.required_perm)
        return qs


class ProductViewSet(TenantReadOnlyViewSet):
    serializer_class = serializers.ProductSerializer
    required_perm = "products.view"

    @property
    def base_queryset(self):
        from apps.catalog.models import Product

        return Product.objects.prefetch_related("variants").select_related("category")


class CategoryViewSet(TenantReadOnlyViewSet):
    serializer_class = serializers.CategorySerializer
    required_perm = "products.view"

    @property
    def base_queryset(self):
        from apps.catalog.models import Category

        return Category.objects


class CustomerViewSet(TenantReadOnlyViewSet):
    serializer_class = serializers.CustomerSerializer
    required_perm = "customers.view"

    @property
    def base_queryset(self):
        from apps.customers.models import Customer

        return Customer.objects


class SaleViewSet(TenantReadOnlyViewSet):
    serializer_class = serializers.SaleSerializer
    required_perm = "sales.view"

    @property
    def base_queryset(self):
        from apps.sales.models import Sale

        return Sale.objects.exclude(status="draft").prefetch_related(
            "items").select_related("customer", "branch")


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Unauthenticated health check for load balancers / Docker."""
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return Response({"status": "ok" if db_ok else "degraded", "database": db_ok})


@api_view(["GET"])
@permission_classes([IsAuthenticated, HasBusinessAPIAccess])
def me(request):
    m = request.api_membership
    return Response({
        "user": {"email": request.user.email, "name": request.user.full_name},
        "business": {"name": m.business.name,
                     "public_id": str(m.business.public_id),
                     "currency": m.business.currency_code},
        "role": m.role.name,
        "permissions": sorted(m.permission_set),
    })
