from django import forms as django_forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import F, Q
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.branches.models import Branch, Warehouse
from apps.catalog.forms import QuickProductForm
from apps.core.date_ranges import (
    business_localdate,
    date_range_querystring,
    resolve_date_range,
)
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.core.money import D
from apps.subscriptions import services as subscription_services
from apps.subscriptions.access import (
    AccessAction,
    evaluate_access,
    evaluate_public_access,
)
from apps.subscriptions.decorators import module_permission_required
from apps.suppliers.models import Supplier, SupplierPayment

from . import services
from .models import Purchase, PurchaseReturnItem


def _purchases_for_request(request, queryset=None):
    """Scope purchase reads and mutations to assigned branches/warehouses."""
    qs = queryset if queryset is not None else Purchase.objects.all()
    allowed_branches = request.membership.allowed_branch_ids
    allowed_warehouses = request.membership.allowed_warehouse_ids
    if allowed_branches is not None:
        qs = qs.filter(branch_id__in=allowed_branches)
    if allowed_warehouses is not None:
        qs = qs.filter(warehouse_id__in=allowed_warehouses)
    return qs.filter(
        business=request.business,
        supplier__business=request.business,
        branch__business=request.business,
        warehouse__business=request.business,
    ).filter(
        Q(warehouse__branch_id=F("branch_id")) | Q(warehouse__branch__isnull=True)
    )


def _validated_purchase_children(purchase):
    """Return display children only after strict tenant/relation validation."""

    business_id = purchase.business_id
    if (
        purchase.supplier.business_id != business_id
        or purchase.branch.business_id != business_id
        or purchase.warehouse.business_id != business_id
        or purchase.warehouse.branch_id not in (None, purchase.branch_id)
    ):
        raise Http404

    items = list(purchase.items.select_related("product", "variant"))
    for item in items:
        if (
            item.business_id != business_id
            or item.product.business_id != business_id
            or (
                item.variant_id is not None
                and (
                    item.variant.business_id != business_id
                    or item.variant.product_id != item.product_id
                )
            )
        ):
            raise Http404

    payments = list(
        purchase.payments.select_related("supplier", "payment_method")
    )
    for payment in payments:
        if (
            payment.business_id != business_id
            or payment.supplier_id != purchase.supplier_id
            or payment.supplier.business_id != business_id
            or (
                payment.payment_method_id is not None
                and payment.payment_method.business_id != business_id
            )
        ):
            raise Http404

    returns = list(
        purchase.purchase_returns.select_related("supplier", "warehouse")
    )
    for purchase_return in returns:
        if (
            purchase_return.business_id != business_id
            or purchase_return.supplier_id != purchase.supplier_id
            or purchase_return.supplier.business_id != business_id
            or purchase_return.warehouse_id != purchase.warehouse_id
            or purchase_return.warehouse.business_id != business_id
        ):
            raise Http404

    item_ids = {item.pk for item in items}
    return_ids = {purchase_return.pk for purchase_return in returns}
    return_items = PurchaseReturnItem.objects.select_related(
        "purchase_return", "purchase_item"
    ).filter(
        Q(purchase_return_id__in=return_ids) | Q(purchase_item_id__in=item_ids)
    )
    for return_item in return_items:
        if (
            return_item.business_id != business_id
            or return_item.purchase_return_id not in return_ids
            or return_item.purchase_item_id not in item_ids
            or return_item.purchase_item.business_id != business_id
            or return_item.purchase_item.purchase_id != purchase.pk
            or not return_item.quantity.is_finite()
            or return_item.quantity <= 0
        ):
            raise Http404

    return items, payments, returns


@module_permission_required("purchases", "purchases.view", action=AccessAction.READ)
def purchase_list(request):
    qs = services.with_pending_cheques(
        _purchases_for_request(
            request,
            Purchase.objects.for_business(request.business).select_related(
                "supplier", "warehouse", "created_by"
            ),
        )
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(purchase_number__icontains=q) |
                       Q(supplier__name__icontains=q) |
                       Q(supplier_invoice_number__icontains=q))
    status = request.GET.get("status", "")
    if status:
        qs = qs.filter(status=status)
    date_from, date_to = resolve_date_range(request.GET, request.business)
    qs = qs.filter(
        purchase_date__gte=date_from,
        purchase_date__lte=date_to,
    ).order_by("-purchase_date", "-created_at")
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    querystring = date_range_querystring(request.GET, date_from, date_to)
    return render(request, "purchases/list.html", {
        "page_obj": page_obj, "q": q, "active_nav": "purchases",
        "statuses": Purchase.Status.choices,
        "date_from": date_from, "date_to": date_to,
        "querystring": f"{querystring}&" if querystring else "",
    })


