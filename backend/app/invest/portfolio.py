import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import prices
from .ee.models import EEConnection
from .ee.sync import platform_view, rand_rate, statement_rows
from .models import InvestTxn, ManualPrice, PortfolioSnapshot, PropertyAsset

log = logging.getLogger(__name__)

ZERO = Decimal(0)


def _f(v):
    return float(v) if v is not None else None


def holdings(txns):
    """Average-cost positions per symbol from buy/sell/dividend rows (oldest first)."""
    pos = defaultdict(lambda: {"quantity": ZERO, "cost": ZERO, "realised": ZERO, "dividends": ZERO,
                               "name": "", "asset_class": "share", "last_price": None})
    for t in txns:
        if not t.symbol:
            continue
        p = pos[t.symbol]
        p["name"] = p["name"] or t.name
        if t.kind == "buy":  # sells/dividends default to "share"; buys say what it really is
            p["asset_class"] = t.asset_class or p["asset_class"]
        qty = Decimal(t.quantity or 0)
        if t.kind == "buy":
            p["quantity"] += qty
            p["cost"] += Decimal(t.amount) + Decimal(t.fees or 0)
            p["last_price"] = t.price or p["last_price"]
        elif t.kind == "sell" and p["quantity"] > 0:
            share = min(qty / p["quantity"], Decimal(1))
            cost_out = p["cost"] * share
            p["realised"] += Decimal(t.amount) - Decimal(t.fees or 0) - cost_out
            p["cost"] -= cost_out
            p["quantity"] -= qty
            p["last_price"] = t.price or p["last_price"]
        elif t.kind == "dividend":
            p["dividends"] += Decimal(t.amount)
    return pos


def cash_balance(txns):
    """Only meaningful if deposits are recorded; otherwise None."""
    if not any(t.kind == "deposit" for t in txns):
        return None
    bal = ZERO
    for t in txns:
        amt, fees = Decimal(t.amount), Decimal(t.fees or 0)
        if t.kind in ("deposit", "sell", "dividend", "interest"):
            bal += amt - (fees if t.kind == "sell" else 0)
        elif t.kind in ("withdrawal", "fee"):
            bal -= amt
        elif t.kind == "buy":
            bal -= amt + fees
    return bal


def contributions(txns):
    """Money that went in (+) or came out (-), dated. Deposits if recorded, else buys/sells."""
    if any(t.kind == "deposit" for t in txns):
        return [(t.date, Decimal(t.amount) if t.kind == "deposit" else -Decimal(t.amount))
                for t in txns if t.kind in ("deposit", "withdrawal")]
    return [(t.date, Decimal(t.amount) + Decimal(t.fees or 0) if t.kind == "buy" else -Decimal(t.amount))
            for t in txns if t.kind in ("buy", "sell")]


def month_flows(db: Session, user_id: int):
    """Net money in (+) or out (-) per month across all accounts, in rand, from the monthly statements'
    Contributions and Withdrawals rows (dated at month end). Moves between your own accounts cancel out."""
    import calendar

    from .ee.reader import month_series

    net, rates = defaultdict(float), {}
    for (account, currency, month), kinds in month_series(db, user_id).items():
        rate = rates.setdefault(currency, rand_rate(db, currency) or 0.0)
        net[month] += (abs(kinds.get("money_in", 0.0)) - abs(kinds.get("money_out", 0.0))) * rate
    out = []
    for month, v in sorted(net.items()):
        y, m = int(month[:4]), int(month[5:7])
        if round(v, 2):
            out.append((date(y, m, calendar.monthrange(y, m)[1]), Decimal(str(round(v, 2)))))
    return out


def money_flows(txns, statement, months=()):
    """(dated money in/out, whether real deposits are known). EasyEquities' own records have every deposit AND
    withdrawal, so they win over emails (which can miss withdrawals): statement lines if they carry deposits,
    else the monthly statements' Contributions/Withdrawals. Hand-entered ones are added."""
    stmt = statement_flows(statement) or list(months)
    if stmt:
        manual = [(t.date, Decimal(t.amount) if t.kind == "deposit" else -Decimal(t.amount))
                  for t in txns if t.source != "easyequities" and t.kind in ("deposit", "withdrawal")]
        return sorted(stmt + manual), True
    return contributions(txns), any(t.kind == "deposit" for t in txns)


def what_if(db: Session, flows, symbol):
    """Value today had each contribution bought `symbol` on the same day (price only, no dividends)."""
    row = prices.quote(db, symbol)
    if not row or row.price is None or not row.history or not flows:
        return None
    units = 0.0
    for day, amt in flows:
        p = prices.price_on(row.history, day)
        if p:
            units += float(amt) / p
    return units * float(row.price)


