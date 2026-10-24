from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.invest import prices
from app.main import app

api = TestClient(app)

START = date(2024, 1, 1)
# symbol -> (price on START, price today, currency as Yahoo reports it, dividends over last 12m in that currency)
MARKET = {
    "GRT.JO": (1000, 1600, "ZAc", 120),     # Yahoo quotes JSE in cents
    "STX40.JO": (7000, 9000, "ZAc", 0),
    "STXPRO.JO": (1000, 1200, "ZAc", 0),
    "ZAR=X": (18.0, 18.0, "ZAR", 0),
}


def fake_fetch(symbol):
    if symbol not in MARKET:
        raise ValueError(f"Unknown symbol {symbol!r}")
    start, end, cur, divs = MARKET[symbol]
    days = (date.today() - START).days
    currency, scale = prices.MINOR_UNITS.get(cur, (cur, Decimal(1)))
    history = [[(START + timedelta(days=i)).isoformat(),
                float(Decimal(str(start + (end - start) * i / days)) * scale)] for i in range(days + 1)]
    return {"name": f"{symbol} Ltd", "currency": currency, "price": Decimal(str(end)) * scale,
            "history": history, "dividends_12m": Decimal(divs) * scale}


@pytest.fixture(scope="module", autouse=True)
def market():
    mp = pytest.MonkeyPatch()
    mp.setattr(prices, "fetch", fake_fetch)
    yield
    mp.undo()


def register(name):
    r = api.post("/api/auth/register", json={"first_name": name, "last_name": "X", "email": f"{name}@inv.example.com",
                                            "password": "Str0ng-pass!"})
    if r.status_code != 201:  # already registered by another test module
        r = api.post("/api/auth/login", json={"email": f"{name}@inv.example.com", "password": "Str0ng-pass!"})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": f"Bearer {r.json()['access']}"}


@pytest.fixture(scope="module")
def me():
    return register("investor")


def add(h, **t):
    r = api.post("/api/invest/transactions", headers=h, json=t)
    assert r.status_code == 201, r.text
    return r.json()


def test_quote_converts_cents_to_rand(me):
    q = api.get("/api/invest/quote/grt.jo", headers=me).json()
    assert q["currency"] == "ZAR" and q["price"] == 16.0
    assert round(q["dividend_yield"], 4) == 0.075  # R1.20 / R16
    assert q["high_52w"] <= 16.0 and q["change_1y"] > 0
    assert api.get("/api/invest/quote/NOPE.JO", headers=me).status_code == 404


def test_portfolio_maths(me):
    add(me, date="2025-01-10", kind="deposit", amount="30000")
    add(me, date="2025-01-11", kind="buy", symbol="GRT.JO", asset_class="reit", quantity="1000", price="12", fees="20")
    add(me, date="2025-01-11", kind="buy", symbol="STX40.JO", asset_class="etf", quantity="100", price="80")
    add(me, date="2025-06-30", kind="dividend", symbol="GRT.JO", amount="500")
    add(me, date="2025-07-01", kind="sell", symbol="GRT.JO", quantity="200", price="15", fees="10")

    s = api.get("/api/invest/summary", headers=me).json()
    h = {x["symbol"]: x for x in s["holdings"]}
    grt, stx = h["GRT.JO"], h["STX40.JO"]
    assert grt["quantity"] == 800 and grt["price"] == 16.0 and grt["price_source"] == "market"
    assert grt["cost"] == pytest.approx(9616)          # 12,020 * 800/1000
    assert grt["realised"] == pytest.approx(586)       # 3,000 - 10 - 2,404
    assert grt["dividends"] == 500 and grt["name"] == "GRT.JO Ltd"
    assert stx["value"] == pytest.approx(9000)
    assert s["cash"] == pytest.approx(13470)           # 30,000 - 12,020 - 8,000 + 500 + 2,990
    assert s["value"] == pytest.approx(12800 + 9000 + 13470)
    assert s["invested"] == 30000 and s["gain"] == pytest.approx(5270)
    assert s["return_pct"] == pytest.approx(5270 / 30000)
    assert s["allocation"]["cash"] == pytest.approx(13470) and "reit" in s["allocation"]

    bench = {b["symbol"]: b for b in s["benchmarks"]}
    top40 = bench["STX40.JO"]
    start_price = prices.price_on(fake_fetch("STX40.JO")["history"], date(2025, 1, 10))
    assert top40["value"] == pytest.approx(30000 / start_price * 90)
    assert bench["ZAR=X"]["value"] == pytest.approx(30000)  # flat rand/dollar in the fake market


def test_manual_price_overrides_market(me):
    add(me, date="2025-03-01", kind="buy", symbol="EP-HOUSE1", asset_class="easyproperties", quantity="10", price="100")
    s = api.get("/api/invest/summary", headers=me).json()
    ep = next(x for x in s["holdings"] if x["symbol"] == "EP-HOUSE1")
    assert ep["price_source"] == "last trade" and ep["value"] == 1000
    api.put("/api/invest/prices/ep-house1", headers=me, json={"price": "130"})
    s = api.get("/api/invest/summary", headers=me).json()
    ep = next(x for x in s["holdings"] if x["symbol"] == "EP-HOUSE1")
    assert ep["price"] == 130 and ep["price_source"].startswith("manual")


