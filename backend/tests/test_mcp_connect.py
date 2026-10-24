import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app import mcp_oauth
from app.config import settings
from app.main import app
from tests.test_invest import register

api = TestClient(app)
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
EMAIL, PASSWORD = "mcpuser@inv.example.com", "Str0ng-pass!"


def _pkce():
    verifier = "v" * 64
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _code(client_id, challenge, password=PASSWORD):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "s1",
              "code_challenge": challenge, "code_challenge_method": "S256"}
    assert "Connect" in api.get("/oauth/authorize", params=params).text
    return api.post("/oauth/authorize", data={**params, "email": EMAIL, "password": password}, follow_redirects=False)


def test_claude_custom_connector_signs_in_and_gets_a_revocable_key():
    headers = register("mcpuser")
    r = api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]
    assert api.get("/.well-known/oauth-authorization-server").json()["registration_endpoint"].endswith("/oauth/register")

    reg = api.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [CALLBACK]}).json()
    verifier, challenge = _pkce()
    assert _code(reg["client_id"], challenge, "wrong").status_code == 401
    loc = _code(reg["client_id"], challenge).headers["location"]
    code = parse_qs(urlparse(loc).query)["code"][0]
    tok = api.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                                         "redirect_uri": CALLBACK, "code_verifier": verifier}).json()
    key = tok["access_token"]
    r = api.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200 and r.json()["result"]["tools"]

    keys = api.get("/api/mcp/keys", headers=headers).json()
    mine = next(k for k in keys if k["name"] == "Claude (connector)")
    api.delete(f"/api/mcp/keys/{mine['id']}", headers=headers)
    assert api.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_own_client_id_and_secret():
    register("mcpuser")
    creds = mcp_oauth.own_client("c-lab", settings.secret_key)
    page = api.post("/oauth/client", data={"email": EMAIL, "password": PASSWORD}).text
    assert creds["client_id"] in page and creds["client_secret"] in page
    assert api.post("/oauth/client", data={"email": EMAIL, "password": "nope"}).status_code == 401
    verifier, challenge = _pkce()
    code = parse_qs(urlparse(_code(creds["client_id"], challenge).headers["location"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "client_id": creds["client_id"], "redirect_uri": CALLBACK,
            "code_verifier": verifier}
    assert api.post("/oauth/token", data={**form, "client_secret": "bad"}).status_code == 401
    assert api.post("/oauth/token", data={**form, "client_secret": creds["client_secret"]}).json()["access_token"].startswith("clab_")
