"""Tenant-scoped WMS employees, production categories, and assignments."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from apps.wms_core.models import ValidatedTenantModel, WmsLocation


def _normalized_spaces(value):
    return " ".join((value or "").split())


class WmsEmployee(ValidatedTenantModel):
    class CompensationType(models.TextChoices):
        FIXED_SALARY = "fixed_salary", "Fixed Salary"
        PER_PIECE = "per_piece", "Per Piece"

    location = models.ForeignKey(
        WmsLocation,
        on_delete=models.PROTECT,
        related_name="employees",
    )
    employee_code = models.CharField(max_length=40)
    full_name = models.CharField(max_length=160)
    mobile = models.CharField(max_length=30, blank=True)
    joining_date = models.DateField()
    compensation_type = models.CharField(
        max_length=20,
        choices=CompensationType.choices,
    )
    fixed_monthly_salary = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
    )
    default_per_piece_rate = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_employees_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_employees_updated",
    )

    class Meta:
        ordering = ["full_name", "employee_code"]
        constraints = [
            models.UniqueConstraint(
                Lower("employee_code"),
                "business",
                name="uniq_wms_employee_code_ci_business",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    compensation_type__in=("fixed_salary", "per_piece")
                ),
                name="valid_wms_employee_comp_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(fixed_monthly_salary__isnull=True)
                    | models.Q(fixed_monthly_salary__gte=0)
                ),
                name="nonnegative_wms_fixed_salary",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(default_per_piece_rate__isnull=True)
                    | models.Q(default_per_piece_rate__gte=0)
                ),
                name="nonnegative_wms_piece_rate",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        compensation_type="fixed_salary",
                        fixed_monthly_salary__isnull=False,
                        default_per_piece_rate__isnull=True,
                    )
                    | models.Q(
                        compensation_type="per_piece",
                        fixed_monthly_salary__isnull=True,
                        default_per_piece_rate__isnull=False,
                    )
                ),
                name="valid_wms_employee_comp_values",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "is_active", "full_name"],
                name="wms_emp_active_name_idx",
            ),
            models.Index(
                fields=["business", "location", "is_active"],
                name="wms_emp_business_location_idx",
            ),
        ]

    def clean(self):
        super().clean()
        self.employee_code = _normalized_spaces(self.employee_code).upper()
        self.full_name = _normalized_spaces(self.full_name)
        self.mobile = (self.mobile or "").strip()
        self.notes = (self.notes or "").strip()

        if not self.employee_code:
            raise ValidationError({"employee_code": "Employee code is required."})
        if not self.full_name:
            raise ValidationError({"full_name": "Full name is required."})

        if self.compensation_type == self.CompensationType.FIXED_SALARY:
            if self.fixed_monthly_salary is None:
                raise ValidationError(
                    {"fixed_monthly_salary": "Fixed monthly salary is required."}
                )
            if self.default_per_piece_rate is not None:
                raise ValidationError(
                    {
                        "default_per_piece_rate": (
                            "Leave the per-piece rate blank for Fixed Salary."
                        )
                    }
                )
        elif self.compensation_type == self.CompensationType.PER_PIECE:
            if self.default_per_piece_rate is None:
                raise ValidationError(
                    {
                        "default_per_piece_rate": (
                            "Default per-piece rate is required."
                        )
                    }
                )
            if self.fixed_monthly_salary is not None:
                raise ValidationError(
                    {
                        "fixed_monthly_salary": (
                            "Leave fixed salary blank for Per Piece."
                        )
                    }
                )

        for field_name in ("fixed_monthly_salary", "default_per_piece_rate"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValidationError(
                    {field_name: "Compensation values cannot be negative."}
                )

        if not self.location_id or not self.business_id:
            return
        if self.location.business_id != self.business_id:
            raise ValidationError(
                {"location": "The WMS location must belong to the same business."}
            )

        original = None
        if self.pk:
            original = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("location_id", "is_active")
                .first()
            )
        location_changed = original is None or original["location_id"] != self.location_id
        reactivating = (
            original is not None and not original["is_active"] and self.is_active
        )
        if (
            (location_changed or reactivating)
            and (
                not self.location.is_active
                or not self.location.branch.is_active
            )
        ):
            raise ValidationError(
                {"location": "Select an active WMS location."}
            )

    def __str__(self):
        return f"{self.employee_code} — {self.full_name}"


class WmsProductionCategory(ValidatedTenantModel):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=40, blank=True)
    description = models.TextField(blank=True)
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_categories_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_categories_updated",
    )

    class Meta:
        ordering = ["display_order", "name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "business",
                name="uniq_wms_category_name_ci_business",
            ),
            models.UniqueConstraint(
                Lower("code"),
                "business",
                condition=~models.Q(code=""),
                name="uniq_wms_category_code_ci_business",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "is_active", "display_order", "name"],
                name="wms_cat_active_order_idx",
            ),
        ]

    def clean(self):
        super().clean()
        self.name = _normalized_spaces(self.name)
        self.code = _normalized_spaces(self.code).upper()
        self.description = (self.description or "").strip()
        if not self.name:
            raise ValidationError({"name": "Category name is required."})

    def __str__(self):
        return self.name


class WmsEmployeeCategoryAssignment(ValidatedTenantModel):
    employee = models.ForeignKey(
        WmsEmployee,
        on_delete=models.PROTECT,
        related_name="category_assignments",
    )
    category = models.ForeignKey(
        WmsProductionCategory,
        on_delete=models.PROTECT,
        related_name="employee_assignments",
    )
    per_piece_rate = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="Optional override; blank uses the employee default rate.",
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_assignments_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_assignments_updated",
    )

    class Meta:
        ordering = ["category__display_order", "category__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "employee", "category"],
                name="uniq_wms_employee_category_business",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(per_piece_rate__isnull=True)
                    | models.Q(per_piece_rate__gte=0)
                ),
                name="nonnegative_wms_assignment_rate",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "employee", "is_active"],
                name="wms_assign_emp_active_idx",
            ),
            models.Index(
                fields=["business", "category", "is_active"],
                name="wms_assign_cat_active_idx",
            ),
        ]

    def clean(self):
        super().clean()
        if self.per_piece_rate is not None and self.per_piece_rate < 0:
            raise ValidationError(
                {"per_piece_rate": "Per-piece rate cannot be negative."}
            )
        if not self.business_id:
            return
        if self.employee_id and self.employee.business_id != self.business_id:
            raise ValidationError(
                {"employee": "The employee must belong to the same business."}
            )
        if self.category_id and self.category.business_id != self.business_id:
            raise ValidationError(
                {"category": "The category must belong to the same business."}
            )
        if (
            self.per_piece_rate is not None
            and self.employee_id
            and self.employee.compensation_type
            != WmsEmployee.CompensationType.PER_PIECE
        ):
            raise ValidationError(
                {
                    "per_piece_rate": (
                        "Category rates are available only for Per Piece employees."
                    )
                }
            )
        if self.is_active and self.employee_id and self.category_id:
            if not self.employee.is_active:
                raise ValidationError(
                    {"employee": "Inactive employees cannot receive assignments."}
                )
            if not self.category.is_active:
                raise ValidationError(
                    {"category": "Inactive categories cannot be assigned."}
                )
            if (
                not self.employee.location.is_active
                or not self.employee.location.branch.is_active
            ):
                raise ValidationError(
                    {"employee": "The employee must have an active WMS location."}
                )

    @property
    def effective_per_piece_rate(self):
        if self.per_piece_rate is not None:
            return self.per_piece_rate
        return self.employee.default_per_piece_rate

    def __str__(self):
        return f"{self.employee} → {self.category}"
