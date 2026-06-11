"""Decimal helpers — money and quantity are ALWAYS Decimal, never float."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")

# Storage precision: all money columns store 3 decimal places so that
# 3-dp currencies (e.g. OMR, KWD, BHD) round-trip exactly. Businesses
# configure their own display precision (0–3).
MONEY_PLACES = Decimal("0.001")
QTY_PLACES = Decimal("0.001")


def D(value, default=ZERO):
    """Coerce any input (str, int, Decimal, None) safely to Decimal."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # Floats are never trusted for money; go through str().
        value = repr(value)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def money(value):
    return D(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def qty(value):
    return D(value).quantize(QTY_PLACES, rounding=ROUND_HALF_UP)


def round_to_precision(value, places: int):
    places = max(0, min(int(places), 3))
    q = Decimal(1).scaleb(-places)
    return D(value).quantize(q, rounding=ROUND_HALF_UP)
