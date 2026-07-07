from decimal import Decimal

from django import template
from django.utils.safestring import mark_safe

register = template.Library()

NUMERIC_TOKENS = (
    "amount", "bank", "balance", "card", "cash", "cost", "credit",
    "discount", "gross", "margin", "paid", "payable", "profit", "qty",
    "quantity", "receivable", "refund", "returned", "sales", "taxable",
    "total", "transactions", "unit price", "vat", "value",
)
TEXT_NUMERIC_EXCLUSIONS = ("customer credit", "credit / receivable", "credit limit")
NOWRAP_TOKENS = ("date", "invoice", "number", "no", "phone", "sku", "status")
DAILY_SALES_COLUMNS = (
    "date", "invoice no", "sales amount", "bank transfer", "card", "cash",
    "credit / receivable", "discount", "vat", "gross",
)
DAILY_SALES_WIDTHS = (13, 13, 10, 10, 8, 8, 12, 8, 8, 10)


def _column(columns, index):
    try:
        return str(columns[int(index)])
    except (TypeError, ValueError, IndexError):
        return ""


def _is_numeric(label):
    text = label.lower()
    if text in TEXT_NUMERIC_EXCLUSIONS:
        return True
    return any(token in text for token in NUMERIC_TOKENS)


def _is_nowrap(label):
    text = label.lower()
    return any(token in text for token in NOWRAP_TOKENS)


def _column_weight(label):
    text = label.lower()
    if "product" in text or "customer" in text or "reason" in text:
        return Decimal("1.55")
    if "invoice" in text or "phone" in text or "bank transfer" in text:
        return Decimal("1.20")
    if "date" in text or "status" in text or "method" in text:
        return Decimal("1.05")
    if _is_numeric(label):
        return Decimal("0.90")
    return Decimal("1.00")


def _format_width(value):
    value = value.quantize(Decimal("0.01"))
    if value == value.to_integral():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


@register.filter
def report_cell(value):
    return "-" if value is None or value == "" else value


@register.filter
def report_cell_class(columns, index):
    label = _column(columns, index)
    classes = []
    if _is_numeric(label):
        classes.append("text-end")
        classes.append("report-num")
    if _is_nowrap(label):
        classes.append("text-nowrap")
    return " ".join(classes)


@register.filter
def report_pdf_class(columns, index):
    label = _column(columns, index)
    return "nowrap" if _is_nowrap(label) else ""


@register.filter
def report_pdf_style(columns, index):
    label = _column(columns, index)
    normalized = tuple(str(col).lower() for col in columns)
    try:
        idx = int(index)
    except (TypeError, ValueError):
        idx = 0

    if normalized == DAILY_SALES_COLUMNS and idx < len(DAILY_SALES_WIDTHS):
        width = str(DAILY_SALES_WIDTHS[idx])
    else:
        weights = [_column_weight(str(col)) for col in columns]
        total = sum(weights, Decimal("0")) or Decimal("1")
        weight = weights[idx] if 0 <= idx < len(weights) else Decimal("1")
        width = _format_width(weight / total * Decimal("100"))

    align = "right" if _is_numeric(label) else "left"
    return mark_safe(f"width:{width}%; text-align:{align};")
