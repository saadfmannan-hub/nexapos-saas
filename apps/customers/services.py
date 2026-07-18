"""Customer helpers and balance maintenance."""
import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F
from django.http import Http404

from apps.audit import services as audit
from apps.core.money import D, money
from apps.subscriptions.access import (
    AccessAction,
    evaluate_actor_access,
    require_actor_access,
)

from .models import Customer, CustomerGroup, CustomerPayment

# Export column order ↔ model field mapping (reused by export + import template)
BASE_EXPORT_COLUMNS = [
    "Customer Code", "Customer Name", "Mobile", "WhatsApp", "Email", "Address",
    "City", "Country", "Group", "Credit Limit", "Outstanding Balance",
    "Opening Balance", "Store Credit", "Notes", "Status", "Created Date",
]
BASE_IMPORT_COLUMNS = [
    "customer code", "customer name", "mobile", "whatsapp", "email",
    "address", "city", "country", "group", "credit limit",
    "opening balance", "notes", "active",
]
CREDIT_EXPORT_COLUMNS = {
    "Credit Limit", "Outstanding Balance", "Opening Balance", "Store Credit",
}
CREDIT_IMPORT_COLUMNS = {"credit limit", "opening balance"}
CUSTOM_FIELD_HEADER_WORDS = ("custom", "measurement", "moreoption", "moreoptions")


def ensure_walk_in_customer(business):
    customer, _ = Customer.objects.get_or_create(
        business=business,
        is_walk_in=True,
        defaults={"code": "WALK-IN", "full_name": "Walk-In Customer"},
    )
    return customer


def more_option_values(business, customer):
    values = customer.more_options or {}
    options = []
    for option in business.settings.more_option_labels:
        value = str(values.get(option["key"], "")).strip()
        if value:
            options.append({"label": option["label"], "value": value})
    return options


def export_columns(business, *, include_credit=True):
    columns = BASE_EXPORT_COLUMNS
    if not include_credit:
        columns = [column for column in columns if column not in CREDIT_EXPORT_COLUMNS]
    return columns + [
        option["label"] for option in business.settings.more_option_labels
    ]


def import_columns(business, *, include_credit=True):
    columns = BASE_IMPORT_COLUMNS
    if not include_credit:
        columns = [column for column in columns if column not in CREDIT_IMPORT_COLUMNS]
    return columns + [
        option["label"].lower() for option in business.settings.more_option_labels
    ]


