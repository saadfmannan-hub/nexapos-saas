from django.urls import path

from . import views

app_name = "registers"

urlpatterns = [
    path("", views.shift_list, name="shift_list"),
    path("open/", views.shift_open, name="shift_open"),
    path("registers/new/", views.register_create, name="register_create"),
    path("<uuid:public_id>/", views.shift_detail, name="shift_detail"),
    path("<uuid:public_id>/close/", views.shift_close, name="shift_close"),
    path("<uuid:public_id>/approve/", views.shift_approve, name="shift_approve"),
    path("<uuid:public_id>/reopen/", views.shift_reopen, name="shift_reopen"),
]
