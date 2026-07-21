from django import forms
from django.db.models import Q

from apps.branches.forms import TenantStyledModelForm
from apps.branches.models import Branch

from .models import CashRegister, Shift


class RegisterForm(TenantStyledModelForm):
    class Meta:
        model = CashRegister
        fields = ["name", "code", "branch", "receipt_printer"]

    def __init__(self, business, *args, membership=None, **kwargs):
        super().__init__(business, *args, **kwargs)
        branches = Branch.objects.for_business(business).filter(
            is_active=True,
            usage_type=Branch.UsageType.SALES_BRANCH,
        )
        if self.instance.pk and self.instance.branch_id:
            branches = Branch.objects.for_business(business).filter(
                Q(
                    is_active=True,
                    usage_type=Branch.UsageType.SALES_BRANCH,
                )
                | Q(pk=self.instance.branch_id)
            )
        if membership and membership.allowed_branch_ids is not None:
            branches = branches.filter(pk__in=membership.allowed_branch_ids)
        self.fields["branch"].queryset = branches.order_by("name")

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Name is required.")
        return name

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        if not code:
            raise forms.ValidationError("Code is required.")
        registers = CashRegister.objects.for_business(self.business).filter(
            code__iexact=code
        )
        if self.instance.pk:
            registers = registers.exclude(pk=self.instance.pk)
        if registers.exists():
            raise forms.ValidationError("This register code is already in use.")
        return code

    def clean_branch(self):
        branch = self.cleaned_data["branch"]
        if (
            branch.business_id != self.business.id
            or branch.usage_type != Branch.UsageType.SALES_BRANCH
        ):
            raise forms.ValidationError("Select a valid branch.")
        return branch

    def clean(self):
        cleaned_data = super().clean()
        branch = cleaned_data.get("branch")
        if (
            self.instance.pk
            and branch
            and branch.pk != self.instance.branch_id
            and Shift.objects.for_business(self.business).filter(
                register=self.instance,
                status=Shift.Status.OPEN,
            ).exists()
        ):
            self.add_error(
                "branch",
                "Close the register's open shift before changing its branch.",
            )
        return cleaned_data