def _header_token(value):
    """Normalize import headers enough to match punctuation-heavy tailoring labels."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _more_option_column_map(business, row):
    exact = {str(key).strip(): key for key in row}
    casefolded = {str(key).strip().lower(): key for key in row}
    normalized = {_header_token(key): key for key in row if _header_token(key)}
    mapped = {}
    used_columns = set()
    for option in business.settings.more_option_labels:
        label = option["label"]
        column = None
        for candidate in (label, label.strip()):
            if candidate in exact:
                column = exact[candidate]
                break
        if column is None:
            lowered = label.strip().lower()
            column = casefolded.get(lowered)
        if column is None:
            column = normalized.get(_header_token(label))
        if column is not None:
            mapped[option["key"]] = column
            used_columns.add(column)
    return mapped, used_columns


def _unknown_custom_columns(business, row, used_more_option_columns):
    known_headers = {_header_token(column) for column in BASE_IMPORT_COLUMNS}
    known_headers.update(
        _header_token(option["label"])
        for option in business.settings.more_option_labels
    )
    unknowns = []
    for column, value in row.items():
        if column in used_more_option_columns or not str(value or "").strip():
            continue
        token = _header_token(column)
        if not token or token in known_headers:
            continue
        if any(word in token for word in CUSTOM_FIELD_HEADER_WORDS):
            unknowns.append(str(column).strip())
    return unknowns


def next_customer_code(business):
    count = Customer.objects.for_business(business).count()
    n = count + 1
    while Customer.objects.for_business(business).filter(code=f"CUST-{n:05d}").exists():
        n += 1
    return f"CUST-{n:05d}"


CUSTOMER_FORM_FIELDS = (
    "full_name", "code", "mobile", "whatsapp", "email", "address", "city",
    "country", "group", "tax_number", "credit_limit", "notes", "is_active",
    "more_options",
)


@transaction.atomic
def save_customer(*, customer, business, user, membership=None, request=None):
    """Persist a basic customer behind POS Core and customer-role access."""
    require_actor_access(
        user,
        business,
        "pos_core",
        permission_code="customers.manage",
        action=AccessAction.WRITE,
        membership=membership,
        request=request,
    )
    if customer.business_id not in (None, business.id):
        require_actor_access(
            user,
            business,
            "pos_core",
            permission_code="customers.manage",
            action=AccessAction.WRITE,
            membership=membership,
            request=request,
            scope_allowed=False,
        )
    if customer.group_id is not None:
        canonical_group = CustomerGroup.objects.filter(
            pk=customer.group_id, business=business
        ).first()
        if canonical_group is None:
            require_actor_access(
                user,
                business,
                "pos_core",
                permission_code="customers.manage",
                action=AccessAction.WRITE,
                membership=membership,
                request=request,
                scope_allowed=False,
            )
        customer.group = canonical_group
    credit_limit_changed = customer.pk is None and D(customer.credit_limit) != 0
    if customer.pk:
        canonical_customer = (
            Customer.objects.select_for_update()
            .filter(pk=customer.pk, business=business)
            .first()
        )
        if canonical_customer is None:
            require_actor_access(
                user,
                business,
                "pos_core",
                permission_code="customers.manage",
                action=AccessAction.WRITE,
                membership=membership,
                request=request,
                scope_allowed=False,
            )
        credit_limit_changed = D(customer.credit_limit) != D(
            canonical_customer.credit_limit
        )
        if credit_limit_changed:
            require_actor_access(
                user,
                business,
                "customer_credit",
                permission_code="customers.manage",
                action=AccessAction.WRITE,
                membership=membership,
                request=request,
            )
        for field_name in CUSTOMER_FORM_FIELDS:
            setattr(canonical_customer, field_name, getattr(customer, field_name))
        customer = canonical_customer
    elif credit_limit_changed:
        require_actor_access(
            user,
            business,
            "customer_credit",
            permission_code="customers.manage",
            action=AccessAction.WRITE,
            membership=membership,
            request=request,
        )
    customer.business = business
    customer.save()
    return customer


@transaction.atomic
def apply_balance_change(customer_id, delta: Decimal):
    """Atomically shift a customer's receivable balance."""
    Customer.objects.filter(pk=customer_id).update(balance=F("balance") + delta)


@transaction.atomic
def apply_store_credit_change(customer_id, delta: Decimal):
    Customer.objects.filter(pk=customer_id).update(store_credit=F("store_credit") + delta)


