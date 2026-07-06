import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.core.money import D, money
from apps.customers import services as customer_services
from apps.customers.models import Customer
from apps.inventory.models import StockLevel
from apps.registers import services as register_services
from apps.subscriptions import services as subscriptions

from . import calculations, services
from .models import HeldSale, PaymentMethod, Sale, SaleItem, SaleReturn
from .services import SaleError


def _user_branches(request):
    """Branches the member may operate in."""
    from apps.branches.models import Branch

    qs = Branch.objects.for_business(request.business).filter(is_active=True)
    allowed = request.membership.allowed_branch_ids
    if allowed is not None:
        qs = qs.filter(id__in=allowed)
    return qs


def _branch_warehouse(branch):
    """Default warehouse used when selling from a branch."""
    from apps.branches.models import Warehouse

    return (
        Warehouse.objects.for_business(branch.business)
        .filter(is_active=True)
        .filter(Q(branch=branch) | Q(branch__isnull=True))
        .order_by("-is_default")
        .first()
    )


TAILORING_CHECKOUT_FIELDS = (
    "design_type",
    "daraz_details",
    "vip_3d_design",
    "computer_design",
    "priority",
    "customer_notes",
    "workshop_notes",
)


def _checkout_tailoring_details(raw):
    raw_details = raw.get("tailoring_details") or raw.get("tailoring") or {}
    if not isinstance(raw_details, dict):
        return {}
    return {
        key: str(raw_details.get(key, "") or "").strip()[:500]
        for key in TAILORING_CHECKOUT_FIELDS
        if str(raw_details.get(key, "") or "").strip()
    }


# ---------------------------------------------------------------------------
# POS screen
# ---------------------------------------------------------------------------
@require_permission("sales.create")
def pos_view(request):
    from apps.catalog.models import Category

    branches = list(_user_branches(request))
    if not branches:
        messages.error(request, "You are not assigned to any active branch.")
        return redirect("dashboard")

    shift = register_services.get_open_shift(request.business, request.user)
    branch = shift.branch if shift else branches[0]
    warehouse = _branch_warehouse(branch)
    if warehouse is None:
        messages.error(request, "No active warehouse is configured for this branch.")
        return redirect("dashboard")

    categories = Category.objects.for_business(request.business).filter(is_active=True)
    payment_methods = PaymentMethod.objects.for_business(request.business).filter(
        is_active=True
    )
    held_sales = HeldSale.objects.for_business(request.business).filter(
        cashier=request.user
    )[:20]
    walk_in = Customer.objects.for_business(request.business).filter(
        is_walk_in=True
    ).first()
    settings_obj = request.business.settings
    can_sell_without_shift = settings_obj.allow_sale_without_shift
    vat_rate = settings_obj.effective_vat_rate

    return render(request, "pos/pos.html", {
        "active_nav": "pos",
        "branch": branch,
        "warehouse": warehouse,
        "shift": shift,
        "categories": categories,
        "payment_methods": payment_methods,
        "held_sales": held_sales,
        "walk_in": walk_in,
        "can_sell_without_shift": can_sell_without_shift,
        "currency": request.business.currency_display,
        "precision": request.business.currency_precision,
        "max_discount": settings_obj.max_discount_percent,
        "can_discount": request.membership.has_perm("sales.discount"),
        "can_override_price": request.membership.has_perm("sales.price_override"),
        "can_credit": request.membership.has_perm("sales.credit"),
        "vat_enabled": settings_obj.vat_enabled,
        "vat_rate": vat_rate,
        "show_vat_on_invoice_receipt": settings_obj.show_vat_on_invoice_receipt,
    })


