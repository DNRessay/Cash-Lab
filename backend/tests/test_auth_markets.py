from fastapi.testclient import TestClient

from app.invest import prices
from app.main import app, handler
from tests.test_invest import fake_fetch

api = TestClient(app)


def test_health():
    assert api.get("/api/health").json() == {"status": "ok", "database": True}


def test_signup_closed_to_strangers_after_owner():
    # test_invest registers the owner first ("investor"); make sure one exists regardless of order
    api.post("/api/auth/register", json={"first_name": "investor", "email": "investor@inv.example.com", "password": "Str0ng-pass!"})
    r = api.post("/api/auth/register", json={"first_name": "x", "email": "stranger@example.com", "password": "Str0ng-pass!"})
    assert r.status_code == 403


def test_login_refresh_me_password():
    r = api.post("/api/auth/login", json={"email": "INVESTOR@inv.example.com", "password": "Str0ng-pass!"})
    assert r.status_code == 200
    tok = r.json()
    h = {"Authorization": f"Bearer {tok['access']}"}
    assert api.get("/api/auth/me", headers=h).json()["email"] == "investor@inv.example.com"
    assert "access" in api.post("/api/auth/token/refresh", json={"refresh": tok["refresh"]}).json()
    assert api.post("/api/auth/login", json={"email": "investor@inv.example.com", "password": "nope"}).status_code == 401
    assert api.post("/api/auth/change-password", headers=h,
                    json={"old_password": "wrong", "new_password": "An0ther-pass!"}).status_code == 400


def test_markets_board(monkeypatch):
    monkeypatch.setattr(prices, "fetch", lambda s: fake_fetch(s) if s in ("ZAR=X", "STX40.JO", "STXPRO.JO") else (_ for _ in ()).throw(ValueError("x")))
    h = {"Authorization": "Bearer " + api.post("/api/auth/login", json={"email": "investor@inv.example.com", "password": "Str0ng-pass!"}).json()["access"]}
    m = {x["symbol"]: x for x in api.get("/api/markets", headers=h).json()["markets"]}
    assert m["ZAR=X"]["label"] == "USD/ZAR" and m["ZAR=X"]["price"] == 18.0
    assert m["STX40.JO"]["price"] == 90.0  # cents converted to rand
    assert m["^GSPC"]["price"] is None     # feed failure shows as missing, not a crash


def test_schedule_event_runs_alerts(monkeypatch):
    import app.invest.alerts as alerts

    import app.invest.ee.sync as ee_sync

    monkeypatch.setattr(alerts, "check_all", lambda db: 3)
    import app.invest.portfolio as portfolio

    monkeypatch.setattr(ee_sync, "sync_all", lambda db: 1)
    monkeypatch.setattr(portfolio, "record_all", lambda db: 2)
    import app.invest.blog as blog

    monkeypatch.setattr(blog, "refresh", lambda db: {"links": 0, "read": 0})
    import app.ai.router as ai

    monkeypatch.setattr(ai, "warm_all", lambda db, deadline=None: 0)
    assert handler({"source": "aws.events", "detail-type": "Scheduled Event"}, None) == \
        {"easyequities_synced": 1, "bank_statements": 0, "blog": {"links": 0, "read": 0}, "ai_suggestions": 0, "alerts_sent": 3, "snapshots": 2}


def test_admin_event_sets_pdf_password():
    from app.db import SessionLocal
    from app.invest.ee.models import EESetting
    from app.security import unseal

    api.post("/api/auth/register", json={"first_name": "A", "last_name": "B", "email": "nosy@inv.example.com",
                                         "password": "Str0ng-pass!"})
    r = handler({"source": "clab.admin", "action": "set_pdf_password", "email": "NOSY@inv.example.com",
                 "password": " 8001015009087 "}, None)
    assert r["ok"] and r["pdf_password_set"] is True
    db = SessionLocal()
    uid = db.execute(__import__("sqlalchemy").text("select id from users where email='nosy@inv.example.com'")).scalar()
    row = db.query(EESetting).filter(EESetting.user_id == uid).one()
    assert unseal(row.pdf_password) == "8001015009087" and row.pdf_password.startswith("enc:")
    db.close()
    assert handler({"source": "clab.admin", "action": "nope", "email": "nosy@inv.example.com"}, None)["ok"] is False
