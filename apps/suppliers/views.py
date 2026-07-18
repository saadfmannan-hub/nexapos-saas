from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import redirect, render

from apps.branches.forms import TenantStyledModelForm
from apps.core.date_ranges import resolve_date_range
from apps.core.mixins import get_tenant_object
from apps.subscriptions import services as subscription_services
from apps.subscriptions.access import AccessAction, evaluate_access
from apps.subscriptions.decorators import module_permission_required

from . import services
from .models import Supplier, SupplierPayment


class SupplierForm(TenantStyledModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "code", "contact_person", "mobile", "email", "address",
                  "tax_number", "payment_terms", "notes", "is_active"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        self.fields["code"].required = False

    def clean_code(self):
        code = self.cleaned_data.get("code", "").strip()
        if not code:
            return code
        qs = Supplier.objects.for_business(self.business).filter(code__iexact=code)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This supplier code is already in use.")
        return code


@module_permission_required("suppliers", "suppliers.view", action=AccessAction.READ)
def supplier_list(request):
    qs = Supplier.objects.for_business(request.business)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) |
                       Q(mobile__icontains=q))
    show_business_balance = (
        request.membership.allowed_branch_ids is None
        and request.membership.allowed_warehouse_ids is None
    )
    payables = None
    if show_business_balance:
        payables = qs.filter(balance__gt=0).aggregate(t=Sum("balance"))["t"] or 0
    paginator = Paginator(qs.order_by("name"), 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "suppliers/list.html", {
        "page_obj": page_obj, "q": q, "payables": payables,
        "active_nav": "suppliers", "querystring": "",
        "show_business_balance": show_business_balance,
    })


@module_permission_required("suppliers", "suppliers.manage", action=AccessAction.WRITE)
def supplier_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(Supplier, request.business, public_id=public_id)
    form = SupplierForm(request.business, request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        try:
            supplier = services.save_supplier(
                business=request.business,
                values=form.cleaned_data,
                supplier=instance,
                user=request.user,
                membership=request.membership,
                request=request,
            )
        except (ValidationError, subscription_services.LimitExceeded) as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Supplier saved.")
            return redirect("suppliers:detail", public_id=supplier.public_id)
    return render(request, "suppliers/form.html",
                  {"form": form, "supplier": instance, "active_nav": "suppliers"})


@module_permission_required("suppliers", "suppliers.view", action=AccessAction.READ)
def supplier_detail(request, public_id):
    from apps.purchases.models import Purchase, PurchaseReturn

    supplier = get_tenant_object(Supplier, request.business, public_id=public_id)
    date_from, date_to = resolve_date_range(request.GET, request.business)
    purchases_decision = evaluate_access(
        request,
        "purchases",
        permission_code="purchases.view",
        action=AccessAction.READ,
    )
    purchases_access = purchases_decision.allowed
    allowed_branches = request.membership.allowed_branch_ids
    allowed_warehouses = request.membership.allowed_warehouse_ids
    show_business_balance = (
        allowed_branches is None and allowed_warehouses is None
    )
    purchases = ()
    payments = ()
    returns = ()
    stats = {"total": 0, "paid": 0}
    if purchases_access:
        purchase_scope = Purchase.objects.for_business(request.business).filter(
            supplier=supplier
        )
        if allowed_branches is not None:
            purchase_scope = purchase_scope.filter(branch_id__in=allowed_branches)
        if allowed_warehouses is not None:
            purchase_scope = purchase_scope.filter(warehouse_id__in=allowed_warehouses)

        purchases = purchase_scope.filter(
            purchase_date__gte=date_from,
            purchase_date__lte=date_to,
        ).order_by("-purchase_date")[:25]
        payment_scope = SupplierPayment.objects.for_business(request.business).filter(
            supplier=supplier,
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
        )
        if allowed_branches is None and allowed_warehouses is None:
            payment_scope = payment_scope.filter(
                Q(purchase__in=purchase_scope) | Q(purchase__isnull=True)
            )
        else:
            payment_scope = payment_scope.filter(purchase__in=purchase_scope)
        payments = payment_scope.select_related("purchase", "payment_method")[:25]
        returns = (
            PurchaseReturn.objects.for_business(request.business)
            .filter(
                supplier=supplier,
                purchase__in=purchase_scope,
                created_at__date__gte=date_from,
                created_at__date__lte=date_to,
            )
            .select_related("purchase")[:15]
        )
        stats = purchase_scope.exclude(status="cancelled").aggregate(
            total=Sum("total"),
            paid=Sum("amount_paid"),
        )
    return render(request, "suppliers/detail.html", {
        "supplier": supplier, "purchases": purchases, "payments": payments,
        "returns": returns, "stats": stats, "active_nav": "suppliers",
        "date_from": date_from, "date_to": date_to,
        "purchases_access": purchases_access,
        "show_business_balance": show_business_balance,
    })
