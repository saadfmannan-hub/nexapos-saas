"""Transactional linked-order production mutations with cumulative PCS caps."""

from collections import Counter, defaultdict

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum

from apps.audit import services as audit
from apps.wms_core.models import WmsLocation
from apps.wms_orders.models import WmsWorkshopOrder
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
)

from .models import WmsProductionEntry, WmsProductionEntryLine


def _actor(user=None, request=None):
    return user or getattr(request, "user", None)


def _validate_quantity(value, label, *, positive=False):
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if positive else "nonnegative"
        raise ValidationError(f"{label} must be a whole {qualifier} number.")
    return value


def _validate_user_access(
    user_access,
    business,
    location,
    *,
    require_active_location,
    permission_code,
):
    if user_access is None or user_access.business_id != business.pk:
        raise ValidationError("Explicit WMS user access is required.")
    if not user_access.is_active or not user_access.role.is_active:
        raise ValidationError("Active WMS user access is required.")
    if not user_access.has_perm(permission_code):
        raise ValidationError(f"WMS permission '{permission_code}' is required.")
    if location.business_id != business.pk:
        raise ValidationError("The WMS location belongs to another business.")
    allowed_ids = user_access.allowed_location_ids
    if allowed_ids is not None and location.pk not in allowed_ids:
        raise ValidationError("The selected WMS location is outside your allowed scope.")
    if require_active_location and (
        not location.is_active or not location.branch.is_active
    ):
        raise ValidationError("Inactive WMS locations cannot receive new production.")


def _entry_state(entry, lines=None):
    lines = list(
        lines
        if lines is not None
        else entry.lines.filter(is_removed=False).select_related("order")
    )
    return {
        "business_public_id": str(entry.business.public_id),
        "employee_public_id": str(entry.employee.public_id),
        "location_public_id": str(entry.location.public_id),
        "production_date": entry.production_date.isoformat(),
        "daily_total_pieces": entry.daily_total_pieces,
        "notes": entry.notes,
        "is_corrected": entry.is_corrected,
        "correction_reason": entry.correction_reason,
        "production_rows": [
            {
                "line_public_id": str(line.public_id),
                "order_public_id": (
                    str(line.order.public_id) if line.order_id else None
                ),
                "order_reference": (
                    line.order.order_reference if line.order_id else None
                ),
                "assignment_public_id": str(line.assignment.public_id),
                "category_public_id": str(line.category.public_id),
                "category_name": line.category_name_snapshot,
                "category_code": line.category_code_snapshot,
                "quantity": line.quantity,
            }
            for line in lines
        ],
    }


def _change_summary(old_values, new_values):
    parent_changes = {}
    for field in ("daily_total_pieces", "notes"):
        if old_values[field] != new_values[field]:
            parent_changes[field] = {
                "old": old_values[field],
                "new": new_values[field],
            }

    old_rows = {
        row["line_public_id"]: row for row in old_values["production_rows"]
    }
    new_rows = {
        row["line_public_id"]: row for row in new_values["production_rows"]
    }
    row_changes = []
    for line_id in sorted(old_rows.keys() - new_rows.keys()):
        row_changes.append(
            {
                "change": "removed",
                "line_public_id": line_id,
                "old": old_rows[line_id],
                "new": None,
            }
        )
    for line_id in sorted(new_rows.keys() - old_rows.keys()):
        row_changes.append(
            {
                "change": "added",
                "line_public_id": line_id,
                "old": None,
                "new": new_rows[line_id],
            }
        )
    for line_id in sorted(old_rows.keys() & new_rows.keys()):
        old_row = old_rows[line_id]
        new_row = new_rows[line_id]
        if old_row != new_row:
            row_changes.append(
                {
                    "change": "updated",
                    "line_public_id": line_id,
                    "old": old_row,
                    "new": new_row,
                }
            )
    return {
        "parent_fields": parent_changes,
        "production_rows": row_changes,
    }


def _row_identity(value, *, label):
    pk = getattr(value, "pk", None)
    if pk is not None:
        return ("pk", pk)
    if value:
        return ("public_id", value)
    raise ValidationError(f"Every production row requires a {label}.")


