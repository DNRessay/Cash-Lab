import re

AMOUNT = r"-?\d{1,3}(?:,\d{3})*(?:\.\d{2})?"


def to_float(s, default=0.0):
    if s is None:
        return default
    s = str(s).replace(",", "").replace("R", "").replace(" ", "").strip()
    if not s or s == "-":
        return default
    try:
        return float(s)
    except ValueError:
        return default


def make_txn(date, description, amount, kind, prefix, index, **extra):
    """Normalised row every PDF parser returns."""
    return {
        "date": date,
        "description": re.sub(r"\s+", " ", description).strip(),
        "amount": abs(amount),
        "type": kind,
        "reference": f"{prefix}-{date:%Y%m%d}-{index}",
        **extra,
    }