def summary(db: Session, user_id: int):
    txns = list(db.scalars(select(InvestTxn).where(InvestTxn.user_id == user_id)
                           .order_by(InvestTxn.date, InvestTxn.id)))
    manual = {m.symbol: m for m in db.scalars(select(ManualPrice).where(ManualPrice.user_id == user_id))}
    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user_id))
    ee_view, ee_prices = platform_view(db, conn) if conn and conn.snapshot else (None, {})

    # EasyEquities' own holdings are the truth for whatever is in those accounts; transactions fill in the rest.
    ee_rows = [h for a in (ee_view or {}).get("accounts", []) for h in a["holdings"]]
    ee_symbols = {h["symbol"] for h in ee_rows}
    ee_names = {(h["name"] or "").strip().lower() for h in ee_rows}
    taken = (ee_view or {}).get("taken_at") or ""

    rows, total_value, total_cost = [], 0.0, 0.0
    # With EasyEquities connected, its live holdings are the truth: trades read from emails are history only
    # (shares can leave through schemes, delistings or bundle changes without a sell email). Manual entries still count.
    txn_pos = holdings([t for t in txns if t.source != "easyequities"] if ee_view else txns)
    extra_pos = holdings(txns) if ee_view else txn_pos  # dividends/realised gains per symbol, for the EE rows
    for h in ee_rows:
        extra = extra_pos.get(h["symbol"], {})
        qty = h.get("shares")
        rows.append({
            "symbol": h["symbol"], "name": h["name"], "asset_class": h["asset_class"], "account": h["account"],
            "quantity": qty, "avg_cost": h["cost_zar"] / qty if qty else None, "cost": h["cost_zar"],
            "price": h["price_zar"], "price_source": f"EasyEquities ({taken[:10]})", "value": h["value_zar"],
            "gain": h["gain_zar"], "gain_pct": (h["value_zar"] / h["cost_zar"] - 1) if h["cost_zar"] else None,
            "realised": float(extra.get("realised", 0)), "dividends": float(extra.get("dividends", 0)),
            "rental_yield": h.get("rental_yield"), "rental_income": h.get("rental_income"),
        })
        total_value += h["value_zar"]
        total_cost += h["cost_zar"]
    for symbol, p in txn_pos.items():
        if symbol in ee_symbols or (p["name"] or "").strip().lower() in ee_names:
            continue  # already in EasyEquities' own holdings (matched by code or by name)
        if p["quantity"] <= Decimal("0.000001") and not p["realised"] and not p["dividends"]:
            continue
        source, price = "none", None
        if symbol in manual:
            price, source = float(manual[symbol].price), f"manual ({manual[symbol].as_of})"
        elif symbol in ee_prices:
            price, source = ee_prices[symbol], f"EasyEquities ({ee_view['taken_at'][:10]})"
        elif not symbol.startswith("EE:"):  # EE: symbols (EasyProperties etc.) aren't on Yahoo
            q = prices.quote(db, symbol)
            if q and q.price is not None:
                price, source = float(q.price), "market"
                p["name"] = p["name"] or q.name
        if price is None and p["last_price"] is not None:
            price, source = float(p["last_price"]), "last trade"
        qty, cost = float(p["quantity"]), float(p["cost"])
        value = qty * price if price is not None else 0.0
        total_value += value
        total_cost += cost
        rows.append({
            "symbol": symbol, "name": p["name"], "asset_class": p["asset_class"], "account": "", "quantity": qty,
            "avg_cost": cost / qty if qty else None, "cost": cost, "price": price, "price_source": source,
            "value": value, "gain": value - cost if qty else 0.0, "gain_pct": (value / cost - 1) if cost else None,
            "realised": float(p["realised"]), "dividends": float(p["dividends"]),
        })
    rows.sort(key=lambda r: -r["value"])

    statement = statement_rows(db, conn) if ee_view else []
    income = statement_income(db, statement)
    flows, has_deposits = money_flows(txns, statement, month_flows(db, user_id) if conn else ())
    if ee_view:
        # Cash sitting in EasyEquities wallets, straight from EasyEquities.
        cash = Decimal(str(round(sum(a["cash_zar"] for a in ee_view["accounts"]), 2)))
    else:
        cash = cash_balance(txns)
    invested = float(sum((a for _, a in flows), ZERO))
    portfolio_value = total_value + (float(cash) if cash is not None else 0.0)
    if ee_view and not has_deposits:
        # No deposit history (Gmail not connected): measure against what the holdings cost, cash counts as put in.
        invested = total_cost + float(cash or 0)
        received = 0.0
    else:
        # Without a cash record, sells already reduce `invested`; only dividends left the portfolio as cash.
        received = sum(r["dividends"] for r in rows) if cash is None else 0.0
    # Return is measured on the money still in (deposits less withdrawals); gross deposits only if that's not positive.
    gross = float(sum((a for _, a in flows if a > 0), ZERO)) if has_deposits or not ee_view else invested
    put_in = invested if invested > 0 else gross
    benchmarks = []
    for symbol, label in prices.BENCHMARKS:
        v = what_if(db, flows, symbol)
        if v is not None:
            benchmarks.append({"symbol": symbol, "label": label, "value": v,
                               "gain": v - invested, "return_pct": (v - invested) / put_in if put_in > 0 else None})

    allocation = defaultdict(float)
    for r in rows:
        allocation[r["asset_class"]] += r["value"]
    if cash and cash > 0:
        allocation["cash"] += float(cash)

    props = [property_view(p) for p in db.scalars(select(PropertyAsset).where(PropertyAsset.user_id == user_id)
                                                  .order_by(PropertyAsset.name))]
    physical_equity = sum(p["equity"] for p in props)
    ep_value = sum(r["value"] for r in rows if r["asset_class"] == "easyproperties")
    from ..banking.reader import position

    bank = position(db, user_id)
    net_worth = portfolio_value + physical_equity + bank["cash"] - bank["debt"]
    tracked = rows or props or txns or bank["accounts"] or bank["liabilities"]
    history = record_snapshot(db, user_id, portfolio_value, invested, net_worth) if tracked else []
    return {
        "history": history,
        "statement_history": statement_history(db, user_id) if conn else [],
        "holdings": rows,
        "cash": _f(cash),
        "invested": invested,
        "value": portfolio_value,
        "gain": portfolio_value + received - invested,
        "return_pct": (portfolio_value + received - invested) / put_in if put_in > 0 else None,
        "since": min([t.date for t in txns] + [d for d, _ in flows]).isoformat() if txns or flows else None,
        "income": income,
        "benchmarks": benchmarks,
        "allocation": {k: v for k, v in sorted(allocation.items(), key=lambda kv: -kv[1]) if v},
        "properties": props,
        # EasyProperties is property too; it's already inside the portfolio value, so net worth adds only own property.
        "property_equity": physical_equity + ep_value,
        "easyproperties_value": ep_value,
        "net_worth": net_worth,
        "blog": _blog(db, user_id, rows),
        # Real net worth = investments + own property equity + bank balances - debts. Fees already came out of balances.
        "banking": {"cash": bank["cash"], "debt": bank["debt"], "fees_12m": bank["fees_12m"],
                    "accounts": len(bank["accounts"])},
        "easyequities": {"value": ee_view["value_zar"], "taken_at": ee_view["taken_at"],
                         "accounts": [{k: a[k] for k in ("name", "currency", "value", "value_zar", "cash_zar", "cost_zar",
                                                         "warnings")} | {"holdings": len(a["holdings"])}
                                      for a in ee_view["accounts"]]}
        if ee_view else None,
    }


