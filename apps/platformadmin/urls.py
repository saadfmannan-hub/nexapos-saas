from django.urls import path

from . import views

app_name = "platformadmin"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("businesses/", views.business_list, name="business_list"),
    path("businesses/new/", views.business_create, name="business_create"),
    path("businesses/<uuid:public_id>/", views.business_detail, name="business_detail"),
    path(
        "businesses/<uuid:business_public_id>/payments/"
        "<uuid:payment_public_id>/edit/",
        views.subscription_payment_edit,
        name="payment_edit",
    ),
    path(
        "businesses/<uuid:business_public_id>/payments/"
        "<uuid:payment_public_id>/reverse/",
        views.subscription_payment_reverse,
        name="payment_reverse",
    ),
    path("businesses/<uuid:public_id>/support-access/", views.support_access, name="support_access"),
    path("businesses/<uuid:public_id>/login-as/", views.support_login_as, name="login_as"),
    path("support/exit/", views.support_exit, name="support_exit"),
    path("settings/", views.platform_settings, name="settings"),
    path("businesses/<uuid:public_id>/<str:action>/", views.business_action, name="business_action"),
    path("plans/", views.plan_list, name="plan_list"),
    path("plans/new/", views.plan_form, name="plan_create"),
    path("plans/<int:pk>/edit/", views.plan_form, name="plan_edit"),
    path("coupons/", views.coupon_list, name="coupon_list"),
    path("announcements/", views.announcement_list, name="announcements"),
    path("audit/", views.platform_audit, name="audit"),
]
