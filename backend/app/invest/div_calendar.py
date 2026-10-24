"""Dividend calendar: the next 12 months of dividends, declared ones from EasyEquities' blog and expected ones
guessed from when each company paid before (Yahoo's ~5 years of ex-dates plus the blog's history). A guess is only
a pattern: companies change, skip or move dividends, so the page says how sure each one is."""
import logging
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import utcnow
from .blog import MONTH_NAMES, BlogDividend, BlogSymbol, fx_rates, matcher, symbol_key
from .models import DividendHistory, InvestTxn, PriceCache

log = logging.getLogger(__name__)

NEAR = 21  # days apart (ignoring the year) for two payouts to count as the same one each year
SAME = 12  # a blog dividend and a Yahoo ex-date this close are the same payout
STOPPED = 470  # no payout in a slot for ~15 months: the company dropped or moved it
WARNING = ("Expected dates and amounts are guesses from past payouts. Companies change, skip, move or cut dividends, "
           "so follow their news and wait for the declaration before you buy for one.")


def weekday_before(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def on_weekday(d: date) -> date:
    return d if d.weekday() < 5 else d - timedelta(days=d.weekday() - 4)


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + n, day=28)


def apart(a: date, b: date) -> int:
    """Days between two dates ignoring the year (so 28 Dec and 3 Jan are 6 apart)."""
    x = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(x, 365 - x)


def slots(events):
    """Group payouts that come around the same time each year. events: dicts with ldt, sorted by date."""
    groups = []
    for e in events:
        fits = [g for g in groups if apart(g[-1]["ldt"], e["ldt"]) <= NEAR and (e["ldt"] - g[-1]["ldt"]).days > 200]
        if fits:
            min(fits, key=lambda g: apart(g[-1]["ldt"], e["ldt"])).append(e)
        else:
            groups.append([e])
    return groups


def merge(yahoo, blog):
    """One list of payouts: the blog's (exact last day to trade and pay date) over Yahoo's for the same payout."""
    out = list(blog)
    for y in yahoo:
        if not any(abs((y["ldt"] - b["ldt"]).days) <= SAME for b in blog):
            out.append(y)
    return sorted(out, key=lambda e: e["ldt"])


def predict(events, today: date, until: date, start: date):
    """Payouts per slot from `start` to `until`: the declared one if there is one, else the next expected date."""
    lags = [(e["pay_date"] - e["ldt"]).days for e in events if e.get("pay_date")]
    lag = int(median(lags)) if lags else None
    out = []
    for g in slots(events):
        last = g[-1]
        if last["ldt"] >= start:  # declared (or already paid this month)
            out.append({**last, "status": "paid" if (last.get("pay_date") or last["ldt"]) < today else "declared",
                        "slot": g})
            continue
        if (today - last["ldt"]).days > STOPPED:
            continue
        n = 1
        while add_years(last["ldt"], n) < start:
            n += 1
        when = on_weekday(add_years(last["ldt"], n))
        if when > until:
            continue
        out.append({"ldt": when, "amount": last["amount"], "currency": last["currency"],
                    "pay_date": when + timedelta(days=lag) if lag is not None else None,
                    "status": "late" if when < today else "expected", "slot": g})
    return out


def received(db: Session, user_id, since: date):
    """What this user was actually paid, per month: the income tracker's totals (dividends + interest, from their
    transactions and EasyEquities statements) and the dividend rows they recorded by name."""
    if user_id is None:
        return {}, {}
    from .portfolio import monthly

    try:
        m = monthly(db, user_id)
        totals = {mo: v for mo, v in zip(m["months"], m["income"]) if v and mo >= since.strftime("%Y-%m")}
    except Exception:
        db.rollback()
        log.exception("Income tracker failed for the dividend calendar")
        totals = {}
    rows = defaultdict(list)
    for t in db.scalars(select(InvestTxn).where(InvestTxn.user_id == user_id, InvestTxn.kind == "dividend",
                                                InvestTxn.date >= since).order_by(InvestTxn.date)):
        rows[t.date.strftime("%Y-%m")].append({"instrument": t.name or t.symbol, "symbol": t.symbol,
                                               "date": t.date.isoformat(), "amount": float(t.amount or 0)})
    return totals, rows