@transaction.atomic
def record_customer_payment(
    *,
    business,
    customer,
    amount,
    payment_method,
    user,
    reference="",
    notes="",
    shift=None,
    membership=None,
    request=None,
):
    """Collect a receivable behind Customer Credit and exact actor scope."""
    from apps.sales.models import PaymentMethod
    from apps.tenants.models import Business

    canonical_business = (
        Business.objects.select_for_update(no_key=True)
        .filter(pk=getattr(business, "pk", None))
        .first()
    )
    if canonical_business is None:
        raise Http404
    context = require_actor_access(
        user,
        canonical_business,
        "customer_credit",
        permission_code="customers.payments",
        action=AccessAction.WRITE,
        membership=membership,
        request=request,
    )
    customer = (
        Customer.objects.select_for_update()
        .filter(
            pk=getattr(customer, "pk", None),
            business=canonical_business,
        )
        .first()
    )
    payment_method = (
        PaymentMethod.objects.select_for_update()
        .filter(
            pk=getattr(payment_method, "pk", None),
            business=canonical_business,
            is_active=True,
        )
        .first()
    )
    if customer is None or payment_method is None:
        raise Http404
    if payment_method.kind in (
        PaymentMethod.Kind.CUSTOMER_CREDIT,
        PaymentMethod.Kind.STORE_CREDIT,
    ):
        raise ValidationError("Use a real payment method to collect a balance.")

    if context.membership.allowed_branch_ids is not None:
        require_actor_access(
            user,
            canonical_business,
            "customer_credit",
            permission_code="customers.payments",
            action=AccessAction.WRITE,
            membership=context.membership,
            request=request,
            scope_allowed=False,
        )

    branch = None
    if shift is not None:
        from apps.registers.models import Shift

        shift = (
            Shift.objects.select_for_update()
            .select_related("branch")
            .filter(
                pk=getattr(shift, "pk", None),
                business=canonical_business,
                cashier=user,
                status="open",
            )
            .first()
        )
        if shift is None or not context.membership.can_access_branch(shift.branch):
            require_actor_access(
                user,
                canonical_business,
                "customer_credit",
                permission_code="customers.payments",
                action=AccessAction.WRITE,
                membership=context.membership,
                request=request,
                scope_allowed=False,
            )
        branch = shift.branch
    amount = money(amount)
    if amount <= 0:
        raise ValidationError("Payment amount must be positive.")
    if amount > customer.balance:
        raise ValidationError(
            "Payment exceeds the customer's outstanding balance."
        )
    number = CustomerPayment.objects.for_business(canonical_business).count() + 1
    while CustomerPayment.objects.for_business(canonical_business).filter(
        receipt_number=f"RCV-{number:06d}"
    ).exists():
        number += 1
    payment = CustomerPayment.objects.create(
        business=canonical_business,
        receipt_number=f"RCV-{number:06d}",
        customer=customer,
        branch=branch,
        kind=CustomerPayment.Kind.COLLECTION,
        amount=amount,
        payment_method=payment_method,
        reference=str(reference or "")[:120],
        notes=str(notes or "")[:300],
        received_by=user,
        shift=shift,
    )
    customer.balance = money(customer.balance - amount)
    customer.save(update_fields=["balance", "updated_at"])
    audit.log(
        "customer.payment",
        business=canonical_business,
        user=user,
        request=request,
        module="customers",
        obj=payment,
        description=(
            f"Collected {amount} from {customer.full_name} "
            f"({payment.receipt_number})."
        ),
    )
    return payment


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------
def export_dataset(business, queryset, *, include_credit=True):
    """Build {columns, rows} for customer export (CSV/XLSX)."""
    rows = []
    option_labels = business.settings.more_option_labels
    for c in queryset.select_related("group"):
        more_values = c.more_options or {}
        row = [
            c.code, c.full_name, c.mobile, c.whatsapp, c.email, c.address,
            c.city, c.country, c.group.name if c.group else "",
        ]
        if include_credit:
            row.extend([
                c.credit_limit, c.balance, c.opening_balance, c.store_credit,
            ])
        row.extend([
            c.notes, "Active" if c.is_active else "Inactive",
            c.created_at.strftime("%Y-%m-%d"),
            *[more_values.get(option["key"], "") for option in option_labels],
        ])
        rows.append(row)
    return {
        "columns": export_columns(business, include_credit=include_credit),
        "rows": rows,
        "totals": None,
    }


