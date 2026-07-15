from django.urls import path

from . import views

app_name = "expenses"

urlpatterns = [
    path("", views.expense_list, name="list"),
    path("new/", views.expense_create, name="create"),
    path("categories/", views.category_manage, name="categories"),
    path("recurring/", views.recurring_template_list, name="recurring_list"),
    path(
        "recurring/new/",
        views.recurring_template_form,
        name="recurring_create",
    ),
    path(
        "recurring/<uuid:public_id>/edit/",
        views.recurring_template_form,
        name="recurring_edit",
    ),
    path(
        "recurring/<uuid:public_id>/delete/",
        views.recurring_template_delete,
        name="recurring_delete",
    ),
    path(
        "recurring/<uuid:public_id>/<str:action>/",
        views.recurring_template_action,
        name="recurring_action",
    ),
    path("<uuid:public_id>/edit/", views.expense_create, name="edit"),
    path("<uuid:public_id>/<str:action>/", views.expense_action, name="action"),
]
