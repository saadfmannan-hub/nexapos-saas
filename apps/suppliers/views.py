from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import redirect, render

from apps.audit import services as audit
from apps.branches.forms import TenantStyledModelForm
from apps.core.date_ranges import resolve_date_range
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object

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


def _next_supplier_code(business):
    n = Supplier.objects.for_business(business).count() + 1
    while Supplier.objects.for_business(business).filter(code=f"SUP-{n:04d}").exists():
        n += 1
    return f"SUP-{n:04d}"


@require_permission("suppliers.view")
def supplier_list(request):
    qs = Supplier.objects.for_business(request.business)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) |
                       Q(mobile__icontains=q))
    payables = qs.filter(balance__gt=0).aggregate(t=Sum("balance"))["t"] or 0
    paginator = Paginator(qs.order_by("name"), 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "suppliers/list.html", {
        "page_obj": page_obj, "q": q, "payables": payables,
        "active_nav": "suppliers", "querystring": "",
    })


@require_permission("suppliers.manage")
def supplier_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(Supplier, request.business, public_id=public_id)
    form = SupplierForm(request.business, request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        supplier = form.save(commit=False)
        supplier.business = request.business
        if not supplier.code:
            supplier.code = _next_supplier_code(request.business)
        if instance is None and supplier.opening_balance:
            supplier.balance = supplier.opening_balance
        supplier.save()
        audit.log("supplier.saved", request=request, module="suppliers", obj=supplier,
                  description=f"Supplier '{supplier.name}' saved.")
        messages.success(request, "Supplier saved.")
        return redirect("suppliers:detail", public_id=supplier.public_id)
    return render(request, "suppliers/form.html",
                  {"form": form, "supplier": instance, "active_nav": "suppliers"})


@require_permission("suppliers.view")
def supplier_detail(request, public_id):
    from apps.purchases.models import Purchase, PurchaseReturn

    supplier = get_tenant_object(Supplier, request.business, public_id=public_id)
    date_from, date_to = resolve_date_range(request.GET, request.business)
    purchases = (
        Purchase.objects.for_business(request.business)
        .filter(
            supplier=supplier,
            purchase_date__gte=date_from,
            purchase_date__lte=date_to,
        )
        .order_by("-purchase_date")[:25]
    )
    payments = (
        SupplierPayment.objects.for_business(request.business)
        .filter(
            supplier=supplier,
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
        )
        .select_related("purchase", "payment_method")[:25]
    )
    returns = (
        PurchaseReturn.objects.for_business(request.business)
        .filter(
            supplier=supplier,
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
        )
        .select_related("purchase")[:15]
    )
    stats = Purchase.objects.for_business(request.business).filter(
        supplier=supplier
    ).exclude(status="cancelled").aggregate(total=Sum("total"), paid=Sum("amount_paid"))
    return render(request, "suppliers/detail.html", {
        "supplier": supplier, "purchases": purchases, "payments": payments,
        "returns": returns, "stats": stats, "active_nav": "suppliers",
        "date_from": date_from, "date_to": date_to,
    })
