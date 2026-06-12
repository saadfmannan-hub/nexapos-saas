from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.core.money import D
from apps.subscriptions import services as subscriptions

from . import services
from .models import CashRegister, Shift
from .services import ShiftError


@require_permission("shifts.open")
def shift_list(request):
    from apps.branches.models import Branch

    my_shift = services.get_open_shift(request.business, request.user)
    registers = (
        CashRegister.objects.for_business(request.business)
        .filter(is_active=True).select_related("branch")
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

    qs = (
        Shift.objects.for_business(request.business)
        .select_related("register", "branch", "cashier", "approved_by")
    )
    if not request.membership.has_perm("shifts.approve"):
        qs = qs.filter(cashier=request.user)
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "registers/shift_list.html", {
        "my_shift": my_shift, "registers": registers, "branches": branches,
        "page_obj": page_obj, "active_nav": "registers", "querystring": "",
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
def register_create(request):
    if request.method == "POST":
        from apps.branches.models import Branch

        branch = get_tenant_object(Branch, request.business,
                                   pk=request.POST.get("branch_id"))
        if not request.membership.can_access_branch(branch):
            messages.error(request, "You cannot create a register for that branch.")
            return redirect("registers:shift_list")
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip().upper()
        if not name or not code:
            messages.error(request, "Name and code are required.")
        elif CashRegister.objects.for_business(request.business).filter(
            code=code
        ).exists():
            messages.error(request, "This register code is already in use.")
        else:
            CashRegister.objects.create(
                business=request.business, name=name[:80], code=code[:20],
                branch=branch,
                receipt_printer=request.POST.get("receipt_printer", "80mm"),
            )
            messages.success(request, "Register created.")
    return redirect("registers:shift_list")
