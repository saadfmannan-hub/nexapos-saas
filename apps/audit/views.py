from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from apps.core.decorators import require_permission

from .models import AuditLog


@require_permission("audit.view")
def audit_list(request):
    qs = AuditLog.objects.filter(business=request.business).select_related("user")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(action__icontains=q) | Q(description__icontains=q) |
                       Q(user__email__icontains=q) | Q(object_type__icontains=q))
    module = request.GET.get("module", "")
    if module:
        qs = qs.filter(module=module)
    date_from = request.GET.get("from", "")
    date_to = request.GET.get("to", "")
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    modules = (
        AuditLog.objects.filter(business=request.business)
        .exclude(module="").values_list("module", flat=True).distinct()
    )
    paginator = Paginator(qs, 40)
    page_obj = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "audit/list.html", {
        "page_obj": page_obj, "q": q, "modules": modules, "active_nav": "audit",
        "querystring": (params.urlencode() + "&") if params else "",
    })
