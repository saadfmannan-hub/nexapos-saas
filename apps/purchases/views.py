from django import forms as django_forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.branches.models import Branch, Warehouse
from apps.catalog.forms import QuickProductForm
from apps.catalog.models import Product
from apps.core.date_ranges import (
    business_localdate,
    date_range_querystring,
    resolve_date_range,
)
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.core.money import D
from apps.subscriptions import services as subscriptions
from apps.suppliers.models import Supplier, SupplierPayment

from . import services
from .models import Purchase


@require_permission("purchases.view")
def purchase_list(request):
    if not subscriptions.has_feature(request.business, "purchases"):
        return render(request, "inventory/feature_locked.html",
                      {"feature": "Purchases", "active_nav": "purchases"})
    qs = services.with_pending_cheques(
        Purchase.objects.for_business(request.business)
        .select_related("supplier", "warehouse", "created_by")
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
            raise ValidationError("Invalid product in line items.")
        variant = None
        vid = vids[i] if i < len(vids) else ""
        if vid:
            try:
                variant = ProductVariant.objects.for_business(business).get(
                    pk=int(vid), product=product)
            except (ProductVariant.DoesNotExist, ValueError):
                raise ValidationError("Invalid variant in line items.")
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


@require_permission("purchases.manage")
def purchase_create(request):
    if not subscriptions.has_feature(request.business, "purchases"):
        messages.warning(request, "Purchases are not included in your plan.")
        return redirect("dashboard")
    suppliers = Supplier.objects.for_business(request.business).filter(is_active=True)
    warehouses = Warehouse.objects.for_business(request.business).filter(is_active=True)
    branches = Branch.objects.for_business(request.business).filter(is_active=True)
    if request.method == "POST":
        try:
            subscriptions.require_operational(request.business)
            supplier = get_tenant_object(Supplier, request.business,
                                         pk=request.POST.get("supplier_id"))
            warehouse = get_tenant_object(Warehouse, request.business,
                                          pk=request.POST.get("warehouse_id"))
            branch = get_tenant_object(Branch, request.business,
                                       pk=request.POST.get("branch_id"))
            rows = _parse_purchase_rows(request)
            purchase = services.create_purchase(
                business=request.business, supplier=supplier, branch=branch,
                warehouse=warehouse, rows=rows, user=request.user,
                purchase_date=request.POST.get("purchase_date") or timezone.now().date(),
                due_date=request.POST.get("due_date") or None,
                supplier_invoice_number=request.POST.get("supplier_invoice_number", ""),
                discount=D(request.POST.get("discount")),
                shipping=D(request.POST.get("shipping")),
                other=D(request.POST.get("other")),
                notes=request.POST.get("notes", ""),
                attachment=request.FILES.get("attachment"),
                request=request,
            )
            messages.success(request, f"Purchase {purchase.purchase_number} created.")
            return redirect("purchases:detail", public_id=purchase.public_id)
        except (ValidationError, django_forms.ValidationError,
                subscriptions.SubscriptionInactive) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "purchases/form.html", {
        "suppliers": suppliers, "warehouses": warehouses, "branches": branches,
        "active_nav": "purchases", "today": timezone.now().date(),
        "quick_product_form": QuickProductForm(request.business),
        "can_quick_add_product": request.membership.has_perm("products.manage"),
    })


def _form_errors(form):
    return {
        field: [str(error) for error in errors]
        for field, errors in form.errors.items()
    }


@require_permission("purchases.manage")
@require_permission("products.manage")
@require_POST
def quick_add_product(request):
    """Create a standard product for the current purchase without posting stock."""
    if not subscriptions.has_feature(request.business, "purchases"):
        return JsonResponse({
            "ok": False,
            "errors": {"__all__": ["Purchases are not included in your plan."]},
        }, status=403)

    form = QuickProductForm(request.business, request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"ok": False, "errors": _form_errors(form)}, status=400,
        )

    try:
        subscriptions.check_limit(request.business, "products")
        with transaction.atomic():
            product = form.save(commit=False)
            product.business = request.business
            product.product_type = Product.Type.STANDARD
            product.is_active = True
            product.save()
    except (subscriptions.LimitExceeded,
            subscriptions.SubscriptionInactive) as exc:
        return JsonResponse({
            "ok": False, "errors": {"__all__": [str(exc)]},
        }, status=400)
    except IntegrityError:
        return JsonResponse({
            "ok": False,
            "errors": {"sku": ["This SKU is already in use."]},
        }, status=400)

    audit.log(
        "product.saved", request=request, module="catalog", obj=product,
        description=f"Product '{product.name}' quick-added from a purchase.",
    )
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