def _blog(db: Session, user_id: int, rows):
    from .blog import view
    from .models import WatchItem

    try:
        return view(db, rows, list(db.scalars(select(WatchItem).where(WatchItem.user_id == user_id))))
    except Exception:
        log.exception("Blog view failed")
        return {"posts": [], "upcoming": []}


def statement_history(db: Session, user_id: int):
    """Month-end worth (sum of every account's closing value) and money put in, from the monthly statements."""
    from .ee.reader import month_series

    by_month, rates = defaultdict(lambda: {"value": 0.0, "net_in": 0.0, "opening": 0.0}), {}
    for (account, currency, month), kinds in month_series(db, user_id).items():
        rate = rates.setdefault(currency, rand_rate(db, currency) or 0.0)
        cell = by_month[month]
        cell["value"] += (kinds.get("closing") or 0.0) * rate
        cell["opening"] += (kinds.get("opening") or 0.0) * rate
        cell["net_in"] += (abs(kinds.get("money_in", 0.0)) - abs(kinds.get("money_out", 0.0))) * rate
    out, invested = [], None
    for month in sorted(by_month):
        cell = by_month[month]
        invested = (cell["opening"] if invested is None else invested) + cell["net_in"]
        out.append({"month": month, "value": round(cell["value"], 2), "invested": round(invested, 2)})
    return out


