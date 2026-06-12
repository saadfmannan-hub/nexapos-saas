from django.urls import path

from . import views

app_name = "suppliers"

urlpatterns = [
    path("", views.supplier_list, name="list"),
    path("new/", views.supplier_form, name="create"),
    path("<uuid:public_id>/", views.supplier_detail, name="detail"),
    path("<uuid:public_id>/edit/", views.supplier_form, name="edit"),
]