@require_permission("sales.create")
def pos_products(request):
    """JSON product grid/search for the POS screen."""
    from apps.catalog.models import Product

    q = request.GET.get("q", "").strip()
    category_id = request.GET.get("category", "")
    qs = (
        Product.objects.for_business(request.business)
        .filter(is_active=True, is_archived=False)
        .select_related("tax_rate")
        .prefetch_related("variants")
    )
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q) |
                       Q(barcode__icontains=q) | Q(internal_code__icontains=q))
    if category_id.isdigit():
        qs = qs.filter(Q(category_id=category_id) | Q(category__parent_id=category_id))
    qs = qs.order_by("name")[:60]

    warehouse_id = request.GET.get("warehouse_id")
    stock_map = {}
    if warehouse_id and str(warehouse_id).isdigit():
        for row in StockLevel.objects.for_business(request.business).filter(
            warehouse_id=warehouse_id, product__in=[p.pk for p in qs]
        ).values("product_id", "variant_id", "quantity"):
            stock_map[(row["product_id"], row["variant_id"])] = float(row["quantity"])

    items = []
    for p in qs:
        tax_rate = calculations.resolve_tax_rate(request.business, p)
        if p.has_variants:
            for v in p.variants.all():
                if not v.is_active:
                    continue
                items.append({
                    "product_id": p.id, "variant_id": v.id,
                    "name": f"{p.name} — {v.name}",
                    "price": str(v.sale_price if v.sale_price > 0 else p.sale_price),
                    "sku": v.sku or p.sku,
                    "tax_rate": str(tax_rate),
                    "stocked": p.is_stocked,
                    "allow_discount": p.allow_discount,
                    "min_price": str(p.minimum_sale_price),
                    "stock": stock_map.get((p.id, v.id), None),
                    "image": v.image.url if v.image else (p.image.url if p.image else None),
                })
        else:
            items.append({
                "product_id": p.id, "variant_id": None,
                "name": p.name,
                "price": str(p.sale_price),
                "sku": p.sku,
                "tax_rate": str(tax_rate),
                "stocked": p.is_stocked,
                "allow_discount": p.allow_discount,
                "min_price": str(p.minimum_sale_price),
                "stock": stock_map.get((p.id, None), None),
                "image": p.image.url if p.image else None,
            })
    return JsonResponse({"items": items})


@require_permission("sales.create")
def pos_barcode(request):
    """Exact barcode/SKU lookup — used by scanner input."""
    from apps.catalog.models import Product, ProductVariant

    code = request.GET.get("code", "").strip()
    if not code:
        return JsonResponse({"found": False})
    variant = (
        ProductVariant.objects.for_business(request.business)
        .filter(Q(barcode=code) | Q(sku=code), is_active=True)
        .select_related("product__tax_rate", "product")
        .first()
    )
    if variant and not variant.product.is_archived:
        p = variant.product
        tax_rate = calculations.resolve_tax_rate(request.business, p)
        return JsonResponse({"found": True, "item": {
            "product_id": p.id, "variant_id": variant.id,
            "name": f"{p.name} — {variant.name}",
            "price": str(variant.sale_price if variant.sale_price > 0 else p.sale_price),
            "sku": variant.sku or p.sku, "tax_rate": str(tax_rate),
            "stocked": p.is_stocked, "allow_discount": p.allow_discount,
            "min_price": str(p.minimum_sale_price),
        }})
    product = (
        Product.objects.for_business(request.business)
        .filter(Q(barcode=code) | Q(sku=code), is_active=True, is_archived=False)
        .select_related("tax_rate")
        .first()
    )
    if product and not product.has_variants:
        tax_rate = calculations.resolve_tax_rate(request.business, product)
        return JsonResponse({"found": True, "item": {
            "product_id": product.id, "variant_id": None,
            "name": product.name, "price": str(product.sale_price),
            "sku": product.sku, "tax_rate": str(tax_rate),
            "stocked": product.is_stocked, "allow_discount": product.allow_discount,
            "min_price": str(product.minimum_sale_price),
        }})
    return JsonResponse({"found": False})


@require_permission("sales.create")
def pos_customers(request):
    q = request.GET.get("q", "").strip()
    qs = Customer.objects.for_business(request.business).filter(is_active=True)
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(mobile__icontains=q) |
                       Q(code__icontains=q) | Q(email__icontains=q))
    results = [{
        "id": c.id, "name": c.full_name, "mobile": c.mobile,
        "balance": str(c.balance), "store_credit": str(c.store_credit),
        "credit_limit": str(c.credit_limit), "is_walk_in": c.is_walk_in,
        "more_options": customer_services.more_option_values(request.business, c),
    } for c in qs.order_by("-is_walk_in", "full_name")[:15]]
    return JsonResponse({"results": results})


