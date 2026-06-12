from django.urls import path

from . import views

app_name = "branches"

urlpatterns = [
    path("", views.branch_list, name="list"),
    path("new/", views.branch_form, name="branch_create"),
    path("<uuid:public_id>/edit/", views.branch_form, name="branch_edit"),
    path("warehouses/new/", views.warehouse_form, name="warehouse_create"),
    path("warehouses/<uuid:public_id>/edit/", views.warehouse_form, name="warehouse_edit"),
]
