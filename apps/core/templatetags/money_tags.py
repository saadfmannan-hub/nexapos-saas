"""Money formatting template tags.

Usage:
    {% load money_tags %}
    {% money sale.total %}          -> "1,234.50"  (business precision)
    {% money sale.total symbol=1 %} -> "1,234.50 $"
    {{ value|money_p:3 }}           -> "0.005"     (explicit precision)

Guarantees:
- Always Decimal-quantized to the business's currency precision
- Never shows raw storage decimals (61.425000000000000)
- Never shows negative zero (-0.000)
- Thousands separators for readability
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import template

register = template.Library()


def _format(value, precision):
    try:
        d = Decimal(str(value if value is not None and value != "" else 0))
    except (InvalidOperation, ValueError, TypeError):
        return value
    precision = max(0, min(int(precision), 3))
    q = Decimal(1).scaleb(-precision)
    d = d.quantize(q, rounding=ROUND_HALF_UP)
    if d == 0:
        d = abs(d)  # normalize -0.000 -> 0.000
    return f"{d:,.{precision}f}"


@register.simple_tag(takes_context=True)
def money(context, value, symbol=False):
    business = context.get("current_business")
    precision = business.currency_precision if business else 3
    text = _format(value, precision)
    if symbol and business:
        return f"{text} {business.currency_display}"
    return text


@register.filter
def money_p(value, precision=3):
    return _format(value, precision)
