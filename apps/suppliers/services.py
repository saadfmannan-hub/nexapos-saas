"""Authorized Supplier profile mutations."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from apps.audit import services as audit
from apps.subscriptions import services as subscription_services
from apps.subscriptions.access import AccessAction, require_actor_access

from .models import Supplier

SUPPLIER_PROFILE_FIELDS = (
    "name",
    "code",
    "contact_person",
    "mobile",
    "email",
    "address",
    "tax_number",
    "payment_terms",
    "notes",
    "is_active",
)


def _next_supplier_code(business):
    number = Supplier.objects.for_business(business).count() + 1
    while Supplier.objects.for_business(business).filter(code=f"SUP-{number:04d}").exists():
        number += 1
    return f"SUP-{number:04d}"


@transaction.atomic
def save_supplier(
    *,
    business,
    values,
    user,
    supplier=None,
    membership=None,
    request=None,
):
    """Create or update one tenant Supplier after central authorization."""

    request_business = getattr(request, "business", None)
    if request_business is not None and request_business.pk != getattr(business, "pk", None):
        raise Http404
    business = (
        business.__class__.objects.select_for_update(no_key=True).filter(pk=business.pk).first()
    )
    if business is None:
        raise Http404

    require_actor_access(
        user,
        business,
        "suppliers",
        permission_code="suppliers.manage",
        action=AccessAction.WRITE,
        membership=membership,
        request=None,
    )

    if supplier is None:
        target = Supplier(business=business)
        was_active = False
    else:
        target = (
            Supplier.objects.select_for_update()
            .filter(pk=getattr(supplier, "pk", None), business=business)
            .first()
        )
        if target is None:
            raise Http404
        was_active = target.is_active

    for field in SUPPLIER_PROFILE_FIELDS:
        if field in values:
            setattr(target, field, values[field])

    if target.is_active and not was_active:
        subscription_services.check_limit(business, "suppliers")

    target.code = str(target.code or "").strip() or _next_supplier_code(business)
    duplicate_codes = Supplier.objects.for_business(business).filter(code__iexact=target.code)
    if target.pk:
        duplicate_codes = duplicate_codes.exclude(pk=target.pk)
    if duplicate_codes.exists():
        raise ValidationError("This supplier code is already in use.")

    target.save()
    audit.log(
        "supplier.saved",
        business=business,
        user=user,
        request=request,
        module="suppliers",
        obj=target,
        description=f"Supplier '{target.name}' saved.",
    )
    return target
