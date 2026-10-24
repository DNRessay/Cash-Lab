import json

import pytest
from fastapi.testclient import TestClient

from app.invest import prices
from app.main import app
from tests.test_invest import fake_fetch, register

api = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def market():
    mp = pytest.MonkeyPatch()
    mp.setattr(prices, "fetch", fake_fetch)
    yield
    mp.undo()


@pytest.fixture(scope="module")
def user():
    headers = register("mcpuser")
    api.post("/api/invest/transactions", headers=headers, json={"date": "2024-01-02", "kind": "buy", "symbol": "GRT.JO",
                                                                "asset_class": "reit", "quantity": 100, "price": 10, "amount": 1000})
    return headers


def rpc(key, method, params=None, mid=1):
    r = api.post("/mcp", headers={"Authorization": f"Bearer {key}"},
                 json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
    return r


def call(key, name, args=None):
    body = rpc(key, "tools/call", {"name": name, "arguments": args or {}}).json()["result"]
    return body, (json.loads(body["content"][0]["text"]) if not body["isError"] else body["content"][0]["text"])


def test_key_lifecycle_and_auth(user):
    made = api.post("/api/mcp/keys", headers=user, json={"name": "assistant"})
    assert made.status_code == 201 and made.json()["key"].startswith("clab_")
    key = made.json()["key"]
    listed = api.get("/api/mcp/keys", headers=user).json()
    assert listed[-1]["hint"] == key[-4:] and "key" not in listed[-1]

    assert rpc(key, "ping").json()["result"] == {}
    assert api.get("/api/mcp/keys", headers=user).json()[-1]["last_used_at"]

    # A normal login token is not an MCP key, and a revoked key stops working.
    assert rpc(user["Authorization"][7:], "ping").status_code == 401
    assert api.delete(f"/api/mcp/keys/{made.json()['id']}", headers=user).status_code == 204
    assert rpc(key, "ping").status_code == 401


def test_initialize_and_tools_list(user):
    key = api.post("/api/mcp/keys", headers=user, json={}).json()["key"]
    init = rpc(key, "initialize", {"protocolVersion": "2025-03-26"}).json()["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["serverInfo"]["name"] == "c-lab"
    names = {t["name"] for t in rpc(key, "tools/list").json()["result"]["tools"]}
    assert {"overview", "portfolio", "markets", "quote", "report", "bank_transactions"} <= names
    note = api.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202


def test_read_tools_return_this_users_data(user):
    key = api.post("/api/mcp/keys", headers=user, json={}).json()["key"]
    _, portfolio = call(key, "portfolio")
    assert [h["symbol"] for h in portfolio["holdings"]] == ["GRT.JO"]
    _, overview = call(key, "overview")
    assert overview["currency"] == "ZAR (rand)" and overview["investments"]["worth"] > 0
    _, quote = call(key, "quote", {"symbol": "grt.jo"})
    assert quote["symbol"] == "GRT.JO"
    _, txns = call(key, "investment_transactions", {"symbol": "GRT.JO"})
    assert txns[0]["kind"] == "buy"
    _, report = call(key, "report", {"start": "2024-01-01", "end": "2024-12-31"})
    assert report["period"]["start"] == "2024-01-01"
    for name in ("markets", "watchlist", "properties", "bank_spending", "bank_transactions"):
        body, _ = call(key, name)
        assert body["isError"] is False, name


def test_tool_errors_are_results_and_other_users_data_stays_separate(user):
    key = api.post("/api/mcp/keys", headers=user, json={}).json()["key"]
    body, text = call(key, "quote", {"symbol": "NOPE"})
    assert body["isError"] and "No price found" in text
    other = api.post("/api/mcp/keys", headers=register("mcpother"), json={}).json()["key"]
    _, theirs = call(other, "portfolio")
    assert theirs["holdings"] == []
    assert rpc(key, "tools/call", {"name": "delete_everything"}).json()["error"]["code"] == -32602