@require_POST
@require_permission("customers.manage")
def pos_quick_customer(request):
    """Create a customer from the POS without leaving the screen."""
    from apps.customers.services import next_customer_code

    try:
        subscriptions.check_limit(request.business, "customers")
    except (subscriptions.LimitExceeded, subscriptions.SubscriptionInactive) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    name = request.POST.get("name", "").strip()
    mobile = request.POST.get("mobile", "").strip()
    if not name:
        return JsonResponse({"ok": False, "error": "Name is required."}, status=400)
    if mobile and Customer.objects.for_business(request.business).filter(
        mobile=mobile
    ).exists():
        return JsonResponse(
            {"ok": False, "error": "A customer with this mobile already exists."},
            status=400,
        )
    customer = Customer.objects.create(
        business=request.business, code=next_customer_code(request.business),
        full_name=name[:160], mobile=mobile[:30],
    )
    return JsonResponse({"ok": True, "customer": {
        "id": customer.id, "name": customer.full_name, "mobile": customer.mobile,
        "balance": "0", "store_credit": "0", "credit_limit": "0", "is_walk_in": False,
        "more_options": [],
    }})


@require_POST
@require_permission("sales.create")
def pos_checkout(request):
    """Finalize the cart. Body: JSON contract from the POS screen."""
    from apps.branches.models import Branch
    from apps.catalog.models import Product, ProductVariant

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid request."}, status=400)

    try:
        branch = Branch.objects.for_business(request.business).get(
            pk=payload.get("branch_id"), is_active=True
        )
    except Branch.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Invalid branch."}, status=400)
    if not request.membership.can_access_branch(branch):
        return JsonResponse({"ok": False, "error": "You cannot sell from this branch."},
                            status=403)
    warehouse = _branch_warehouse(branch)
    if warehouse is None:
        return JsonResponse({"ok": False, "error": "Branch has no warehouse."}, status=400)

    try:
        customer = Customer.objects.for_business(request.business).get(
            pk=payload.get("customer_id"), is_active=True
        )
    except Customer.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Invalid customer."}, status=400)

    shift = register_services.get_open_shift(request.business, request.user)

    items = []
    for raw in payload.get("items", []):
        try:
            product = Product.objects.for_business(request.business).get(
                pk=raw.get("product_id")
            )
        except Product.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Invalid product in cart."},
                                status=400)
        variant = None
        if raw.get("variant_id"):
            try:
                variant = ProductVariant.objects.for_business(request.business).get(
                    pk=raw["variant_id"], product=product
                )
            except ProductVariant.DoesNotExist:
                return JsonResponse({"ok": False, "error": "Invalid variant in cart."},
                                    status=400)
        items.append({
            "product": product, "variant": variant,
            "quantity": D(raw.get("quantity")),
            "unit_price": D(raw.get("unit_price")),
            "discount_amount": D(raw.get("discount_amount")),
            "tailoring_details": _checkout_tailoring_details(raw),
        })

    payments = []
    for raw in payload.get("payments", []):
        try:
            method = PaymentMethod.objects.for_business(request.business).get(
                pk=raw.get("method_id"), is_active=True
            )
        except PaymentMethod.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Invalid payment method."},
                                status=400)
        payments.append({"method": method, "amount": D(raw.get("amount")),
                         "reference": str(raw.get("reference", ""))[:120]})

    delivery_date = None
    raw_delivery = str(payload.get("delivery_date") or "").strip()
    if not raw_delivery:
        return JsonResponse({
            "ok": False,
            "error": "Please select delivery date before completing sale.",
        }, status=400)

    import datetime as _dt

    try:
        delivery_date = _dt.date.fromisoformat(raw_delivery)
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid delivery date."},
                            status=400)

    try:
        sale = services.complete_sale(
            business=request.business,
            branch=branch,
            warehouse=warehouse,
            cashier=request.user,
            customer=customer,
            items=items,
            payments=payments,
            membership=request.membership,
            register=shift.register if shift else None,
            shift=shift,
            invoice_discount=D(payload.get("invoice_discount")),
            notes=str(payload.get("notes", ""))[:1000],
            delivery_date=delivery_date,
            request=request,
        )
    except (SaleError, subscriptions.LimitExceeded,
            subscriptions.SubscriptionInactive) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:  # ValidationError from inventory etc.
        msg = "; ".join(getattr(exc, "messages", [str(exc)]))
        return JsonResponse({"ok": False, "error": msg}, status=400)

    held_id = payload.get("held_id")
    if held_id:
        HeldSale.objects.for_business(request.business).filter(
            pk=held_id, cashier=request.user
        ).delete()

    return JsonResponse({"ok": True, "sale": {
        "public_id": str(sale.public_id),
        "invoice_number": sale.invoice_number,
        "total": str(sale.total),
        "change_due": str(sale.change_due),
        "receipt_url": f"/sales/{sale.public_id}/receipt/",
        "invoice_url": f"/sales/{sale.public_id}/invoice/",
    }})


