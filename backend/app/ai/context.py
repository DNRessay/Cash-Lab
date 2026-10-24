# What the AI gets to see: totals and categories only. No account numbers, names, IDs or statement text.
from datetime import date

from sqlalchemy.orm import Session


def _r(v):
    return round(float(v), 2) if v is not None else None


def build(db: Session, user_id: int) -> dict:
    from ..banking.reader import charts as bank_charts, position
    from ..invest import portfolio

    s = portfolio.summary(db, user_id)
    pos = position(db, user_id)
    bank = bank_charts(db, user_id, 12)
    money = portfolio.money_picture(db, user_id, 12)
    holdings = [h for h in s["holdings"] if h.get("value")]
    total = sum(h["value"] for h in holdings) or 1
    top = sorted(holdings, key=lambda h: -h["value"])[:15]
    divs = [d for d in (s.get("blog") or {}).get("upcoming", []) if d["state"] != "paid"][:12]
    return {
        "today": date.today().isoformat(),
        "currency": "ZAR (rand)",
        "net_worth": _r(s["net_worth"]),
        "investments": {
            "worth": _r(s["value"]), "cash_in_easyequities": _r(s["cash"]), "money_put_in": _r(s["invested"]),
            "gain": _r(s["gain"]), "return_pct": _r((s["return_pct"] or 0) * 100),
            "dividends_and_interest_all_time": _r((s["income"] or {}).get("dividend", 0) + (s["income"] or {}).get("interest", 0)) if s["income"] else None,
            "fees_and_tax_all_time": _r((s["income"] or {}).get("fee", 0) + (s["income"] or {}).get("tax", 0)) if s["income"] else None,
            "allocation": {k: _r(v) for k, v in s["allocation"].items()},
            "accounts": [{"name": a["name"], "currency": a["currency"], "worth_rand": _r(a["value_zar"])}
                         for a in (s.get("easyequities") or {}).get("accounts", [])],
            "top_holdings": [{"name": h.get("name") or h["symbol"], "type": h["asset_class"], "worth": _r(h["value"]),
                              "weight_pct": _r(h["value"] / total * 100), "gain_pct": _r((h.get("gain_pct") or 0) * 100),
                              "dividends_received": _r(h.get("dividends"))} for h in top],
            "benchmarks": [{"name": b["label"], "worth_if_invested_there": _r(b["value"])} for b in s["benchmarks"]],
        },
        "property": {"equity": _r(s["property_equity"]), "easyproperties": _r(s.get("easyproperties_value")),
                     "own": [{"name": p["name"], "worth": _r(p["valuation"]), "bond": _r(p["bond_balance"]),
                              "monthly_cashflow": _r(p["monthly_cashflow"])} for p in s["properties"]]},
        "banking": {
            "money_in_bank": pos["cash"], "debt": pos["debt"], "bank_fees_12m": pos["fees_12m"],
            "accounts": [{"bank": a.bank, "type": a.kind, "balance": _r(a.balance)} for a in pos["accounts"] if not a.hidden],
            "debts_added": [{"type": m.kind, "owing": _r(m.balance), "rate_pct": m.rate, "monthly": m.monthly}
                            for m in pos["liabilities"]],
            "last_12_months": money["income_12m"],
            "money_in_by_category_12m": dict(list(bank["income_categories"].items())[:10]),
            "spending_by_category_12m": dict(list(bank["categories"].items())[:10]),
            "monthly": [{"month": m["month"], "in": m["in"], "not_on_statements": m["unrecorded_in"], "out": m["out"],
                         "fees": m["fees"]} for m in bank["months"][-12:]],
        },
        "month_by_month": [{k: r[k] for k in ("month", "investments", "bank", "debt", "net_worth", "invested")}
                           for r in money["months"][-12:]],
        "dividends_open_or_paying": [{"company": d["instrument"], "dividend": d["amount"], "currency": d["currency"],
                                      "price": d["price"], "yield_pct": _r((d["this_pct"] or 0) * 100),
                                      "last_day_to_trade": d["ldt"], "you": d["mine"] or "no"} for d in divs],
    }
