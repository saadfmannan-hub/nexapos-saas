"""Currency registry — symbols and standard decimal precision.

Nothing is hardcoded per business: businesses pick any code; this
registry only supplies sensible defaults (symbol + precision). Unknown
codes fall back to the code itself with the business-configured
precision.
"""

CURRENCIES = {
    "OMR": {"name": "Omani Rial", "symbol": "ر.ع.", "precision": 3},
    "USD": {"name": "US Dollar", "symbol": "$", "precision": 2},
    "EUR": {"name": "Euro", "symbol": "€", "precision": 2},
    "GBP": {"name": "British Pound", "symbol": "£", "precision": 2},
    "AED": {"name": "UAE Dirham", "symbol": "د.إ", "precision": 2},
    "SAR": {"name": "Saudi Riyal", "symbol": "﷼", "precision": 2},
    "QAR": {"name": "Qatari Riyal", "symbol": "ر.ق", "precision": 2},
    "KWD": {"name": "Kuwaiti Dinar", "symbol": "د.ك", "precision": 3},
    "BHD": {"name": "Bahraini Dinar", "symbol": "د.ب", "precision": 3},
    "INR": {"name": "Indian Rupee", "symbol": "₹", "precision": 2},
    "PKR": {"name": "Pakistani Rupee", "symbol": "₨", "precision": 2},
    "EGP": {"name": "Egyptian Pound", "symbol": "E£", "precision": 2},
    "KES": {"name": "Kenyan Shilling", "symbol": "KSh", "precision": 2},
    "NGN": {"name": "Nigerian Naira", "symbol": "₦", "precision": 2},
}


def currency_choices(current=""):
    """Choices for a currency select, keeping any nonstandard current code."""
    choices = [
        (code, f"{code} — {info['name']} ({info['precision']} dp)")
        for code, info in CURRENCIES.items()
    ]
    if current and current.upper() not in CURRENCIES:
        choices.append((current, current))
    return choices


def symbol_for(code, fallback=None):
    info = CURRENCIES.get((code or "").upper())
    return info["symbol"] if info else (fallback or code)


def precision_for(code, default=2):
    info = CURRENCIES.get((code or "").upper())
    return info["precision"] if info else default
