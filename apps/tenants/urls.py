from django.urls import path

from . import views

app_name = "tenants"

urlpatterns = [
    path("register/", views.register_view, name="register"),
    path("no-business/", views.no_business_view, name="no_business"),
    path("switch/", views.switch_business, name="switch_business"),
    path("onboarding/", views.onboarding_view, name="onboarding"),
    path("settings/profile/", views.profile_view, name="profile"),
    path("settings/", views.settings_view, name="settings"),
]