def _resolve_submitted_rows(*, business, employee, production_rows):
    prepared = []
    order_pks = set()
    order_public_ids = set()
    assignment_pks = set()
    assignment_public_ids = set()
    for row in production_rows:
        if not isinstance(row, dict):
            raise ValidationError("Each production row must contain Order, Operation, and PCS.")
        quantity = _validate_quantity(row.get("quantity"), "Production PCS", positive=True)
        order_key = _row_identity(row.get("order"), label="Workshop Order")
        assignment_key = _row_identity(row.get("assignment"), label="Operation")
        (order_pks if order_key[0] == "pk" else order_public_ids).add(order_key[1])
        (
            assignment_pks
            if assignment_key[0] == "pk"
            else assignment_public_ids
        ).add(assignment_key[1])
        prepared.append((order_key, assignment_key, quantity))
    if not prepared:
        raise ValidationError("Add at least one production row.")

    orders = list(
        WmsWorkshopOrder.objects.for_business(business)
        .select_for_update()
        .select_related("location__branch")
        .filter(pk__in=order_pks)
        .order_by("pk")
    )
    if order_public_ids:
        orders.extend(
            WmsWorkshopOrder.objects.for_business(business)
            .select_for_update()
            .select_related("location__branch")
            .filter(public_id__in=order_public_ids)
            .order_by("pk")
        )
    order_map = {
        ("pk", order.pk): order for order in orders
    } | {
        ("public_id", str(order.public_id)): order for order in orders
    }

    assignments = list(
        WmsEmployeeCategoryAssignment.objects.for_business(business)
        .select_for_update()
        .select_related("category")
        .filter(pk__in=assignment_pks)
        .order_by("pk")
    )
    if assignment_public_ids:
        assignments.extend(
            WmsEmployeeCategoryAssignment.objects.for_business(business)
            .select_for_update()
            .select_related("category")
            .filter(public_id__in=assignment_public_ids)
            .order_by("pk")
        )
    assignment_map = {
        ("pk", assignment.pk): assignment for assignment in assignments
    } | {
        ("public_id", str(assignment.public_id)): assignment
        for assignment in assignments
    }

    resolved = []
    for order_key, assignment_key, quantity in prepared:
        normalized_order_key = (
            order_key[0],
            str(order_key[1]) if order_key[0] == "public_id" else order_key[1],
        )
        normalized_assignment_key = (
            assignment_key[0],
            str(assignment_key[1])
            if assignment_key[0] == "public_id"
            else assignment_key[1],
        )
        order = order_map.get(normalized_order_key)
        assignment = assignment_map.get(normalized_assignment_key)
        if order is None:
            raise ValidationError("The selected Workshop Order is unavailable.")
        if assignment is None:
            raise ValidationError("The selected production operation is unavailable.")
        if assignment.employee_id != employee.pk:
            raise ValidationError("The selected operation is not assigned to this employee.")
        if not assignment.is_active or not assignment.category.is_active:
            raise ValidationError("Only active employee operations can receive production.")
        resolved.append(
            {"order": order, "assignment": assignment, "quantity": quantity}
        )
    return resolved


def _cap_error(order, category_name, already_recorded, requested):
    return ValidationError(
        "Production quantity exceeded.\n"
        f"Order: {order.order_reference}\n"
        f"Operation: {category_name}\n"
        f"Order PCS: {order.eligible_piece_count}\n"
        f"Already recorded: {already_recorded} PCS\n"
        f"Requested additional: {requested} PCS"
    )