@require_POST
@require_permission("sales.create")
def pos_hold(request):
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid request."}, status=400)
    cart = payload.get("cart") or {}
    if not cart.get("items"):
        return JsonResponse({"ok": False, "error": "Cart is empty."}, status=400)
    from apps.branches.models import Branch

    try:
        branch = Branch.objects.for_business(request.business).get(
            pk=payload.get("branch_id")
        )
    except Branch.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Invalid branch."}, status=400)
    held = HeldSale.objects.create(
        business=request.business, branch=branch, cashier=request.user,
        label=str(payload.get("label", ""))[:80], cart=cart,
    )
    return JsonResponse({"ok": True, "held_id": held.pk})


@require_permission("sales.create")
def pos_held_list(request):
    held = HeldSale.objects.for_business(request.business).filter(
        cashier=request.user
    ).order_by("-created_at")[:20]
    return JsonResponse({"held": [
        {"id": h.pk, "label": h.label or f"Held #{h.pk}",
         "created": h.created_at.strftime("%H:%M"),
         "items": len(h.cart.get("items", [])), "cart": h.cart}
        for h in held
    ]})


@require_POST
@require_permission("sales.create")
def pos_held_delete(request, pk):
    HeldSale.objects.for_business(request.business).filter(
        pk=pk, cashier=request.user
    ).delete()
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Sales list / detail / invoice / receipt / void
# ---------------------------------------------------------------------------
def _qs_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return f"{encoded}&" if encoded else ""


@require_permission("sales.view")
def sale_list(request):
    qs = (
        Sale.objects.for_business(request.business)
        .exclude(status=Sale.Status.DRAFT)
        .select_related("customer", "branch", "cashier")
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(invoice_number__icontains=q) |
                       Q(customer__full_name__icontains=q) |
                       Q(customer__mobile__icontains=q))
    status = request.GET.get("status", "")
    if status:
        qs = qs.filter(status=status)
    branch_id = request.GET.get("branch", "")
    if branch_id.isdigit():
        qs = qs.filter(branch_id=branch_id)
    date_from = request.GET.get("from", "")
    date_to = request.GET.get("to", "")
    if date_from:
        qs = qs.filter(sale_date__date__gte=date_from)
    if date_to:
        qs = qs.filter(sale_date__date__lte=date_to)

    # Delivery filters
    from django.utils import timezone as _tz

    today = _tz.localdate()
    delivery = request.GET.get("delivery", "")
    open_delivery = ~Q(delivery_status__in=["delivered", "cancelled"])
    if delivery == "today":
        qs = qs.filter(open_delivery, delivery_date=today)
    elif delivery == "upcoming":
        qs = qs.filter(open_delivery, delivery_date__gt=today)
    elif delivery == "overdue":
        qs = qs.filter(open_delivery, delivery_date__lt=today)
    elif delivery == "scheduled":
        qs = qs.filter(delivery_date__isnull=False)

    totals = qs.aggregate(
        total=Sum("total"),
        paid=Sum("amount_paid"),
        discount=Sum("discount_amount"),
    )
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "sales/list.html", {
        "page_obj": page_obj, "q": q, "active_nav": "sales",
        "statuses": Sale.Status.choices, "branches": _user_branches(request),
        "totals": totals, "querystring": _qs_without_page(request),
    })


