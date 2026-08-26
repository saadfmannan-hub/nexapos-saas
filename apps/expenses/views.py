from datetime import timedelta

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.db.models.deletion import ProtectedError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.branches.forms import TenantStyledModelForm
from apps.branches.models import Branch
from apps.core.date_ranges import (
    business_date_bounds,
    business_localtime,
    date_range_querystring,
    resolve_date_range,
)
from apps.core.mixins import get_tenant_object
from apps.registers import services as register_services
from apps.subscriptions.access import AccessAction
from apps.subscriptions.decorators import module_permission_required

from .models import Expense, ExpenseCategory, RecurringExpenseTemplate
from .services import (
    filter_by_payment_medium,
    matching_open_drawer_shift,
    next_expense_number,
    save_manual_expense,
    set_expense_status,
)


class HistoricalShiftChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, shift):
        opened = business_localtime(
            shift.business, value=shift.opened_at
        ).strftime("%Y-%m-%d %H:%M")
        return (
            f"{opened} — {shift.register.name} — "
            f"{shift.cashier.full_name} — {shift.get_status_display()}"
        )


class ExpenseForm(TenantStyledModelForm):
    paid_from_drawer = forms.BooleanField(
        required=False,
        label="Paid from current cash drawer",
    )
    historical_shift = HistoricalShiftChoiceField(
        queryset=register_services.Shift.objects.none(),
        required=False,
        label="Register shift / drawer",
        empty_label="Cash – Non-register (no shift)",
    )

    class Meta:
        model = Expense
        fields = ["expense_date", "branch", "category", "payee", "supplier",
                  "amount", "tax_amount", "payment_method", "reference",
                  "description", "attachment"]
        widgets = {
            "expense_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(
        self,
        business,
        *args,
        membership=None,
        user=None,
        correction_only=False,
        **kwargs,
    ):
        super().__init__(business, *args, **kwargs)
        from apps.registers.models import Shift
        from apps.sales.models import PaymentMethod
        from apps.suppliers.models import Supplier

        branches = Branch.objects.for_business(business).filter(
            Q(is_active=True)
            | Q(pk=self.instance.branch_id if self.instance.pk else None)
        )
        if membership is not None and membership.allowed_branch_ids is not None:
            branches = branches.filter(id__in=membership.allowed_branch_ids)
        self.fields["branch"].queryset = branches
        branch_choices = list(branches[:2])
        if not self.instance.pk and len(branch_choices) == 1:
            self.initial["branch"] = branch_choices[0].pk
            self.fields["branch"].disabled = True
        elif self.instance.pk and len(branch_choices) == 1:
            self.fields["branch"].disabled = True
        self.fields["category"].queryset = (
            ExpenseCategory.objects.for_business(business)
            .filter(
                Q(is_active=True)
                | Q(pk=self.instance.category_id if self.instance.pk else None)
            )
        )
        self.fields["supplier"].queryset = (
            Supplier.objects.for_business(business)
            .filter(
                Q(is_active=True)
                | Q(pk=self.instance.supplier_id if self.instance.pk else None)
            )
        )
        self.fields["supplier"].required = False
        current_method_id = (
            self.instance.payment_method_id if self.instance.pk else None
        )
        self.fields["payment_method"].queryset = (
            PaymentMethod.objects.for_business(business).filter(
                Q(pk=current_method_id)
                | (
                    Q(is_active=True)
                    & ~Q(kind__in=["customer_credit", "store_credit"])
                )
            )
        )
        self.fields["payment_method"].label = "Payment Medium / Paid Via"
        self.fields["payment_method"].required = not self.instance.pk

        branch_id = None
        if self.is_bound:
            raw_branch = self.data.get(self.add_prefix("branch"), "")
            if str(raw_branch).isdigit():
                branch_id = int(raw_branch)
            elif self.instance.pk:
                branch_id = self.instance.branch_id
            else:
                initial_branch = self.initial.get("branch")
                branch_id = getattr(initial_branch, "pk", initial_branch)
        elif self.instance.pk:
            branch_id = self.instance.branch_id
        else:
            initial_branch = self.initial.get("branch")
            branch_id = getattr(initial_branch, "pk", initial_branch)

        branch = branches.filter(pk=branch_id).first() if branch_id else None
        current_shift = None
        if user is not None:
            current_shift = matching_open_drawer_shift(
                business=business,
                user=user,
                branch=branch,
                membership=membership,
            )
            if not self.instance.pk and branch is None:
                from apps.registers.services import get_open_shift

                open_shift = get_open_shift(
                    business, user, membership=membership
                )
                if open_shift and branches.filter(pk=open_shift.branch_id).exists():
                    self.initial["branch"] = open_shift.branch_id
                    branch = open_shift.branch
                    current_shift = open_shift
        self.current_drawer_shift = current_shift

        if correction_only:
            self.fields.pop("paid_from_drawer")
            for name, field in self.fields.items():
                if name not in {"payment_method", "historical_shift"}:
                    field.disabled = True
            candidates = Shift.objects.none()
            if self.instance.pk and self.instance.branch_id:
                start = self.instance.expense_date - timedelta(days=2)
                end = self.instance.expense_date + timedelta(days=2)
                opened_after, opened_before = business_date_bounds(
                    business,
                    start,
                    end,
                )
                candidates = (
                    Shift.objects.for_business(business)
                    .filter(branch_id=self.instance.branch_id)
                    .filter(
                        Q(
                            opened_at__gte=opened_after,
                            opened_at__lt=opened_before,
                        )
                        | Q(pk=self.instance.shift_id)
                    )
                    .select_related("register", "cashier", "branch")
                    .order_by("-opened_at", "-pk")
                )
            self.fields["historical_shift"].queryset = candidates
            self.fields["historical_shift"].initial = self.instance.shift_id
            self.fields["historical_shift"].help_text = (
                "Choose only the documented shift that funded this cash expense. "
                "Leaving this empty records cash as non-register cash."
            )
        else:
            self.fields.pop("historical_shift")
            self.fields["paid_from_drawer"].initial = bool(
                self.instance.pk
                and current_shift
                and self.instance.shift_id == current_shift.pk
            )
            if current_shift:
                self.fields["paid_from_drawer"].help_text = (
                    f"Current drawer: {current_shift.register.name} — "
                    f"{current_shift.cashier.full_name}. Applies only when "
                    "Payment Medium is Cash."
                )
            else:
                self.fields["paid_from_drawer"].disabled = True
                self.fields["paid_from_drawer"].help_text = (
                    "No matching open drawer exists for this branch. A cash "
                    "expense will be recorded as Cash – Non-register."
                )

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Amount must be positive.")
        return amount

    def _post_clean(self):
        # The service owns drawer-link validation and stale-link clearing.
        # Avoid validating the persisted link against a newly selected
        # non-cash method or branch before the service can authorize the
        # closed-shift reconciliation change.
        payment_method = self.cleaned_data.get("payment_method")
        branch = self.cleaned_data.get("branch")
        payment_changed_away_from_cash = (
            "payment_method" in self.cleaned_data
            and (
                payment_method is None
                or payment_method.kind != "cash"
            )
        )
        if self.instance.shift_id and (
            payment_changed_away_from_cash
            or (branch and branch.pk != self.instance.shift.branch_id)
        ):
            self.instance.shift = None
        super()._post_clean()


