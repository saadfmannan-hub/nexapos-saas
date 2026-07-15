import logging

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.deletion import ProtectedError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.branches.forms import TenantStyledModelForm
from apps.branches.models import Branch
from apps.core.date_ranges import date_range_querystring, resolve_date_range
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.registers import services as register_services
from apps.subscriptions import services as subscriptions

from .models import Expense, ExpenseCategory, RecurringExpenseTemplate
from .services import (
    RecurringExpenseGenerationError,
    ensure_recurring_expenses_for_month,
    next_expense_number,
)

logger = logging.getLogger(__name__)


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


class RecurringExpenseTemplateForm(TenantStyledModelForm):
    class Meta:
        model = RecurringExpenseTemplate
        fields = [
            "name", "category", "default_amount", "due_day", "start_date",
            "end_date", "notes", "is_active",
        ]
        labels = {
            "name": "Expense name",
            "category": "Expense category",
            "default_amount": "Monthly amount",
            "due_day": "Due day",
            "is_active": "Active",
        }
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "due_day": forms.NumberInput(attrs={"min": 1, "max": 31}),
        }

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        categories = ExpenseCategory.objects.for_business(business)
        if self.instance.pk and self.instance.category_id:
            categories = categories.filter(
                Q(is_active=True) | Q(pk=self.instance.category_id)
            )
        else:
            categories = categories.filter(is_active=True)
        self.fields["category"].queryset = categories.order_by("name")

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Expense name is required.")
        return name

    def clean_default_amount(self):
        amount = self.cleaned_data["default_amount"]
        if amount < 0:
            raise forms.ValidationError("Monthly amount cannot be negative.")
        return amount

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        if start_date and end_date and end_date < start_date:
            self.add_error("end_date", "End date cannot be before start date.")
        return cleaned_data