@require_permission("sales.view")
def sale_detail(request, public_id):
    sale = get_tenant_object(
        Sale.objects.select_related("customer", "branch", "cashier", "register"),
        request.business, public_id=public_id,
    )
    items = _invoice_display_items(list(sale.items.select_related("product", "variant")))
    has_tailoring_jobs = any(item.has_tailoring_details for item in items)
    payments = sale.payments.select_related("method", "received_by")
    returns = _invoice_display_returns(list(sale.returns.prefetch_related("items")))
    settings_obj = request.business.settings
    first_taxed_item = next((item for item in items if item.tax_rate), None)
    vat_rate = first_taxed_item.tax_rate if first_taxed_item else settings_obj.effective_vat_rate
    show_profit = request.membership.has_perm("profit.view")
    collect_methods = PaymentMethod.objects.for_business(request.business).filter(
        is_active=True
    ).exclude(kind__in=["customer_credit", "store_credit"])
    return render(request, "sales/detail.html", {
        "sale": sale, "items": items, "payments": payments, "returns": returns,
        "active_nav": "sales", "show_profit": show_profit,
        "collect_methods": collect_methods,
        "has_tailoring_jobs": has_tailoring_jobs,
        "discounted_subtotal": money(sale.subtotal - sale.discount_amount),
        "invoice_label": "TAX INVOICE" if settings_obj.vat_enabled else "INVOICE",
        "show_vat": bool(settings_obj.show_vat_on_invoice_receipt and (vat_rate or sale.tax_amount)),
        "vat_rate": vat_rate,
    })


def _invoice_display_items(items):
    for item in items:
        item.display_subtotal = money(
            item.quantity * item.unit_price - item.discount_amount
        )
    return items


def _invoice_display_returns(returns):
    for sale_return in returns:
        sale_return.display_returned_quantity = sum(
            (item.quantity for item in sale_return.items.all()), D("0")
        )
    return returns


def _invoice_status_label(sale):
    if sale.returned_amount > 0:
        return "Refunded"
    if sale.balance <= 0:
        return "Paid"
    if sale.net_amount_paid > 0:
        return "Partially Paid"
    return "Unpaid"


def _sale_item_sequence(sale_item):
    ids = list(
        sale_item.sale.items.order_by("id").values_list("id", flat=True)
    )
    return ids.index(sale_item.id) + 1 if sale_item.id in ids else 1


def _job_card_data(sale, request, items, sale_item=None):
    items = list(items)
    if sale_item is not None:
        items = [sale_item]
    priority_options = {
        "normal": ("Normal", "normal"),
        "urgent": ("Urgent", "urgent"),
        "vip": ("VIP", "vip"),
    }
    tailoring = sale_item.tailoring_details if sale_item is not None else {}
    priority_key = (
        tailoring.get("priority")
        or request.GET.get("priority", "normal")
    ).strip().lower()
    priority_label, priority_class = priority_options.get(
        priority_key, priority_options["normal"]
    )
    copy_type = request.GET.get("copy", "").strip().lower()
    if copy_type == "copy":
        copy_label = "Copy"
    elif copy_type == "reprint" or sale.reprint_count > 0:
        copy_label = "Reprint"
    else:
        copy_label = "Original"
    sequence = _sale_item_sequence(sale_item) if sale_item is not None else 1
    return {
        "sale": sale,
        "items": items,
        "job_item": sale_item,
        "tailoring": tailoring,
        "business": sale.business,
        "more_options": customer_services.more_option_values(
            request.business, sale.customer
        ),
        "job_card_number": f"JC-{sale.invoice_number}-{sequence:02d}",
        "workshop_copy_number": sale.reprint_count + 1,
        "copy_type": copy_label,
        "priority_label": priority_label,
        "priority_class": priority_class,
        "job_delivery_date": sale.delivery_date,
    }


def _job_card_context(sale, request, items, sale_item=None):
    card = _job_card_data(sale, request, items, sale_item=sale_item)
    return {**card, "job_cards": [card]}


def _invoice_context(sale, *, items=None, payments=None, returns=None, is_reprint=False,
                     pdf_mode=False):
    items = _invoice_display_items(list(items if items is not None else sale.items.all()))
    payments = payments if payments is not None else sale.payments.select_related(
        "method", "received_by"
    )
    returns = _invoice_display_returns(list(
        returns if returns is not None else sale.returns.prefetch_related("items")
    ))
    settings_obj = sale.business.settings
    first_taxed_item = next((item for item in items if item.tax_rate), None)
    vat_rate = first_taxed_item.tax_rate if first_taxed_item else settings_obj.effective_vat_rate
    show_vat = bool(settings_obj.show_vat_on_invoice_receipt and (vat_rate or sale.tax_amount))
    status_label = _invoice_status_label(sale)
    return {
        "sale": sale, "items": items, "payments": payments,
        "returns": returns,
        "business": sale.business, "settings_obj": settings_obj,
        "is_reprint": is_reprint, "copy_label": "DUPLICATE COPY" if is_reprint else "ORIGINAL COPY",
        "invoice_label": "TAX INVOICE" if settings_obj.vat_enabled else "INVOICE",
        "invoice_status_label": status_label,
        "invoice_status_class": status_label.lower().replace(" ", "-"),
        "pdf_mode": pdf_mode, "vat_rate": vat_rate,
        "show_vat": show_vat, "vat_number": settings_obj.vat_number,
        "discounted_subtotal": money(sale.subtotal - sale.discount_amount),
    }


