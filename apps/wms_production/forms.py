from collections import Counter

from django import forms
from django.forms import BaseFormSet, formset_factory

from apps.core.date_ranges import business_localdate
from apps.wms_core.models import WmsLocation
from apps.wms_orders.models import WmsWorkshopOrder
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
)

from . import selectors
from .models import WmsProductionEntry

DUPLICATE_ROW_MESSAGE = (
    "This Workshop Order and operation already exist in this production "
    "record. Update the existing row instead."
)


class ProductionStyledModelForm(forms.ModelForm):
    def __init__(self, business, *args, **kwargs):
        self.business = business
        super().__init__(*args, **kwargs)
        self.instance.business = business
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")


class ProductionEntryForm(ProductionStyledModelForm):
    location = forms.ModelChoiceField(
        queryset=WmsLocation.objects.none(),
        to_field_name="public_id",
    )
    employee = forms.ModelChoiceField(
        queryset=WmsEmployee.objects.none(),
        to_field_name="public_id",
    )

    class Meta:
        model = WmsProductionEntry
        fields = [
            "production_date",
            "location",
            "employee",
            "daily_total_pieces",
            "notes",
        ]
        widgets = {
            "production_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "production_date": "Production date",
            "daily_total_pieces": "Daily Total Pieces",
        }
        help_texts = {
            "daily_total_pieces": (
                "Completed pieces for the day. This remains independent from "
                "the operation-row quantities."
            ),
        }

    def __init__(
        self,
        business,
        user_access,
        *args,
        selected_employee=None,
        **kwargs,
    ):
        self.user_access = user_access
        super().__init__(business, *args, **kwargs)
        self.fields["location"].queryset = (
            selectors.active_production_locations_for_access(user_access)
        )
        self.fields["employee"].queryset = (
            selectors.active_employees_for_production(user_access)
        )
        self.selected_employee = self._resolve_employee(selected_employee)
        if self.selected_employee is not None:
            self.initial.setdefault("employee", self.selected_employee)
            self.initial.setdefault("location", self.selected_employee.location)
            self.fields["employee"].widget = forms.HiddenInput()
            self.fields["location"].widget = forms.HiddenInput()
        if not self.is_bound:
            self.initial.setdefault(
                "production_date",
                business_localdate(business),
            )

    def _resolve_employee(self, selected_employee):
        if selected_employee is not None:
            return self.fields["employee"].queryset.filter(
                pk=selected_employee.pk
            ).first()
        if not self.is_bound:
            return None
        raw_employee = self.data.get("employee", "")
        return self.fields["employee"].queryset.filter(
            public_id=raw_employee
        ).first()

    def clean(self):
        cleaned_data = super().clean()
        self.existing_entry = None
        employee = cleaned_data.get("employee")
        location = cleaned_data.get("location")
        production_date = cleaned_data.get("production_date")
        if employee is not None and location is not None:
            if employee.location_id != location.pk:
                self.add_error(
                    "location",
                    "Production location must match the employee's WMS location.",
                )
            if (
                not employee.is_active
                or not employee.location.is_active
                or not employee.location.branch.is_active
            ):
                self.add_error(
                    "employee",
                    "Select an active employee at an active WMS location.",
                )
        if employee is not None and production_date is not None:
            self.existing_entry = (
                selectors.production_entries_for_access(self.user_access)
                .filter(
                    employee=employee,
                    production_date=production_date,
                )
                .first()
            )
        if employee is not None and not selectors.active_assignments_for_employee(
            employee
        ).exists():
            self.add_error(
                "employee",
                "Assign at least one active production category first.",
            )
        return cleaned_data


class OrderChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, order):
        return (
            f"{order.order_reference} — {order.eligible_piece_count} PCS — "
            f"{order.get_status_display()}"
        )


class AssignmentChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, assignment):
        code = f" ({assignment.category.code})" if assignment.category.code else ""
        return f"{assignment.category.name}{code}"


