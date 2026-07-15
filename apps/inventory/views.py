from django import forms as django_forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import F, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render

from apps.core.date_ranges import date_range_querystring, resolve_date_range
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.core.money import D
from apps.subscriptions import services as subscriptions

from . import services, workflows
from .forms import AdjustmentForm, CountForm, TransferForm, parse_item_rows
from .models import (
    StockAdjustment,
    StockCount,
    StockLevel,
    StockMovement,
    StockTransfer,
)


def _qs_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return f"{encoded}&" if encoded else ""


@require_permission("inventory.view")
def stock_list(request):
    qs = (
        StockLevel.objects.for_business(request.business)
        .select_related("product", "variant", "warehouse")
        .filter(product__is_archived=False)
        .order_by("product__name")
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(product__name__icontains=q) | Q(product__sku__icontains=q) |
                       Q(product__barcode__icontains=q))
    warehouse_id = request.GET.get("warehouse", "")
    if warehouse_id.isdigit():
        qs = qs.filter(warehouse_id=warehouse_id)
    level = request.GET.get("level", "")
    if level == "low":
        qs = qs.filter(quantity__lte=F("product__reorder_level"),
                       product__reorder_level__gt=0)
    elif level == "out":
        qs = qs.filter(quantity__lte=0)

    paginator = Paginator(qs, 30)
    page_obj = paginator.get_page(request.GET.get("page"))
    show_cost = request.membership.has_perm("cost.view")
    if show_cost:
        from apps.core.money import money as money_q

        for lvl in page_obj:
            target = lvl.variant or lvl.product
            lvl.unit_cost = target.average_cost or getattr(
                target, "purchase_price", 0)
            lvl.stock_value = money_q(lvl.quantity * lvl.unit_cost)

    from apps.branches.models import Warehouse

    warehouses = Warehouse.objects.for_business(request.business).filter(is_active=True)
    total_value = services.stock_value(request.business) if show_cost else None
    return render(request, "inventory/stock_list.html", {
        "page_obj": page_obj, "q": q, "warehouses": warehouses,
        "active_nav": "inventory", "show_cost": show_cost,
        "total_value": total_value, "querystring": _qs_without_page(request),
    })


@require_permission("inventory.export")
def inventory_export(request):
    from apps.audit import services as audit
    from apps.reports import exports

    def _int(name):
        v = request.GET.get(name, "")
        return int(v) if v.isdigit() else None

    filters = {"warehouse_id": _int("warehouse"), "branch_id": _int("branch")}
    data = services.inventory_export_dataset(request.business, filters)
    audit.log("inventory.exported", request=request, module="inventory",
              description=f"Exported {len(data['rows'])} stock rows "
                          f"({request.GET.get('format', 'csv')}).")
    if request.GET.get("format") == "xlsx":
        return exports.export_xlsx("inventory", data)
    return exports.export_csv("inventory", data)


@require_permission("inventory.import")
def inventory_import_template(request):
    from apps.reports import exports

    data = {
        "columns": [c.title() for c in services.IMPORT_COLUMNS],
        "rows": [["WID-A", "1000000000017", "Widget A", "", "", "Head Office",
                  "Main Warehouse", "50", "10", "Opening count",
                  "Initial load", "4.000"]],
        "totals": None,
    }
    if request.GET.get("format") == "xlsx":
        return exports.export_xlsx("inventory_import_template", data)
    return exports.export_csv("inventory_import_template", data)


@require_permission("inventory.import")
def inventory_import(request):
    from apps.audit import services as audit
    from apps.core.imports import error_report_response, parse_tabular_file

    if request.GET.get("errors") == "1":
        errors = request.session.get("inventory_import_errors", [])
        return error_report_response("inventory_import_errors.csv", errors)

    results = None
    import_error = None
    if request.method == "POST":
        try:
            subscriptions.require_operational(request.business)
        except subscriptions.SubscriptionInactive as exc:
            messages.error(request, str(exc))
            return redirect("inventory:stock_list")
        upload = request.FILES.get("file")
        mode = request.POST.get("mode", "add")
        if not upload:
            import_error = "Choose a file to import."
            messages.error(request, "Choose a file to import.")
        elif mode not in services.IMPORT_MODES:
            import_error = "Choose a valid import mode."
            messages.error(request, "Choose a valid import mode.")
        else:
            rows, parse_error = parse_tabular_file(upload)
            if parse_error:
                import_error = parse_error
                messages.error(request, parse_error)
            else:
                summary, errors = services.import_inventory(
                    business=request.business, rows=rows, mode=mode,
                    user=request.user)
                request.session["inventory_import_errors"] = errors
                results = {"summary": summary, "errors": errors,
                           "total": len(rows)}
                # Dedicated audit record with file/mode/counts
                audit.log("inventory.imported", request=request,
                          module="inventory",
                          description=(f"Inventory import '{upload.name}' "
                                       f"mode={mode}: {summary['imported']} applied, "
                                       f"{summary['updated']} min-updated, "
                                       f"{summary['failed']} failed."),
                          new_values={"file": upload.name, "mode": mode,
                                      **summary})
    return render(request, "inventory/import.html", {
        "results": results, "import_error": import_error,
        "active_nav": "inventory",
        "columns": [c.title() for c in services.IMPORT_COLUMNS],
    })