@require_permission("expenses.view")
def expense_list(request):
    if not subscriptions.has_feature(request.business, "expenses"):
        return render(request, "inventory/feature_locked.html",
                      {"feature": "Expenses", "active_nav": "expenses"})
    try:
        ensure_recurring_expenses_for_month(
            request.business,
            timezone.localdate(),
        )
    except RecurringExpenseGenerationError as exc:
        logger.warning(
            "Fixed-expense generation skipped for business %s: %s",
            request.business.pk,
            exc,
        )
    qs = (
        Expense.objects.for_business(request.business)
        .filter(recurring_template__isnull=True)
        .select_related(
            "category", "branch", "created_by", "payment_method",
        )
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
    date_from, date_to = resolve_date_range(request.GET, request.business)
    qs = qs.filter(
        expense_date__gte=date_from,
        expense_date__lte=date_to,
    )
    total = qs.exclude(status__in=["rejected", "cancelled"]).aggregate(
        t=Sum("amount"))["t"] or 0
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    categories = ExpenseCategory.objects.for_business(request.business)
    fixed_templates = (
        RecurringExpenseTemplate.objects.for_business(request.business)
        .select_related("category")
        .annotate(generated_count=Count("generated_expenses"))
        .order_by("name", "id")
    )
    querystring = date_range_querystring(request.GET, date_from, date_to)
    return render(request, "expenses/list.html", {
        "page_obj": page_obj, "q": q, "total": total, "categories": categories,
        "statuses": Expense.Status.choices, "active_nav": "expenses",
        "date_from": date_from, "date_to": date_to,
        "querystring": f"{querystring}&" if querystring else "",
        "can_approve": request.membership.has_perm("expenses.approve"),
        "can_manage": request.membership.has_perm("expenses.manage"),
        "fixed_templates": fixed_templates,
    })


@require_permission("expenses.manage")
def expense_create(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(Expense, request.business, public_id=public_id)
        editable_statuses = (
            Expense.Status.DRAFT,
            Expense.Status.SUBMITTED,
            Expense.Status.REJECTED,
        )
        recurring_approved = (
            instance.recurring_template_id
            and instance.status == Expense.Status.APPROVED
        )
        if instance.status not in editable_statuses and not recurring_approved:
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


def _recurring_feature_lock(request):
    if subscriptions.has_feature(request.business, "expenses"):
        return None
    return render(
        request,
        "inventory/feature_locked.html",
        {"feature": "Expenses", "active_nav": "expenses"},
    )


def _fixed_expenses_url():
    return f"{reverse('expenses:list')}#fixed-expenses"


def _fixed_expenses_redirect():
    return redirect(_fixed_expenses_url())


@require_permission("expenses.view")
def recurring_template_list(request):
    locked = _recurring_feature_lock(request)
    if locked is not None:
        return locked
    return _fixed_expenses_redirect()


@require_permission("expenses.manage")
def recurring_template_form(request, public_id=None):
    locked = _recurring_feature_lock(request)
    if locked is not None:
        return locked
    instance = None
    if public_id:
        instance = get_tenant_object(
            RecurringExpenseTemplate,
            request.business,
            public_id=public_id,
        )
    form = RecurringExpenseTemplateForm(
        request.business,
        request.POST or None,
        instance=instance,
    )
    if request.method == "POST" and form.is_valid():
        try:
            subscriptions.require_operational(request.business)
        except subscriptions.SubscriptionInactive as exc:
            messages.error(request, str(exc))
            return _fixed_expenses_redirect()
        template = form.save(commit=False)
        template.business = request.business
        template.save()
        action = "updated" if instance else "created"
        audit.log(
            f"recurring_expense_template.{action}",
            request=request,
            module="expenses",
            obj=template,
            description=f"Fixed expense '{template.name}' {action}.",
        )
        messages.success(request, "Fixed expense saved.")
        return _fixed_expenses_redirect()
    return render(request, "expenses/recurring_form.html", {
        "form": form,
        "template": instance,
        "active_nav": "expenses",
    })


@require_permission("expenses.manage")
@require_POST
def recurring_template_action(request, public_id, action):
    locked = _recurring_feature_lock(request)
    if locked is not None:
        return locked
    template = get_tenant_object(
        RecurringExpenseTemplate,
        request.business,
        public_id=public_id,
    )
    if action == "archive":
        template.is_active = False
        message = "Fixed expense made inactive. Previous expenses were preserved."
        audit_action = "archived"
    elif action == "restore":
        template.is_active = True
        message = "Fixed expense made active."
        audit_action = "restored"
    else:
        messages.error(request, "Unknown fixed expense action.")
        return _fixed_expenses_redirect()
    template.save(update_fields=["is_active", "updated_at"])
    audit.log(
        f"recurring_expense_template.{audit_action}",
        request=request,
        module="expenses",
        obj=template,
        description=f"Fixed expense '{template.name}' {audit_action}.",
    )
    messages.success(request, message)
    return _fixed_expenses_redirect()


@require_permission("expenses.manage")
def recurring_template_delete(request, public_id):
    locked = _recurring_feature_lock(request)
    if locked is not None:
        return locked
    template = get_tenant_object(
        RecurringExpenseTemplate,
        request.business,
        public_id=public_id,
    )
    if template.generated_expenses.exists():
        messages.error(
            request,
            "This fixed expense already has monthly history and cannot be "
            "deleted. Make it inactive instead.",
        )
        return _fixed_expenses_redirect()
    if request.method != "POST":
        return render(request, "expenses/recurring_delete_confirm.html", {
            "template": template,
            "active_nav": "expenses",
        })

    try:
        with transaction.atomic():
            template = get_tenant_object(
                RecurringExpenseTemplate.objects.select_for_update(),
                request.business,
                public_id=public_id,
            )
            if template.generated_expenses.exists():
                raise ProtectedError(
                    "Recurring expense history exists.", [template]
                )
            template.delete()
    except ProtectedError:
        messages.error(
            request,
            "This fixed expense already has monthly history and cannot be "
            "deleted. Make it inactive instead.",
        )
        return _fixed_expenses_redirect()

    audit.log(
        "recurring_expense_template.deleted",
        request=request,
        module="expenses",
        obj=template,
        description=f"Fixed expense '{template.name}' deleted.",
    )
    messages.success(request, "Fixed expense deleted.")
    return _fixed_expenses_redirect()


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
