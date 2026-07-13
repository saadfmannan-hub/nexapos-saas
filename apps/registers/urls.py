from django.urls import path

from . import views

app_name = "registers"

urlpatterns = [
    path("", views.shift_list, name="shift_list"),
    path("open/", views.shift_open, name="shift_open"),
    path("registers/new/", views.register_form, name="register_create"),
    path(
        "registers/<uuid:public_id>/edit/",
        views.register_form,
        name="register_edit",
    ),
    path(
        "registers/<uuid:public_id>/archive/",
        views.register_archive,
        name="register_archive",
    ),
    path(
        "registers/<uuid:public_id>/reactivate/",
        views.register_reactivate,
        name="register_reactivate",
    ),
    path(
        "registers/<uuid:public_id>/delete/",
        views.register_delete,
        name="register_delete",
    ),
    path("<uuid:public_id>/", views.shift_detail, name="shift_detail"),
    path("<uuid:public_id>/close/", views.shift_close, name="shift_close"),
    path("<uuid:public_id>/approve/", views.shift_approve, name="shift_approve"),
    path("<uuid:public_id>/reopen/", views.shift_reopen, name="shift_reopen"),
]