def _parse_purchase_rows(request):
    """Parse product_id[]/variant_id[]/quantity[]/unit_cost[] POST arrays."""
    from apps.catalog.models import Product, ProductVariant

    business = request.business
    pids = request.POST.getlist("product_id")
    vids = request.POST.getlist("variant_id")
    qtys = request.POST.getlist("quantity")
    costs = request.POST.getlist("unit_cost")
    rows = []
    for i, pid in enumerate(pids):
        if not pid:
            continue
        try:
            product = Product.objects.for_business(business).get(pk=int(pid))
        except (Product.DoesNotExist, ValueError):
            raise ValidationError("Invalid product in line items.") from None
        variant = None
        vid = vids[i] if i < len(vids) else ""
        if vid:
            try:
                variant = ProductVariant.objects.for_business(business).get(
                    pk=int(vid), product=product)
            except (ProductVariant.DoesNotExist, ValueError):
                raise ValidationError("Invalid variant in line items.") from None
        if product.is_meter_tailoring and product.has_variants and variant is None:
            raise ValidationError(f"Select a variant/color for {product.name}.")
        qty = D(qtys[i] if i < len(qtys) else 0)
        if qty == 0:
            continue
        rows.append({
            "product": product, "variant": variant, "quantity": qty,
            "unit_cost": D(costs[i] if i < len(costs) else 0),
        })
    if not rows:
        raise ValidationError("Add at least one line with a quantity.")
    return rows


