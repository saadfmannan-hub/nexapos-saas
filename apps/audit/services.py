"""Single entry point for writing audit records."""
from .models import AuditLog


def client_ip(request):
    if request is None:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log(
    action,
    *,
    business=None,
    user=None,
    request=None,
    module="",
    obj=None,
    description="",
    old_values=None,
    new_values=None,
):
    if request is not None:
        user = user or (request.user if request.user.is_authenticated else None)
        business = business or getattr(request, "business", None)
    object_type = obj.__class__.__name__ if obj is not None else ""
    object_id = str(getattr(obj, "public_id", getattr(obj, "pk", ""))) if obj is not None else ""
    return AuditLog.objects.create(
        business=business,
        user=user,
        action=action,
        module=module,
        object_type=object_type,
        object_id=object_id,
        description=description[:400],
        old_values=old_values,
        new_values=new_values,
        ip_address=client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:300] if request else ""),
    )
