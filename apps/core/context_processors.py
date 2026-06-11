from django.conf import settings


def platform_branding(request):
    return {
        "PRODUCT_NAME": settings.PRODUCT_NAME,
        "PLATFORM_SUPPORT_EMAIL": settings.PLATFORM_SUPPORT_EMAIL,
        "PLATFORM_WEBSITE": settings.PLATFORM_WEBSITE,
        "PLATFORM_TERMS_URL": settings.PLATFORM_TERMS_URL,
        "PLATFORM_PRIVACY_URL": settings.PLATFORM_PRIVACY_URL,
        "PLATFORM_PRIMARY_COLOR": settings.PLATFORM_PRIMARY_COLOR,
    }


def business_context(request):
    business = getattr(request, "business", None)
    membership = getattr(request, "membership", None)
    perms = membership.permission_set if membership else set()
    unread = 0
    if business and membership:
        from apps.notifications.models import Notification

        unread = Notification.objects.for_business(business).filter(
            recipient=request.user, is_read=False
        ).count()
    return {
        "current_business": business,
        "current_membership": membership,
        "business_perms": perms,
        "unread_notifications": unread,
    }
