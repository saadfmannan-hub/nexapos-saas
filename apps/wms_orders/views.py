from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from apps.core.date_ranges import business_localtime
from apps.subscriptions.access import AccessAction
from apps.wms_core.access import wms_permission_required

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


@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def order_list(request):
    query = request.GET.get("q", "").strip()
    received_date = _optional_date(request)
    locations = list(
        selectors.order_locations_for_access(request.wms_user_access)
    )
    location_map = {str(location.public_id): location for location in locations}
    location_id = request.GET.get("location", "")
    if location_id not in location_map:
        location_id = ""
    status = request.GET.get("status", "")
    valid_statuses = {value for value, _label in WmsWorkshopOrder.Status.choices}
    if status not in valid_statuses:
        status = ""
    orders = selectors.filtered_workshop_orders(
        request.wms_user_access,
        query=query,
        received_date=received_date,
        location_id=location_id,
        status=status,
    )
    page = Paginator(orders, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "wms/orders/index.html",
        {
            "page": page,
            "record_count": orders.count(),
            "q": query,
            "selected_date": received_date,
            "locations": locations,
            "location_id": location_id,
            "status": status,
            "status_choices": WmsWorkshopOrder.Status.choices,
            "querystring": _querystring_without(request, "page"),
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