def record_snapshot(db: Session, user_id: int, value, invested, net_worth):
    """Keep today's numbers (last write of the day wins) and return the history for the chart."""
    today = date.today()
    row = db.scalar(select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id, PortfolioSnapshot.date == today))
    if not row:
        row = PortfolioSnapshot(user_id=user_id, date=today)
        db.add(row)
    row.value, row.invested, row.net_worth = round(value, 2), round(invested, 2), round(net_worth, 2)
    db.commit()
    rows = db.scalars(select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id)
                      .order_by(PortfolioSnapshot.date.desc()).limit(730))
    return [{"date": r.date.isoformat(), "value": float(r.value), "invested": float(r.invested),
             "net_worth": float(r.net_worth)} for r in reversed(list(rows))]


def _day(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def statement_flows(statement):
    """Dated money in (+) / out (-) from EasyEquities statement lines. Foreign wallets are funded from the rand one."""
    out = []
    for r in statement:
        d = _day(r["date"])
        if r["category"] not in ("deposit", "withdrawal") or r["amount"] is None or not d or r["currency"] != "ZAR":
            continue
        amt = abs(Decimal(str(r["amount"])))
        out.append((d, amt if r["category"] == "deposit" else -amt))
    return sorted(out)


def statement_income(db: Session, statement):
    """All-time dividends, interest, fees and tax from the statement, in rand (fees/tax as amounts paid)."""
    totals = defaultdict(float)
    for r in statement:
        if r["category"] in ("dividend", "interest", "fee", "tax") and r["amount"] is not None:
            rate = rand_rate(db, r["currency"]) or 0.0
            totals[r["category"]] += abs(r["amount"]) * rate
    return {k: round(totals[k], 5) for k in ("dividend", "interest", "fee", "tax")} if statement else None


def property_view(p: PropertyAsset):
    val, bond = float(p.valuation or 0), float(p.bond_balance or 0)
    rent, costs, bond_pay = float(p.monthly_rent or 0), float(p.monthly_costs or 0), float(p.monthly_bond_payment or 0)
    buy = float(p.purchase_price or 0)
    years = ((date.today() - p.purchase_date).days / 365.25) if p.purchase_date else None
    growth = (val / buy - 1) if buy and val else None
    return {
        "id": p.id, "name": p.name, "kind": p.kind, "purchase_date": p.purchase_date, "purchase_price": buy,
        "valuation": val, "valuation_date": p.valuation_date, "bond_balance": bond,
        "monthly_bond_payment": bond_pay, "monthly_rent": rent, "monthly_costs": costs, "notes": p.notes,
        "equity": val - bond,
        "loan_to_value": bond / val if val else None,
        "gross_yield": rent * 12 / val if val and rent else None,
        "net_yield": (rent - costs) * 12 / val if val and rent else None,
        "monthly_cashflow": rent - costs - bond_pay,
        "growth": growth,
        "growth_per_year": ((1 + growth) ** (1 / years) - 1) if growth is not None and years and years >= 1 else None,
    }


def record_all(db: Session):
    """Nightly: compute (and so snapshot) every user's portfolio, so the chart grows even on days nobody looks."""
    from ..models import User

    done = 0
    for uid in db.scalars(select(User.id).where(User.is_active.is_(True))):
        try:
            summary(db, uid)
            done += 1
        except Exception:
            db.rollback()
    return done


def _month(d):
    return d.isoformat()[:7] if d else None


def monthly(db: Session, user_id: int):
    """Per-month money in/out, buys/sells and income/costs, for the charts. All in rand."""
    txns = list(db.scalars(select(InvestTxn).where(InvestTxn.user_id == user_id)))
    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user_id))
    statement = statement_rows(db, conn) if conn and conn.snapshot else []
    series = defaultdict(lambda: defaultdict(float))

    has_deposits = any(t.kind == "deposit" for t in txns)
    for t in txns:
        m, amt = _month(t.date), float(t.amount or 0)
        if t.kind == "deposit":
            series["money_in"][m] += amt
        elif t.kind == "withdrawal":
            series["money_out"][m] += amt
        elif t.kind == "buy":
            series["buys"][m] += amt + float(t.fees or 0)
        elif t.kind == "sell":
            series["sells"][m] += amt
        elif t.kind in ("dividend", "interest"):
            series["income"][m] += amt
        elif t.kind == "fee":
            series["costs"][m] += amt
        if t.kind in ("buy", "sell") and t.fees:
            series["costs"][m] += float(t.fees)

    rates = {}
    for r in statement:
        d, amt = _day(r["date"]), r["amount"]
        if not d or amt is None:
            continue
        if r["currency"] not in rates:
            rates[r["currency"]] = rand_rate(db, r["currency"]) or 0.0
        m, v = _month(d), abs(amt) * rates[r["currency"]]
        if r["category"] in ("dividend", "interest"):
            series["income"][m] += v
        elif r["category"] in ("fee", "tax"):
            series["costs"][m] += v
        elif r["category"] in ("deposit", "withdrawal") and not has_deposits and r["currency"] == "ZAR":
            series["money_in" if r["category"] == "deposit" else "money_out"][m] += v

    # Months covered by the monthly statements use the statement's own figures (whole history, all accounts).
    from .ee.reader import month_series

    stmt = defaultdict(lambda: defaultdict(float))
    for (account, currency, month), kinds in month_series(db, user_id).items():
        rate = rates.setdefault(currency, rand_rate(db, currency) or 0.0)
        for kind in ("money_in", "money_out", "income", "costs"):
            if kind in kinds:
                stmt[kind][month] += abs(kinds[kind]) * rate
    covered = {m for s in stmt.values() for m in s}
    for kind in ("money_in", "money_out", "income", "costs"):
        for m in covered:
            series[kind][m] = stmt[kind].get(m, 0.0)

    used = sorted({m for s in series.values() for m, v in s.items() if m and v})
    if not used:
        return {"months": [], **{k: [] for k in ("money_in", "money_out", "buys", "sells", "income", "costs")}}
    months, (y, mo) = [], map(int, used[0].split("-"))
    end = date.today().isoformat()[:7]
    while f"{y:04d}-{mo:02d}" <= end:
        months.append(f"{y:04d}-{mo:02d}")
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return {"months": months, **{k: [round(series[k][m], 5) for m in months]
                                 for k in ("money_in", "money_out", "buys", "sells", "income", "costs")}}