def _render_invoice(request, sale, template, mark_reprint=False):
    is_reprint = sale.reprint_count > 0
    if mark_reprint:
        sale.reprint_count += 1
        sale.save(update_fields=["reprint_count"])
    return render(request, template, _invoice_context(sale, is_reprint=is_reprint))


@require_permission("sales.view")
def sale_invoice(request, public_id):
    sale = get_tenant_object(
        Sale.objects.select_related("customer", "branch", "business"),
        request.business, public_id=public_id,
    )
    return _render_invoice(request, sale, "invoices/invoice_a4.html",
                           mark_reprint=True)


@require_permission("sales.view")
def sale_receipt(request, public_id):
    sale = get_tenant_object(
        Sale.objects.select_related("customer", "branch", "business", "register"),
        request.business, public_id=public_id,
    )
    width = sale.register.receipt_printer if sale.register else "80mm"
    template = ("invoices/receipt_58mm.html" if width == "58mm"
                else "invoices/receipt_80mm.html")
    return _render_invoice(request, sale, template, mark_reprint=True)


@require_permission("sales.view")
def sale_invoice_pdf(request, public_id):
    from apps.reports.pdf import render_pdf

    sale = get_tenant_object(
        Sale.objects.select_related("customer", "branch", "business"),
        request.business, public_id=public_id,
    )
    items = _invoice_display_items(list(sale.items.all()))
    payments = sale.payments.select_related("method", "received_by")
    pdf = render_pdf(
        "invoices/invoice_a4.html",
        _invoice_context(sale, items=items, payments=payments, is_reprint=False,
                         pdf_mode=True),
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="invoice-{sale.invoice_number}.pdf"'
    )
    return response


@require_permission("sales.view")
def sale_workshop_job_card_pdf(request, public_id):
    from apps.reports.pdf import render_pdf

    sale = get_tenant_object(
        Sale.objects.select_related("customer", "branch", "business"),
        request.business,
        public_id=public_id,
    )
    items = list(sale.items.select_related("product__unit", "variant").order_by("id"))
    tailoring_items = [item for item in items if item.has_tailoring_details]
    cards = [
        _job_card_data(sale, request, [item], sale_item=item)
        for item in tailoring_items
    ] or [
        _job_card_data(sale, request, [item], sale_item=item)
        for item in items
    ] or [_job_card_data(sale, request, [])]
    pdf = render_pdf(
        "invoices/workshop_job_card.html",
        {"job_cards": cards},
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="workshop-job-cards-{sale.invoice_number}.pdf"'
    )
    return response


@require_permission("sales.view")
def sale_item_workshop_job_card_pdf(request, public_id, item_id):
    from apps.reports.pdf import render_pdf

    sale = get_tenant_object(
        Sale.objects.select_related("customer", "branch", "business"),
        request.business,
        public_id=public_id,
    )
    sale_item = get_tenant_object(
        SaleItem.objects.select_related("sale", "product__unit", "variant"),
        request.business,
        pk=item_id,
        sale=sale,
    )
    pdf = render_pdf(
        "invoices/workshop_job_card.html",
        _job_card_context(sale, request, [sale_item], sale_item=sale_item),
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="workshop-job-card-{sale.invoice_number}-'
        f'{_sale_item_sequence(sale_item):02d}.pdf"'
    )
    return response


@require_POST
@require_permission("customers.payments")
def sale_payment_add(request, public_id):
    """Record a later payment against a credit/partially-paid sale."""
    sale = get_tenant_object(Sale, request.business, public_id=public_id)
    try:
        subscriptions.require_operational(request.business)
        method = get_tenant_object(PaymentMethod, request.business,
                                   pk=request.POST.get("method_id"))
        payment_date = None
        raw_date = request.POST.get("payment_date", "").strip()
        if raw_date:
            import datetime as _dt

            payment_date = _dt.date.fromisoformat(raw_date)
        shift = register_services.get_open_shift(request.business, request.user)
        payment = services.add_sale_payment(
            sale=sale, amount=D(request.POST.get("amount")), method=method,
            user=request.user, payment_date=payment_date,
            reference=request.POST.get("reference", ""),
            notes=request.POST.get("notes", ""), shift=shift, request=request,
        )
        messages.success(
            request,
            f"Payment {payment.amount} recorded — balance is now {sale.balance}.",
        )
    except (SaleError, ValueError, subscriptions.SubscriptionInactive) as exc:
        messages.error(request, str(exc))
    return redirect("sales:detail", public_id=public_id)


