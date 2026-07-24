from django.urls import path

from . import views

app_name = "wms"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/locations/new/", views.location_form, name="location_create"),
    path(
        "settings/locations/<uuid:public_id>/",
        views.location_form,
        name="location_edit",
    ),
    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_access_form, name="user_access_create"),
    path(
        "users/<uuid:public_id>/",
        views.user_access_form,
        name="user_access_edit",
    ),
]
