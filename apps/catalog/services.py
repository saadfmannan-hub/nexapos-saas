"""Catalog defaults and helpers."""
from .models import Unit


DEFAULT_UNITS = [
    ("Piece", "pc", False),
    ("Box", "box", False),
    ("Pack", "pack", False),
    ("Set", "set", False),
    ("Kilogram", "kg", True),
    ("Gram", "g", True),
    ("Liter", "L", True),
    ("Milliliter", "ml", True),
    ("Meter", "m", True),
    ("Hour", "hr", True),
    ("Service", "svc", False),
]


def create_default_catalog(business):
    for name, abbr, dec in DEFAULT_UNITS:
        Unit.objects.get_or_create(
            business=business, name=name,
            defaults={"abbreviation": abbr, "allow_decimal": dec},
        )
