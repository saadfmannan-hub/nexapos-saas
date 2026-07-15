from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.core.date_ranges import date_range_querystring, resolve_date_range
from apps.core.decorators import business_required, require_permission
from apps.core.mixins import get_tenant_object
from apps.core.money import D
from apps.subscriptions import services as subscriptions
from apps.subscriptions.helpers import guard_limit, limit_blocked_response

from . import services
from .forms import RegisterForm
from .models import CashRegister, Shift
from .services import RegisterLifecycleError, ShiftError


def _manageable_register(request, public_id, *, lock=False):
    registers = CashRegister.objects.select_related("branch")
    if lock:
        registers = registers.select_for_update()
    register = get_tenant_object(
        registers,
        request.business,
        public_id=public_id,
    )
    if not request.membership.can_access_branch(register.branch):
        raise Http404
    return register


@business_required
def shift_list(request):
    from apps.branches.models import Branch
    from apps.sales.models import Sale

    can_open_shifts = request.membership.has_perm("shifts.open")
    can_manage_registers = request.membership.has_perm("registers.manage")
    if not can_open_shifts and not can_manage_registers:
        raise PermissionDenied

    my_shift = (
        services.get_open_shift(
            request.business,
            request.user,
            membership=request.membership,
        )
        if can_open_shifts
        else None
    )
    registers = (
        CashRegister.objects.for_business(request.business)
        .filter(is_active=True, branch__is_active=True)
        .select_related("branch")
    )
    # All active branches for the business; restricted only when the
    # member is explicitly branch-limited (owners/admins see everything).
    branches = Branch.objects.for_business(request.business).filter(
        is_active=True
    ).order_by("name")
    allowed = request.membership.allowed_branch_ids
    if allowed is not None:
        registers = registers.filter(branch_id__in=allowed)
        branches = branches.filter(id__in=allowed)

    page_obj = None
    date_from, date_to = resolve_date_range(request.GET, request.business)
    if can_open_shifts:
        shifts = (
            Shift.objects.for_business(request.business)
            .select_related("register", "branch", "cashier", "approved_by")
            .filter(
                opened_at__date__gte=date_from,
                opened_at__date__lte=date_to,
            )
        )
        if not request.membership.has_perm("shifts.approve"):
            shifts = shifts.filter(cashier=request.user)
        page_obj = Paginator(shifts, 25).get_page(request.GET.get("page"))

    managed_registers = CashRegister.objects.none()
    register_usage = None
    if can_manage_registers:
        managed_registers = (
            CashRegister.objects.for_business(request.business)
            .select_related("branch")
            .annotate(
                has_shifts=Exists(Shift.objects.filter(register=OuterRef("pk"))),
                has_sales=Exists(Sale.objects.filter(register=OuterRef("pk"))),
            )
            .order_by("branch__name", "name")
        )
        if allowed is not None:
            managed_registers = managed_registers.filter(branch_id__in=allowed)
        current, limit, allowed_by_plan = subscriptions.limit_state(
            request.business, "pos_terminals"
        )
        register_usage = {
            "current": current,
            "limit": limit,
            "allowed": allowed_by_plan,
        }

    querystring = date_range_querystring(request.GET, date_from, date_to)
    return render(request, "registers/shift_list.html", {
        "my_shift": my_shift, "registers": registers, "branches": branches,
        "page_obj": page_obj, "active_nav": "registers",
        "date_from": date_from, "date_to": date_to,
        "querystring": f"{querystring}&" if querystring else "",
        "managed_registers": managed_registers,
        "register_usage": register_usage,
        "can_open_shifts": can_open_shifts,
        "can_manage_registers": can_manage_registers,
    })


@require_permission("shifts.open")
def shift_open(request):
    if request.method != "POST":
        return redirect("registers:shift_list")
    register = get_tenant_object(
        CashRegister, request.business, pk=request.POST.get("register_id")
    )
    if not request.membership.can_access_branch(register.branch):
        messages.error(request, "You cannot open a shift on that branch's register.")
        return redirect("registers:shift_list")
    try:
        subscriptions.require_operational(request.business)
        shift = services.open_shift(
            business=request.business, register=register, cashier=request.user,
            opening_cash=D(request.POST.get("opening_cash")),
            notes=request.POST.get("notes", "")[:300], request=request,
        )
        messages.success(request, f"Shift opened on {shift.register.name}. Good selling!")
        return redirect("sales:pos")
    except (ShiftError, subscriptions.SubscriptionInactive) as exc:
        messages.error(request, str(exc))
    return redirect("registers:shift_list")


@require_permission("shifts.open")
def shift_detail(request, public_id):
    shift = get_tenant_object(
        Shift.objects.select_related("register", "branch", "cashier"),
        request.business, public_id=public_id,
    )
    if shift.cashier_id != request.user.id and not request.membership.has_perm(
        "shifts.approve"
    ):
        from django.http import Http404
        raise Http404
    totals = services.shift_totals(shift)
    sales = shift.sales.select_related("customer").order_by("-sale_date")[:100]
    return render(request, "registers/shift_detail.html", {
        "shift": shift, "totals": totals, "sales": sales,
        "active_nav": "registers",
        "can_approve": request.membership.has_perm("shifts.approve"),
        "can_reopen": request.membership.has_perm("shifts.reopen"),
    })


