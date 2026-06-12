from django.urls import path

from . import views

app_name = "expenses"

urlpatterns = [
    path("", views.expense_list, name="list"),
    path("new/", views.expense_create, name="create"),
    path("categories/", views.category_manage, name="categories"),
    path("<uuid:public_id>/edit/", views.expense_create, name="edit"),
    path("<uuid:public_id>/<str:action>/", views.expense_action, name="action"),
]