@require_permission("inventory.view")
def movement_list(request):
    qs = (
        StockMovement.objects.for_business(request.business)
        .select_related("product", "variant", "warehouse", "user")
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(product__name__icontains=q) | Q(reference_id__icontains=q))
    mtype = request.GET.get("type", "")
    if mtype:
        qs = qs.filter(movement_type=mtype)
    date_from, date_to = resolve_date_range(request.GET, request.business)
    qs = qs.filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )
    paginator = Paginator(qs, 40)
    page_obj = paginator.get_page(request.GET.get("page"))
    querystring = date_range_querystring(request.GET, date_from, date_to)
    return render(request, "inventory/movement_list.html", {
        "page_obj": page_obj, "q": q, "active_nav": "inventory",
        "movement_types": StockMovement.Type.choices,
        "date_from": date_from, "date_to": date_to,
        "querystring": f"{querystring}&" if querystring else "",
    })


# ---------------------------------------------------------------------------
# Item search endpoint (used by transfer/adjustment/purchase forms)
# ---------------------------------------------------------------------------
@require_permission("inventory.view")
def item_search(request):
    from apps.catalog.models import Product

    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})
    products = (
        Product.objects.for_business(request.business)
        .filter(is_active=True, is_archived=False)
        .filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q))
        .prefetch_related("variants")[:15]
    )
    results = []
    for p in products:
        if p.has_variants:
            for v in p.variants.filter(is_active=True):
                results.append({"product_id": p.id, "variant_id": v.id,
                                "label": str(v), "sku": v.sku or p.sku})
        else:
            results.append({"product_id": p.id, "variant_id": None,
                            "label": p.name, "sku": p.sku})
    return JsonResponse({"results": results})


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------
@require_permission("inventory.transfer")
def transfer_list(request):
    if not subscriptions.has_feature(request.business, "transfers"):
        return render(request, "inventory/feature_locked.html",
                      {"feature": "Stock transfers", "active_nav": "inventory"})
    qs = (
        StockTransfer.objects.for_business(request.business)
        .select_related("from_warehouse", "to_warehouse", "requested_by")
        .prefetch_related("items")
    )
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "inventory/transfer_list.html",
                  {"page_obj": page_obj, "active_nav": "inventory",
                   "querystring": _qs_without_page(request)})


@require_permission("inventory.transfer")
def transfer_create(request):
    if not subscriptions.has_feature(request.business, "transfers"):
        messages.warning(request, "Stock transfers are not included in your plan.")
        return redirect("inventory:stock_list")
    form = TransferForm(request.business, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            subscriptions.require_operational(request.business)
            rows = parse_item_rows(request, request.business)
            transfer = workflows.create_transfer(
                business=request.business,
                from_warehouse=form.cleaned_data["from_warehouse"],
                to_warehouse=form.cleaned_data["to_warehouse"],
                rows=rows, user=request.user,
                notes=form.cleaned_data["notes"],
            )
            messages.success(request, f"Transfer {transfer.transfer_number} created as draft.")
            return redirect("inventory:transfer_list")
        except (django_forms.ValidationError, ValidationError,
                subscriptions.SubscriptionInactive) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "inventory/transfer_form.html",
                  {"form": form, "active_nav": "inventory"})


@require_permission("inventory.transfer")
def transfer_action(request, public_id, action):
    transfer = get_tenant_object(StockTransfer, request.business, public_id=public_id)
    if request.method != "POST":
        return redirect("inventory:transfer_list")
    try:
        subscriptions.require_operational(request.business)
        if action == "dispatch":
            workflows.dispatch_transfer(transfer=transfer, user=request.user, request=request)
            messages.success(request, "Transfer dispatched — stock left the source warehouse.")
        elif action == "receive":
            workflows.receive_transfer(transfer=transfer, user=request.user, request=request)
            messages.success(request, "Transfer received — stock added to destination.")
        elif action == "cancel":
            workflows.cancel_transfer(transfer=transfer, user=request.user, request=request)
            messages.success(request, "Transfer cancelled.")
    except (ValidationError, subscriptions.SubscriptionInactive) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("inventory:transfer_list")


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------
@require_permission("inventory.adjust")
def adjustment_list(request):
    qs = (
        StockAdjustment.objects.for_business(request.business)
        .select_related("warehouse", "created_by", "approved_by")
        .prefetch_related("items__product")
    )
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "inventory/adjustment_list.html",
                  {"page_obj": page_obj, "active_nav": "inventory",
                   "querystring": _qs_without_page(request)})


