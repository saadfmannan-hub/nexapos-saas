"""Human-readable, business-local activity labels for platform staff."""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.core.date_ranges import business_localtime

from .models import Membership

ACTIVITY_INTERVAL = timedelta(minutes=1)


def record_membership_activity(membership, *, user, now=None):
    """Persist activity at most once per minute for one active membership."""
    if (
        not user
        or not user.is_authenticated
        or not user.is_active
        or not isinstance(membership, Membership)
        or membership.pk is None
        or not membership.is_active
        or membership.user_id != user.pk
    ):
        return False

    current = now or timezone.now()
    cutoff = current - ACTIVITY_INTERVAL
    if membership.last_activity_at and membership.last_activity_at > cutoff:
        return False

    # The conditional update avoids duplicate writes from simultaneous tabs.
    updated = (
        Membership.objects.filter(
            pk=membership.pk,
            business_id=membership.business_id,
            user_id=user.pk,
            is_active=True,
        )
        .filter(Q(last_activity_at__isnull=True) | Q(last_activity_at__lte=cutoff))
        .update(last_activity_at=current)
    )
    if updated:
        membership.last_activity_at = current
    return bool(updated)


def format_last_activity(value, *, business=None, now=None):
    """Return (label, online) using the activity timestamp, never last_login."""
    if value is None:
        return "Never active", False

    current = now or timezone.now()
    age_seconds = max(0, (current - value).total_seconds())
    if age_seconds <= 5 * 60:
        return "Online now", True
    if age_seconds < 60 * 60:
        return f"{max(6, int(age_seconds // 60))} min ago", False
    if age_seconds < 24 * 60 * 60:
        hours = int(age_seconds // (60 * 60))
        return f"{hours} {'hour' if hours == 1 else 'hours'} ago", False
    if age_seconds < 30 * 24 * 60 * 60:
        days = int(age_seconds // (24 * 60 * 60))
        return f"{days} {'day' if days == 1 else 'days'} ago", False

    local = business_localtime(business, value=value)
    return f"{local:%b} {local.day}, {local.year}", False
