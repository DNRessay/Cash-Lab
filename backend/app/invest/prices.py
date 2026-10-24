import bisect
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote as urlquote

import requests
from sqlalchemy.orm import Session

from ..models import utcnow
from .models import DividendHistory, PriceCache

log = logging.getLogger(__name__)

try:
    # Yahoo rate-limits plain Python TLS clients (429); a browser-like handshake gets through.
    from curl_cffi import requests as http
    HTTP_KW = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    http, HTTP_KW = requests, {}

CHART_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
TTL = timedelta(minutes=30)
# Yahoo quotes JSE shares in cents (ZAc); everything here is shown in rand.
MINOR_UNITS = {"ZAc": ("ZAR", Decimal("0.01")), "GBp": ("GBP", Decimal("0.01")), "ILA": ("ILS", Decimal("0.01"))}

BENCHMARKS = [
    ("STX40.JO", "Satrix 40 (JSE Top 40)"),
    ("STXPRO.JO", "Satrix Property"),
    ("ZAR=X", "US dollar"),
]


def fetch(symbol: str) -> dict:
    """One call: current price, name, ~5y of daily closes and dividends."""
    for host in CHART_HOSTS:
        r = http.get(f"https://{host}/v8/finance/chart/{urlquote(symbol)}", headers=HEADERS, timeout=15,
                     params={"range": "5y", "interval": "1d", "events": "div"}, **HTTP_KW)
        if r.status_code != 429:
            break
    r.raise_for_status()
    chart = r.json().get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"Unknown symbol {symbol!r}")
    res = chart["result"][0]
    meta = res.get("meta", {})
    currency, scale = MINOR_UNITS.get(meta.get("currency", ""), (meta.get("currency", ""), Decimal(1)))

    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    history = [
        [datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(), float(Decimal(str(c)) * scale)]
        for t, c in zip(res.get("timestamp") or [], closes) if c is not None
    ]
    price = meta.get("regularMarketPrice")
    price = Decimal(str(price)) * scale if price is not None else (Decimal(str(history[-1][1])) if history else None)

    year_ago = datetime.now(timezone.utc).timestamp() - 365 * 86400
    events = ((res.get("events") or {}).get("dividends") or {}).values()
    divs = sum(Decimal(str(d.get("amount", 0))) for d in events if d.get("date", 0) >= year_ago) * scale
    paid = sorted([datetime.fromtimestamp(d["date"], tz=timezone.utc).date().isoformat(), float(Decimal(str(d["amount"])) * scale)]
                  for d in events if d.get("date") and d.get("amount"))
    return {"name": meta.get("longName") or meta.get("shortName") or symbol, "currency": currency,
            "price": price, "history": history, "dividends_12m": divs, "dividends": paid}


def quote(db: Session, symbol: str, force=False):
    """Cached quote; refreshes when older than TTL. Returns the PriceCache row (may carry an error) or None."""
    symbol = symbol.strip().upper()
    if not symbol:
        return None
    row = db.get(PriceCache, symbol)
    if row and not force and row.price is not None and utcnow() - row.fetched_at < TTL:
        return row
    try:
        data = fetch(symbol)
    except Exception as e:
        log.warning("Price fetch failed for %s: %s", symbol, e)
        if row:
            row.error = str(e)[:500]
            db.commit()
        return row  # stale is better than nothing
    row = row or PriceCache(symbol=symbol)
    row.name, row.currency, row.price = data["name"][:200], data["currency"], data["price"]
    row.history, row.dividends_12m, row.error, row.fetched_at = data["history"], data["dividends_12m"], "", utcnow()
    db.add(row)
    if "dividends" in data:
        divs = db.get(DividendHistory, symbol) or DividendHistory(symbol=symbol)
        divs.events, divs.fetched_at = data["dividends"], utcnow()
        db.add(divs)
    db.commit()
    return row


def price_on(history, day: date):
    """Close on or before `day` (first close if day is before the history starts)."""
    if not history:
        return None
    dates = [h[0] for h in history]
    i = bisect.bisect_right(dates, day.isoformat()) - 1
    return history[max(i, 0)][1]


def change(row: PriceCache, days: int):
    if not row or row.price is None or not row.history:
        return None
    past = price_on(row.history, date.today() - timedelta(days=days))
    return (float(row.price) / past - 1) if past else None


def stats(row: PriceCache):
    """What the watchlist shows for a symbol."""
    if not row or row.price is None:
        return {"price": None, "error": row.error if row else "No data"}
    year = [h[1] for h in row.history if h[0] >= (date.today() - timedelta(days=365)).isoformat()]
    price = float(row.price)
    return {
        "name": row.name, "currency": row.currency, "price": price,
        "change_1d": _prev_change(row), "change_1m": change(row, 30), "change_1y": change(row, 365),
        "high_52w": max(year) if year else None, "low_52w": min(year) if year else None,
        "dividend_yield": float(row.dividends_12m) / price if price and row.dividends_12m else None,
        "as_of": row.fetched_at, "error": row.error or None,
        "spark": spark(row.history),
    }


def spark(history, days=365, points=52):
    """About weekly closes over the last year, for a sparkline."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    closes = [h[1] for h in history or [] if h[0] >= cutoff]
    if len(closes) <= points:
        return [round(c, 4) for c in closes]
    step = len(closes) / points
    picked = [closes[int(i * step)] for i in range(points)]
    return [round(c, 4) for c in picked[:-1] + [closes[-1]]]


def _prev_change(row):
    closes = [h[1] for h in row.history if h[0] < date.today().isoformat()]
    return float(row.price) / closes[-1] - 1 if closes else None
