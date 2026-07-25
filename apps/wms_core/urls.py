from django.urls import path

from apps.wms_alterations import views as alteration_views
from apps.wms_attendance import views as attendance_views
from apps.wms_orders import views as order_views
from apps.wms_production import views as production_views
from apps.wms_salary import views as salary_views
from apps.wms_workforce import views as workforce_views

from . import views

app_name = "wms"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path(
        "orders/",
        order_views.order_list,
        name="order_list",
    ),
    path(
        "orders/new/",
        order_views.order_create_batch,
        name="order_create_batch",
    ),
    path(
        "orders/finish/",
        order_views.order_finish_batch,
        name="order_finish_batch",
    ),
    path(
        "orders/<uuid:public_id>/",
        order_views.order_detail,
        name="order_detail",
    ),
    path(
        "alterations/",
        alteration_views.alteration_list,
        name="alteration_list",
    ),
    path(
        "alterations/new/",
        alteration_views.alteration_create,
        name="alteration_create",
    ),
    path(
        "alterations/<uuid:public_id>/",
        alteration_views.alteration_detail,
        name="alteration_detail",
    ),
    path(
        "alterations/<uuid:public_id>/edit/",
        alteration_views.alteration_edit,
        name="alteration_edit",
    ),
    path(
        "alterations/<uuid:public_id>/complete/",
        alteration_views.alteration_complete,
        name="alteration_complete",
    ),
    path(
        "attendance/",
        attendance_views.attendance_list,
        name="attendance_list",
    ),
    path(
        "attendance/new/",
        attendance_views.attendance_create,
        name="attendance_create",
    ),
    path(
        "attendance/<uuid:public_id>/",
        attendance_views.attendance_detail,
        name="attendance_detail",
    ),
    path(
        "attendance/<uuid:public_id>/correct/",
        attendance_views.attendance_correct,
        name="attendance_correct",
    ),
    path(
        "production/",
        production_views.production_entry_list,
        name="production_entry_list",
    ),
    path(
        "production/new/",
        production_views.production_entry_create,
        name="production_entry_create",
    ),
    path(
        "production/<uuid:public_id>/",
        production_views.production_entry_detail,
        name="production_entry_detail",
    ),
    path(
        "production/<uuid:public_id>/correct/",
        production_views.production_entry_correct,
        name="production_entry_correct",
    ),
    path(
        "salary/",
        salary_views.salary_list,
        name="salary_list",
    ),
    path(
        "salary/calculate/",
        salary_views.salary_calculate,
        name="salary_calculate",
    ),
    path(
        "salary/<uuid:public_id>/",
        salary_views.salary_detail,
        name="salary_detail",
    ),
    path(
        "salary/<uuid:public_id>/finalize/",
        salary_views.salary_finalize,
        name="salary_finalize",
    ),
    path("employees/", workforce_views.employee_list, name="employee_list"),
    path(
        "employees/new/",
        workforce_views.employee_form,
        name="employee_create",
    ),
    path(
        "employees/<uuid:public_id>/",
        workforce_views.employee_detail,
        name="employee_detail",
    ),
    path(
        "employees/<uuid:public_id>/edit/",
        workforce_views.employee_form,
        name="employee_edit",
    ),
    path(
        "employees/<uuid:public_id>/<str:action>/",
        workforce_views.employee_status,
        name="employee_status",
    ),
    path(
        "employees/<uuid:employee_public_id>/assignments/add/",
        workforce_views.assignment_add,
        name="assignment_add",
    ),
    path(
        "employees/<uuid:employee_public_id>/assignments/"
        "<uuid:assignment_public_id>/rate/",
        workforce_views.assignment_rate,
        name="assignment_rate",
    ),
    path(
        "employees/<uuid:employee_public_id>/assignments/"
        "<uuid:assignment_public_id>/<str:action>/",
        workforce_views.assignment_status,
        name="assignment_status",
    ),
    path(
        "categories/",
        workforce_views.category_list,
        name="category_list",
    ),
    path(
        "categories/new/",
        workforce_views.category_form,
        name="category_create",
    ),
    path(
        "categories/<uuid:public_id>/edit/",
        workforce_views.category_form,
        name="category_edit",
    ),
    path(
        "categories/<uuid:public_id>/<str:action>/",
        workforce_views.category_status,
        name="category_status",
    ),
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
