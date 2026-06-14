"""Subscription status badge rendering for the platform admin."""
from django import template
from django.utils.html import format_html

register = template.Library()

# status key -> (label, css modifier)
STATUS = {
    "trial": ("Trial", "sub-trial"),
    "active": ("Active", "sub-active"),
    "grace": ("Grace period", "sub-grace"),
    "expiring_soon": ("Expiring soon", "sub-expiring"),
    "past_due": ("Past due", "sub-expiring"),
    "expired": ("Expired", "sub-expired"),
    "cancelled": ("Cancelled", "sub-expired"),
    "suspended": ("Suspended", "sub-suspended"),
}


@register.simple_tag
def sub_status_badge(subscription):
    """Render the colour-coded badge for a subscription's display status."""
    if subscription is None:
        return format_html('<span class="sub-badge sub-expired">No subscription</span>')
    status = subscription.display_status
    label, css = STATUS.get(status, (status.replace("_", " ").title(), "sub-grace"))
    return format_html('<span class="sub-badge {}">{}</span>', css, label)


@register.filter
def status_label(status):
    return STATUS.get(status, (status.replace("_", " ").title(), ""))[0]
