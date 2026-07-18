"""Core views: landing redirect, PWA manifest, offline page, protected media,
error handlers."""
import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render


def home(request):
    if request.user.is_authenticated:
        from apps.accounts.services import post_login_redirect

        return post_login_redirect(request)
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


def service_worker(request):
    """Serve the service worker from the site root so its scope covers '/'."""
    from django.contrib.staticfiles import finders
    from django.http import HttpResponse

    path = finders.find("js/sw.js")
    if not path:
        raise Http404
    with open(path, "rb") as f:
        return HttpResponse(f.read(), content_type="application/javascript")


def protected_media(request, path):
    """Serve uploaded files with tenant checks in development.

    In production, front this with X-Accel-Redirect (nginx) using the
    same authorization rules. Business logos are public-ish (shown on
    receipts); everything else requires membership of the owning tenant.
    """
    if not request.user.is_authenticated:
        raise Http404
    media_root = Path(settings.MEDIA_ROOT).resolve()
    full_path = media_root / path
    try:
        full_path = full_path.resolve()
        normalized = full_path.relative_to(media_root).as_posix()
    except (ValueError, OSError):
        raise Http404 from None
    if not full_path.is_file():
        raise Http404

    # Tenant ownership check for sensitive folders
    normalized_kind = normalized.casefold()
    force_attachment = False
    if normalized_kind.startswith(("expenses/", "purchases/")):
        business = getattr(request, "business", None)
        if business is None:
            raise Http404
        if normalized_kind.startswith("expenses/"):
            from apps.expenses.models import Expense
            from apps.subscriptions.access import AccessAction, evaluate_access

            expense = (
                Expense.objects.for_business(business)
                .select_related("branch")
                .filter(attachment=normalized, branch__business=business)
                .first()
            )
            decision = evaluate_access(
                request,
                "expenses",
                permission_code="expenses.view",
                action=AccessAction.READ,
            )
            if expense is None or not decision.allowed:
                raise Http404
            allowed_branches = decision.context.membership.allowed_branch_ids
            if (
                allowed_branches is not None
                and expense.branch_id not in allowed_branches
            ):
                raise Http404
            force_attachment = True
        else:
            from django.db.models import F, Q

            from apps.purchases.models import Purchase
            from apps.subscriptions.access import AccessAction, evaluate_access

            purchase = (
                Purchase.objects.for_business(business)
                .select_related("supplier", "branch", "warehouse")
                .filter(
                    attachment=normalized,
                    supplier__business=business,
                    branch__business=business,
                    warehouse__business=business,
                )
                .filter(
                    Q(warehouse__branch_id=F("branch_id"))
                    | Q(warehouse__branch__isnull=True)
                )
                .first()
            )
            decision = evaluate_access(
                request,
                "purchases",
                permission_code="purchases.view",
                action=AccessAction.READ,
            )
            if purchase is None or not decision.allowed:
                raise Http404
            membership = decision.context.membership
            allowed_branches = membership.allowed_branch_ids
            allowed_warehouses = membership.allowed_warehouse_ids
            if (
                allowed_branches is not None
                and purchase.branch_id not in allowed_branches
            ) or (
                allowed_warehouses is not None
                and purchase.warehouse_id not in allowed_warehouses
            ):
                raise Http404
            force_attachment = True

    content_type, _ = mimetypes.guess_type(str(full_path))
    response = FileResponse(
        open(full_path, "rb"),
        content_type=content_type or "application/octet-stream",
        as_attachment=force_attachment,
        filename=full_path.name if force_attachment else "",
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


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
