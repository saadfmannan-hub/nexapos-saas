"""Notification helpers."""
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
