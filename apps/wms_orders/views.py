from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_safe

from apps.core.date_ranges import business_localtime
from apps.reports.pdf import render_pdf
from apps.subscriptions.access import AccessAction
from apps.wms_core.access import wms_permission_required
from apps.wms_reports.services import export_xlsx

from . import selectors, services
from .forms import FinishOrdersBatchForm, NewOrdersBatchForm, OrderPieceCountForm
from .models import WmsWorkshopOrder


def _querystring_without(request, *keys):
    params = request.GET.copy()
    for key in keys:
        params.pop(key, None)
    return urlencode(params, doseq=True)


def _optional_date(request):
    value = request.GET.get("date", "").strip()
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def _order_production_context(user_access, order):
    production_history = list(
        selectors.production_lines_for_order_access(
            user_access,
            order,
        )
    )
    progress_by_category = {}
    for line in production_history:
        progress = progress_by_category.setdefault(
            line.category_id,
            {
                "operation": line.category.name,
                "display_order": line.category.display_order,
                "completed_pieces": 0,
            },
        )
        progress["completed_pieces"] += line.quantity
    production_progress = sorted(
        progress_by_category.values(),
        key=lambda item: (
            item["display_order"],
            item["operation"].casefold(),
        ),
    )
    return {
        "production_progress": production_progress,
        "production_history": production_history,
    }


def _filtered_order_list(request):
    query = request.GET.get("q", "").strip()
    received_date = _optional_date(request)
    month = None
    month_value = request.GET.get("month", "").strip()
    if month_value:
        try:
            month = date.fromisoformat(f"{month_value}-01")
        except ValueError as exc:
            raise Http404("Invalid order month.") from exc
    locations = list(
        selectors.order_locations_for_access(request.wms_user_access)
    )
    location_map = {str(location.public_id): location for location in locations}
    location_id = request.GET.get("location", "")
    selected_location = location_map.get(location_id)
    if month is not None and location_id and selected_location is None:
        raise Http404("Order location is unavailable.")
    if location_id not in location_map:
        location_id = ""
    status = request.GET.get("status", "")
    status_labels = dict(WmsWorkshopOrder.Status.choices)
    if status not in status_labels:
        status = ""
    orders = selectors.filtered_workshop_orders(
        request.wms_user_access,
        query=query,
        received_date=received_date,
        month=month,
        location_id=location_id,
        status=status,
    )
    return orders, {
        "q": query,
        "selected_date": received_date,
        "selected_month": month,
        "selected_location": selected_location,
        "locations": locations,
        "location_id": location_id,
        "status": status,
        "status_label": status_labels.get(status, "All statuses"),
    }


@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def order_list(request):
    orders, filters = _filtered_order_list(request)
    page = Paginator(orders, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "wms/orders/index.html",
        {
            "page": page,
            "record_count": page.paginator.count,
            **filters,
            "status_choices": WmsWorkshopOrder.Status.choices,
            "querystring": _querystring_without(request, "page"),
            "can_export_orders": (
                filters["selected_month"] is not None
                and request.wms_user_access.has_perm("wms.reports.view")
                and request.wms_user_access.has_perm("wms.reports.export")
            ),
            "can_manage_orders": request.wms_user_access.has_perm(
                "wms.orders.manage"
            ),
            "can_finish_orders": request.wms_user_access.has_perm(
                "wms.orders.finish"
            ),
            "active_nav": "wms",
            "wms_active_nav": "orders",
        },
    )


@require_safe
@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def order_list_export(request, export_format):
    orders, filters = _filtered_order_list(request)
    month = filters["selected_month"]
    if month is None:
        raise Http404("Select an order month to export.")

    rows = [
        (
            order.order_reference,
            order.location.branch.name,
            order.received_date,
            order.eligible_piece_count,
            order.get_status_display(),
            order.finished_date,
        )
        for order in orders.prefetch_related(None)
    ]
    location = filters["selected_location"]
    metadata = [
        ("Month", month.strftime("%B %Y")),
        ("Status", filters["status_label"]),
        ("Location", location.branch.name if location else "All permitted locations"),
        ("Total matching orders", len(rows)),
    ]
    if filters["q"]:
        metadata.append(("Search", filters["q"]))
    if filters["selected_date"]:
        metadata.append(("Received date filter", filters["selected_date"].isoformat()))

    status_slug = filters["status"].lower().replace("_", "-") or "all-statuses"
    report = {
        "title": "Workshop Orders",
        "sheet_name": "Workshop Orders",
        "filename": f"wms-workshop-orders-{month:%Y-%m}-{status_slug}",
        "columns": (
            "Order / Bill Reference", "WMS Location", "Received Date",
            "Eligible PCS", "Status", "Finished Date",
        ),
        "export_rows": rows,
        "text_columns": (0,),
        "column_formats": {0: "@", 2: "yyyy-mm-dd", 5: "yyyy-mm-dd"},
    }
    if export_format == "xlsx":
        return export_xlsx(report=report, business=request.business, metadata=metadata)

    pdf = render_pdf("wms/orders/export_pdf.html", {
        "report": report,
        "business": request.business,
        "metadata": metadata,
    })
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{report["filename"]}.pdf"'
    return response


