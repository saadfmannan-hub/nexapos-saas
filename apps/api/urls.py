from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from . import views

app_name = "api"

router = DefaultRouter()
router.register("products", views.ProductViewSet, basename="product")
router.register("categories", views.CategoryViewSet, basename="category")
router.register("customers", views.CustomerViewSet, basename="customer")
router.register("sales", views.SaleViewSet, basename="sale")

urlpatterns = [
    path("v1/health/", views.health, name="health"),
    path("v1/auth/token/", obtain_auth_token, name="token"),
    path("v1/me/", views.me, name="me"),
    path("v1/", include(router.urls)),
]
