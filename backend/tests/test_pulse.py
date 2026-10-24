import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.ai import llm
from app.config import settings
from app.invest import pulse
from app.main import app
from tests.test_invest import market, register  # noqa: F401

api = TestClient(app)


def test_indicators():
    up = [float(i) for i in range(1, 101)]
    r = pulse.rsi(up)
    assert r[13] is None and r[14] == 100.0 and r[-1] == 100.0
    line, sig, hist = pulse.macd(up)
    assert line[-1] > 0 and hist[-1] >= 0
    u, m, lo = pulse.bollinger([10.0] * 30)
    assert (u[-1], m[-1], lo[-1]) == (10.0, 10.0, 10.0) and u[0] is None
    assert pulse.rsi([10.0] * 30)[-1] == 50.0
    flat = pulse.technical([[f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", 5.0] for i in range(250)])
    assert flat["bias"] == "mixed" and flat["rsi"] == 50.0
    hist = [[(date(2025, 1, 1) + timedelta(days=i)).isoformat(), 100 + i * 0.5] for i in range(250)]
    t = pulse.technical(hist, days=30)
    assert t["bias"] in ("up", "mixed") and len(t["series"]["close"]) == 30 and "long trend up" in " ".join(t["reads"])


def test_rule_calendar():
    cal = pulse.rule_calendar(date(2026, 10, 1))
    nfp = [e for e in cal if "NFP" in e["event"]]
    assert nfp[0]["date"] == "2026-10-02" and date.fromisoformat(nfp[0]["date"]).weekday() == 4
    pmi = [e for e in cal if "PMI" in e["event"]]
    assert pmi[0]["date"] == "2026-10-01"
    assert pulse.lexicon_sentiment("Rand firms as SARB cuts rates") == "positive"
    assert pulse.lexicon_sentiment("JSE slumps on recession fears") == "negative"


def test_pulse_endpoint(market, monkeypatch):
    h = register("aiuser")
    monkeypatch.setattr(settings, "serpapi_keys", ["s"])
    monkeypatch.setattr(settings, "groq_api_keys", ["g"])
    tomorrow = (pulse.utcnow().date() + timedelta(days=5)).isoformat()
    monkeypatch.setattr(pulse, "serp_news", lambda q, num=8: [
        {"title": f"{q}: SARB to announce rate decision on {tomorrow}", "source": "News24", "date": "2026-09-29",
         "link": f"https://example.com/{len(q)}", "snippet": "Economists expect a 25bp cut."}])
    monkeypatch.setattr(llm, "groq", lambda messages, max_tokens=900, json_mode=False: json.dumps({
        "sentiment": [{"i": 0, "s": "negative"}],
        "events": [{"date": tomorrow, "event": "SARB rate decision", "country": "ZA", "expect": "25bp cut expected", "from": 0}],
        "outlook": {"rand": "Firm into the decision.", "jse": "Mixed.", "mood": "mixed", "watch": ["SARB"]}}))
    p = api.get("/api/invest/pulse?refresh=true", headers=h).json()
    assert p["outlook"]["mood"] == "mixed" and p["sentiment"]["negative"] >= 1
    sarb = [e for e in p["calendar"] if e["event"] == "SARB rate decision"][0]
    assert sarb["expect"] == "25bp cut expected" and sarb["source"] == "News24" and not sarb["rule"]
    assert "ZAR=X" in p["technical"] and "series" not in p["technical"]["ZAR=X"]
    assert api.get("/api/invest/pulse", headers=h).json()["built_at"] == p["built_at"]  # cached
    c = api.get("/api/invest/technical/GRT.JO", headers=h).json()
    assert c["series"]["close"] and c["rsi"] is not None