@module_permission_required(
    "purchases", "purchases.manage", action=AccessAction.WRITE
)
def purchase_create(request):
    suppliers = Supplier.objects.for_business(request.business).filter(is_active=True)
    warehouses = Warehouse.objects.for_business(request.business).filter(is_active=True)
    branches = Branch.objects.for_business(request.business).filter(is_active=True)
    allowed = request.membership.allowed_branch_ids
    if allowed is not None:
        branches = branches.filter(pk__in=allowed)
        warehouses = warehouses.filter(
            Q(branch_id__in=allowed) | Q(branch__isnull=True)
        )
    allowed_warehouses = request.membership.allowed_warehouse_ids
    if allowed_warehouses is not None:
        warehouses = warehouses.filter(pk__in=allowed_warehouses)
    if request.method == "POST":
        try:
            supplier = get_tenant_object(Supplier, request.business,
                                         pk=request.POST.get("supplier_id"))
            warehouse = get_tenant_object(warehouses, request.business,
                                          pk=request.POST.get("warehouse_id"))
            branch = get_tenant_object(branches, request.business,
                                       pk=request.POST.get("branch_id"))
            rows = _parse_purchase_rows(request)
            purchase = services.create_purchase(
                business=request.business, supplier=supplier, branch=branch,
                warehouse=warehouse, rows=rows, user=request.user,
                purchase_date=(
                    request.POST.get("purchase_date")
                    or business_localdate(request.business)
                ),
                due_date=request.POST.get("due_date") or None,
                supplier_invoice_number=request.POST.get("supplier_invoice_number", ""),
                discount=D(request.POST.get("discount")),
                shipping=D(request.POST.get("shipping")),
                other=D(request.POST.get("other")),
                notes=request.POST.get("notes", ""),
                attachment=request.FILES.get("attachment"),
                membership=request.membership,
                request=request,
            )
            messages.success(request, f"Purchase {purchase.purchase_number} created.")
            return redirect("purchases:detail", public_id=purchase.public_id)
        except (ValidationError, django_forms.ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    quick_tailoring_enabled = evaluate_access(
        request,
        "tailoring",
        permission_code="products.manage",
        action=AccessAction.WRITE,
    ).allowed
    return render(request, "purchases/form.html", {
        "suppliers": suppliers, "warehouses": warehouses, "branches": branches,
        "active_nav": "purchases", "today": business_localdate(request.business),
        "quick_product_form": QuickProductForm(
            request.business,
            tailoring_enabled=quick_tailoring_enabled,
        ),
        "can_quick_add_product": request.membership.has_perm("products.manage"),
    })


def _form_errors(form):
    return {
        field: [str(error) for error in errors]
        for field, errors in form.errors.items()
    }


@module_permission_required(
    "purchases", "purchases.manage", action=AccessAction.WRITE
)
@require_permission("products.manage")
@require_POST
def quick_add_product(request):
    """Create a standard product for the current purchase without posting stock."""
    tailoring_enabled = evaluate_access(
        request,
        "tailoring",
        permission_code="products.manage",
        action=AccessAction.WRITE,
    ).allowed
    form = QuickProductForm(
        request.business,
        request.POST,
        tailoring_enabled=tailoring_enabled,
    )
    if not form.is_valid():
        return JsonResponse(
            {"ok": False, "errors": _form_errors(form)}, status=400,
        )

    try:
        product = services.quick_add_product(
            business=request.business,
            form=form,
            user=request.user,
            membership=request.membership,
            request=request,
        )
    except (
        subscription_services.LimitExceeded,
        subscription_services.SubscriptionInactive,
    ) as exc:
        return JsonResponse({
            "ok": False, "errors": {"__all__": [str(exc)]},
        }, status=400)
    except IntegrityError:
        return JsonResponse({
            "ok": False,
            "errors": {"sku": ["This SKU is already in use."]},
        }, status=400)

    unit_label = product.unit.abbreviation or product.unit.name
    return JsonResponse({
        "ok": True,
        "product": {
            "product_id": product.id,
            "variant_id": None,
            "label": product.name,
            "sku": product.sku,
            "unit": unit_label,
            "unit_cost": str(product.purchase_price),
        },
    }, status=201)


@module_permission_required("purchases", "purchases.view", action=AccessAction.READ)
def purchase_detail(request, public_id):
    purchase = get_tenant_object(
        _purchases_for_request(
            request,
            Purchase.objects.select_related(
                "supplier", "warehouse", "branch", "created_by"
            ),
        ),
        request.business, public_id=public_id,
    )
    items, payments, returns = _validated_purchase_children(purchase)
    manage_decision = evaluate_access(
        request,
        "purchases",
        permission_code="purchases.manage",
        action=AccessAction.WRITE,
    )
    email_decision = evaluate_access(
        request,
        "purchases",
        permission_code="purchases.view",
        action=AccessAction.WRITE,
    )
    supplier_decision = evaluate_access(
        request,
        "suppliers",
        permission_code="suppliers.view",
        action=AccessAction.READ,
    )
    return render(request, "purchases/detail.html", {
        "purchase": purchase, "items": items, "payments": payments,
        "returns": returns, "active_nav": "purchases",
        "can_manage": manage_decision.allowed,
        "can_email": email_decision.allowed,
        "can_view_supplier": supplier_decision.allowed,
        "cheque_issue_date_default": business_localdate(request.business),
    })


def _collect_quantities(request, prefix):
    quantities = {}
    for key, value in request.POST.items():
        if key.startswith(prefix) and value.strip():
            try:
                quantities[int(key[len(prefix):])] = D(value)
            except (ValueError, TypeError):
                continue
    return quantities


@module_permission_required(
    "purchases", "purchases.manage", action=AccessAction.WRITE
)
@require_POST
def purchase_receive(request, public_id):
    purchase = get_tenant_object(
        _purchases_for_request(request),
        request.business,
        public_id=public_id,
    )
    if request.method == "POST":
        try:
            services.receive_purchase(
                purchase=purchase,
                quantities=_collect_quantities(request, "receive_"),
                user=request.user,
                membership=request.membership,
                request=request,
            )
            messages.success(request, "Goods received — stock and supplier payable updated.")
        except ValidationError as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("purchases:detail", public_id=public_id)


def _parse_payment_rows(request):
    fields = [
        "method", "amount", "cheque_number", "bank_name", "cheque_issue_date",
        "due_date", "reference",
    ]
    values = {field: request.POST.getlist(field) for field in fields}
    count = max((len(items) for items in values.values()), default=0)
    rows = []
    for index in range(count):
        row = {
            field: items[index] if index < len(items) else ""
            for field, items in values.items()
        }
        if any(str(value).strip() for value in row.values()):
            rows.append(row)
    return rows


@module_permission_required(
    "purchases", "purchases.manage", action=AccessAction.WRITE
)
@require_POST
def purchase_pay(request, public_id):
    purchase = get_tenant_object(
        _purchases_for_request(request),
        request.business,
        public_id=public_id,
    )
    try:
        payments = services.record_purchase_payments(
            purchase=purchase,
            rows=_parse_payment_rows(request),
            user=request.user,
            membership=request.membership,
            request=request,
        )
        messages.success(
            request,
            f"{len(payments)} payment row{'s' if len(payments) != 1 else ''} recorded.",
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("purchases:detail", public_id=public_id)


@module_permission_required(
    "purchases", "purchases.manage", action=AccessAction.WRITE
)
@require_POST
def purchase_cheque_status(request, public_id, payment_public_id):
    purchase = get_tenant_object(
        _purchases_for_request(request),
        request.business,
        public_id=public_id,
    )
    payment = get_tenant_object(
        SupplierPayment.objects.select_related("purchase", "supplier"),
        request.business,
        public_id=payment_public_id,
        purchase=purchase,
    )
    try:
        services.update_cheque_status(
            payment=payment,
            status=request.POST.get("status", ""),
            user=request.user,
            membership=request.membership,
            request=request,
        )
        messages.success(request, "Cheque status updated.")
    except ValidationError as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("purchases:detail", public_id=public_id)


@module_permission_required(
    "purchases", "purchases.manage", action=AccessAction.WRITE
)
@require_POST
def purchase_return(request, public_id):
    purchase = get_tenant_object(
        _purchases_for_request(request),
        request.business,
        public_id=public_id,
    )
    if request.method == "POST":
        try:
            services.return_purchase(
                purchase=purchase,
                quantities=_collect_quantities(request, "return_"),
                user=request.user, reason=request.POST.get("reason", ""),
                membership=request.membership,
                request=request,
            )
            messages.success(request, "Purchase return processed.")
        except ValidationError as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("purchases:detail", public_id=public_id)


# ---------------------------------------------------------------------------
# Purchase order documents: print / PDF / supplier share link / email
# ---------------------------------------------------------------------------
PO_SHARE_SALT = "purchase-order-share"
PO_SHARE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _po_context(purchase, **extra):
    items, _payments, _returns = _validated_purchase_children(purchase)
    return {
        "purchase": purchase,
        "items": items,
        "business": purchase.business,
        "settings_obj": purchase.business.settings,
        **extra,
    }


@module_permission_required("purchases", "purchases.view", action=AccessAction.READ)
def purchase_print(request, public_id):
    purchase = get_tenant_object(
        _purchases_for_request(
            request,
            Purchase.objects.select_related(
                "supplier", "warehouse", "branch", "business"
            ),
        ),
        request.business, public_id=public_id,
    )
    from django.shortcuts import render as _render

    return _render(request, "invoices/purchase_order.html", _po_context(purchase))


@module_permission_required("purchases", "purchases.view", action=AccessAction.READ)
def purchase_pdf(request, public_id):
    from django.http import HttpResponse

    from apps.reports.pdf import render_pdf

    purchase = get_tenant_object(
        _purchases_for_request(
            request,
            Purchase.objects.select_related(
                "supplier", "warehouse", "branch", "business"
            ),
        ),
        request.business, public_id=public_id,
    )
    pdf = render_pdf("invoices/purchase_order.html",
                     _po_context(purchase, pdf_mode=True))
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="PO-{purchase.purchase_number}.pdf"'
    )
    return response


@module_permission_required("purchases", "purchases.view")
def purchase_share(request, public_id):
    """Generate (and display) a time-limited signed link the supplier can
    open without an account. The token is unguessable and tenant-bound."""
    from django.core import signing

    from apps.audit import services as audit

    purchase = get_tenant_object(
        _purchases_for_request(request),
        request.business,
        public_id=public_id,
    )
    token = signing.dumps({"po": str(purchase.public_id)}, salt=PO_SHARE_SALT)
    share_url = request.build_absolute_uri(f"/purchases/shared/{token}/")
    if request.method == "POST":
        audit.log("purchase.share_link", request=request, module="purchases",
                  obj=purchase,
                  description=f"Share link generated for {purchase.purchase_number}.")
    from django.shortcuts import render as _render

    return _render(request, "purchases/share.html", {
        "purchase": purchase, "share_url": share_url,
        "days": PO_SHARE_MAX_AGE // 86400, "active_nav": "purchases",
    })


def purchase_shared(request, token):
    """Public supplier view of a purchase order (signed, time-limited)."""
    from django.core import signing
    from django.http import Http404
    from django.shortcuts import render as _render

    try:
        data = signing.loads(token, salt=PO_SHARE_SALT, max_age=PO_SHARE_MAX_AGE)
    except signing.BadSignature:
        raise Http404 from None
    try:
        purchase = Purchase.objects.select_related(
            "supplier", "warehouse", "branch", "business"
        ).get(public_id=data.get("po"))
    except (Purchase.DoesNotExist, ValueError):
        raise Http404 from None
    if not evaluate_public_access(
        purchase.business,
        "purchases",
        action=AccessAction.READ,
    ).allowed:
        raise Http404
    return _render(request, "invoices/purchase_order.html",
                   _po_context(purchase, shared_mode=True))


@module_permission_required(
    "purchases", "purchases.view", action=AccessAction.WRITE
)
def purchase_email(request, public_id):
    """Email the PO PDF to the supplier (or a provided address)."""
    from django.core import signing
    from django.core.mail import EmailMessage

    from apps.audit import services as audit
    from apps.reports.pdf import render_pdf

    purchase = get_tenant_object(
        _purchases_for_request(
            request,
            Purchase.objects.select_related("supplier", "business"),
        ),
        request.business, public_id=public_id,
    )
    if request.method != "POST":
        return redirect("purchases:detail", public_id=public_id)
    purchase = services.authorize_purchase_write(
        purchase=purchase,
        user=request.user,
        permission_code="purchases.view",
        membership=request.membership,
        request=request,
    )
    to_email = (request.POST.get("email") or purchase.supplier.email or "").strip()
    if not to_email:
        messages.error(request, "The supplier has no email address — enter one "
                                "in the email field or on the supplier profile.")
        return redirect("purchases:detail", public_id=public_id)

    token = signing.dumps({"po": str(purchase.public_id)}, salt=PO_SHARE_SALT)
    share_url = request.build_absolute_uri(f"/purchases/shared/{token}/")
    pdf = render_pdf("invoices/purchase_order.html",
                     _po_context(purchase, pdf_mode=True))
    email = EmailMessage(
        subject=f"Purchase Order {purchase.purchase_number} from "
                f"{request.business.name}",
        body=(f"Dear {purchase.supplier.contact_person or purchase.supplier.name},\n\n"
              f"Please find attached purchase order {purchase.purchase_number} "
              f"dated {purchase.purchase_date}.\n\n"
              f"You can also view it online: {share_url}\n\n"
              f"Regards,\n{request.business.name}"),
        to=[to_email],
    )
    email.attach(f"PO-{purchase.purchase_number}.pdf", pdf, "application/pdf")
    try:
        email.send(fail_silently=False)
        audit.log("purchase.emailed", request=request, module="purchases",
                  obj=purchase,
                  description=f"PO {purchase.purchase_number} emailed to {to_email}.")
        messages.success(request, f"Purchase order emailed to {to_email}.")
    except Exception:
        messages.error(request, "Email could not be sent — check the email "
                                "settings (EMAIL_HOST) on the server.")
    return redirect("purchases:detail", public_id=public_id)


@module_permission_required(
    "purchases", "purchases.manage", action=AccessAction.WRITE
)
@require_POST
def purchase_cancel(request, public_id):
    purchase = get_tenant_object(
        _purchases_for_request(request),
        request.business,
        public_id=public_id,
    )
    if request.method == "POST":
        try:
            services.cancel_purchase(
                purchase=purchase,
                user=request.user,
                membership=request.membership,
                request=request,
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "Purchase cancelled.")
    return redirect("purchases:detail", public_id=public_id)
