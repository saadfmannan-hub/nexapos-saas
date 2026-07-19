from django.contrib import messages
from django.shortcuts import redirect, render

from apps.audit import services as audit
from apps.core.mixins import get_tenant_object
from apps.subscriptions import services as subscriptions
from apps.subscriptions.decorators import module_permission_required
from apps.subscriptions.helpers import guard_limit

from .forms import BranchForm, WarehouseForm
from .models import Branch, Warehouse


def _allowed_branches(request):
    branches = Branch.objects.for_business(request.business)
    allowed_ids = request.membership.allowed_branch_ids
    if allowed_ids is not None:
        branches = branches.filter(pk__in=allowed_ids)
    return branches


def _allowed_warehouses(request):
    warehouses = Warehouse.objects.for_business(request.business)
    allowed_ids = request.membership.allowed_warehouse_ids
    if allowed_ids is not None:
        warehouses = warehouses.filter(pk__in=allowed_ids)
    return warehouses


@module_permission_required("pos_core", "branches.manage")
def branch_list(request):
    branches = _allowed_branches(request).order_by("name")
    warehouses = (
        _allowed_warehouses(request).select_related("branch").order_by("name")
    )
    b_cur, b_lim, _ = subscriptions.limit_state(request.business, "branches")
    w_cur, w_lim, _ = subscriptions.limit_state(request.business, "warehouses")
    return render(request, "branches/list.html", {
        "branches": branches, "warehouses": warehouses, "active_nav": "branches",
        "branch_count": b_cur, "branch_limit": b_lim,
        "warehouse_count": w_cur, "warehouse_limit": w_lim,
    })


@module_permission_required("pos_core", "branches.manage")
def branch_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(
            _allowed_branches(request), request.business, public_id=public_id
        )
    else:
        blocked = guard_limit(request, "branches")
        if blocked:
            return blocked

    form = BranchForm(request.business, request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        creating = instance is None
        branch = form.save(commit=False)
        branch.business = request.business
        branch.save()
        if creating:
            from apps.customers.services import ensure_walk_in_customer

            ensure_walk_in_customer(request.business, branch)
        audit.log("branch.saved", request=request, module="branches", obj=branch,
                  description=f"Branch '{branch.name}' saved.")
        messages.success(request, "Branch saved.")
        return redirect("branches:list")
    return render(request, "branches/branch_form.html",
                  {"form": form, "branch": instance, "active_nav": "branches"})


@module_permission_required("pos_core", "branches.manage")
def warehouse_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(
            _allowed_warehouses(request), request.business, public_id=public_id
        )
    else:
        blocked = guard_limit(request, "warehouses")
        if blocked:
            return blocked

    form = WarehouseForm(
        request.business,
        request.POST or None,
        instance=instance,
        membership=request.membership,
    )
    if request.method == "POST" and form.is_valid():
        warehouse = form.save(commit=False)
        warehouse.business = request.business
        warehouse.save()
        if warehouse.is_default:
            _allowed_warehouses(request).exclude(pk=warehouse.pk).update(
                is_default=False
            )
        audit.log("warehouse.saved", request=request, module="branches", obj=warehouse,
                  description=f"Warehouse '{warehouse.name}' saved.")
        messages.success(request, "Warehouse saved.")
        return redirect("branches:list")
    return render(request, "branches/warehouse_form.html",
                  {"form": form, "warehouse": instance, "active_nav": "branches"})