def money_picture(db: Session, user_id: int, months=24):
    """How banking relates to the rest, per month: investments' month-end worth (statements), bank balances and card
    debt, bank money in/out and fees, and net money moved into EasyEquities. Plus where the last 12 months' income went."""
    from ..banking.models import Liability
    from ..banking.reader import charts as bank_charts, month_balances

    balances = month_balances(db, user_id, months)
    keys = [b["month"] for b in balances]
    worth = {p["month"]: p["value"] for p in statement_history(db, user_id)}
    moved = defaultdict(float)
    for d, a in month_flows(db, user_id):
        moved[d.strftime("%Y-%m")] += float(a)
    flows = {m["month"]: m for m in bank_charts(db, user_id, months)["months"]}
    manual_debt = sum(max(0.0, x.balance) for x in db.scalars(select(Liability).where(Liability.user_id == user_id)))
    props = [property_view(p) for p in db.scalars(select(PropertyAsset).where(PropertyAsset.user_id == user_id))]
    own_equity = sum(p["equity"] for p in props)

    rows, last_worth = [], None
    for b in balances:
        k = b["month"]
        last_worth = worth.get(k, last_worth)
        f = flows.get(k, {})
        inv = last_worth or 0.0
        rows.append({"month": k, "investments": round(inv, 2), "bank": b["cash"], "debt": round(b["debt"] + manual_debt, 2),
                     "property": round(own_equity, 2),
                     "net_worth": round(inv + b["cash"] + own_equity - b["debt"] - manual_debt, 2),
                     "money_in": f.get("in", 0.0), "money_out": f.get("out", 0.0), "fees": f.get("fees", 0.0),
                     # Balance rose by more than the statement lines explain (money from people the statements miss).
                     "unrecorded_in": f.get("unrecorded_in", 0.0),
                     # Money into investments: seen in the bank lines if there are any that month, else the statements.
                     "invested": round(f["invested"] - f["from_investments"], 2) if f.get("invested") or f.get("from_investments")
                     else round(moved.get(k, 0.0), 2)})
    last12 = rows[-12:]
    income = sum(r["money_in"] for r in last12)
    unrecorded = sum(r["unrecorded_in"] for r in last12)
    invested = sum(max(0.0, r["invested"]) for r in last12)
    fees = sum(r["fees"] for r in last12)
    spent = max(0.0, sum(r["money_out"] for r in last12) - fees)
    kept = income + unrecorded - spent - fees - invested
    return {"months": rows, "since": keys[0] if keys else None,
            "income_12m": {"income": round(income, 2), "not_on_statements": round(unrecorded, 2), "spent": round(spent, 2),
                           "fees": round(fees, 2), "invested": round(invested, 2), "kept": round(kept, 2)}}
