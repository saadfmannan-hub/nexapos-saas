from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.decorators import business_required

from .models import Notification


@business_required
def notification_list(request):
    qs = Notification.objects.for_business(request.business).filter(
        recipient=request.user
    )
    severity = request.GET.get("severity", "")
    if severity:
        qs = qs.filter(severity=severity)
    unread = request.GET.get("unread", "")
    if unread:
        qs = qs.filter(is_read=False)
    paginator = Paginator(qs, 30)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "notifications/list.html", {
        "page_obj": page_obj, "active_nav": "", "querystring": "",
        "severities": Notification.Severity.choices,
    })


@require_POST
@business_required
def mark_read(request, pk):
    Notification.objects.for_business(request.business).filter(
        pk=pk, recipient=request.user
    ).update(is_read=True, read_at=timezone.now())
    next_url = request.POST.get("next", "")
    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect("notifications:list")


@require_POST
@business_required
def mark_all_read(request):
    Notification.objects.for_business(request.business).filter(
        recipient=request.user, is_read=False
    ).update(is_read=True, read_at=timezone.now())
    messages.success(request, "All notifications marked as read.")
    return redirect("notifications:list")
