from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.audit import services as audit
from apps.branches.forms import TenantStyledModelForm
from apps.branches.models import Branch
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.registers import services as register_services
from apps.subscriptions import services as subscriptions

from .models import Expense, ExpenseCategory
from .services import next_expense_number


class ExpenseForm(TenantStyledModelForm):
    class Meta:
        model = Expense
        fields = ["expense_date", "branch", "category", "payee", "supplier",
                  "amount", "tax_amount", "payment_method", "reference",
                  "description", "attachment"]
        widgets = {
            "expense_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        from apps.sales.models import PaymentMethod
        from apps.suppliers.models import Supplier

        self.fields["branch"].queryset = Branch.objects.for_business(business).filter(is_active=True)
        self.fields["category"].queryset = ExpenseCategory.objects.for_business(business).filter(is_active=True)
        self.fields["supplier"].queryset = Supplier.objects.for_business(business).filter(is_active=True)
        self.fields["supplier"].required = False
        self.fields["payment_method"].queryset = (
            PaymentMethod.objects.for_business(business).filter(is_active=True)
            .exclude(kind__in=["customer_credit", "store_credit"])
        )
        self.fields["payment_method"].required = False

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Amount must be positive.")
        return amount


class ExpenseCategoryForm(TenantStyledModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "parent", "is_active"]

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        self.fields["parent"].queryset = ExpenseCategory.objects.for_business(
            business).filter(parent__isnull=True)
        self.fields["parent"].required = False


@require_permission("expenses.view")
def expense_list(request):
    if not subscriptions.has_feature(request.business, "expenses"):
        return render(request, "inventory/feature_locked.html",
                      {"feature": "Expenses", "active_nav": "expenses"})
    qs = (
        Expense.objects.for_business(request.business)
        .select_related("category", "branch", "created_by", "payment_method")
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(expense_number__icontains=q) | Q(payee__icontains=q) |
                       Q(description__icontains=q))
    status = request.GET.get("status", "")
    if status:
        qs = qs.filter(status=status)
    category_id = request.GET.get("category", "")
    if category_id.isdigit():
        qs = qs.filter(category_id=category_id)
    date_from, date_to = request.GET.get("from", ""), request.GET.get("to", "")
    if date_from:
        qs = qs.filter(expense_date__gte=date_from)
    if date_to:
        qs = qs.filter(expense_date__lte=date_to)
    total = qs.exclude(status__in=["rejected", "cancelled"]).aggregate(
        t=Sum("amount"))["t"] or 0
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    categories = ExpenseCategory.objects.for_business(request.business)
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "expenses/list.html", {
        "page_obj": page_obj, "q": q, "total": total, "categories": categories,
        "statuses": Expense.Status.choices, "active_nav": "expenses",
        "querystring": (params.urlencode() + "&") if params else "",
        "can_approve": request.membership.has_perm("expenses.approve"),
    })


@require_permission("expenses.manage")
def expense_create(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(Expense, request.business, public_id=public_id)
        if instance.status not in (Expense.Status.DRAFT, Expense.Status.SUBMITTED,
                                   Expense.Status.REJECTED):
            messages.error(request, "Approved or paid expenses cannot be edited.")
            return redirect("expenses:list")
    form = ExpenseForm(request.business, request.POST or None,
                       request.FILES or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        try:
            subscriptions.require_operational(request.business)
        except subscriptions.SubscriptionInactive as exc:
            messages.error(request, str(exc))
            return redirect("expenses:list")
        expense = form.save(commit=False)
        expense.business = request.business
        if instance is None:
            expense.expense_number = next_expense_number(request.business)
            expense.created_by = request.user
            expense.shift = register_services.get_open_shift(
                request.business, request.user)
        threshold = request.business.settings.expense_approval_threshold
        needs_approval = (
            threshold > 0 and expense.amount >= threshold
            and not request.membership.has_perm("expenses.approve")
        )
        expense.status = (Expense.Status.SUBMITTED if needs_approval
                          else Expense.Status.APPROVED)
        if not needs_approval:
            expense.approved_by = request.user
        expense.save()
        if needs_approval:
            from apps.notifications.services import notify_role

            notify_role(request.business, "expenses.approve",
                        f"Expense {expense.expense_number} needs approval "
                        f"({expense.amount})",
                        severity="warning", category="expense_pending",
                        link="/expenses/")
            messages.info(request, "Expense submitted for approval.")
        else:
            messages.success(request, "Expense recorded.")
        audit.log("expense.saved", request=request, module="expenses", obj=expense,
                  description=f"Expense {expense.expense_number} "
                              f"({expense.amount}) saved.")
        return redirect("expenses:list")
    return render(request, "expenses/form.html",
                  {"form": form, "expense": instance, "active_nav": "expenses"})


@require_permission("expenses.approve")
def expense_action(request, public_id, action):
    expense = get_tenant_object(Expense, request.business, public_id=public_id)
    if request.method == "POST":
        if action == "approve" and expense.status == Expense.Status.SUBMITTED:
            expense.status = Expense.Status.APPROVED
            expense.approved_by = request.user
            expense.save(update_fields=["status", "approved_by", "updated_at"])
            messages.success(request, "Expense approved.")
        elif action == "reject" and expense.status == Expense.Status.SUBMITTED:
            expense.status = Expense.Status.REJECTED
            expense.approved_by = request.user
            expense.save(update_fields=["status", "approved_by", "updated_at"])
            messages.success(request, "Expense rejected.")
        elif action == "cancel" and expense.status in (
            Expense.Status.DRAFT, Expense.Status.SUBMITTED, Expense.Status.APPROVED
        ):
            expense.status = Expense.Status.CANCELLED
            expense.save(update_fields=["status", "updated_at"])
            messages.success(request, "Expense cancelled.")
        audit.log(f"expense.{action}", request=request, module="expenses",
                  obj=expense,
                  description=f"Expense {expense.expense_number} {action}d.")
    return redirect("expenses:list")


@require_permission("expenses.manage")
def category_manage(request):
    instance = None
    edit_id = request.GET.get("edit")
    if edit_id:
        instance = get_tenant_object(ExpenseCategory, request.business,
                                     public_id=edit_id)
    form = ExpenseCategoryForm(request.business, request.POST or None,
                               instance=instance)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.business = request.business
        obj.save()
        messages.success(request, "Expense category saved.")
        return redirect("expenses:categories")
    items = ExpenseCategory.objects.for_business(request.business)
    return render(request, "expenses/categories.html",
                  {"form": form, "items": items, "editing": instance,
                   "active_nav": "expenses"})