@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def order_detail(request, public_id):
    order = selectors.get_workshop_order_for_access(
        request.wms_user_access,
        public_id,
    )
    production_context = _order_production_context(
        request.wms_user_access,
        order,
    )
    return render(
        request,
        "wms/orders/detail.html",
        {
            "order": order,
            **production_context,
            "can_finish_orders": (
                request.wms_user_access.has_perm("wms.orders.finish")
                and order.status == WmsWorkshopOrder.Status.IN_PROCESS
            ),
            "can_manage_orders": request.wms_user_access.has_perm(
                "wms.orders.manage"
            ),
            "active_nav": "wms",
            "wms_active_nav": "orders",
        },
    )


@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def order_print(request, public_id):
    order = selectors.get_workshop_order_for_access(
        request.wms_user_access,
        public_id,
    )
    production_context = _order_production_context(
        request.wms_user_access,
        order,
    )
    return render(
        request,
        "wms/orders/print.html",
        {
            "business": request.business,
            "order": order,
            "printed_at": business_localtime(request.business),
            **production_context,
        },
    )


@wms_permission_required("wms.orders.manage", action=AccessAction.WRITE)
def order_create_batch(request):
    form = NewOrdersBatchForm(
        request.business,
        request.wms_user_access,
        request.POST or None,
    )
    if request.method == "POST" and form.is_valid():
        try:
            orders = services.create_order_batch(
                business=request.business,
                user_access=request.wms_user_access,
                location=form.cleaned_data["location"],
                received_date=form.cleaned_data["received_date"],
                order_rows=form.cleaned_data["orders"],
                notes=form.cleaned_data["notes"],
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(
                request,
                f"{len(orders)} order(s) received In Process.",
            )
            return redirect("wms:order_list")
    return render(
        request,
        "wms/orders/batch_form.html",
        {
            "form": form,
            "is_finish": False,
            "active_nav": "wms",
            "wms_active_nav": "orders",
        },
    )


@wms_permission_required("wms.orders.manage", action=AccessAction.WRITE)
def order_piece_count_update(request, public_id):
    order = selectors.get_workshop_order_for_access(
        request.wms_user_access,
        public_id,
    )
    form = OrderPieceCountForm(
        request.POST or None,
        initial={"eligible_piece_count": order.eligible_piece_count},
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.update_order_piece_count(
                business=request.business,
                user_access=request.wms_user_access,
                order=order,
                eligible_piece_count=form.cleaned_data["eligible_piece_count"],
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "Workshop Order PCS updated.")
            return redirect("wms:order_detail", public_id=order.public_id)
    return render(
        request,
        "wms/orders/piece_count_form.html",
        {
            "order": order,
            "form": form,
            "active_nav": "wms",
            "wms_active_nav": "orders",
        },
    )


@wms_permission_required("wms.orders.finish", action=AccessAction.WRITE)
def order_finish_batch(request):
    initial = None
    if request.method == "GET":
        initial = {"references": request.GET.get("reference", "")}
    form = FinishOrdersBatchForm(
        request.business,
        request.POST or None,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            orders = services.finish_order_batch(
                business=request.business,
                user_access=request.wms_user_access,
                finished_date=form.cleaned_data["finished_date"],
                references=form.cleaned_data["references"],
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(
                request,
                f"{len(orders)} order(s) marked Finished / Ready.",
            )
            return redirect("wms:order_list")
    return render(
        request,
        "wms/orders/batch_form.html",
        {
            "form": form,
            "is_finish": True,
            "active_nav": "wms",
            "wms_active_nav": "orders",
        },
    )
