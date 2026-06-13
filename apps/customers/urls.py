from django.urls import path

from . import views

app_name = "customers"

urlpatterns = [
    path("", views.customer_list, name="list"),
    path("new/", views.customer_form, name="create"),
    path("export/", views.customer_export, name="export"),
    path("import/", views.customer_import, name="import"),
    path("import/template/", views.customer_import_template, name="import_template"),
    path("<uuid:public_id>/", views.customer_detail, name="detail"),
    path("<uuid:public_id>/edit/", views.customer_form, name="edit"),
    path("<uuid:public_id>/payment/", views.customer_payment, name="payment"),
    path("<uuid:public_id>/statement/", views.customer_statement, name="statement"),
]
