"""Notification helpers."""
from django.db import transaction
from django.utils import timezone

from .models import Notification


def notify(business, recipient, title, *, body="", severity="info", category="", link=""):
    return Notification.objects.create(
        business=business,
        recipient=recipient,
        title=title[:160],
        body=body[:500],
        severity=severity,
        category=category,
        link=link,
    )


def notify_role(business, permission_code, title, **kwargs):
    """Notify every active member of the business holding a permission."""
    from apps.accounts.models import Membership

    sent = []
    for m in Membership.objects.for_business(business).filter(
        is_active=True
    ).select_related("role", "user"):
        if m.has_perm(permission_code):
            sent.append(notify(business, m.user, title, **kwargs))
    return sent


def mark_read(notification):
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at"])


@transaction.atomic
def broadcast_announcement(announcement, *, link=""):
    """Fan out a platform announcement to every active member of every
    active (non-suspended) business as an in-app notification.

    One Notification row is created per recipient, each scoped to that
    recipient's business — so the existing tenant-isolated bell count,
    notifications list and mark-as-read all work unchanged. Suspended
    businesses (``is_active=False``) are skipped. Returns the number of
    notifications created.
    """
    from apps.accounts.models import Membership
    from apps.tenants.models import Business

    title = announcement.title
    body = announcement.body or ""
    sent = 0
    active_business_ids = Business.objects.filter(is_active=True).values_list(
        "id", flat=True
    )
    memberships = (
        Membership.objects.filter(
            is_active=True, business_id__in=list(active_business_ids)
        )
        .select_related("business", "user")
    )
    for membership in memberships:
        notify(
            membership.business, membership.user, title,
            body=body, severity="info", category="announcement", link=link,
        )
        sent += 1
    return sent
