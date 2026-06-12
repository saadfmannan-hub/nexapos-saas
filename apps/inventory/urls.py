from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("stock/", views.stock_list, name="stock_list"),
    path("movements/", views.movement_list, name="movement_list"),
    path("item-search/", views.item_search, name="item_search"),
    path("transfers/", views.transfer_list, name="transfer_list"),
    path("transfers/new/", views.transfer_create, name="transfer_create"),
    path("transfers/<uuid:public_id>/<str:action>/", views.transfer_action, name="transfer_action"),
    path("adjustments/", views.adjustment_list, name="adjustment_list"),
    path("adjustments/new/", views.adjustment_create, name="adjustment_create"),
    path("adjustments/<uuid:public_id>/<str:action>/", views.adjustment_action, name="adjustment_action"),
    path("counts/", views.count_list, name="count_list"),
    path("counts/<uuid:public_id>/", views.count_detail, name="count_detail"),
]
