from django.urls import path

from . import views

app_name = "customers"

urlpatterns = [
    path("", views.customer_list, name="list"),
    path("new/", views.customer_form, name="create"),
    path("<uuid:public_id>/", views.customer_detail, name="detail"),
    path("<uuid:public_id>/edit/", views.customer_form, name="edit"),
    path("<uuid:public_id>/payment/", views.customer_payment, name="payment"),
    path("<uuid:public_id>/statement/", views.customer_statement, name="statement"),
]