class ProductionLineForm(forms.Form):
    order = OrderChoiceField(
        queryset=WmsWorkshopOrder.objects.none(),
        to_field_name="public_id",
        empty_label="Select Workshop Order",
    )
    assignment = AssignmentChoiceField(
        queryset=WmsEmployeeCategoryAssignment.objects.none(),
        to_field_name="public_id",
        label="Operation",
        empty_label="Select operation",
    )
    quantity = forms.IntegerField(
        label="PCS",
        min_value=1,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "min": "1", "step": "1"}
        ),
    )

    def __init__(self, *args, business, user_access, employee, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.user_access = user_access
        self.employee = employee
        location = employee.location if employee is not None else None
        self.fields["order"].queryset = selectors.eligible_orders_for_production(
            user_access,
            location,
        )
        self.fields["assignment"].queryset = (
            selectors.active_assignments_for_employee(employee)
            if employee is not None
            else WmsEmployeeCategoryAssignment.objects.none()
        )
        self.fields["order"].widget.attrs.update(
            {"class": "form-select", "data-order-select": ""}
        )
        self.fields["assignment"].widget.attrs.setdefault("class", "form-select")

    def clean(self):
        cleaned = super().clean()
        order = cleaned.get("order")
        assignment = cleaned.get("assignment")
        if order is not None and self.employee is not None:
            if order.location_id != self.employee.location_id:
                self.add_error(
                    "order",
                    "Workshop Order location does not match the employee.",
                )
        if assignment is not None and self.employee is not None:
            if assignment.employee_id != self.employee.pk:
                self.add_error(
                    "assignment",
                    "Operation is not assigned to this employee.",
                )
        return cleaned


class BaseProductionLineFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        combinations = Counter()
        for form in self.forms:
            if not form.cleaned_data:
                continue
            order = form.cleaned_data.get("order")
            assignment = form.cleaned_data.get("assignment")
            if order is None or assignment is None:
                continue
            combinations[(order.pk, assignment.category_id)] += 1
        if any(count > 1 for count in combinations.values()):
            raise forms.ValidationError(DUPLICATE_ROW_MESSAGE)


ProductionLineFormSet = formset_factory(
    ProductionLineForm,
    formset=BaseProductionLineFormSet,
    extra=1,
    min_num=1,
    validate_min=True,
)


class ProductionCorrectionForm(ProductionStyledModelForm):
    class Meta:
        model = WmsProductionEntry
        fields = ["daily_total_pieces", "notes", "correction_reason"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "correction_reason": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "daily_total_pieces": "Daily Total Pieces",
            "correction_reason": "Correction reason",
        }
        help_texts = {
            "daily_total_pieces": (
                "Completed pieces for the day. This is independent from "
                "operation quantities."
            ),
        }

    def __init__(self, business, *args, **kwargs):
        super().__init__(business, *args, **kwargs)
        self.fields["correction_reason"].required = True
        self.fields["correction_reason"].help_text = (
            "Required. Explain why the saved production is being changed."
        )

    def clean_correction_reason(self):
        reason = (self.cleaned_data.get("correction_reason") or "").strip()
        if not reason:
            raise forms.ValidationError("Enter a correction reason.")
        return reason



class ProductionCorrectionLineForm(ProductionLineForm):
    line_id = forms.UUIDField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, existing_lines, **kwargs):
        self.existing_lines = existing_lines
        super().__init__(*args, **kwargs)
        raw_line_id = self.initial.get("line_id")
        if self.is_bound:
            raw_line_id = self.data.get(self.add_prefix("line_id"), raw_line_id)
        self.existing_line = existing_lines.get(str(raw_line_id))

        if self.existing_line is not None:
            if self.existing_line.order_id:
                historical_order = WmsWorkshopOrder.objects.for_business(
                    self.business
                ).filter(pk=self.existing_line.order_id)
                self.fields["order"].queryset = (
                    self.fields["order"].queryset | historical_order
                ).distinct().order_by("order_reference")
            else:
                self.fields["order"].required = False
                self.fields["order"].empty_label = "Legacy / Not recorded"

            historical_assignment = (
                WmsEmployeeCategoryAssignment.objects.for_business(self.business)
                .filter(pk=self.existing_line.assignment_id)
                .select_related("category")
            )
            self.fields["assignment"].queryset = (
                self.fields["assignment"].queryset | historical_assignment
            ).distinct().order_by("category__display_order", "category__name")


class BaseProductionCorrectionLineFormSet(BaseFormSet):
    deletion_widget = forms.HiddenInput

    def __init__(self, *args, existing_lines, **kwargs):
        self.existing_lines = {
            str(line.public_id): line for line in existing_lines
        }
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["existing_lines"] = self.existing_lines
        return kwargs

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        submitted_line_ids = set()
        proposed_combinations = Counter()
        proposed_forms = []
        for form in self.forms:
            if not form.cleaned_data or self._should_delete_form(form):
                continue
            line_id = form.cleaned_data.get("line_id")
            if line_id is not None:
                line_key = str(line_id)
                if line_key not in self.existing_lines:
                    raise forms.ValidationError(
                        "A submitted production row is unavailable. Refresh and try again."
                    )
                if line_key in submitted_line_ids:
                    raise forms.ValidationError(
                        "Submit each saved production row at most once."
                    )
                submitted_line_ids.add(line_key)

            order = form.cleaned_data.get("order")
            assignment = form.cleaned_data.get("assignment")
            if order is not None and assignment is not None:
                combination = (order.pk, assignment.category_id)
                proposed_combinations[combination] += 1
                proposed_forms.append((form, combination))

        for form, combination in proposed_forms:
            original = form.existing_line
            original_combination = (
                (original.order_id, original.category_id)
                if original is not None and original.order_id
                else None
            )
            if (
                proposed_combinations[combination] > 1
                and original_combination != combination
            ):
                raise forms.ValidationError(DUPLICATE_ROW_MESSAGE)

    def production_rows(self):
        rows = []
        for form in self.forms:
            if not form.cleaned_data or self._should_delete_form(form):
                continue
            line_id = form.cleaned_data.get("line_id")
            rows.append(
                {
                    "line_id": str(line_id) if line_id is not None else None,
                    "order": form.cleaned_data.get("order"),
                    "assignment": form.cleaned_data["assignment"],
                    "quantity": form.cleaned_data["quantity"],
                }
            )
        return rows


ProductionCorrectionLineFormSet = formset_factory(
    ProductionCorrectionLineForm,
    formset=BaseProductionCorrectionLineFormSet,
    extra=0,
    can_delete=True,
)