def _validate_new_order_rows(*, business, location, rows):
    submitted = defaultdict(int)
    order_by_id = {}
    category_by_id = {}
    for row in rows:
        order = row["order"]
        assignment = row["assignment"]
        if order.business_id != business.pk:
            raise ValidationError("The selected Workshop Order belongs to another business.")
        if order.location_id != location.pk:
            raise ValidationError("Workshop Order location must match the employee location.")
        if order.status != WmsWorkshopOrder.Status.IN_PROCESS:
            raise ValidationError("Only In Process Workshop Orders can receive new production.")
        if not order.location.is_active or not order.location.branch.is_active:
            raise ValidationError("Inactive WMS locations cannot receive new production.")
        if not order.eligible_piece_count or order.eligible_piece_count <= 0:
            raise ValidationError(
                f"Workshop Order {order.order_reference} requires eligible PCS before production."
            )
        bucket = (order.pk, assignment.category_id)
        submitted[bucket] += row["quantity"]
        order_by_id[order.pk] = order
        category_by_id[assignment.category_id] = assignment.category

    existing = {
        (item["order_id"], item["category_id"]): item["total"] or 0
        for item in (
            WmsProductionEntryLine.objects.for_business(business)
            .filter(
                is_removed=False,
                order_id__in={key[0] for key in submitted},
                category_id__in={key[1] for key in submitted},
            )
            .values("order_id", "category_id")
            .annotate(total=Sum("quantity"))
        )
    }
    for (order_id, category_id), requested in submitted.items():
        already = existing.get((order_id, category_id), 0)
        order = order_by_id[order_id]
        if already + requested > order.eligible_piece_count:
            raise _cap_error(
                order,
                category_by_id[category_id].name,
                already,
                requested,
            )


def _validate_new_duplicate_combinations(rows):
    combinations = Counter(
        (row["order"].pk, row["assignment"].category_id) for row in rows
    )
    if any(count > 1 for count in combinations.values()):
        raise ValidationError(
            "This Workshop Order and operation already exist in this production "
            "record. Update the existing row instead."
        )