class ExpenseCategoryForm(TenantStyledModelForm):
    duplicate_name_error = "An expense category with this name already exists."

    class Meta:
        model = ExpenseCategory
        fields = ["name", "parent", "is_active"]

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        self.fields["parent"].queryset = ExpenseCategory.objects.for_business(
            business).filter(parent__isnull=True)
        self.fields["parent"].required = False

    def _duplicate_name_exists(self, name, parent):
        categories = ExpenseCategory.objects.for_business(self.business).filter(
            name__iexact=name,
            parent=parent,
        )
        if self.instance.pk:
            categories = categories.exclude(pk=self.instance.pk)
        return categories.exists()

    def clean(self):
        cleaned_data = super().clean()
        name = (cleaned_data.get("name") or "").strip()
        if name:
            cleaned_data["name"] = name
        if name and "parent" in cleaned_data and self._duplicate_name_exists(
            name,
            cleaned_data["parent"],
        ):
            self.add_error("name", self.duplicate_name_error)
        return cleaned_data


class RecurringExpenseTemplateForm(TenantStyledModelForm):
    class Meta:
        model = RecurringExpenseTemplate
        fields = [
            "name", "branch", "category", "default_amount", "due_day",
            "start_date", "end_date", "notes", "is_active",
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

    def __init__(self, business, *args, membership=None, **kwargs):
        super().__init__(business, *args, **kwargs)
        branches = Branch.objects.for_business(business).filter(is_active=True)
        if membership is not None and membership.allowed_branch_ids is not None:
            branches = branches.filter(id__in=membership.allowed_branch_ids)
        if self.instance.pk and self.instance.branch_id:
            branches = Branch.objects.for_business(business).filter(
                Q(is_active=True) | Q(pk=self.instance.branch_id)
            )
            if membership is not None and membership.allowed_branch_ids is not None:
                branches = branches.filter(id__in=membership.allowed_branch_ids)
        branches = branches.order_by("name")
        self.fields["branch"].queryset = branches
        self.fields["branch"].required = True
        branch_choices = list(branches[:2])
        if not self.instance.pk and len(branch_choices) == 1:
            self.initial["branch"] = branch_choices[0].pk
            self.fields["branch"].disabled = True
        elif self.instance.pk and len(branch_choices) == 1:
            self.fields["branch"].disabled = True
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


def _expenses_for_request(request, queryset=None):
    queryset = queryset if queryset is not None else Expense.objects.all()
    queryset = queryset.for_business(request.business)
    allowed_branch_ids = request.membership.allowed_branch_ids
    if allowed_branch_ids is not None:
        queryset = queryset.filter(branch_id__in=allowed_branch_ids)
    return queryset


def _branches_for_request(request):
    queryset = Branch.objects.for_business(request.business).filter(is_active=True)
    allowed_branch_ids = request.membership.allowed_branch_ids
    if allowed_branch_ids is not None:
        queryset = queryset.filter(id__in=allowed_branch_ids)
    return queryset.order_by("name")


def _recurring_templates_for_request(request, queryset=None):
    queryset = queryset if queryset is not None else RecurringExpenseTemplate.objects.all()
    queryset = queryset.for_business(request.business)
    allowed_branch_ids = request.membership.allowed_branch_ids
    if allowed_branch_ids is not None:
        queryset = queryset.filter(branch_id__in=allowed_branch_ids)
    return queryset


def _add_service_errors(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field
            if field == "shift" and "historical_shift" in form.fields:
                target = "historical_shift"
            if target not in form.fields:
                target = None
            for error in errors:
                form.add_error(target, error)
        return
    for error in getattr(exc, "messages", [str(exc)]):
        form.add_error(None, error)


@module_permission_required(
    "expenses", "expenses.view", action=AccessAction.READ
)
def expense_list(request):
    qs = (
        _expenses_for_request(request)
        .filter(recurring_template__isnull=True)
        .select_related(
            "category", "branch", "created_by", "payment_method", "shift",
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
    branch_id = request.GET.get("branch", "")
    if branch_id.isdigit():
        qs = qs.filter(branch_id=branch_id)
    payment_medium = request.GET.get("method", "").strip().lower()
    valid_payment_media = {value for value, _label in Expense.PAYMENT_MEDIUM_CHOICES}
    if payment_medium in valid_payment_media:
        qs = filter_by_payment_medium(qs, payment_medium)
    else:
        payment_medium = ""
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
        _recurring_templates_for_request(request)
        .select_related("category", "branch")
        .annotate(generated_count=Count("generated_expenses"))
        .order_by("name", "id")
    )
    if branch_id.isdigit():
        fixed_templates = fixed_templates.filter(branch_id=branch_id)
    querystring = date_range_querystring(request.GET, date_from, date_to)
    return render(request, "expenses/list.html", {
        "page_obj": page_obj, "q": q, "total": total, "categories": categories,
        "statuses": Expense.Status.choices, "active_nav": "expenses",
        "date_from": date_from, "date_to": date_to,
        "querystring": f"{querystring}&" if querystring else "",
        "can_approve": request.membership.has_perm("expenses.approve"),
        "can_manage": request.membership.has_perm("expenses.manage"),
        "fixed_templates": fixed_templates,
        "branches": _branches_for_request(request),
        "payment_medium": payment_medium,
        "payment_medium_choices": Expense.PAYMENT_MEDIUM_CHOICES,
        "can_correct_history": (
            request.membership.has_perm("expenses.approve")
            and request.membership.has_perm("shifts.approve")
        ),
    })


@module_permission_required(
    "expenses", "expenses.manage", action=AccessAction.WRITE
)
def expense_create(request, public_id=None):
    instance = None
    correction_only = False
    if public_id:
        instance = get_tenant_object(
            _expenses_for_request(request),
            request.business,
            public_id=public_id,
        )
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
            can_correct_history = (
                request.membership.has_perm("expenses.approve")
                and request.membership.has_perm("shifts.approve")
            )
            if not can_correct_history:
                messages.error(request, "Approved or paid expenses cannot be edited.")
                return redirect("expenses:list")
            correction_only = True
    form = ExpenseForm(
        request.business,
        request.POST or None,
        request.FILES or None,
        instance=instance,
        membership=request.membership,
        user=request.user,
        correction_only=correction_only,
    )
    if request.method == "POST" and form.is_valid():
        expense = form.save(commit=False)
        expense.business = request.business
        if instance is None:
            expense.expense_number = next_expense_number(request.business)
            expense.created_by = request.user
        if correction_only:
            needs_approval = expense.status == Expense.Status.SUBMITTED
            requested_shift = form.cleaned_data.get("historical_shift")
            drawer_requested = requested_shift is not None
        else:
            threshold = request.business.settings.expense_approval_threshold
            needs_approval = (
                threshold > 0 and expense.amount >= threshold
                and not request.membership.has_perm("expenses.approve")
            )
            expense.status = (Expense.Status.SUBMITTED if needs_approval
                              else Expense.Status.APPROVED)
            if not needs_approval:
                expense.approved_by = request.user
            drawer_requested = form.cleaned_data.get("paid_from_drawer", False)
            requested_shift = (
                matching_open_drawer_shift(
                    business=request.business,
                    user=request.user,
                    branch=expense.branch,
                    membership=request.membership,
                )
                if drawer_requested else None
            )
            if (
                not drawer_requested
                and instance is not None
                and instance.shift_id
                and instance.shift.status in ("closed", "approved")
                and expense.payment_method_id
                and expense.payment_method.kind == "cash"
                and expense.branch_id == instance.shift.branch_id
            ):
                # An ordinary edit does not expose historical shift choices.
                # Retain the exact closed drawer unless another submitted
                # financial field explicitly makes that link invalid.
                requested_shift = instance.shift
                drawer_requested = True
        try:
            save_manual_expense(
                expense=expense,
                business=request.business,
                user=request.user,
                membership=request.membership,
                requested_shift=requested_shift,
                drawer_requested=drawer_requested,
                historical_correction=correction_only,
                request=request,
            )
        except ValidationError as exc:
            _add_service_errors(form, exc)
        else:
            if needs_approval:
                from apps.notifications.services import notify_role

                notify_role(request.business, "expenses.approve",
                            f"Expense {expense.expense_number} needs approval "
                            f"({expense.amount})",
                            severity="warning", category="expense_pending",
                            link="/expenses/")
                messages.info(request, "Expense submitted for approval.")
            elif correction_only:
                messages.success(
                    request,
                    "Expense payment medium and register link corrected.",
                )
            else:
                messages.success(request, "Expense recorded.")
            audit.log("expense.saved", request=request, module="expenses", obj=expense,
                      description=f"Expense {expense.expense_number} "
                                  f"({expense.amount}) saved.")
            return redirect("expenses:list")
    return render(request, "expenses/form.html",
                  {
                      "form": form,
                      "expense": instance,
                      "active_nav": "expenses",
                      "correction_only": correction_only,
                      "current_drawer_shift": form.current_drawer_shift,
                  })


@require_POST
@module_permission_required(
    "expenses", "expenses.approve", action=AccessAction.WRITE
)
def expense_action(request, public_id, action):
    expense = get_tenant_object(
        _expenses_for_request(request), request.business, public_id=public_id
    )
    if action == "approve" and expense.status == Expense.Status.SUBMITTED:
        expense = set_expense_status(
            expense=expense,
            status=Expense.Status.APPROVED,
            approved_by=request.user,
            user=request.user,
            membership=request.membership,
            request=request,
        )
        messages.success(request, "Expense approved.")
    elif action == "reject" and expense.status == Expense.Status.SUBMITTED:
        expense = set_expense_status(
            expense=expense,
            status=Expense.Status.REJECTED,
            approved_by=request.user,
            user=request.user,
            membership=request.membership,
            request=request,
        )
        messages.success(request, "Expense rejected.")
    elif action == "cancel" and expense.status in (
        Expense.Status.DRAFT,
        Expense.Status.SUBMITTED,
        Expense.Status.APPROVED,
    ):
        expense = set_expense_status(
            expense=expense,
            status=Expense.Status.CANCELLED,
            user=request.user,
            membership=request.membership,
            request=request,
        )
        messages.success(request, "Expense cancelled.")
    audit.log(f"expense.{action}", request=request, module="expenses",
              obj=expense,
              description=f"Expense {expense.expense_number} {action}d.")
    return redirect("expenses:list")


def _fixed_expenses_url():
    return f"{reverse('expenses:list')}#fixed-expenses"


def _fixed_expenses_redirect():
    return redirect(_fixed_expenses_url())


@module_permission_required(
    "expenses", "expenses.view", action=AccessAction.READ
)
def recurring_template_list(request):
    return _fixed_expenses_redirect()


@module_permission_required(
    "expenses", "expenses.manage", action=AccessAction.WRITE
)
def recurring_template_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(
            _recurring_templates_for_request(request),
            request.business,
            public_id=public_id,
        )
    form = RecurringExpenseTemplateForm(
        request.business,
        request.POST or None,
        instance=instance,
        membership=request.membership,
    )
    if request.method == "POST" and form.is_valid():
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


@require_POST
@module_permission_required(
    "expenses", "expenses.manage", action=AccessAction.WRITE
)
def recurring_template_action(request, public_id, action):
    template = get_tenant_object(
        _recurring_templates_for_request(request),
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


@module_permission_required(
    "expenses", "expenses.manage", action=AccessAction.WRITE
)
def recurring_template_delete(request, public_id):
    template = get_tenant_object(
        _recurring_templates_for_request(request),
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
                _recurring_templates_for_request(
                    request,
                    RecurringExpenseTemplate.objects.select_for_update(),
                ),
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


@module_permission_required(
    "expenses", "expenses.manage", action=AccessAction.WRITE
)
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
        try:
            with transaction.atomic():
                obj.save()
        except IntegrityError:
            if obj.parent_id is None:
                raise
            conflict = (
                ExpenseCategory.objects.for_business(request.business)
                .filter(name=obj.name, parent=obj.parent)
                .exclude(pk=obj.pk)
                .exists()
            )
            if not conflict:
                raise
            form.add_error("name", form.duplicate_name_error)
        else:
            messages.success(request, "Expense category saved.")
            return redirect("expenses:categories")
    items = ExpenseCategory.objects.for_business(request.business)
    return render(request, "expenses/categories.html",
                  {"form": form, "items": items, "editing": instance,
                   "active_nav": "expenses"})
