from django.urls import path

from . import views

app_name = "platformadmin"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("businesses/", views.business_list, name="business_list"),
    path("businesses/<uuid:public_id>/", views.business_detail, name="business_detail"),
    path("businesses/<uuid:public_id>/support-access/", views.support_access, name="support_access"),
    path("businesses/<uuid:public_id>/<str:action>/", views.business_action, name="business_action"),
    path("plans/", views.plan_list, name="plan_list"),
    path("plans/new/", views.plan_form, name="plan_create"),
    path("plans/<int:pk>/edit/", views.plan_form, name="plan_edit"),
    path("coupons/", views.coupon_list, name="coupon_list"),
    path("announcements/", views.announcement_list, name="announcements"),
    path("audit/", views.platform_audit, name="audit"),
]
