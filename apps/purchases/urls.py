from django.urls import path

from . import views

app_name = "purchases"

urlpatterns = [
    path("", views.purchase_list, name="list"),
    path("new/", views.purchase_create, name="create"),
    path("<uuid:public_id>/", views.purchase_detail, name="detail"),
    path("<uuid:public_id>/receive/", views.purchase_receive, name="receive"),
    path("<uuid:public_id>/pay/", views.purchase_pay, name="pay"),
    path("<uuid:public_id>/return/", views.purchase_return, name="return"),
    path("<uuid:public_id>/cancel/", views.purchase_cancel, name="cancel"),
]