@require_POST
@require_permission("sales.delete")
def sale_delete(request, public_id):
    sale = get_tenant_object(Sale, request.business, public_id=public_id)
    try:
        services.delete_sale(sale=sale, user=request.user, request=request)
        messages.success(request, "Draft sale deleted.")
        return redirect("sales:list")
    except SaleError as exc:
        messages.error(request, str(exc))
        return redirect("sales:detail", public_id=public_id)


@require_POST
@require_permission("sales.create")
def sale_set_delivery(request, public_id):
    sale = get_tenant_object(Sale, request.business, public_id=public_id)
    try:
        services.set_delivery_status(
            sale=sale, status=request.POST.get("delivery_status", ""),
            user=request.user, request=request,
        )
        messages.success(request, f"Delivery status updated to "
                                  f"{sale.get_delivery_status_display()}.")
    except SaleError as exc:
        messages.error(request, str(exc))
    return redirect("sales:detail", public_id=public_id)


@require_POST
@require_permission("sales.void")
def sale_void(request, public_id):
    sale = get_tenant_object(Sale, request.business, public_id=public_id)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "A reason is required to void a sale.")
        return redirect("sales:detail", public_id=public_id)
    try:
        subscriptions.require_operational(request.business)
        services.void_sale(sale=sale, user=request.user, reason=reason, request=request)
        messages.success(request, f"Invoice {sale.invoice_number} voided.")
    except (SaleError, subscriptions.SubscriptionInactive) as exc:
        messages.error(request, str(exc))
    return redirect("sales:detail", public_id=public_id)


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------
@require_permission("sales.refund")
def return_list(request):
    qs = (
        SaleReturn.objects.for_business(request.business)
        .select_related("sale", "customer", "processed_by")
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(return_number__icontains=q) |
                       Q(sale__invoice_number__icontains=q) |
                       Q(customer__full_name__icontains=q))
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "sales/return_list.html", {
        "page_obj": page_obj, "q": q, "active_nav": "returns",
        "querystring": _qs_without_page(request),
    })


@require_permission("sales.refund")
def return_create(request, public_id):
    sale = get_tenant_object(
        Sale.objects.select_related("customer", "warehouse"),
        request.business, public_id=public_id,
    )
    items = list(sale.items.select_related("product", "variant"))
    returnable = [i for i in items if i.returnable_quantity > 0]
    if request.method == "POST":
        selected = []
        for item in items:
            raw = request.POST.get(f"qty_{item.pk}", "").strip()
            if raw:
                qty = D(raw)
                if qty > 0:
                    selected.append({
                        "sale_item": item, "quantity": qty,
                        "restock": request.POST.get(f"restock_{item.pk}") == "on",
                    })
        refund_method = request.POST.get("refund_method", "")
        if refund_method not in dict(SaleReturn.RefundMethod.choices):
            messages.error(request, "Choose a refund method.")
        else:
            try:
                subscriptions.require_operational(request.business)
                shift = register_services.get_open_shift(request.business, request.user)
                sale_return = services.process_return(
                    sale=sale, items=selected, refund_method=refund_method,
                    user=request.user, reason=request.POST.get("reason", ""),
                    restock=True, shift=shift, request=request,
                )
                messages.success(
                    request,
                    f"Return {sale_return.return_number} processed — refund "
                    f"{sale_return.refund_amount} via "
                    f"{sale_return.get_refund_method_display()}.",
                )
                return redirect("sales:detail", public_id=sale.public_id)
            except (SaleError, subscriptions.SubscriptionInactive) as exc:
                messages.error(request, str(exc))
            except Exception as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "sales/return_form.html", {
        "sale": sale, "items": returnable, "active_nav": "returns",
        "refund_methods": SaleReturn.RefundMethod.choices,
    })