@require_permission("inventory.adjust")
def adjustment_create(request):
    form = AdjustmentForm(request.business, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            subscriptions.require_operational(request.business)
            rows = parse_item_rows(request, request.business)
            requires_approval = (
                request.business.settings.adjustment_requires_approval
                and not request.membership.has_perm("inventory.adjust_approve")
            )
            adjustment = workflows.create_adjustment(
                business=request.business,
                warehouse=form.cleaned_data["warehouse"],
                reason=form.cleaned_data["reason"],
                rows=rows, user=request.user,
                notes=form.cleaned_data["notes"],
                requires_approval=requires_approval, request=request,
            )
            if adjustment.status == StockAdjustment.Status.PENDING:
                messages.info(request, f"Adjustment {adjustment.adjustment_number} "
                                       "submitted for approval.")
            else:
                messages.success(request, f"Adjustment {adjustment.adjustment_number} applied.")
            return redirect("inventory:adjustment_list")
        except (django_forms.ValidationError, ValidationError,
                subscriptions.SubscriptionInactive) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "inventory/adjustment_form.html",
                  {"form": form, "active_nav": "inventory"})


@require_permission("inventory.adjust_approve")
def adjustment_action(request, public_id, action):
    adjustment = get_tenant_object(StockAdjustment, request.business, public_id=public_id)
    if request.method == "POST":
        try:
            if action == "approve":
                workflows.approve_adjustment(adjustment=adjustment, user=request.user,
                                             request=request)
                messages.success(request, "Adjustment approved and applied.")
            elif action == "reject":
                workflows.reject_adjustment(adjustment=adjustment, user=request.user,
                                            request=request)
                messages.success(request, "Adjustment rejected.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("inventory:adjustment_list")


# ---------------------------------------------------------------------------
# Physical counts
# ---------------------------------------------------------------------------
@require_permission("inventory.count")
def count_list(request):
    qs = (
        StockCount.objects.for_business(request.business)
        .select_related("warehouse", "created_by")
    )
    form = CountForm(request.business, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            subscriptions.require_operational(request.business)
            count = workflows.start_count(
                business=request.business,
                warehouse=form.cleaned_data["warehouse"],
                user=request.user, notes=form.cleaned_data["notes"],
            )
            messages.success(request, f"Count session {count.count_number} started — "
                                      "expected quantities frozen.")
            return redirect("inventory:count_detail", public_id=count.public_id)
        except subscriptions.SubscriptionInactive as exc:
            messages.error(request, str(exc))
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "inventory/count_list.html",
                  {"page_obj": page_obj, "form": form, "active_nav": "inventory",
                   "querystring": _qs_without_page(request)})


@require_permission("inventory.count")
def count_detail(request, public_id):
    count = get_tenant_object(StockCount, request.business, public_id=public_id)
    items = count.items.select_related("product", "variant").order_by("product__name")
    if request.method == "POST" and count.status in ("open", "review"):
        action = request.POST.get("action", "save")
        for item in items:
            raw = request.POST.get(f"counted_{item.pk}", "")
            if raw != "":
                item.counted_quantity = D(raw)
                item.save(update_fields=["counted_quantity"])
            elif action == "approve":
                continue
        if action == "approve":
            if not request.membership.has_perm("inventory.adjust_approve"):
                messages.error(request, "You need approval permission to apply a count.")
            else:
                try:
                    workflows.approve_count(count=count, user=request.user, request=request)
                    messages.success(request, "Count approved — variances applied to stock.")
                except ValidationError as exc:
                    messages.error(request, "; ".join(exc.messages))
            return redirect("inventory:count_detail", public_id=count.public_id)
        messages.success(request, "Counted quantities saved.")
        return redirect("inventory:count_detail", public_id=count.public_id)
    total_variance = sum((i.variance or 0) for i in items if i.counted_quantity is not None)
    return render(request, "inventory/count_detail.html", {
        "count": count, "items": items, "active_nav": "inventory",
        "total_variance": total_variance,
    })
