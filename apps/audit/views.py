from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from apps.core.date_ranges import date_range_querystring, resolve_date_range
from apps.subscriptions.access import AccessAction
from apps.subscriptions.decorators import module_permission_required

from .models import AuditLog


@module_permission_required("audit_logs", "audit.view", action=AccessAction.READ)
def audit_list(request):
    qs = AuditLog.objects.filter(business=request.business).select_related("user")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(action__icontains=q) | Q(description__icontains=q) |
                       Q(user__email__icontains=q) | Q(object_type__icontains=q))
    module = request.GET.get("module", "")
    if module:
        qs = qs.filter(module=module)
    date_from, date_to = resolve_date_range(request.GET, request.business)
    qs = qs.filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )
    modules = (
        AuditLog.objects.filter(business=request.business)
        .exclude(module="").values_list("module", flat=True).distinct()
    )
    paginator = Paginator(qs, 40)
    page_obj = paginator.get_page(request.GET.get("page"))
    querystring = date_range_querystring(request.GET, date_from, date_to)
    return render(request, "audit/list.html", {
        "page_obj": page_obj, "q": q, "modules": modules, "active_nav": "audit",
        "date_from": date_from, "date_to": date_to,
        "querystring": f"{querystring}&" if querystring else "",
    })
