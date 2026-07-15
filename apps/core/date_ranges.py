"""Business-local defaults for date-range filters."""
from datetime import UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone


def _zoneinfo(timezone_name):
    if not timezone_name:
        return None
    try:
        return ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None


def business_timezone(business=None):
    """Resolve business timezone, then Django's timezone, then UTC."""
    business_zone = _zoneinfo(getattr(business, "timezone", ""))
    if business_zone is not None:
        return business_zone
    return _zoneinfo(settings.TIME_ZONE) or UTC


def business_localdate(business=None, *, now=None):
    """Return the local date in the business timezone, with a safe fallback."""
    local_timezone = business_timezone(business)
    current = now or timezone.now()
    if timezone.is_naive(current):
        current = timezone.make_aware(current, local_timezone)
    return timezone.localtime(current, local_timezone).date()


def current_month_date_range(business=None, *, now=None):
    """Return the first day of this business-local month through today."""
    today = business_localdate(business, now=now)
    return today.replace(day=1), today


def resolve_date_range(
    query_params,
    business=None,
    *,
    start_key="from",
    end_key="to",
    now=None,
):
    """Preserve supplied dates and fill only missing range boundaries."""
    default_start, default_end = current_month_date_range(business, now=now)
    date_from = query_params.get(start_key) or default_start.isoformat()
    date_to = query_params.get(end_key) or default_end.isoformat()
    return date_from, date_to


def date_range_querystring(
    query_params,
    date_from,
    date_to,
    *,
    start_key="from",
    end_key="to",
    exclude=("page", "export"),
):
    """Return a query string with the active range and other filters intact."""
    params = query_params.copy()
    for key in exclude:
        params.pop(key, None)
    params[start_key] = str(date_from)
    params[end_key] = str(date_to)
    return params.urlencode()
