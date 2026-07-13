"""API v1 serializers — read-focused foundation for future integrations."""
from rest_framework import serializers

from apps.catalog.models import Category, Product, ProductVariant
from apps.customers.models import Customer
from apps.sales.models import Sale, SaleItem


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["public_id", "name", "parent_id", "is_active"]


class VariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariant
        fields = ["public_id", "name", "attributes", "sku", "barcode",
                  "sale_price", "is_active"]


class ProductSerializer(serializers.ModelSerializer):
    variants = VariantSerializer(many=True, read_only=True)
    category = serializers.StringRelatedField()

    class Meta:
        model = Product
        fields = ["public_id", "name", "sku", "barcode", "product_type",
                  "category", "sale_price", "wholesale_price", "track_inventory",
                  "is_tailoring_item", "is_active", "variants"]


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["public_id", "code", "full_name", "mobile", "email",
                  "balance", "store_credit", "is_active"]
        read_only_fields = ["balance", "store_credit"]


class SaleItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SaleItem
        fields = ["product_name", "sku", "quantity", "unit_price",
                  "discount_amount", "tax_amount", "line_total",
                  "garment_classification"]


class SaleSerializer(serializers.ModelSerializer):
    items = SaleItemSerializer(many=True, read_only=True)
    customer = serializers.StringRelatedField()
    branch = serializers.StringRelatedField()

    class Meta:
        model = Sale
        fields = ["public_id", "invoice_number", "sale_date", "status",
                  "priority", "delivery_date", "customer", "branch",
                  "subtotal", "discount_amount",
                  "tax_amount", "total", "amount_paid", "items"]