@require_permission("shifts.close")
def shift_close(request, public_id):
    shift = get_tenant_object(Shift, request.business, public_id=public_id)
    if shift.cashier_id != request.user.id and not request.membership.has_perm(
        "shifts.approve"
    ):
        messages.error(request, "Only the shift's cashier or a manager can close it.")
        return redirect("registers:shift_list")
    if request.method == "POST":
        try:
            services.close_shift(
                shift=shift, actual_cash=D(request.POST.get("actual_cash")),
                notes=request.POST.get("notes", "")[:300], user=request.user,
                request=request,
            )
            messages.success(
                request,
                f"Shift closed. Expected {shift.expected_cash}, actual "
                f"{shift.actual_cash}, difference {shift.difference}.",
            )
            return redirect("registers:shift_detail", public_id=shift.public_id)
        except ShiftError as exc:
            messages.error(request, str(exc))
    totals = services.shift_totals(shift)
    return render(request, "registers/shift_close.html",
                  {"shift": shift, "totals": totals, "active_nav": "registers"})


@require_permission("shifts.approve")
def shift_approve(request, public_id):
    shift = get_tenant_object(Shift, request.business, public_id=public_id)
    if request.method == "POST" and shift.status == Shift.Status.CLOSED:
        shift.status = Shift.Status.APPROVED
        shift.approved_by = request.user
        shift.save(update_fields=["status", "approved_by", "updated_at"])
        from apps.audit import services as audit

        audit.log("shift.approved", request=request, module="registers", obj=shift,
                  description=f"Shift {shift.pk} approved "
                              f"(difference {shift.difference}).")
        messages.success(request, "Shift approved.")
    return redirect("registers:shift_detail", public_id=shift.public_id)


@require_permission("shifts.reopen")
def shift_reopen(request, public_id):
    shift = get_tenant_object(Shift, request.business, public_id=public_id)
    if request.method == "POST":
        try:
            services.reopen_shift(shift=shift, user=request.user, request=request)
            messages.warning(request, "Shift reopened — this was recorded in the audit log.")
        except ShiftError as exc:
            messages.error(request, str(exc))
    return redirect("registers:shift_detail", public_id=shift.public_id)


@require_permission("registers.manage")
@transaction.atomic
def register_form(request, public_id=None):
    register = (
        _manageable_register(request, public_id, lock=True) if public_id else None
    )
    old_values = None
    if register is not None:
        old_values = {
            "name": register.name,
            "code": register.code,
            "branch_id": register.branch_id,
            "receipt_printer": register.receipt_printer,
        }
    if register is None:
        blocked = guard_limit(request, "pos_terminals")
        if blocked:
            return blocked

    data = None
    if request.method == "POST":
        data = request.POST.copy()
        # Preserve compatibility with the previous inline form payload.
        if not data.get("branch") and data.get("branch_id"):
            data["branch"] = data["branch_id"]
    initial = None
    if register is None and request.method == "GET" and request.GET.get("branch"):
        initial = {"branch": request.GET["branch"]}
    form = RegisterForm(
        request.business,
        data=data,
        instance=register,
        membership=request.membership,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        saved_register = form.save(commit=False)
        saved_register.business = request.business
        try:
            with transaction.atomic():
                saved_register.save()
        except IntegrityError:
            form.add_error("code", "This register code is already in use.")
        else:
            new_values = {
                "name": saved_register.name,
                "code": saved_register.code,
                "branch_id": saved_register.branch_id,
                "receipt_printer": saved_register.receipt_printer,
            }
            audit.log(
                "register.updated" if register else "register.created",
                request=request,
                module="registers",
                obj=saved_register,
                description=(
                    f"Register {saved_register.code} updated."
                    if register
                    else f"Register {saved_register.code} created."
                ),
                old_values=old_values,
                new_values=new_values,
            )
            messages.success(
                request,
                "Register updated." if register else "Register created.",
            )
            return redirect("registers:shift_list")

    return render(request, "registers/register_form.html", {
        "form": form,
        "register": register,
        "active_nav": "registers",
    })


@require_permission("registers.manage")
def register_archive(request, public_id):
    register = _manageable_register(request, public_id)
    if request.method == "POST":
        try:
            services.archive_register(
                register=register,
                user=request.user,
                request=request,
            )
            messages.success(request, "Register archived. Its history remains available.")
            return redirect("registers:shift_list")
        except RegisterLifecycleError as exc:
            messages.error(request, str(exc))
            return redirect("registers:shift_list")
    return render(request, "registers/register_archive_confirm.html", {
        "register": register,
        "active_nav": "registers",
    })


@require_permission("registers.manage")
@require_POST
def register_reactivate(request, public_id):
    register = _manageable_register(request, public_id)
    try:
        services.reactivate_register(
            register=register,
            user=request.user,
            request=request,
        )
        messages.success(request, "Register reactivated.")
    except (subscriptions.LimitExceeded, subscriptions.SubscriptionInactive) as exc:
        return limit_blocked_response(request, exc, resource="pos_terminals")
    except RegisterLifecycleError as exc:
        messages.error(request, str(exc))
    return redirect("registers:shift_list")


@require_permission("registers.manage")
def register_delete(request, public_id):
    register = _manageable_register(request, public_id)
    assessment = services.assess_register_deletion(register)
    if request.method == "POST":
        try:
            services.delete_register_if_safe(
                register=register,
                user=request.user,
                request=request,
            )
            messages.success(request, "Unused register permanently deleted.")
            return redirect("registers:shift_list")
        except RegisterLifecycleError as exc:
            messages.error(request, str(exc))
            return redirect("registers:shift_list")
    return render(request, "registers/register_delete_confirm.html", {
        "register": register,
        "assessment": assessment,
        "active_nav": "registers",
    })
