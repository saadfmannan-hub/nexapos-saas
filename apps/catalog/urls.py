from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.product_list, name="product_list"),
    path("new/", views.product_form, name="product_create"),
    path("import/", views.product_import, name="product_import"),
    path("import/template/", views.import_template, name="import_template"),
    path("export/", views.product_export, name="product_export"),
    path("categories/", views.category_list, name="category_list"),
    path("brands/", views.brand_list, name="brand_list"),
    path("units/", views.unit_list, name="unit_list"),
    path("taxes/", views.tax_list, name="tax_list"),
    path("<uuid:public_id>/", views.product_detail, name="product_detail"),
    path("<uuid:public_id>/edit/", views.product_form, name="product_edit"),
    path("<uuid:public_id>/archive/", views.product_archive, name="product_archive"),
    path("<uuid:public_id>/restore/", views.product_restore, name="product_restore"),
    path("<uuid:public_id>/delete/", views.product_delete, name="product_delete"),
    path("<uuid:public_id>/barcode.svg", views.product_barcode_svg, name="product_barcode"),
    path("<uuid:public_id>/labels/", views.product_labels, name="product_labels"),
    path("<uuid:product_id>/variants/new/", views.variant_form, name="variant_create"),
    path("<uuid:product_id>/variants/<uuid:public_id>/edit/", views.variant_form, name="variant_edit"),
]
