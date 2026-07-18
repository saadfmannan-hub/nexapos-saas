from django import forms as django_forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import F, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render

from apps.core.date_ranges import date_range_querystring, resolve_date_range
from apps.core.mixins import get_tenant_object
from apps.core.money import D
from apps.subscriptions import services as subscriptions
from apps.subscriptions.access import get_access_context
from apps.subscriptions.decorators import module_permission_required

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


def _warehouse_scoped(request, queryset, *, field="warehouse"):
    allowed = request.membership.allowed_branch_ids
    if allowed is None:
        return queryset
    return queryset.filter(
        Q(**{f"{field}__branch_id__in": allowed})
        | Q(**{f"{field}__branch__isnull": True})
    )


def _allowed_warehouse_ids(request):
    """Return None for tenant-wide access, otherwise allowed + central IDs."""
    allowed = request.membership.allowed_branch_ids
    if allowed is None:
        return None
    from apps.branches.models import Warehouse

    return list(
        Warehouse.objects.for_business(request.business)
        .filter(Q(branch_id__in=allowed) | Q(branch__isnull=True))
        .values_list("id", flat=True)
    )


def _warehouse_queryset(request):
    from apps.branches.models import Warehouse

    qs = Warehouse.objects.for_business(request.business).filter(is_active=True)
    allowed_ids = _allowed_warehouse_ids(request)
    if allowed_ids is not None:
        qs = qs.filter(id__in=allowed_ids)
    return qs


def _transfer_scoped(request, queryset):
    allowed = request.membership.allowed_branch_ids
    if allowed is None:
        return queryset
    from_allowed = (
        Q(from_warehouse__branch_id__in=allowed)
        | Q(from_warehouse__branch__isnull=True)
    )
    to_allowed = (
        Q(to_warehouse__branch_id__in=allowed)
        | Q(to_warehouse__branch__isnull=True)
    )
    return queryset.filter(from_allowed & to_allowed)


@module_permission_required("inventory", "inventory.view")
def stock_list(request):
    tailoring_enabled = get_access_context(request).has_module("tailoring")
    qs = (
        _warehouse_scoped(
            request,
            StockLevel.objects.for_business(request.business),
        )
        .select_related("product", "variant", "warehouse")
        .filter(product__is_archived=False)
        .order_by("product__name")
    )
    if not tailoring_enabled:
        qs = qs.filter(product__is_tailoring_item=False)
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

    warehouses = _warehouse_queryset(request)
    total_value = (
        services.stock_value(
            request.business,
            allowed_warehouse_ids=_allowed_warehouse_ids(request),
            include_tailoring=tailoring_enabled,
        )
        if show_cost
        else None
    )
    return render(request, "inventory/stock_list.html", {
        "page_obj": page_obj, "q": q, "warehouses": warehouses,
        "active_nav": "inventory", "show_cost": show_cost,
        "total_value": total_value, "querystring": _qs_without_page(request),
    })


@module_permission_required("inventory", "inventory.export")
def inventory_export(request):
    from apps.audit import services as audit
    from apps.reports import exports

    def _int(name):
        v = request.GET.get(name, "")
        return int(v) if v.isdigit() else None

    filters = {"warehouse_id": _int("warehouse"), "branch_id": _int("branch")}
    data = services.inventory_export_dataset(
        request.business,
        filters,
        allowed_warehouse_ids=_allowed_warehouse_ids(request),
        include_tailoring=get_access_context(request).has_module("tailoring"),
    )
    audit.log("inventory.exported", request=request, module="inventory",
              description=f"Exported {len(data['rows'])} stock rows "
                          f"({request.GET.get('format', 'csv')}).")
    if request.GET.get("format") == "xlsx":
        return exports.export_xlsx("inventory", data)
    return exports.export_csv("inventory", data)


@module_permission_required("inventory", "inventory.import")
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


@module_permission_required("inventory", "inventory.import")
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
                    user=request.user,
                    allowed_warehouse_ids=_allowed_warehouse_ids(request),
                    membership=request.membership,
                    request=request,
                )
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


@module_permission_required("inventory", "inventory.view")
def movement_list(request):
    qs = (
        StockMovement.objects.for_business(request.business)
        .select_related("product__unit", "variant", "warehouse", "user")
    )
    if not get_access_context(request).has_module("tailoring"):
        qs = qs.filter(product__is_tailoring_item=False)
    allowed = request.membership.allowed_branch_ids
    if allowed is not None:
        qs = qs.filter(
            Q(warehouse__branch_id__in=allowed) | Q(warehouse__branch__isnull=True)
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
@module_permission_required("inventory", "inventory.view")
def item_search(request):
    from apps.catalog.models import Product

    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})
    products = (
        Product.objects.for_business(request.business)
        .filter(is_active=True, is_archived=False)
        .filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q))
    )
    if not get_access_context(request).has_module("tailoring"):
        products = products.filter(is_tailoring_item=False)
    products = products.prefetch_related("variants")[:15]
    results = []
    include_parent_meter_repair = request.GET.get("parent_meter_repair") == "1"
    for p in products:
        if p.has_variants:
            if (
                include_parent_meter_repair
                and p.is_meter_tailoring
                and p.stock_levels.filter(
                    variant__isnull=True
                ).exclude(quantity=0).exists()
            ):
                results.append({
                    "product_id": p.id,
                    "variant_id": None,
                    "label": f"{p.name} — Legacy parent stock (correct to zero)",
                    "sku": p.sku,
                })
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
@module_permission_required("inventory", "inventory.transfer")
def transfer_list(request):
    if not subscriptions.has_feature(request.business, "transfers"):
        return render(request, "inventory/feature_locked.html",
                      {"feature": "Stock transfers", "active_nav": "inventory"})
    qs = (
        _transfer_scoped(
            request,
            StockTransfer.objects.for_business(request.business),
        )
        .select_related("from_warehouse", "to_warehouse", "requested_by")
        .prefetch_related("items")
    )
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "inventory/transfer_list.html",
                  {"page_obj": page_obj, "active_nav": "inventory",
                   "querystring": _qs_without_page(request)})