@require_permission("purchases.view")
def purchase_detail(request, public_id):
    purchase = get_tenant_object(
        Purchase.objects.select_related("supplier", "warehouse", "branch", "created_by"),
        request.business, public_id=public_id,
    )
    items = purchase.items.select_related("product", "variant")
    payments = purchase.payments.select_related("payment_method")
    returns = purchase.purchase_returns.all()
    return render(request, "purchases/detail.html", {
        "purchase": purchase, "items": items, "payments": payments,
        "returns": returns, "active_nav": "purchases",
        "can_manage": request.membership.has_perm("purchases.manage"),
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


@require_permission("purchases.manage")
def purchase_receive(request, public_id):
    purchase = get_tenant_object(Purchase, request.business, public_id=public_id)
    if request.method == "POST":
        try:
            subscriptions.require_operational(request.business)
            services.receive_purchase(
                purchase=purchase,
                quantities=_collect_quantities(request, "receive_"),
                user=request.user, request=request,
            )
            messages.success(request, "Goods received — stock and supplier payable updated.")
        except (ValidationError, subscriptions.SubscriptionInactive) as exc:
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


@require_permission("purchases.manage")
@require_POST
def purchase_pay(request, public_id):
    purchase = get_tenant_object(Purchase, request.business, public_id=public_id)
    try:
        subscriptions.require_operational(request.business)
        payments = services.record_purchase_payments(
            purchase=purchase,
            rows=_parse_payment_rows(request),
            user=request.user,
            request=request,
        )
        messages.success(
            request,
            f"{len(payments)} payment row{'s' if len(payments) != 1 else ''} recorded.",
        )
    except (ValidationError, subscriptions.SubscriptionInactive) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("purchases:detail", public_id=public_id)


@require_permission("purchases.manage")
@require_POST
def purchase_cheque_status(request, public_id, payment_public_id):
    purchase = get_tenant_object(Purchase, request.business, public_id=public_id)
    payment = get_tenant_object(
        SupplierPayment.objects.select_related("purchase", "supplier"),
        request.business,
        public_id=payment_public_id,
        purchase=purchase,
    )
    try:
        subscriptions.require_operational(request.business)
        services.update_cheque_status(
            payment=payment,
            status=request.POST.get("status", ""),
            user=request.user,
            request=request,
        )
        messages.success(request, "Cheque status updated.")
    except (ValidationError, subscriptions.SubscriptionInactive) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("purchases:detail", public_id=public_id)


@require_permission("purchases.manage")
def purchase_return(request, public_id):
    purchase = get_tenant_object(Purchase, request.business, public_id=public_id)
    if request.method == "POST":
        try:
            subscriptions.require_operational(request.business)
            services.return_purchase(
                purchase=purchase,
                quantities=_collect_quantities(request, "return_"),
                user=request.user, reason=request.POST.get("reason", ""),
                request=request,
            )
            messages.success(request, "Purchase return processed.")
        except (ValidationError, subscriptions.SubscriptionInactive) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("purchases:detail", public_id=public_id)


# ---------------------------------------------------------------------------
# Purchase order documents: print / PDF / supplier share link / email
# ---------------------------------------------------------------------------
PO_SHARE_SALT = "purchase-order-share"
PO_SHARE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _po_context(purchase, **extra):
    return {
        "purchase": purchase,
        "items": purchase.items.all(),
        "business": purchase.business,
        "settings_obj": purchase.business.settings,
        **extra,
    }


@require_permission("purchases.view")
def purchase_print(request, public_id):
    purchase = get_tenant_object(
        Purchase.objects.select_related("supplier", "warehouse", "branch", "business"),
        request.business, public_id=public_id,
    )
    from django.shortcuts import render as _render

    return _render(request, "invoices/purchase_order.html", _po_context(purchase))


@require_permission("purchases.view")
def purchase_pdf(request, public_id):
    from django.http import HttpResponse

    from apps.reports.pdf import render_pdf

    purchase = get_tenant_object(
        Purchase.objects.select_related("supplier", "warehouse", "branch", "business"),
        request.business, public_id=public_id,
    )
    pdf = render_pdf("invoices/purchase_order.html",
                     _po_context(purchase, pdf_mode=True))
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="PO-{purchase.purchase_number}.pdf"'
    )
    return response


@require_permission("purchases.view")
def purchase_share(request, public_id):
    """Generate (and display) a time-limited signed link the supplier can
    open without an account. The token is unguessable and tenant-bound."""
    from django.core import signing

    from apps.audit import services as audit

    purchase = get_tenant_object(Purchase, request.business, public_id=public_id)
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
        raise Http404
    try:
        purchase = Purchase.objects.select_related(
            "supplier", "warehouse", "branch", "business"
        ).get(public_id=data.get("po"))
    except (Purchase.DoesNotExist, ValueError):
        raise Http404
    return _render(request, "invoices/purchase_order.html",
                   _po_context(purchase, shared_mode=True))


@require_permission("purchases.view")
def purchase_email(request, public_id):
    """Email the PO PDF to the supplier (or a provided address)."""
    from django.core import signing
    from django.core.mail import EmailMessage

    from apps.audit import services as audit
    from apps.reports.pdf import render_pdf

    purchase = get_tenant_object(
        Purchase.objects.select_related("supplier", "business"),
        request.business, public_id=public_id,
    )
    if request.method != "POST":
        return redirect("purchases:detail", public_id=public_id)
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


@require_permission("purchases.manage")
def purchase_cancel(request, public_id):
    purchase = get_tenant_object(Purchase, request.business, public_id=public_id)
    if request.method == "POST":
        if purchase.items.filter(quantity_received__gt=0).exists():
            messages.error(request, "Purchases with received goods cannot be "
                                    "cancelled — use a purchase return instead.")
        elif purchase.amount_paid > 0 or purchase.payments.filter(
            method=SupplierPayment.Method.CHEQUE,
            cheque_status=SupplierPayment.ChequeStatus.PENDING,
        ).exists():
            messages.error(
                request,
                "Purchases with Paid or Pending Cheques cannot be cancelled.",
            )
        else:
            purchase.status = Purchase.Status.CANCELLED
            purchase.save(update_fields=["status", "updated_at"])
            from apps.audit import services as audit

            audit.log("purchase.cancelled", request=request, module="purchases",
                      obj=purchase,
                      description=f"Purchase {purchase.purchase_number} cancelled.")
            messages.success(request, "Purchase cancelled.")
    return redirect("purchases:detail", public_id=public_id)
