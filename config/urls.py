from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.core import views as core_views
from apps.reports import views as report_views

urlpatterns = [
    path("", core_views.home, name="home"),
    path("dashboard/", report_views.dashboard, name="dashboard"),
    path("manifest.webmanifest", core_views.manifest, name="manifest"),
    path("offline/", core_views.offline, name="offline"),
    path("media/<path:path>", core_views.protected_media, name="protected_media"),

    path("accounts/", include("apps.accounts.urls")),
    path("", include("apps.tenants.urls")),
    path("subscription/", include("apps.subscriptions.urls")),
    path("branches/", include("apps.branches.urls")),
    path("products/", include("apps.catalog.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("customers/", include("apps.customers.urls")),
    path("suppliers/", include("apps.suppliers.urls")),
    path("purchases/", include("apps.purchases.urls")),
    path("sales/", include("apps.sales.urls")),
    path("registers/", include("apps.registers.urls")),
    path("expenses/", include("apps.expenses.urls")),
    path("reports/", include("apps.reports.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("audit/", include("apps.audit.urls")),
    path("platform/", include("apps.platformadmin.urls")),
    path("api/", include("apps.api.urls")),

    path("django-admin/", admin.site.urls),
]

handler400 = "apps.core.views.error_400"
handler403 = "apps.core.views.error_403"
handler404 = "apps.core.views.error_404"
handler500 = "apps.core.views.error_500"

if settings.DEBUG:
    from django.conf.urls.static import static

    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
