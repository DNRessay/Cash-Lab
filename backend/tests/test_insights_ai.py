import json

from fastapi.testclient import TestClient

from app.ai import llm
from app.config import settings
from app.main import app
from tests.test_invest import market, register  # noqa: F401

api = TestClient(app)


def test_suggestions_and_chat(market, monkeypatch):
    h = register("aiuser")
    api.post("/api/bank/liabilities", headers=h, json={"name": "Car", "kind": "vehicle", "balance": 90000, "rate": 13})
    assert api.get("/api/ai/status", headers=h).json() == {"suggestions": False, "chat": False}
    assert api.get("/api/ai/suggestions/overview?refresh=true", headers=h).status_code == 503  # no key yet

    monkeypatch.setattr(settings, "cohere_api_key", "k")
    monkeypatch.setattr(settings, "groq_api_keys", ["g1", "g2"])
    sent = {}

    def fake_cohere(system, user, json_mode=False):
        sent["prompt"] = user
        return json.dumps({"suggestions": [
            {"title": "Pay off the car first", "detail": "13% on R90,000 costs more than your shares earn.",
             "impact": "high", "saves_or_gains_rand_per_year": 11700},
            {"title": "", "detail": "dropped: no title"}]})
    monkeypatch.setattr(llm, "cohere", fake_cohere)
    assert api.get("/api/ai/suggestions/overview", headers=h).json()["items"] == []  # nothing yet, no call made
    r = api.get("/api/ai/suggestions/overview?refresh=true", headers=h).json()
    assert r["items"] == [{"title": "Pay off the car first", "detail": "13% on R90,000 costs more than your shares earn.",
                           "impact": "high", "value": 11700}]
    assert '"owing": 90000.0' in sent["prompt"] and "Car" not in sent["prompt"]  # debt amount, not the name
    assert api.get("/api/ai/suggestions/overview", headers=h).json()["cached"] is True
    sent.clear()
    assert api.get("/api/ai/suggestions/overview?refresh=true", headers=h).json()["cached"] is True  # too soon
    assert not sent  # no API call
    assert api.get("/api/ai/suggestions/nope", headers=h).status_code == 404

    calls = []

    def fake_post(url, **kw):
        calls.append(kw["headers"]["Authorization"])

        class R:
            status_code = 429 if len(calls) == 1 else 200
            ok = status_code == 200
            text = ""

            def json(self):
                return {"choices": [{"message": {"content": "Your net worth is ..."}}]}
        return R()
    monkeypatch.setattr(llm.requests, "post", fake_post)
    r = api.post("/api/ai/chat", headers=h, json={"messages": [{"role": "user", "content": "How am I doing?"}]})
    assert r.json() == {"reply": "Your net worth is ..."}
    assert calls[0] != calls[1]  # rate-limited key, then the next one
    again = api.post("/api/ai/chat", headers=h, json={"messages": [{"role": "user", "content": "How am I doing?"}]}).json()
    assert again["cached"] is True and len(calls) == 2  # starter question answered from cache


def test_conversations(market, monkeypatch):
    h = register("aiuser")
    monkeypatch.setattr(settings, "groq_api_keys", ["g"])
    seen = []
    monkeypatch.setattr(llm, "groq", lambda messages, max_tokens=900, json_mode=False: seen.append(messages) or
                        "**You're fine.**\n- Bank: R10\nFOLLOWUPS: Where can I cut? | How are fees? | What about debt?")
    r = api.post("/api/ai/converse", headers=h, json={"message": "How am I doing?", "topic": "banking"}).json()
    assert r["reply"] == "**You're fine.**\n- Bank: R10" and r["followups"] == ["Where can I cut?", "How are fees?", "What about debt?"]
    assert "banking page" in seen[0][0]["content"]
    r2 = api.post("/api/ai/converse", headers=h, json={"message": "And fees?", "chat_id": r["chat_id"]}).json()
    assert r2["chat_id"] == r["chat_id"] and len(seen[1]) == 4  # system + earlier turn + new question
    chats = api.get("/api/ai/chats", headers=h).json()
    assert chats[0]["title"] == "How am I doing?" and chats[0]["count"] == 4
    full = api.get(f"/api/ai/chats/{r['chat_id']}", headers=h).json()
    assert full["messages"][1]["followups"][0] == "Where can I cut?"
    other = register("nosy")
    assert api.get(f"/api/ai/chats/{r['chat_id']}", headers=other).status_code == 404
    assert api.delete(f"/api/ai/chats/{r['chat_id']}", headers=h).status_code == 204
    assert api.get("/api/ai/chats", headers=h).json() == []
