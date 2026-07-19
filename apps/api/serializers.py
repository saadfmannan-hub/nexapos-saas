"""API v1 serializers with module-aware field exposure."""

from decimal import Decimal

from rest_framework import serializers

from apps.catalog.models import Category, Product, ProductVariant
from apps.customers.models import Customer
from apps.sales.models import Sale, SaleItem


class ModuleFieldsMixin:
    """Remove fields owned by modules unavailable to this API request."""

    module_fields = {}

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        access_context = getattr(request, "api_access_context", None)
        effective_modules = (
            access_context.effective_modules
            if access_context is not None
            else frozenset()
        )
        for module_key, owned_fields in self.module_fields.items():
            if module_key not in effective_modules:
                for field_name in owned_fields:
                    fields.pop(field_name, None)
        return fields


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["public_id", "name", "parent_id", "is_active"]


class VariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariant
        fields = [
            "public_id",
            "name",
            "attributes",
            "sku",
            "barcode",
            "sale_price",
            "is_active",
        ]


class ProductSerializer(ModuleFieldsMixin, serializers.ModelSerializer):
    variants = VariantSerializer(many=True, read_only=True)
    category = serializers.StringRelatedField()
    is_meter_tailoring = serializers.BooleanField(read_only=True)
    module_fields = {
        "tailoring": (
            "is_tailoring_item",
            "is_meter_tailoring",
            "estimated_adult_fabric",
            "estimated_child_fabric",
        ),
    }

    class Meta:
        model = Product
        fields = [
            "public_id",
            "name",
            "sku",
            "barcode",
            "product_type",
            "category",
            "sale_price",
            "wholesale_price",
            "track_inventory",
            "is_tailoring_item",
            "is_meter_tailoring",
            "estimated_adult_fabric",
            "estimated_child_fabric",
            "is_active",
            "variants",
        ]


class CustomerSerializer(ModuleFieldsMixin, serializers.ModelSerializer):
    home_branch = serializers.PrimaryKeyRelatedField(read_only=True)
    branch_code = serializers.CharField(source="home_branch.code", read_only=True)
    branch_name = serializers.CharField(source="home_branch.name", read_only=True)
    module_fields = {
        "customer_credit": ("balance", "store_credit"),
    }

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        membership = getattr(request, "api_membership", None)
        if (
            membership is None
            or membership.allowed_branch_ids is not None
        ):
            fields.pop("balance", None)
            fields.pop("store_credit", None)
        return fields

    class Meta:
        model = Customer
        fields = [
            "public_id",
            "home_branch",
            "branch_code",
            "branch_name",
            "code",
            "full_name",
            "mobile",
            "email",
            "balance",
            "store_credit",
            "is_active",
        ]
        read_only_fields = ["balance", "store_credit"]


class SaleItemSerializer(ModuleFieldsMixin, serializers.ModelSerializer):
    fabric_meter_used = serializers.DecimalField(
        max_digits=14,
        decimal_places=3,
        min_value=Decimal("0.001"),
        max_value=Decimal("1000.000"),
        allow_null=True,
        required=False,
    )
    fabric_variance = serializers.DecimalField(
        max_digits=14,
        decimal_places=3,
        read_only=True,
    )
    module_fields = {
        "tailoring": (
            "garment_classification",
            "collection_type",
            "fabric_meter_used",
            "estimated_fabric",
            "actual_fabric_used",
            "fabric_variance",
        ),
    }

    class Meta:
        model = SaleItem
        fields = [
            "product_name",
            "sku",
            "quantity",
            "unit_price",
            "discount_amount",
            "tax_amount",
            "line_total",
            "garment_classification",
            "collection_type",
            "fabric_meter_used",
            "estimated_fabric",
            "actual_fabric_used",
            "fabric_variance",
        ]


class SaleSerializer(ModuleFieldsMixin, serializers.ModelSerializer):
    items = SaleItemSerializer(many=True, read_only=True)
    customer = serializers.StringRelatedField()
    branch = serializers.StringRelatedField()
    module_fields = {
        "tailoring": ("priority", "delivery_date"),
    }

    class Meta:
        model = Sale
        fields = [
            "public_id",
            "invoice_number",
            "sale_date",
            "status",
            "priority",
            "delivery_date",
            "customer",
            "branch",
            "subtotal",
            "discount_amount",
            "tax_amount",
            "total",
            "amount_paid",
            "items",
        ]
