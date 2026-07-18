from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import APIRootView, DefaultRouter

from apps.subscriptions.access import AccessAction
from apps.subscriptions.api_permissions import HasSubscriptionModuleAccess

from . import views

app_name = "api"


class SubscriptionAPIRootView(views.ExplicitAPIContextMixin, APIRootView):
    permission_classes = [HasSubscriptionModuleAccess]
    required_modules = ("pos_core",)
    access_action = AccessAction.READ


class SubscriptionRouter(DefaultRouter):
    APIRootView = SubscriptionAPIRootView


router = SubscriptionRouter()
router.register("products", views.ProductViewSet, basename="product")
router.register("categories", views.CategoryViewSet, basename="category")
router.register("customers", views.CustomerViewSet, basename="customer")
router.register("sales", views.SaleViewSet, basename="sale")

urlpatterns = [
    path("v1/health/", views.health, name="health"),
    path("v1/auth/token/", obtain_auth_token, name="token"),
    path("v1/me/", views.MeView.as_view(), name="me"),
    path("v1/", include(router.urls)),
]