@module_permission_required("inventory", "inventory.transfer")
def transfer_create(request):
    if not subscriptions.has_feature(request.business, "transfers"):
        messages.warning(request, "Stock transfers are not included in your plan.")
        return redirect("inventory:stock_list")
    form = TransferForm(
        request.business,
        request.POST or None,
        membership=request.membership,
    )
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
                membership=request.membership,
                request=request,
            )
            messages.success(request, f"Transfer {transfer.transfer_number} created as draft.")
            return redirect("inventory:transfer_list")
        except (django_forms.ValidationError, ValidationError,
                subscriptions.SubscriptionInactive) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "inventory/transfer_form.html",
                  {"form": form, "active_nav": "inventory"})


@module_permission_required("inventory", "inventory.transfer")
def transfer_action(request, public_id, action):
    transfer = get_tenant_object(
        _transfer_scoped(request, StockTransfer.objects.all()),
        request.business,
        public_id=public_id,
    )
    if request.method != "POST":
        return redirect("inventory:transfer_list")
    try:
        subscriptions.require_operational(request.business)
        if action == "dispatch":
            workflows.dispatch_transfer(
                transfer=transfer,
                user=request.user,
                membership=request.membership,
                request=request,
            )
            messages.success(request, "Transfer dispatched — stock left the source warehouse.")
        elif action == "receive":
            workflows.receive_transfer(
                transfer=transfer,
                user=request.user,
                membership=request.membership,
                request=request,
            )
            messages.success(request, "Transfer received — stock added to destination.")
        elif action == "cancel":
            workflows.cancel_transfer(
                transfer=transfer,
                user=request.user,
                membership=request.membership,
                request=request,
            )
            messages.success(request, "Transfer cancelled.")
    except (ValidationError, subscriptions.SubscriptionInactive) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("inventory:transfer_list")


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------
@module_permission_required("inventory", "inventory.adjust")
def adjustment_list(request):
    qs = (
        _warehouse_scoped(
            request,
            StockAdjustment.objects.for_business(request.business),
        )
        .select_related("warehouse", "created_by", "approved_by")
        .prefetch_related("items__product")
    )
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "inventory/adjustment_list.html",
                  {"page_obj": page_obj, "active_nav": "inventory",
                   "querystring": _qs_without_page(request)})


@module_permission_required("inventory", "inventory.adjust")
def adjustment_create(request):
    form = AdjustmentForm(
        request.business,
        request.POST or None,
        membership=request.membership,
    )
    if request.method == "POST" and form.is_valid():
        try:
            subscriptions.require_operational(request.business)
            rows = parse_item_rows(
                request,
                request.business,
                allow_negative=True,
                allow_parent_meter_repair=True,
            )
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
                requires_approval=requires_approval,
                membership=request.membership,
                request=request,
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
                  {
                      "form": form,
                      "active_nav": "inventory",
                      "allow_parent_meter_repair": True,
                  })


@module_permission_required("inventory", "inventory.adjust_approve")
def adjustment_action(request, public_id, action):
    adjustment = get_tenant_object(
        _warehouse_scoped(request, StockAdjustment.objects.all()),
        request.business,
        public_id=public_id,
    )
    if request.method == "POST":
        try:
            if action == "approve":
                workflows.approve_adjustment(
                    adjustment=adjustment,
                    user=request.user,
                    membership=request.membership,
                    request=request,
                )
                messages.success(request, "Adjustment approved and applied.")
            elif action == "reject":
                workflows.reject_adjustment(
                    adjustment=adjustment,
                    user=request.user,
                    membership=request.membership,
                    request=request,
                )
                messages.success(request, "Adjustment rejected.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("inventory:adjustment_list")


# ---------------------------------------------------------------------------
# Physical counts
# ---------------------------------------------------------------------------
@module_permission_required("inventory", "inventory.count")
def count_list(request):
    qs = (
        _warehouse_scoped(
            request,
            StockCount.objects.for_business(request.business),
        )
        .select_related("warehouse", "created_by")
    )
    form = CountForm(
        request.business,
        request.POST or None,
        membership=request.membership,
    )
    if request.method == "POST" and form.is_valid():
        try:
            subscriptions.require_operational(request.business)
            count = workflows.start_count(
                business=request.business,
                warehouse=form.cleaned_data["warehouse"],
                user=request.user, notes=form.cleaned_data["notes"],
                membership=request.membership,
                request=request,
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


@module_permission_required("inventory", "inventory.count")
def count_detail(request, public_id):
    count = get_tenant_object(
        _warehouse_scoped(request, StockCount.objects.all()),
        request.business,
        public_id=public_id,
    )
    items = count.items.select_related("product", "variant").order_by("product__name")
    if not get_access_context(request).has_module("tailoring"):
        items = items.filter(product__is_tailoring_item=False)
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
                    workflows.approve_count(
                        count=count,
                        user=request.user,
                        membership=request.membership,
                        request=request,
                    )
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
