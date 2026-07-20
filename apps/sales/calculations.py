"""Commercial sales calculation engine.

This module is the source of truth for sale line totals, invoice discounts,
VAT, display-precision rounding, and payment totals. Views may mirror this
logic for cashier feedback, but persisted sale snapshots must come from here.
"""
from decimal import Decimal

from apps.core.money import D, money, round_to_precision

ZERO = Decimal("0")


class CalculationError(ValueError):
    pass


def resolve_tax_rate(business, product, explicit_rate=None):
    """Return the effective rate for a product on a sale.

    The business VAT switch is the master control. When it is disabled every
    new line is untaxed, even if the product or a stale cart carries a rate.
    For VAT-enabled businesses, an explicit/product rate still wins and the
    configured business percentage remains the fallback.
    """
    settings_obj = business.settings if business is not None else None
    if settings_obj is not None and not settings_obj.vat_enabled:
        return ZERO
    if explicit_rate is not None:
        return D(explicit_rate)
    if product.is_meter_tailoring:
        if settings_obj and settings_obj.vat_enabled:
            return D(settings_obj.vat_percentage)
        return ZERO
    product_rate = D(product.effective_tax_rate())
    if product_rate > 0:
        return product_rate
    if settings_obj and settings_obj.vat_enabled:
        return D(settings_obj.vat_percentage)
    return ZERO


def _price_includes_tax(business, product, prices_include_tax=None):
    if product.is_meter_tailoring:
        if prices_include_tax is not None:
            return prices_include_tax
        return business.settings.prices_include_tax if business is not None else False
    if product.price_includes_tax is not None:
        return product.price_includes_tax
    if prices_include_tax is not None:
        return prices_include_tax
    return business.settings.prices_include_tax if business is not None else False


def compute_line(
    product,
    variant,
    quantity,
    unit_price,
    discount_amount,
    *,
    business=None,
    prices_include_tax=None,
    tax_rate=None,
    invoice_discount_share=ZERO,
):
    """Compute one line after item and allocated invoice discounts."""
    quantity = D(quantity)
    unit_price = money(unit_price)
    discount_amount = money(discount_amount)
    invoice_discount_share = money(invoice_discount_share)
    rate = resolve_tax_rate(business, product, tax_rate)

    gross_after_item_discount = money(unit_price * quantity - discount_amount)
    if gross_after_item_discount < 0:
        raise CalculationError("Line discount cannot exceed the line amount.")

    if rate > 0 and _price_includes_tax(business, product, prices_include_tax):
        base_before_invoice_discount = money(
            gross_after_item_discount / (1 + rate / 100)
        )
    else:
        base_before_invoice_discount = gross_after_item_discount

    if invoice_discount_share < 0:
        raise CalculationError("Invoice discount cannot be negative.")
    if invoice_discount_share > base_before_invoice_discount:
        raise CalculationError("Invoice discount cannot exceed the taxable line amount.")

    taxable_base = money(base_before_invoice_discount - invoice_discount_share)
    tax = money(taxable_base * rate / 100) if rate > 0 else ZERO
    return {
        "quantity": quantity,
        "unit_price": unit_price,
        "discount_amount": discount_amount,
        "invoice_discount_share": invoice_discount_share,
        "tax_rate": rate,
        "base_before_invoice_discount": base_before_invoice_discount,
        "base": taxable_base,
        "tax": tax,
        "total": money(taxable_base + tax),
    }


def _allocate_discount(lines, invoice_discount):
    invoice_discount = money(invoice_discount)
    subtotal = sum((line["base_before_invoice_discount"] for line in lines), ZERO)
    if invoice_discount < 0:
        raise CalculationError("Invoice discount cannot be negative.")
    if invoice_discount > subtotal:
        raise CalculationError("Invoice discount cannot exceed the sale subtotal.")
    if invoice_discount == 0 or subtotal == 0:
        return [ZERO for _line in lines]

    shares = []
    allocated = ZERO
    last_index = len(lines) - 1
    for index, line in enumerate(lines):
        if index == last_index:
            share = money(invoice_discount - allocated)
        else:
            share = money(invoice_discount * line["base_before_invoice_discount"] / subtotal)
            allocated += share
        shares.append(share)
    return shares


def calculate_sale_totals(
    *,
    business,
    items,
    invoice_discount=ZERO,
    currency_precision=None,
):
    """Return immutable line snapshots and sale totals."""
    settings_obj = business.settings
    first_pass = []
    line_discounts = ZERO
    for line in items:
        parts = compute_line(
            line["product"],
            line.get("variant"),
            line["quantity"],
            line["unit_price"],
            line.get("discount_amount", ZERO),
            business=business,
            prices_include_tax=settings_obj.prices_include_tax,
        )
        first_pass.append((line, parts))
        line_discounts += parts["discount_amount"]

    shares = _allocate_discount(
        [parts for _line, parts in first_pass],
        invoice_discount,
    )
    computed = []
    subtotal = tax_total = grand_total = ZERO
    for (line, initial), share in zip(first_pass, shares, strict=True):
        parts = compute_line(
            line["product"],
            line.get("variant"),
            line["quantity"],
            line["unit_price"],
            line.get("discount_amount", ZERO),
            business=business,
            prices_include_tax=settings_obj.prices_include_tax,
            tax_rate=initial["tax_rate"],
            invoice_discount_share=share,
        )
        computed.append((line, parts))
        subtotal += initial["base_before_invoice_discount"]
        tax_total += parts["tax"]
        grand_total += parts["total"]

    raw_total = money(grand_total)
    if settings_obj.price_rounding == "nearest":
        precision_total = money(
            round_to_precision(
                raw_total,
                business.currency_precision if currency_precision is None else currency_precision,
            )
        )
    else:
        precision_total = raw_total

    return {
        "lines": computed,
        "subtotal": money(subtotal),
        "line_discounts": money(line_discounts),
        "invoice_discount": money(invoice_discount),
        "discount_total": money(line_discounts + money(invoice_discount)),
        "tax_total": money(tax_total),
        "raw_total": raw_total,
        "total": precision_total,
        "rounding": money(precision_total - raw_total),
    }


def calculate_payment_totals(payments, payment_method_kind):
    totals = {
        "pay_total": ZERO,
        "credit_amount": ZERO,
        "store_credit_amount": ZERO,
        "cash_tendered": ZERO,
    }
    clean_payments = []
    for payment in payments:
        amount = money(payment["amount"])
        if amount <= 0:
            raise CalculationError("Payment amounts must be positive.")
        method = payment["method"]
        kind = payment_method_kind(method)
        if kind == "customer_credit":
            totals["credit_amount"] += amount
        elif kind == "store_credit":
            totals["store_credit_amount"] += amount
        elif kind == "cash":
            totals["cash_tendered"] += amount
        totals["pay_total"] += amount
        clean_payments.append({
            "method": method,
            "amount": amount,
            "reference": payment.get("reference", ""),
        })
    return clean_payments, {key: money(value) for key, value in totals.items()}