def test_transaction_validation(me):
    r = api.post("/api/invest/transactions", headers=me, json={"date": "2025-01-01", "kind": "buy", "amount": "10"})
    assert r.status_code == 400
    r = api.post("/api/invest/transactions", headers=me, json={"date": "2025-01-01", "kind": "gamble", "amount": "10"})
    assert r.status_code == 400


def test_csv_import():
    h = register("csvuser")
    csv_text = api.get("/api/invest/transactions/template", headers=h).text
    csv_text += "not-a-date,buy,GRT.JO,,,1,1,,,\n"
    files = {"file": ("t.csv", csv_text, "text/csv")}
    r = api.post("/api/invest/transactions/import", headers=h, files=files).json()
    assert r["imported"] == 4 and r["duplicates"] == 0 and len(r["errors"]) == 1
    r = api.post("/api/invest/transactions/import", headers=h, files=files).json()
    assert r["imported"] == 0 and r["duplicates"] == 4
    s = api.get("/api/invest/summary", headers=h).json()
    assert {x["symbol"] for x in s["holdings"]} == {"GRT.JO", "STX40.JO"}
    assert s["invested"] == 30000


def test_property(me):
    r = api.post("/api/invest/properties", headers=me, json={
        "name": "Soshanguve rental", "purchase_date": (date.today() - timedelta(days=int(365.25 * 4))).isoformat(),
        "purchase_price": "600000", "valuation": "900000", "bond_balance": "300000",
        "monthly_bond_payment": "5000", "monthly_rent": "7500", "monthly_costs": "1500"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["equity"] == 600000 and p["loan_to_value"] == pytest.approx(1 / 3)
    assert p["gross_yield"] == pytest.approx(0.10) and p["net_yield"] == pytest.approx(0.08)
    assert p["monthly_cashflow"] == 1000 and p["growth"] == pytest.approx(0.5)
    assert p["growth_per_year"] == pytest.approx(1.5 ** 0.25 - 1, abs=1e-3)
    s = api.get("/api/invest/summary", headers=me).json()
    assert s["property_equity"] == pytest.approx(600000 + s["easyproperties_value"])  # own property + EasyProperties
    assert s["net_worth"] == pytest.approx(s["value"] + 600000)


def test_watchlist_and_alerts(me, monkeypatch):
    r = api.post("/api/invest/watchlist", headers=me, json={"symbol": "grt.jo", "alert_above": "15"})
    assert r.status_code == 201 and r.json()["price"] == 16.0
    assert api.post("/api/invest/watchlist", headers=me, json={"symbol": "GRT.JO"}).status_code == 400
    assert api.post("/api/invest/watchlist", headers=me, json={"symbol": "NOPE.JO"}).status_code == 404

    from app.db import SessionLocal
    from app.invest import alerts

    sent = []
    monkeypatch.setattr(alerts, "send_mail", lambda to, subj, body: sent.append((to, subj)))
    db = SessionLocal()
    assert alerts.check_all(db) == 1
    assert alerts.check_all(db) == 0  # not twice in a day
    db.close()
    assert sent[0][0] == "investor@inv.example.com" and "GRT.JO" in sent[0][1]


def test_private_to_each_user(me):
    other = register("nosy")
    assert api.get("/api/invest/transactions", headers=other).json() == []
    assert api.get("/api/invest/summary", headers=other).json()["holdings"] == []
    mine = api.get("/api/invest/transactions", headers=me).json()[0]
    assert api.delete(f"/api/invest/transactions/{mine['id']}", headers=other).status_code == 404


def test_money_flows_prefer_statement_with_withdrawals():
    from datetime import date as d
    from types import SimpleNamespace as T

    from app.invest.portfolio import money_flows

    txns = [T(date=d(2025, 1, 5), kind="deposit", amount="1000", source="easyequities", fees=0),  # from an email
            T(date=d(2025, 3, 1), kind="deposit", amount="200", source="manual", fees=0)]
    statement = [{"date": "2025-01-05", "category": "deposit", "amount": 1000.0, "currency": "ZAR"},
                 {"date": "2025-02-10", "category": "withdrawal", "amount": -400.0, "currency": "ZAR"},  # no email for it
                 {"date": "2025-02-11", "category": "withdrawal", "amount": -5.0, "currency": "USD"}]
    flows, known = money_flows(txns, statement)
    assert known and sum(a for _, a in flows) == 800  # 1000 - 400 + 200 hand-entered
    flows, known = money_flows(txns, [])
    assert known and sum(a for _, a in flows) == 1200
    # No deposit lines in the statement: the monthly Contributions/Withdrawals are used instead of the emails.
    flows, known = money_flows(txns, [], [(d(2025, 1, 31), 1000), (d(2025, 2, 28), -700)])
    assert known and sum(a for _, a in flows) == 500
