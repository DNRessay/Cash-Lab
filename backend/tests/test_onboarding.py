from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.invest.ee import gmail
from app.invest.ee.models import EEConnection, EESetting
from app.main import app
from app.models import User
from app.onboarding import check_sa_id, luhn_ok
from app.security import seal, unseal
from tests.test_invest import market, register  # noqa: F401

api = TestClient(app)


def test_sa_id_check():
    n, info = check_sa_id("800101 5009 087")
    assert n == "8001015009087" and info == {"birth_date": date(1980, 1, 1), "gender": "male", "citizen": "citizen"}
    assert luhn_ok("79927398713") and not luhn_ok("79927398710")
    for bad, why in [("8001015009088", "check digit"), ("8013015009087", "birth date"), ("8001015009387", "11th digit"),
                     ("12345", "13 digits")]:
        with pytest.raises(ValueError, match=why):
            check_sa_id(bad)


def test_signup_onboarding_and_report(market, monkeypatch):
    r = api.post("/api/auth/register", json={"first_name": "New", "email": "newbie@inv.example.com", "password": "Str0ng-pass!",
                                             "id_number": "8001015009088"})
    assert r.status_code == 400 and "check digit" in r.text
    r = api.post("/api/auth/register", json={"first_name": "New", "email": "newbie@inv.example.com", "password": "Str0ng-pass!",
                                             "id_number": "8001015009087"})
    assert r.status_code == 201
    h = {"Authorization": f"Bearer {r.json()['access']}"}
    st = api.get("/api/onboarding", headers=h).json()
    assert st["steps"]["id"]["done"] and not st["steps"]["easyequities"]["done"] and not st["complete"]
    assert not st["steps"]["email"]["done"]  # email is required, so still locked
    assert set(st["pending_optional"]) == {"easyequities", "google", "statements"}
    sent = []
    import app.services.mailer as mailer

    monkeypatch.setattr(mailer, "send_mail", lambda to, subject, body, html=None: sent.append((to, body)))
    assert api.post("/api/onboarding/email/verify", headers=h, json={"code": "123456"}).status_code == 400  # none sent yet
    api.post("/api/onboarding/email/send", headers=h)
    assert api.post("/api/onboarding/email/send", headers=h).status_code == 429  # once a minute
    code = sent[0][1].split("code is ")[1][:6]
    assert sent[0][0] == "newbie@inv.example.com"
    bad = api.post("/api/onboarding/email/verify", headers=h, json={"code": "000000" if code != "000000" else "111111"})
    assert bad.status_code == 400 and "4 tries left" in bad.text
    st = api.post("/api/onboarding/email/verify", headers=h, json={"code": code}).json()
    assert st["steps"]["email"]["done"] and st["complete"]  # verified email + ID opens the app with nothing else connected
    assert "1980" in st["steps"]["id"]["detail"]
    db = SessionLocal()
    uid = db.query(User).filter(User.email == "newbie@inv.example.com").one().id
    assert unseal(db.query(EESetting).filter(EESetting.user_id == uid).one().pdf_password) == "8001015009087"  # reused
    assert api.put("/api/onboarding/id", headers=h, json={"id_number": "123"}).status_code == 400
    db.add(EEConnection(user_id=uid, snapshot={}, username="me", password=seal("x"), platform_status="ok",
                        mail_address="me@gmail.com", mail_password=gmail.PREFIX + seal("rt"), mail_status="ok"))
    db.commit()
    st = api.get("/api/onboarding", headers=h).json()
    assert st["steps"]["google"]["detail"] == "me@gmail.com"
    assert st["complete"] and not st["steps"]["statements"]["done"]  # email + ID are enough; the rest is optional
    assert st["pending_optional"] == ["statements"] and st["steps"]["id"]["required"] and not st["steps"]["google"]["required"]
    from app.banking.models import BankStatement

    db.add(BankStatement(user_id=uid, gmail_id="g1", filename="s.pdf", status="locked", error="password"))
    db.commit()
    assert "didn't open" in api.get("/api/onboarding", headers=h).json()["steps"]["statements"]["detail"]
    db.add(BankStatement(user_id=uid, gmail_id="g2", filename="s.pdf", status="ok"))
    db.commit()
    st = api.get("/api/onboarding", headers=h).json()
    assert st["complete"] and st["steps"]["statements"]["done"]

    rep = api.get("/api/reports?start=2026-01-01&end=2026-09-30", headers=h).json()
    assert rep["period"] == {"start": "2026-01-01", "end": "2026-09-30", "months": 9}
    assert {"net_worth", "investments", "banking", "insights", "property"} <= rep.keys()
    assert api.get("/api/reports?start=nope", headers=h).status_code == 400
    from app.ai import llm
    from app.config import settings
    monkeypatch.setattr(settings, "groq_api_keys", ["g"])
    seen = []
    monkeypatch.setattr(llm, "groq", lambda messages, max_tokens=900, json_mode=False: seen.append(messages) or
                        '{"points": [{"title": "Spending beat income", "detail": "R100 more out than in.", "level": "high"}]}')
    a = api.get("/api/reports/ai?start=2026-01-01&end=2026-09-30", headers=h).json()
    assert a["items"][0]["level"] == "high" and not a["cached"] and '"net_worth"' in seen[0][1]["content"]
    assert api.get("/api/reports/ai?start=2026-01-01&end=2026-09-30", headers=h).json()["cached"] and len(seen) == 1
    db.query(BankStatement).filter(BankStatement.user_id == uid).delete()
    db.query(EEConnection).filter(EEConnection.user_id == uid).delete()
    db.query(EESetting).filter(EESetting.user_id == uid).delete()
    db.commit()
    db.close()
