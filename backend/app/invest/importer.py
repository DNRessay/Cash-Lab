"""Transaction CSV import. Column names are matched loosely so exports from different brokers mostly
just work; the template shows the preferred layout."""
import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .models import ASSET_CLASSES, KINDS, InvestTxn

TEMPLATE = (
    "date,type,symbol,name,asset_class,quantity,price,amount,fees,notes\n"
    "2025-01-15,deposit,,,,,,30000,,Initial deposit\n"
    "2025-01-16,buy,GRT.JO,Growthpoint Properties,reit,1000,12.50,12500,25,\n"
    "2025-02-10,buy,STX40.JO,Satrix 40,etf,100,85.00,8500,15,\n"
    "2025-06-30,dividend,GRT.JO,Growthpoint Properties,reit,,,640,,\n"
)

COLUMNS = {
    "date": ("date", "trade date", "transaction date", "settlement date"),
    "kind": ("type", "action", "transaction type", "kind", "side"),
    "symbol": ("symbol", "ticker", "instrument code", "code", "share code"),
    "name": ("name", "instrument", "security", "company", "description", "comment"),
    "asset_class": ("asset_class", "asset class", "class"),
    "quantity": ("quantity", "qty", "shares", "units", "number of shares"),
    "price": ("price", "unit price", "share price", "price per share"),
    "amount": ("amount", "value", "total", "net amount", "consideration"),
    "fees": ("fees", "fee", "costs", "brokerage", "charges"),
    "notes": ("notes", "note", "reference"),
}
KIND_WORDS = {
    "buy": ("buy", "bought", "purchase"), "sell": ("sell", "sold", "sale"),
    "dividend": ("dividend", "distribution"), "deposit": ("deposit", "contribution", "funding"),
    "withdrawal": ("withdrawal", "withdraw"), "fee": ("fee", "charge", "vat"), "interest": ("interest",),
}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d %H:%M:%S")


def _num(v):
    v = re.sub(r"[R\s,]", "", str(v or ""))
    if not v or v == "-":
        return None
    try:
        return abs(Decimal(v))
    except InvalidOperation:
        return None


def _date(v):
    v = (v or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _kind(v):
    v = (v or "").strip().lower()
    return next((k for k, words in KIND_WORDS.items() if any(w in v for w in words)), None)


def parse(data, user_id):
    text = data.decode("utf-8-sig", errors="ignore") if isinstance(data, bytes) else data
    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower(): h for h in (reader.fieldnames or [])}
    col = {field: next((headers[a] for a in aliases if a in headers), None) for field, aliases in COLUMNS.items()}
    if not col["date"] or not (col["kind"] or col["name"]):
        raise ValueError("CSV needs at least a date column and a type column. Download the template to see the layout.")

    rows, errors = [], []
    for n, raw in enumerate(reader, start=2):
        get = lambda f: (raw.get(col[f]) or "").strip() if col[f] else ""
        day = _date(get("date"))
        kind = _kind(get("kind")) or _kind(get("name"))
        if not day or kind not in KINDS:
            errors.append(f"row {n}: couldn't read date/type")
            continue
        qty, price, amount = _num(get("quantity")), _num(get("price")), _num(get("amount"))
        if amount is None and qty is not None and price is not None:
            amount = (qty * price).quantize(Decimal("0.01"))
        if amount is None:
            errors.append(f"row {n}: no amount")
            continue
        symbol = get("symbol").upper()
        if kind in ("buy", "sell") and (not symbol or qty is None):
            errors.append(f"row {n}: {kind} needs a symbol and quantity")
            continue
        cls = get("asset_class").lower()
        rows.append(InvestTxn(
            user_id=user_id, date=day, kind=kind, symbol=symbol, name=get("name")[:200],
            asset_class=cls if cls in ASSET_CLASSES else ("reit" if "reit" in get("name").lower() else "share"),
            quantity=qty, price=price if price is not None else (amount / qty if qty else None),
            amount=amount, fees=_num(get("fees")) or Decimal(0), notes=get("notes")[:500], source="csv",
        ))
    return rows, errors
