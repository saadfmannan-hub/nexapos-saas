"""Core views: landing redirect, PWA manifest, offline page, protected media,
error handlers."""
import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render


def home(request):
    if request.user.is_authenticated:
        if getattr(request, "business", None):
            return redirect("dashboard")
        if request.user.is_platform_admin:
            return redirect("platformadmin:dashboard")
        return redirect("tenants:no_business")
    return redirect("accounts:login")


def manifest(request):
    return JsonResponse({
        "name": settings.PRODUCT_NAME,
        "short_name": settings.PRODUCT_NAME,
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": settings.PLATFORM_PRIMARY_COLOR,
        "icons": [
            {"src": "/static/icons/favicon.svg", "sizes": "any",
             "type": "image/svg+xml", "purpose": "any"},
        ],
    }, content_type="application/manifest+json")


def offline(request):
    return render(request, "errors/offline.html")


def protected_media(request, path):
    """Serve uploaded files with tenant checks in development.

    In production, front this with X-Accel-Redirect (nginx) using the
    same authorization rules. Business logos are public-ish (shown on
    receipts); everything else requires membership of the owning tenant.
    """
    if not request.user.is_authenticated:
        raise Http404
    full_path = Path(settings.MEDIA_ROOT) / path
    try:
        full_path = full_path.resolve()
        full_path.relative_to(Path(settings.MEDIA_ROOT).resolve())
    except (ValueError, OSError):
        raise Http404
    if not full_path.is_file():
        raise Http404

    # Tenant ownership check for sensitive folders
    sensitive_prefixes = ("expenses/", "purchases/")
    normalized = path.replace("\\", "/")
    if normalized.startswith(sensitive_prefixes):
        business = getattr(request, "business", None)
        if business is None:
            raise Http404
        from apps.expenses.models import Expense
        from apps.purchases.models import Purchase

        owned = (
            Expense.objects.for_business(business).filter(
                attachment=normalized).exists()
            or Purchase.objects.for_business(business).filter(
                attachment=normalized).exists()
        )
        if not owned:
            raise Http404

    content_type, _ = mimetypes.guess_type(str(full_path))
    return FileResponse(open(full_path, "rb"),
                        content_type=content_type or "application/octet-stream")


def error_400(request, exception=None):
    return render(request, "errors/error.html",
                  {"code": 400, "title": "Bad request",
                   "message": "The request could not be understood."}, status=400)


def error_403(request, exception=None):
    return render(request, "errors/error.html",
                  {"code": 403, "title": "Access denied",
                   "message": "You do not have permission to do that."}, status=403)


def error_404(request, exception=None):
    return render(request, "errors/error.html",
                  {"code": 404, "title": "Page not found",
                   "message": "The page you are looking for does not exist or "
                              "belongs to another business."}, status=404)


def error_500(request):
    import uuid as _uuid

    error_id = str(_uuid.uuid4())[:8]
    import logging

    logging.getLogger("nexapos").error("Unhandled error %s on %s",
                                       error_id, request.path)
    return render(request, "errors/error.html",
                  {"code": 500, "title": "Something went wrong",
                   "message": f"An unexpected error occurred. Support code: {error_id}"},
                  status=500)
