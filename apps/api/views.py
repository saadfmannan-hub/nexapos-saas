"""API v1 -- explicitly tenant-scoped, subscription-aware, read-only."""

from types import SimpleNamespace

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Membership
from apps.subscriptions.access import AccessAction
from apps.subscriptions.api_permissions import HasSubscriptionModuleAccess

from . import serializers

_INVALID_BUSINESS = SimpleNamespace(pk=None, is_active=False)
_INVALID_MEMBERSHIP = SimpleNamespace(
    pk=None,
    is_active=False,
    user_id=None,
    business_id=None,
)


def _resolve_explicit_context(request):
    """Resolve one exact actor/business context without membership guessing.

    Browser sessions may reuse their already-selected business membership.
    Token callers must identify a business or membership using
    ``X-Business-ID`` or ``X-Membership-ID``. Invalid explicit identifiers
    deliberately collapse to the same central membership denial.
    """

    cached = getattr(request, "_nexapos_explicit_api_context", None)
    if cached is not None:
        return cached

    business_id = request.headers.get("X-Business-ID", "").strip()
    membership_id = request.headers.get("X-Membership-ID", "").strip()
    if business_id or membership_id:
        try:
            query = Membership.objects.select_related("business", "role").filter(
                user=request.user,
                is_active=True,
                business__is_active=True,
            )
            if business_id:
                query = query.filter(business__public_id=business_id)
            if membership_id:
                query = query.filter(public_id=membership_id)
            membership = query.get()
        except (
            DjangoValidationError,
            Membership.DoesNotExist,
            Membership.MultipleObjectsReturned,
            ValueError,
        ):
            context = (_INVALID_BUSINESS, _INVALID_MEMBERSHIP)
        else:
            context = (membership.business, membership)
    else:
        business = getattr(request, "business", None)
        membership = getattr(request, "membership", None)
        if (
            business is not None
            and membership is not None
            and membership.user_id == request.user.pk
            and membership.business_id == business.pk
        ):
            context = (business, membership)
        else:
            context = (_INVALID_BUSINESS, _INVALID_MEMBERSHIP)

    request._nexapos_explicit_api_context = context
    return context


class ExplicitAPIContextMixin:
    def get_api_business(self, request):
        return _resolve_explicit_context(request)[0]

    def get_api_membership(self, request):
        return _resolve_explicit_context(request)[1]


class TenantReadOnlyViewSet(ExplicitAPIContextMixin, viewsets.ReadOnlyModelViewSet):
    permission_classes = [HasSubscriptionModuleAccess]
    lookup_field = "public_id"
    required_modules = ("pos_core",)
    required_permission = None
    access_action = AccessAction.READ

    def get_queryset(self):
        return self.base_queryset.for_business(self.request.api_business)


class ProductViewSet(TenantReadOnlyViewSet):
    serializer_class = serializers.ProductSerializer
    required_permission = "products.view"

    def get_queryset(self):
        queryset = super().get_queryset()
        if "tailoring" not in self.request.api_access_context.effective_modules:
            queryset = queryset.filter(is_tailoring_item=False)
        return queryset

    @property
    def base_queryset(self):
        from apps.catalog.models import Product

        return Product.objects.prefetch_related("variants").select_related(
            "category", "unit"
        )


class CategoryViewSet(TenantReadOnlyViewSet):
    serializer_class = serializers.CategorySerializer
    required_permission = "products.view"

    @property
    def base_queryset(self):
        from apps.catalog.models import Category

        return Category.objects


class CustomerViewSet(TenantReadOnlyViewSet):
    serializer_class = serializers.CustomerSerializer
    required_permission = "customers.view"

    def get_queryset(self):
        queryset = super().get_queryset()
        allowed = self.request.api_membership.allowed_branch_ids
        if allowed is not None:
            queryset = queryset.filter(home_branch_id__in=allowed)
        return queryset

    def has_api_object_scope(self, request, context, obj):
        allowed = context.membership.allowed_branch_ids
        return allowed is None or obj.home_branch_id in allowed

    @property
    def base_queryset(self):
        from apps.customers.models import Customer

        return Customer.objects


class SaleViewSet(TenantReadOnlyViewSet):
    serializer_class = serializers.SaleSerializer
    required_permission = "sales.view"

    def get_queryset(self):
        qs = super().get_queryset().filter(
            warehouse__business=self.request.api_business,
        )
        allowed = self.request.api_membership.allowed_branch_ids
        if allowed is None:
            return qs
        return qs.filter(branch_id__in=allowed).filter(
            Q(warehouse__branch_id__in=allowed)
            | Q(warehouse__branch__isnull=True)
        )

    def has_api_object_scope(self, request, context, obj):
        if not context.membership.can_access_branch(obj.branch):
            return False
        allowed_warehouses = context.membership.allowed_warehouse_ids
        return (
            obj.warehouse.business_id == context.business.pk
            and (
                allowed_warehouses is None
                or obj.warehouse_id in allowed_warehouses
            )
        )

    @property
    def base_queryset(self):
        from apps.sales.models import Sale

        return Sale.objects.exclude(status="draft").prefetch_related(
            "items"
        ).select_related("customer", "branch", "warehouse")


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


class MeView(ExplicitAPIContextMixin, APIView):
    permission_classes = [HasSubscriptionModuleAccess]
    required_modules = ("pos_core",)
    access_action = AccessAction.READ

    def get(self, request):
        membership = request.api_membership
        return Response({
            "user": {
                "email": request.user.email,
                "name": request.user.full_name,
            },
            "business": {
                "name": membership.business.name,
                "public_id": str(membership.business.public_id),
                "currency": membership.business.currency_code,
            },
            "role": membership.role.name,
            "permissions": sorted(membership.permission_set),
        })