@transaction.atomic
def create_production_entry(
    *,
    business,
    user_access,
    location,
    employee,
    production_date,
    daily_total_pieces,
    notes,
    production_rows,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    daily_total_pieces = _validate_quantity(daily_total_pieces, "Daily Total Pieces")
    try:
        employee = (
            WmsEmployee.objects.for_business(business)
            .select_for_update()
            .select_related("location__branch")
            .get(pk=employee.pk)
        )
        location = (
            WmsLocation.objects.for_business(business)
            .select_for_update()
            .select_related("branch")
            .get(pk=location.pk)
        )
    except (WmsEmployee.DoesNotExist, WmsLocation.DoesNotExist) as exc:
        raise ValidationError("The employee or WMS location belongs to another business.") from exc

    _validate_user_access(
        user_access,
        business,
        location,
        require_active_location=True,
        permission_code="wms.production.manage",
    )
    if employee.location_id != location.pk:
        raise ValidationError("Production location must match the employee's WMS location.")
    if not employee.is_active:
        raise ValidationError("Inactive employees cannot receive new production.")

    rows = _resolve_submitted_rows(
        business=business,
        employee=employee,
        production_rows=production_rows,
    )
    _validate_new_duplicate_combinations(rows)
    _validate_new_order_rows(business=business, location=location, rows=rows)

    entry = WmsProductionEntry(
        business=business,
        location=location,
        employee=employee,
        production_date=production_date,
        daily_total_pieces=daily_total_pieces,
        notes=notes,
        created_by=actor,
        updated_by=actor,
    )
    try:
        with transaction.atomic():
            entry.save()
    except IntegrityError as exc:
        conflict_exists = WmsProductionEntry.objects.for_business(business).filter(
            employee_id=employee.pk,
            production_date=production_date,
        ).exists()
        if conflict_exists:
            raise ValidationError(
                "Production already exists for this employee on this date."
            ) from exc
        raise

    lines = []
    for row in rows:
        assignment = row["assignment"]
        line = WmsProductionEntryLine(
            business=business,
            entry=entry,
            order=row["order"],
            assignment=assignment,
            category=assignment.category,
            category_name_snapshot=assignment.category.name,
            category_code_snapshot=assignment.category.code,
            quantity=row["quantity"],
        )
        line.save()
        lines.append(line)

    audit.log(
        "wms.production_entry_created",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=entry,
        description=(
            f"Production entry created for employee '{employee.employee_code}' "
            f"on {production_date.isoformat()}."
        ),
        new_values=_entry_state(entry, lines),
    )
    return entry


def _normalize_existing_line_id(value):
    if value in (None, ""):
        return None
    return str(getattr(value, "public_id", value))


def _resolve_corrected_rows(*, business, entry, lines, production_rows):
    existing_by_id = {str(line.public_id): line for line in lines}
    submitted_line_ids = set()
    prepared = []
    order_pks = set()
    order_public_ids = set()
    assignment_pks = set()
    assignment_public_ids = set()

    for row in production_rows:
        if not isinstance(row, dict):
            raise ValidationError(
                "Each production row must contain Workshop Order, Operation, and PCS."
            )
        line_id = _normalize_existing_line_id(row.get("line_id"))
        if line_id is not None:
            if line_id not in existing_by_id:
                raise ValidationError(
                    "A submitted production row is unavailable. Refresh and try again."
                )
            if line_id in submitted_line_ids:
                raise ValidationError("Submit each saved production row at most once.")
            submitted_line_ids.add(line_id)

        quantity = _validate_quantity(
            row.get("quantity"),
            "Production PCS",
            positive=True,
        )
        order_value = row.get("order")
        order_key = None
        if order_value:
            order_key = _row_identity(order_value, label="Workshop Order")
            (order_pks if order_key[0] == "pk" else order_public_ids).add(
                order_key[1]
            )
        assignment_key = _row_identity(row.get("assignment"), label="Operation")
        (
            assignment_pks
            if assignment_key[0] == "pk"
            else assignment_public_ids
        ).add(assignment_key[1])
        prepared.append((line_id, order_key, assignment_key, quantity))

    orders = list(
        WmsWorkshopOrder.objects.for_business(business)
        .select_for_update()
        .select_related("location__branch")
        .filter(pk__in=order_pks)
        .order_by("pk")
    )
    if order_public_ids:
        orders.extend(
            WmsWorkshopOrder.objects.for_business(business)
            .select_for_update()
            .select_related("location__branch")
            .filter(public_id__in=order_public_ids)
            .order_by("pk")
        )
    order_map = {
        ("pk", order.pk): order for order in orders
    } | {
        ("public_id", str(order.public_id)): order for order in orders
    }

    assignments = list(
        WmsEmployeeCategoryAssignment.objects.for_business(business)
        .select_for_update()
        .select_related("category")
        .filter(pk__in=assignment_pks)
        .order_by("pk")
    )
    if assignment_public_ids:
        assignments.extend(
            WmsEmployeeCategoryAssignment.objects.for_business(business)
            .select_for_update()
            .select_related("category")
            .filter(public_id__in=assignment_public_ids)
            .order_by("pk")
        )
    assignment_map = {
        ("pk", assignment.pk): assignment for assignment in assignments
    } | {
        ("public_id", str(assignment.public_id)): assignment
        for assignment in assignments
    }

    resolved = []
    for line_id, order_key, assignment_key, quantity in prepared:
        original = existing_by_id.get(line_id)
        order = None
        if order_key is not None:
            normalized_order_key = (
                order_key[0],
                str(order_key[1])
                if order_key[0] == "public_id"
                else order_key[1],
            )
            order = order_map.get(normalized_order_key)
            if order is None:
                raise ValidationError("The selected Workshop Order is unavailable.")
        normalized_assignment_key = (
            assignment_key[0],
            str(assignment_key[1])
            if assignment_key[0] == "public_id"
            else assignment_key[1],
        )
        assignment = assignment_map.get(normalized_assignment_key)
        if assignment is None:
            raise ValidationError("The selected production operation is unavailable.")
        if original is None and order is None:
            raise ValidationError("Every new production row requires a Workshop Order.")
        if original is not None and original.order_id and order is None:
            raise ValidationError("A linked Workshop Order cannot be cleared.")
        if assignment.employee_id != entry.employee_id:
            raise ValidationError(
                "The selected operation is not assigned to this employee."
            )
        assignment_changed = (
            original is None or original.assignment_id != assignment.pk
        )
        if assignment_changed and (
            not assignment.is_active or not assignment.category.is_active
        ):
            raise ValidationError(
                "Only active employee operations can receive production."
            )
        if order is not None:
            if order.location_id != entry.location_id:
                raise ValidationError(
                    "Workshop Order location must match the employee location."
                )
            order_changed = original is None or original.order_id != order.pk
            if order_changed:
                if order.status != WmsWorkshopOrder.Status.IN_PROCESS:
                    raise ValidationError(
                        "Only In Process Workshop Orders can receive new production."
                    )
                if not order.location.is_active or not order.location.branch.is_active:
                    raise ValidationError(
                        "Inactive WMS locations cannot receive new production."
                    )
                if not order.eligible_piece_count or order.eligible_piece_count <= 0:
                    raise ValidationError(
                        f"Workshop Order {order.order_reference} requires eligible "
                        "PCS before production."
                    )
        resolved.append(
            {
                "line": original,
                "order": order,
                "assignment": assignment,
                "quantity": quantity,
            }
        )

    proposed_combinations = Counter(
        (row["order"].pk, row["assignment"].category_id)
        for row in resolved
        if row["order"] is not None
    )
    for row in resolved:
        if row["order"] is None:
            continue
        combination = (row["order"].pk, row["assignment"].category_id)
        original = row["line"]
        original_combination = (
            (original.order_id, original.category_id)
            if original is not None and original.order_id
            else None
        )
        if (
            proposed_combinations[combination] > 1
            and original_combination != combination
        ):
            raise ValidationError(
                "This Workshop Order and operation already exist in this production "
                "record. Update the existing row instead."
            )
    return resolved


def _validate_corrected_caps(*, business, original_lines, rows):
    linked_rows = [row for row in rows if row["order"] is not None]
    if not linked_rows:
        return
    order_ids = sorted({row["order"].pk for row in linked_rows})
    locked_orders = list(
        WmsWorkshopOrder.objects.for_business(business)
        .select_for_update()
        .filter(pk__in=order_ids)
        .order_by("pk")
    )
    order_map = {order.pk: order for order in locked_orders}
    proposed = defaultdict(int)
    category_names = {}
    for row in linked_rows:
        category = row["assignment"].category
        proposed[(row["order"].pk, category.pk)] += row["quantity"]
        category_names[category.pk] = category.name
    other_totals = {
        (item["order_id"], item["category_id"]): item["total"] or 0
        for item in (
            WmsProductionEntryLine.objects.for_business(business)
            .filter(
                is_removed=False,
                order_id__in=order_ids,
                category_id__in={
                    row["assignment"].category_id for row in linked_rows
                },
            )
            .exclude(pk__in={line.pk for line in original_lines})
            .values("order_id", "category_id")
            .annotate(total=Sum("quantity"))
        )
    }
    original_totals = defaultdict(int)
    original_order_totals = defaultdict(int)
    for line in original_lines:
        if line.order_id:
            original_totals[(line.order_id, line.category_id)] += line.quantity
            original_order_totals[line.order_id] += line.quantity
    proposed_order_totals = defaultdict(int)
    for (order_id, _category_id), quantity in proposed.items():
        proposed_order_totals[order_id] += quantity
    for (order_id, category_id), requested in proposed.items():
        order = order_map.get(order_id)
        if order is None:
            raise ValidationError("The linked Workshop Order PCS is unavailable.")
        already = other_totals.get((order_id, category_id), 0)
        if not order.eligible_piece_count:
            if (
                requested <= original_totals[(order_id, category_id)]
                or proposed_order_totals[order_id]
                <= original_order_totals[order_id]
            ):
                continue
            raise ValidationError("The linked Workshop Order PCS is unavailable.")
        if already + requested > order.eligible_piece_count:
            raise _cap_error(
                order,
                category_names[category_id],
                already,
                requested,
            )


@transaction.atomic
def correct_production_entry(
    *,
    business,
    user_access,
    entry,
    daily_total_pieces,
    notes,
    production_rows=None,
    line_quantities=None,
    correction_reason,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    reason = (correction_reason or "").strip()
    if not reason:
        raise ValidationError("A correction reason is required.")
    daily_total_pieces = _validate_quantity(daily_total_pieces, "Daily Total Pieces")
    try:
        entry = (
            WmsProductionEntry.objects.for_business(business)
            .select_for_update()
            .select_related("employee", "location__branch")
            .get(pk=entry.pk)
        )
    except WmsProductionEntry.DoesNotExist as exc:
        raise ValidationError("The production entry belongs to another business.") from exc
    _validate_user_access(
        user_access,
        business,
        entry.location,
        require_active_location=False,
        permission_code="wms.production.correct",
    )
    lines = list(
        WmsProductionEntryLine.objects.for_business(business)
        .select_for_update()
        .select_related("order", "assignment", "category")
        .filter(entry=entry, is_removed=False)
        .order_by(
            "assignment__category__display_order",
            "category_name_snapshot",
            "pk",
        )
    )
    if production_rows is None:
        if line_quantities is None:
            raise ValidationError("Submit the corrected production rows.")
        expected_ids = {str(line.public_id) for line in lines}
        if set(line_quantities) != expected_ids:
            raise ValidationError("Submit every saved production row exactly once.")
        validated_quantities = {
            line_id: _validate_quantity(value, "Production PCS")
            for line_id, value in line_quantities.items()
        }
        production_rows = [
            {
                "line_id": str(line.public_id),
                "order": line.order,
                "assignment": line.assignment,
                "quantity": validated_quantities[str(line.public_id)],
            }
            for line in lines
        ]
        allow_zero_quantities = True
    else:
        allow_zero_quantities = False

    if allow_zero_quantities:
        corrected_rows = [
            {
                "line": line,
                "order": line.order,
                "assignment": line.assignment,
                "quantity": validated_quantities[str(line.public_id)],
            }
            for line in lines
        ]
    else:
        corrected_rows = _resolve_corrected_rows(
            business=business,
            entry=entry,
            lines=lines,
            production_rows=production_rows,
        )
    _validate_corrected_caps(
        business=business,
        original_lines=lines,
        rows=corrected_rows,
    )

    old_values = _entry_state(entry, lines)
    entry.daily_total_pieces = daily_total_pieces
    entry.notes = notes
    entry.is_corrected = True
    entry.correction_reason = reason
    entry.updated_by = actor
    entry.save()
    retained_line_ids = set()
    saved_lines = []
    for row in corrected_rows:
        line = row["line"]
        assignment = row["assignment"]
        if line is None:
            line = WmsProductionEntryLine(
                business=business,
                entry=entry,
                order=row["order"],
                assignment=assignment,
                category=assignment.category,
                category_name_snapshot=assignment.category.name,
                category_code_snapshot=assignment.category.code,
                quantity=row["quantity"],
            )
        else:
            retained_line_ids.add(line.pk)
            assignment_changed = line.assignment_id != assignment.pk
            line.order = row["order"]
            line.assignment = assignment
            line.category = assignment.category
            if assignment_changed:
                line.category_name_snapshot = assignment.category.name
                line.category_code_snapshot = assignment.category.code
            line.quantity = row["quantity"]
            line._allow_correction_identity_change = True
        line.save()
        saved_lines.append(line)
    for line in lines:
        if line.pk not in retained_line_ids:
            line.is_removed = True
            line.save(update_fields=["is_removed", "updated_at"])
    new_values = _entry_state(entry, saved_lines)
    new_values["change_summary"] = _change_summary(old_values, new_values)
    description = (
        f"Production entry corrected for employee '{entry.employee.employee_code}' "
        f"on {entry.production_date.isoformat()}."
    )
    for action in ("wms.production_entry_updated", "wms.production_entry_corrected"):
        audit.log(
            action,
            business=business,
            user=actor,
            request=request,
            module="wms",
            obj=entry,
            description=description,
            old_values=old_values,
            new_values=new_values,
        )
    return entry