def view(db: Session, holdings, watch, user_id=None):
    today = utcnow().date()
    start = today.replace(day=1)
    until = add_years(start, 1) - timedelta(days=1)
    hist_start = date(today.year - 1, 1, 1)  # the history starts in January last year

    match = matcher([("hold", h.get("symbol"), h.get("name")) for h in holdings] +
                    [("watch", w.symbol, w.name) for w in watch])
    units = {h.get("symbol"): float(h.get("quantity") or 0) for h in holdings}
    symbols = dict(db.execute(select(BlogSymbol.key, BlogSymbol.symbol)).all())

    companies = {}  # key -> {"name", "account", "symbol", "blog": [], "yahoo": []}
    for d in db.scalars(select(BlogDividend).where(BlogDividend.ldt.is_not(None)).order_by(BlogDividend.ldt)):
        if d.amount is None:
            continue
        key = symbol_key(d.account, d.instrument)
        c = companies.setdefault(key, {"name": d.instrument, "account": d.account, "symbol": "", "blog": [], "yahoo": []})
        m = match(d.instrument)
        c["symbol"] = c["symbol"] or (m[1] if m else "") or symbols.get(key, "")
        if not any(e["ldt"] == d.ldt for e in c["blog"]):
            c["blog"].append({"ldt": d.ldt, "pay_date": d.pay_date, "amount": d.amount, "currency": d.currency})

    by_symbol = {}
    for key, c in list(companies.items()):
        first = by_symbol.setdefault(c["symbol"], c) if c["symbol"] else c
        if first is not c:  # the same company under two spellings in the blog
            first["blog"] = sorted(first["blog"] + [e for e in c["blog"] if all(e["ldt"] != f["ldt"] for f in first["blog"])],
                                   key=lambda e: e["ldt"])
            del companies[key]
    watched = {w.symbol for w in watch}
    for sym, name in [(h.get("symbol"), h.get("name")) for h in holdings] + [(w.symbol, w.name) for w in watch]:
        if sym and sym not in by_symbol:
            by_symbol[sym] = companies[f"sym|{sym}"] = {"name": name or sym, "account": "", "symbol": sym, "blog": [], "yahoo": []}

    wanted = list(by_symbol)
    history = {r.symbol: r.events for r in db.scalars(select(DividendHistory).where(DividendHistory.symbol.in_(wanted)))}
    cache = {r.symbol: r for r in db.scalars(select(PriceCache).where(PriceCache.symbol.in_(wanted)))}
    for sym, c in by_symbol.items():
        cur = (cache[sym].currency if sym in cache else "") or ("ZAR" if sym.endswith(".JO") else "USD")
        c["yahoo"] = [{"ldt": weekday_before(date.fromisoformat(d)), "pay_date": None, "amount": a, "currency": cur}
                      for d, a in history.get(sym, [])]

    found = []
    for c in companies.values():
        events = merge(c["yahoo"], c["blog"])
        if not events:
            continue
        for p in predict(events, today, until, start):
            found.append((c, p))
        for e in events:  # what was paid before this month, back to the start of last year
            if hist_start <= e["ldt"] < start:
                prev = next((x["amount"] for x in reversed(events) if 300 < (e["ldt"] - x["ldt"]).days < 430), None)
                found.append((c, {**e, "status": "paid", "slot": [], "prev": prev}))

    fx = fx_rates(db, {p["currency"] for _, p in found})
    months_out = defaultdict(list)
    for c, p in found:
        g = p.pop("slot")
        years = sorted({e["ldt"].year for e in g if (today - e["ldt"]).days < 4 * 366})
        prev = p["prev"] if "prev" in p else g[-2]["amount"] if len(g) > 1 and p["status"] in ("declared", "paid") else None
        past = p["ldt"] < start
        rate = fx.get((p["currency"] or "").upper())
        zar = round(p["amount"] * rate, 4) if rate and p["amount"] is not None else None
        m = match(c["name"])
        mine = ("hold" if c["symbol"] in units else "watch" if c["symbol"] in watched else m[0] if m else "")
        qty = units.get(c["symbol"]) if mine == "hold" and not past else None  # what they held back then isn't known
        expected = p["status"] in ("expected", "late")
        months_out[p["ldt"].strftime("%Y-%m")].append({
            "instrument": c["name"], "account": c["account"], "symbol": c["symbol"], "mine": mine,
            "status": p["status"], "ldt": p["ldt"].isoformat(),
            "pay_date": p["pay_date"].isoformat() if p.get("pay_date") else None,
            "amount": p["amount"], "currency": p["currency"],
            "amount_zar": zar if (p["currency"] or "").upper() != "ZAR" else None,
            "change": round(p["amount"] / prev - 1, 4) if prev and p["amount"] is not None else None,
            "confidence": None if not expected else "high" if len(years) >= 3 else "medium" if len(years) == 2 else "low",
            "seen": [f"{MONTH_NAMES[e['ldt'].month - 1]} {e['ldt'].year}" for e in g[-4:] if e["ldt"] < start],
            "units": qty, "estimate": round(qty * (zar if zar is not None else p["amount"]), 2) if qty and p["amount"] is not None else None})

    got, got_rows = received(db, user_id, hist_start)

    def month(d, period):
        key = d.strftime("%Y-%m")
        items = sorted(months_out.get(key, []), key=lambda x: (not x["mine"], x["ldt"]))
        return {"month": key, "label": f"{MONTH_NAMES[d.month - 1]} {d.year}", "period": period, "items": items,
                "income": round(sum(x["estimate"] or 0 for x in items), 2),
                "declared": sum(x["status"] in ("declared", "paid") for x in items),
                "received": round(got.get(key, 0.0), 2), "received_items": got_rows.get(key, [])}

    out, d = [], start
    while d <= until:
        out.append(month(d, "upcoming"))
        d = (d + timedelta(days=32)).replace(day=1)
    history, d = [], hist_start
    while d < start:
        history.append(month(d, "this_year" if d.year == today.year else "last_year"))
        d = (d + timedelta(days=32)).replace(day=1)
    this_year = sum(m["received"] for m in history + out[:1] if m["month"].startswith(str(today.year)))
    return {"months": out, "history": history, "warning": WARNING,
            "companies": len({x["instrument"] for m in out + history for x in m["items"]}),
            "income": round(sum(m["income"] for m in out), 2), "received_this_year": round(this_year, 2),
            "received_last_year": round(sum(m["received"] for m in history if m["period"] == "last_year"), 2)}
