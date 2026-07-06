"""Customer helpers and balance maintenance."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F

from apps.core.money import D

from .models import Customer, CustomerGroup

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


def export_columns(business):
    return BASE_EXPORT_COLUMNS + [
        option["label"] for option in business.settings.more_option_labels
    ]


def import_columns(business):
    return BASE_IMPORT_COLUMNS + [
        option["label"].lower() for option in business.settings.more_option_labels
    ]


def next_customer_code(business):
    count = Customer.objects.for_business(business).count()
    n = count + 1
    while Customer.objects.for_business(business).filter(code=f"CUST-{n:05d}").exists():
        n += 1
    return f"CUST-{n:05d}"


@transaction.atomic
def apply_balance_change(customer_id, delta: Decimal):
    """Atomically shift a customer's receivable balance."""
    Customer.objects.filter(pk=customer_id).update(balance=F("balance") + delta)


@transaction.atomic
def apply_store_credit_change(customer_id, delta: Decimal):
    Customer.objects.filter(pk=customer_id).update(store_credit=F("store_credit") + delta)


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------
def export_dataset(business, queryset):
    """Build {columns, rows} for customer export (CSV/XLSX)."""
    rows = []
    option_labels = business.settings.more_option_labels
    for c in queryset.select_related("group"):
        more_values = c.more_options or {}
        rows.append([
            c.code, c.full_name, c.mobile, c.whatsapp, c.email, c.address,
            c.city, c.country, c.group.name if c.group else "",
            c.credit_limit, c.balance, c.opening_balance, c.store_credit, c.notes,
            "Active" if c.is_active else "Inactive",
            c.created_at.strftime("%Y-%m-%d"),
            *[more_values.get(option["key"], "") for option in option_labels],
        ])
    return {"columns": export_columns(business), "rows": rows, "totals": None}


@transaction.atomic
def import_customers(*, business, rows, mode, user):
    """Import customer rows. mode: 'skip' | 'update'.

    Returns (summary, errors) where summary has imported/updated/skipped/
    failed counts and errors is a list of (row_number, message).
    Matching is by customer code, then mobile. Decimal-safe.
    """
    from apps.core.imports import normalize_row

    summary = {"imported": 0, "updated": 0, "skipped": 0, "failed": 0}
    errors = []
    seen_codes, seen_mobiles = set(), set()

    for idx, raw in enumerate(rows, start=2):  # row 1 = header
        r = normalize_row(raw)
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
                _apply_fields(business, existing, r)
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
                _apply_fields(business, customer, r)
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


def _apply_fields(business, customer, r):
    customer.full_name = r.get("customer name", customer.full_name)[:160]
    if r.get("mobile"):
        customer.mobile = r["mobile"][:30]
    customer.whatsapp = r.get("whatsapp", customer.whatsapp)[:30]
    customer.email = r.get("email", customer.email)
    customer.address = r.get("address", customer.address)[:255]
    customer.city = r.get("city", customer.city)[:100]
    customer.country = r.get("country", customer.country)[:100]
    if r.get("credit limit"):
        customer.credit_limit = D(r["credit limit"])
    if r.get("opening balance"):
        customer.opening_balance = D(r["opening balance"])
        if not customer.pk:
            customer.balance = customer.opening_balance
    customer.notes = r.get("notes", customer.notes)
    active = r.get("active", "")
    if active:
        customer.is_active = active.lower() not in ("0", "false", "no", "inactive")
    more_options = dict(customer.more_options or {})
    for option in business.settings.more_option_labels:
        key = option["label"].lower()
        if key in r:
            more_options[option["key"]] = r[key]
    customer.more_options = more_options
    group_name = r.get("group", "")
    if group_name:
        customer.group, _ = CustomerGroup.objects.get_or_create(
            business=business, name=group_name[:80])
