"""Transactional WMS workforce mutation services with audit coverage."""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit import services as audit

from .models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
    WmsProductionCategory,
)


def _actor(user=None, request=None):
    return user or getattr(request, "user", None)


def _compensation_state(employee):
    return {
        "compensation_type": employee.compensation_type,
        "fixed_monthly_salary": (
            str(employee.fixed_monthly_salary)
            if employee.fixed_monthly_salary is not None
            else None
        ),
        "default_per_piece_rate": (
            str(employee.default_per_piece_rate)
            if employee.default_per_piece_rate is not None
            else None
        ),
    }


def _safe_compensation_audit_state(employee):
    return {
        "compensation_type": employee.compensation_type,
        "fixed_monthly_salary_configured": (
            employee.fixed_monthly_salary is not None
        ),
        "default_per_piece_rate_configured": (
            employee.default_per_piece_rate is not None
        ),
    }


@transaction.atomic
def save_employee(
    *,
    business,
    cleaned_data,
    instance=None,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    creating = instance is None
    employee = (
        WmsEmployee(business=business)
        if creating
        else WmsEmployee.objects.for_business(business)
        .select_for_update()
        .get(pk=instance.pk)
    )
    before_compensation = None if creating else _compensation_state(employee)
    before_audit_state = (
        None if creating else _safe_compensation_audit_state(employee)
    )
    for field, value in cleaned_data.items():
        setattr(employee, field, value)
    if creating:
        employee.created_by = actor
    employee.updated_by = actor
    employee.save()

    audit.log(
        "wms.employee_created" if creating else "wms.employee_updated",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=employee,
        description=f"WMS employee '{employee.employee_code}' saved.",
    )
    after_compensation = _compensation_state(employee)
    if not creating and before_compensation != after_compensation:
        audit.log(
            "wms.employee_compensation_changed",
            business=business,
            user=actor,
            request=request,
            module="wms",
            obj=employee,
            description=(
                f"Compensation configuration changed for "
                f"'{employee.employee_code}'."
            ),
            old_values=before_audit_state,
            new_values=_safe_compensation_audit_state(employee),
        )
    return employee


@transaction.atomic
def set_employee_active(
    *,
    business,
    employee,
    is_active,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    employee = (
        WmsEmployee.objects.for_business(business)
        .select_for_update()
        .get(pk=employee.pk)
    )
    if employee.is_active == is_active:
        return employee
    employee.is_active = is_active
    employee.updated_by = actor
    employee.save()

    if not is_active:
        assignments = (
            WmsEmployeeCategoryAssignment.objects.select_for_update()
            .filter(
                business=employee.business,
                employee=employee,
                is_active=True,
            )
            .select_related("category", "employee__location__branch")
        )
        for assignment in assignments:
            assignment.is_active = False
            assignment.updated_by = actor
            assignment.save()
            audit.log(
                "wms.employee_category_unassigned",
                business=employee.business,
                user=actor,
                request=request,
                module="wms",
                obj=assignment,
                description=(
                    f"Category '{assignment.category.name}' deactivated for "
                    f"employee '{employee.employee_code}'."
                ),
            )

    audit.log(
        "wms.employee_activated" if is_active else "wms.employee_deactivated",
        business=employee.business,
        user=actor,
        request=request,
        module="wms",
        obj=employee,
        description=(
            f"WMS employee '{employee.employee_code}' "
            f"{'activated' if is_active else 'deactivated'}."
        ),
    )
    return employee


@transaction.atomic
def save_category(
    *,
    business,
    cleaned_data,
    instance=None,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    creating = instance is None
    category = (
        WmsProductionCategory(business=business)
        if creating
        else WmsProductionCategory.objects.for_business(business)
        .select_for_update()
        .get(pk=instance.pk)
    )
    for field, value in cleaned_data.items():
        setattr(category, field, value)
    if creating:
        category.created_by = actor
    category.updated_by = actor
    category.save()
    audit.log(
        "wms.category_created" if creating else "wms.category_updated",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=category,
        description=f"Production category '{category.name}' saved.",
    )
    return category


@transaction.atomic
def set_category_active(
    *,
    business,
    category,
    is_active,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    category = (
        WmsProductionCategory.objects.for_business(business)
        .select_for_update()
        .get(pk=category.pk)
    )
    if category.is_active == is_active:
        return category
    category.is_active = is_active
    category.updated_by = actor
    category.save()

    if not is_active:
        assignments = (
            WmsEmployeeCategoryAssignment.objects.select_for_update()
            .filter(
                business=category.business,
                category=category,
                is_active=True,
            )
            .select_related("employee__location__branch")
        )
        for assignment in assignments:
            assignment.is_active = False
            assignment.updated_by = actor
            assignment.save()
            audit.log(
                "wms.employee_category_unassigned",
                business=category.business,
                user=actor,
                request=request,
                module="wms",
                obj=assignment,
                description=(
                    f"Category '{category.name}' deactivated for employee "
                    f"'{assignment.employee.employee_code}'."
                ),
            )

    audit.log(
        "wms.category_activated" if is_active else "wms.category_deactivated",
        business=category.business,
        user=actor,
        request=request,
        module="wms",
        obj=category,
        description=(
            f"Production category '{category.name}' "
            f"{'activated' if is_active else 'deactivated'}."
        ),
    )
    return category


@transaction.atomic
def save_assignment(
    *,
    business,
    employee,
    category,
    per_piece_rate=None,
    instance=None,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    if employee.business_id != business.pk:
        raise ValidationError("The WMS employee belongs to another business.")
    if category.business_id != business.pk:
        raise ValidationError("The WMS category belongs to another business.")

    creating = False
    reactivating = False
    if instance is not None:
        assignment = WmsEmployeeCategoryAssignment.objects.select_for_update().get(
            pk=instance.pk
        )
        if assignment.business_id != business.pk:
            raise ValidationError("The WMS assignment belongs to another business.")
        if (
            assignment.employee_id != employee.pk
            or assignment.category_id != category.pk
        ):
            raise ValidationError(
                "The assignment employee and category cannot be changed."
            )
        target_active = assignment.is_active
    else:
        assignment = (
            WmsEmployeeCategoryAssignment.objects.select_for_update()
            .filter(
                business=business,
                employee=employee,
                category=category,
            )
            .first()
        )
        if assignment is None:
            creating = True
            assignment = WmsEmployeeCategoryAssignment(
                business=business,
                employee=employee,
                category=category,
                created_by=actor,
            )
            target_active = True
        else:
            reactivating = not assignment.is_active
            target_active = True

    assignment.per_piece_rate = per_piece_rate
    assignment.is_active = target_active
    assignment.updated_by = actor
    assignment.save()

    action = (
        "wms.employee_category_assigned"
        if creating or reactivating
        else "wms.employee_category_updated"
    )
    audit.log(
        action,
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=assignment,
        description=(
            f"Category '{category.name}' assigned to employee "
            f"'{employee.employee_code}'."
        ),
    )
    return assignment


@transaction.atomic
def set_assignment_active(
    *,
    business,
    assignment,
    is_active,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    assignment = (
        WmsEmployeeCategoryAssignment.objects.for_business(business)
        .select_for_update()
        .select_related(
            "employee__location__branch",
            "category",
        )
        .get(pk=assignment.pk)
    )
    if assignment.is_active == is_active:
        return assignment
    assignment.is_active = is_active
    assignment.updated_by = actor
    assignment.save()
    audit.log(
        (
            "wms.employee_category_assigned"
            if is_active
            else "wms.employee_category_unassigned"
        ),
        business=assignment.business,
        user=actor,
        request=request,
        module="wms",
        obj=assignment,
        description=(
            f"Category '{assignment.category.name}' "
            f"{'activated' if is_active else 'deactivated'} for employee "
            f"'{assignment.employee.employee_code}'."
        ),
    )
    return assignment
