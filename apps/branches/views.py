from django.contrib import messages
from django.shortcuts import redirect, render

from apps.audit import services as audit
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.subscriptions import services as subscriptions

from .forms import BranchForm, WarehouseForm
from .models import Branch, Warehouse


@require_permission("branches.manage")
def branch_list(request):
    branches = Branch.objects.for_business(request.business).order_by("name")
    warehouses = (
        Warehouse.objects.for_business(request.business)
        .select_related("branch").order_by("name")
    )
    b_cur, b_lim, _ = subscriptions.limit_state(request.business, "branches")
    w_cur, w_lim, _ = subscriptions.limit_state(request.business, "warehouses")
    return render(request, "branches/list.html", {
        "branches": branches, "warehouses": warehouses, "active_nav": "branches",
        "branch_count": b_cur, "branch_limit": b_lim,
        "warehouse_count": w_cur, "warehouse_limit": w_lim,
    })


@require_permission("branches.manage")
def branch_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(Branch, request.business, public_id=public_id)
    else:
        try:
            subscriptions.check_limit(request.business, "branches")
        except (subscriptions.LimitExceeded, subscriptions.SubscriptionInactive) as exc:
            messages.warning(request, str(exc))
            return redirect("branches:list")

    form = BranchForm(request.business, request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        branch = form.save(commit=False)
        branch.business = request.business
        branch.save()
        audit.log("branch.saved", request=request, module="branches", obj=branch,
                  description=f"Branch '{branch.name}' saved.")
        messages.success(request, "Branch saved.")
        return redirect("branches:list")
    return render(request, "branches/branch_form.html",
                  {"form": form, "branch": instance, "active_nav": "branches"})


@require_permission("branches.manage")
def warehouse_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(Warehouse, request.business, public_id=public_id)
    else:
        try:
            subscriptions.check_limit(request.business, "warehouses")
        except (subscriptions.LimitExceeded, subscriptions.SubscriptionInactive) as exc:
            messages.warning(request, str(exc))
            return redirect("branches:list")

    form = WarehouseForm(request.business, request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        warehouse = form.save(commit=False)
        warehouse.business = request.business
        warehouse.save()
        if warehouse.is_default:
            Warehouse.objects.for_business(request.business).exclude(
                pk=warehouse.pk
            ).update(is_default=False)
        audit.log("warehouse.saved", request=request, module="branches", obj=warehouse,
                  description=f"Warehouse '{warehouse.name}' saved.")
        messages.success(request, "Warehouse saved.")
        return redirect("branches:list")
    return render(request, "branches/warehouse_form.html",
                  {"form": form, "warehouse": instance, "active_nav": "branches"})