@transaction.atomic
def import_customers(*, business, rows, mode, user, membership=None, request=None):
    """Import customer rows. mode: 'skip' | 'update'.

    Returns (summary, errors) where summary has imported/updated/skipped/
    failed counts and errors is a list of (row_number, message).
    Matching is by customer code, then mobile. Decimal-safe.
    """
    from apps.core.imports import normalize_row

    require_actor_access(
        user,
        business,
        "pos_core",
        permission_code="customers.import",
        action=AccessAction.WRITE,
        membership=membership,
        request=request,
    )
    credit_allowed = evaluate_actor_access(
        user,
        business,
        "customer_credit",
        permission_code="customers.import",
        action=AccessAction.WRITE,
        membership=membership,
        request=request,
    ).allowed

    summary = {"imported": 0, "updated": 0, "skipped": 0, "failed": 0}
    errors = []
    seen_codes, seen_mobiles = set(), set()

    for idx, raw in enumerate(rows, start=2):  # row 1 = header
        r = normalize_row(raw)
        if not credit_allowed and any(
            str(r.get(column, "") or "").strip()
            for column in CREDIT_IMPORT_COLUMNS
        ):
            errors.append((
                idx,
                "Customer Credit must be enabled to import credit limits or "
                "opening balances.",
            ))
            summary["failed"] += 1
            continue
        more_option_columns, used_more_option_columns = _more_option_column_map(
            business, raw)
        unknown_custom_columns = _unknown_custom_columns(
            business, raw, used_more_option_columns)
        if unknown_custom_columns:
            errors.append((
                idx,
                "Unmapped customer custom field column(s): "
                f"{', '.join(unknown_custom_columns)}. Configure matching More "
                "Options labels or remove those columns.",
            ))
            summary["failed"] += 1
            continue
        name = r.get("customer name", "")
        code = r.get("customer code", "")
        mobile = r.get("mobile", "")
        email = r.get("email", "")

        if not name:
            errors.append((idx, "Missing required field: customer name."))
            summary["failed"] += 1
            continue
        if email:
            try:
                validate_email(email)
            except ValidationError:
                errors.append((idx, f"Invalid email format: {email}"))
                summary["failed"] += 1
                continue
        # In-file duplicates
        if code and code in seen_codes:
            errors.append((idx, f"Duplicate customer code in file: {code}"))
            summary["failed"] += 1
            continue
        if mobile and mobile in seen_mobiles:
            errors.append((idx, f"Duplicate mobile in file: {mobile}"))
            summary["failed"] += 1
            continue

        existing = None
        if code:
            existing = Customer.objects.for_business(business).filter(code=code).first()
        if existing is None and mobile:
            existing = Customer.objects.for_business(business).filter(
                mobile=mobile).first()

        if existing and existing.is_walk_in:
            errors.append((idx, "Cannot import over the walk-in customer."))
            summary["failed"] += 1
            continue

        if existing:
            if mode == "skip":
                summary["skipped"] += 1
                if code:
                    seen_codes.add(code)
                if mobile:
                    seen_mobiles.add(mobile)
                continue
            # update mode
            try:
                _apply_fields(
                    business,
                    existing,
                    r,
                    raw,
                    more_option_columns,
                    include_credit=credit_allowed,
                )
                existing.save()
                summary["updated"] += 1
            except Exception as exc:
                errors.append((idx, f"Update failed: {exc}"))
                summary["failed"] += 1
                continue
        else:
            try:
                customer = Customer(business=business,
                                    code=code or next_customer_code(business))
                _apply_fields(
                    business,
                    customer,
                    r,
                    raw,
                    more_option_columns,
                    include_credit=credit_allowed,
                )
                customer.full_clean(exclude=["public_id"])
                customer.save()
                summary["imported"] += 1
            except ValidationError as exc:
                errors.append((idx, "; ".join(
                    f"{k}: {', '.join(v)}" for k, v in exc.message_dict.items())))
                summary["failed"] += 1
                continue
            except Exception as exc:
                errors.append((idx, f"Import failed: {exc}"))
                summary["failed"] += 1
                continue
        if code:
            seen_codes.add(code)
        if mobile:
            seen_mobiles.add(mobile)
    return summary, errors


def _apply_fields(
    business, customer, r, raw, more_option_columns, *, include_credit=True
):
    customer.full_name = r.get("customer name", customer.full_name)[:160]
    if r.get("mobile"):
        customer.mobile = r["mobile"][:30]
    customer.whatsapp = r.get("whatsapp", customer.whatsapp)[:30]
    customer.email = r.get("email", customer.email)
    customer.address = r.get("address", customer.address)[:255]
    customer.city = r.get("city", customer.city)[:100]
    customer.country = r.get("country", customer.country)[:100]
    if include_credit and r.get("credit limit"):
        customer.credit_limit = D(r["credit limit"])
    if include_credit and r.get("opening balance"):
        customer.opening_balance = D(r["opening balance"])
        if not customer.pk:
            customer.balance = customer.opening_balance
    customer.notes = r.get("notes", customer.notes)
    active = r.get("active", "")
    if active:
        customer.is_active = active.lower() not in ("0", "false", "no", "inactive")
    more_options = dict(customer.more_options or {})
    for option in business.settings.more_option_labels:
        column = more_option_columns.get(option["key"])
        if column is None:
            continue
        value = str(raw.get(column, "") or "").strip()
        if value:
            more_options[option["key"]] = value
    customer.more_options = more_options
    group_name = r.get("group", "")
    if group_name:
        customer.group, _ = CustomerGroup.objects.get_or_create(
            business=business, name=group_name[:80])
