from django import forms as django_forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.branches.models import Branch, Warehouse
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.core.money import D
from apps.sales.models import PaymentMethod
from apps.subscriptions import services as subscriptions
from apps.suppliers.models import Supplier

from . import services
from .models import Purchase


@require_permission("purchases.view")
def purchase_list(request):
    if not subscriptions.has_feature(request.business, "purchases"):
        return render(request, "inventory/feature_locked.html",
                      {"feature": "Purchases", "active_nav": "purchases"})
    qs = (
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
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "purchases/list.html", {
        "page_obj": page_obj, "q": q, "active_nav": "purchases",
        "statuses": Purchase.Status.choices, "querystring": "",
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
    })


@require_permission("purchases.view")
def purchase_detail(request, public_id):
    purchase = get_tenant_object(
        Purchase.objects.select_related("supplier", "warehouse", "branch", "created_by"),
        request.business, public_id=public_id,
    )
    items = purchase.items.select_related("product", "variant")
    payments = purchase.payments.select_related("payment_method")
    returns = purchase.purchase_returns.all()
    methods = PaymentMethod.objects.for_business(request.business).filter(
        is_active=True).exclude(kind__in=["customer_credit", "store_credit"])
    return render(request, "purchases/detail.html", {
        "purchase": purchase, "items": items, "payments": payments,
        "returns": returns, "methods": methods, "active_nav": "purchases",
        "can_manage": request.membership.has_perm("purchases.manage"),
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


@require_permission("purchases.manage")
def purchase_pay(request, public_id):
    purchase = get_tenant_object(Purchase, request.business, public_id=public_id)
    if request.method == "POST":
        try:
            subscriptions.require_operational(request.business)
            method = get_tenant_object(PaymentMethod, request.business,
                                       pk=request.POST.get("method_id"))
            services.pay_purchase(
                purchase=purchase, amount=D(request.POST.get("amount")),
                method=method, user=request.user,
                reference=request.POST.get("reference", ""),
                notes=request.POST.get("notes", ""), request=request,
            )
            messages.success(request, "Supplier payment recorded.")
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


@require_permission("purchases.manage")
def purchase_cancel(request, public_id):
    purchase = get_tenant_object(Purchase, request.business, public_id=public_id)
    if request.method == "POST":
        if purchase.items.filter(quantity_received__gt=0).exists():
            messages.error(request, "Purchases with received goods cannot be "
                                    "cancelled — use a purchase return instead.")
        else:
            purchase.status = Purchase.Status.CANCELLED
            purchase.save(update_fields=["status", "updated_at"])
            from apps.audit import services as audit

            audit.log("purchase.cancelled", request=request, module="purchases",
                      obj=purchase,
                      description=f"Purchase {purchase.purchase_number} cancelled.")
            messages.success(request, "Purchase cancelled.")
    return redirect("purchases:detail", public_id=public_id)
