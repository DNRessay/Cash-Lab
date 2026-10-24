from datetime import date, datetime

from app.db import SessionLocal
from app.invest import div_calendar as cal
from app.invest.models import DividendHistory
from tests.test_invest import api, register


def ev(d, amount, pay=None, cur="ZAR"):
    return {"ldt": d, "amount": amount, "pay_date": pay, "currency": cur}


def test_expected_payouts_follow_each_slot_and_drop_stopped_ones():
    today = date(2026, 10, 5)
    events = [ev(date(2024, 3, 12), 1.0, date(2024, 3, 18)), ev(date(2024, 9, 10), 1.2),
              ev(date(2025, 3, 11), 1.1, date(2025, 3, 17)), ev(date(2025, 9, 9), 1.3),
              ev(date(2026, 3, 10), 1.2, date(2026, 3, 16)),
              ev(date(2023, 6, 6), 0.5)]  # a June payout that stopped
    got = cal.predict(sorted(events, key=lambda e: e["ldt"]), today, date(2027, 9, 30), date(2026, 10, 1))
    by_month = {p["ldt"].month: p for p in got}
    assert set(by_month) == {3, 9}  # June stopped; September 2026 is before the window, so next is September 2027
    assert by_month[9]["ldt"] == date(2027, 9, 9)
    march = by_month[3]
    assert (march["status"], march["amount"], march["ldt"]) == ("expected", 1.2, date(2027, 3, 10))
    assert march["pay_date"] == date(2027, 3, 16)  # same 6-day gap as before
    assert len(march["slot"]) == 3


def test_slots_keep_december_and_january_apart_for_monthly_payers():
    monthly = [ev(date(y, m, 10), 0.1) for y in (2025, 2026) for m in range(1, 13) if date(y, m, 10) < date(2026, 10, 1)]
    groups = cal.slots(monthly)
    assert len(groups) == 12 and all(len({e["ldt"].month for e in g}) == 1 for g in groups)


def test_calendar_page(monkeypatch):
    monkeypatch.setattr(cal, "utcnow", lambda: datetime(2026, 10, 5))
    h = register("divcalendar")
    api.post("/api/invest/transactions", headers=h, json={"date": "2024-01-02", "kind": "buy", "symbol": "STX40.JO",
                                                         "asset_class": "etf", "quantity": 100, "price": 10, "amount": 1000})
    db = SessionLocal()
    db.merge(DividendHistory(symbol="STX40.JO", events=[["2025-03-12", 0.6], ["2025-10-08", 0.62], ["2026-03-11", 0.65]]))
    db.commit()
    db.close()
    r = api.get("/api/invest/calendar", headers=h).json()
    assert len(r["months"]) == 12 and r["months"][0]["label"] == "Oct 2026" and "guesses" in r["warning"]
    items = {m["month"]: m["items"] for m in r["months"] if m["items"]}
    stx = [x for x in items["2026-10"] if x["symbol"] == "STX40.JO"][0]
    assert (stx["status"], stx["mine"], stx["confidence"]) == ("expected", "hold", "low")
    assert stx["ldt"] == "2026-10-07" and stx["estimate"] == 62.0  # 100 units × R0.62, a day before last year's ex-date
    march = [x for x in items["2027-03"] if x["symbol"] == "STX40.JO"][0]
    assert march["confidence"] == "medium" and march["seen"] == ["Mar 2025", "Mar 2026"]
