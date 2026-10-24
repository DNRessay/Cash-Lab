"""OAuth 2.1 sign-in for this app's MCP server, so Claude's "Add custom connector" (or any MCP client)
connects with the app's own login instead of a pasted key: paste the /mcp URL, sign in, done.

Framework-free (stdlib only) — the same file is reused across apps; each app
adds thin routes around it. Stateless, which suits Lambda:
  - client registration (RFC 7591) returns a client_id that is the client's redirect URIs, HMAC-signed;
    a client_id that is an https URL is a Client ID Metadata Document and is fetched instead,
  - the authorization code is HMAC-signed and short-lived; PKCE (S256) is required, so a stolen code is
    useless without the verifier only the client holds,
  - the access token handed out at the end is an ordinary MCP key of the app, listed and revocable with
    the others in its settings,
  - for "Use your own OAuth client" there is also one fixed confidential client per app: its ID and
    secret are derived from the app's secret and shown at /oauth/client after signing in.
Discovery follows the MCP spec: 401 + WWW-Authenticate → /.well-known/oauth-protected-resource →
/.well-known/oauth-authorization-server."""
import base64
import hashlib
import hmac
import html
import json
import time
import urllib.parse
import urllib.request

CODE_TTL = 300
SCOPE = "mcp"
_cimd_cache: dict[str, tuple[float, dict]] = {}


# ── signing ─────────────────────────────────────────────────────────────────

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key(secret: str) -> bytes:
    return hmac.new(secret.encode(), b"mcp-oauth", hashlib.sha256).digest()


def sign(kind: str, data: dict, secret: str, ttl: int | None = None) -> str:
    body = _b64(json.dumps({**data, "k": kind, **({"x": int(time.time()) + ttl} if ttl else {})},
                           separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest())}"


def unsign(kind: str, token: str, secret: str) -> dict | None:
    body, _, sig = (token or "").partition(".")
    good = _b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest())
    if not body or not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    if data.get("k") != kind or ("x" in data and data["x"] < time.time()):
        return None
    return data


# ── discovery ───────────────────────────────────────────────────────────────

def resource_metadata(base: str, mcp_path: str = "/mcp") -> dict:
    return {"resource": base + mcp_path, "authorization_servers": [base], "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"]}


def server_metadata(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": [SCOPE],
        "client_id_metadata_document_supported": True,
    }


def www_authenticate(base: str) -> str:
    return f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'


# ── clients ─────────────────────────────────────────────────────────────────

def _redirect_ok(uri: str) -> bool:
    p = urllib.parse.urlparse(uri or "")
    if p.fragment or not p.netloc:
        return False
    return p.scheme == "https" or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"))


def register(body: dict, secret: str) -> tuple[int, dict]:
    uris = body.get("redirect_uris") if isinstance(body, dict) else None
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and _redirect_ok(u) for u in uris):
        return 400, {"error": "invalid_redirect_uri", "error_description": "redirect_uris must be https URLs"}
    name = str(body.get("client_name") or "MCP client")[:80]
    client_id = sign("client", {"n": name, "r": uris[:10]}, secret)
    return 201, {"client_id": client_id, "client_name": name, "redirect_uris": uris[:10],
                 "grant_types": ["authorization_code"], "response_types": ["code"],
                 "token_endpoint_auth_method": "none", "client_id_issued_at": int(time.time())}


# Where Claude sends the browser back after sign-in (claude.ai and claude.com), for the fixed client.
CLAUDE_REDIRECTS = ["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"]


def own_client(app_slug: str, secret: str) -> dict:
    """The app's fixed confidential client: ID and secret to paste into "Use your own OAuth client"."""
    return {"client_id": f"{app_slug}-connector",
            "client_secret": _b64(hmac.new(_key(secret), f"client-secret:{app_slug}".encode(), hashlib.sha256).digest())}


def _own(client_id: str, secret: str) -> dict | None:
    if not client_id.endswith("-connector") or "." in client_id:
        return None
    return {"name": "Claude", "redirect_uris": CLAUDE_REDIRECTS + ["http://localhost/callback", "http://127.0.0.1/callback"],
            "secret": own_client(client_id[: -len("-connector")], secret)["client_secret"]}


def _fetch_cimd(url: str) -> dict | None:
    hit = _cimd_cache.get(url)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "mcp-oauth"})
        with urllib.request.urlopen(req, timeout=5) as r:
            doc = json.loads(r.read(65536))
    except Exception:
        return None
    if not isinstance(doc, dict) or doc.get("client_id") != url:
        return None
    _cimd_cache[url] = (time.time(), doc)
    return doc


def client(client_id: str, secret: str) -> dict | None:
    """{"name", "redirect_uris"} for a registered (signed) client or a metadata-document URL."""
    if own := _own(client_id or "", secret):
        return own
    if (client_id or "").startswith("https://"):
        doc = _fetch_cimd(client_id)
        uris = [u for u in (doc or {}).get("redirect_uris") or [] if isinstance(u, str) and _redirect_ok(u)]
        return {"name": str(doc.get("client_name") or urllib.parse.urlparse(client_id).hostname)[:80],
                "redirect_uris": uris} if doc and uris else None
    data = unsign("client", client_id, secret)
    return {"name": data["n"], "redirect_uris": data["r"]} if data else None


# ── authorize / token ───────────────────────────────────────────────────────

AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method",
               "scope", "resource")


def check_authorize(params: dict, secret: str) -> tuple[dict | None, str]:
    """(request, "") when the authorization request is valid, else (None, why) — shown as a page, never
    redirected, so a bad redirect_uri can't be abused as an open redirect."""
    c = client(params.get("client_id", ""), secret)
    if not c:
        return None, "This app isn't registered here, or its registration couldn't be read."
    redirect = params.get("redirect_uri") or (c["redirect_uris"][0] if len(c["redirect_uris"]) == 1 else "")
    if redirect not in c["redirect_uris"]:
        return None, "The redirect address doesn't match what this app registered."
    if params.get("response_type") != "code":
        return None, "Only the authorization code flow is supported."
    if not params.get("code_challenge") or params.get("code_challenge_method", "S256") != "S256":
        return None, "PKCE (S256) is required."
    return {"client_id": params["client_id"], "client_name": c["name"], "redirect_uri": redirect,
            "state": params.get("state", ""), "code_challenge": params["code_challenge"]}, ""


def redirect_to(uri: str, **params) -> str:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    return f"{uri}{'&' if '?' in uri else '?'}{query}"


def issue_code(user_id, req: dict, secret: str) -> str:
    return redirect_to(req["redirect_uri"], code=sign("code", {"u": str(user_id), "c": req["client_id"],
                                                               "r": req["redirect_uri"], "p": req["code_challenge"]},
                                                      secret, ttl=CODE_TTL), state=req["state"])


def basic_auth(header: str) -> dict:
    """client_id/client_secret from an HTTP Basic Authorization header (client_secret_basic)."""
    if not (header or "").lower().startswith("basic "):
        return {}
    try:
        cid, _, csecret = base64.b64decode(header[6:].strip()).decode().partition(":")
    except ValueError:
        return {}
    return {"client_id": urllib.parse.unquote(cid), "client_secret": urllib.parse.unquote(csecret)}


def redeem(form: dict, secret: str, authorization: str = "") -> tuple[str | None, dict | None]:
    """(user_id, client) for a valid token request, else (None, OAuth error body). Pass the request's
    Authorization header so client_secret_basic works too."""
    form = {**form, **basic_auth(authorization)}
    if form.get("grant_type") != "authorization_code":
        return None, {"error": "unsupported_grant_type"}
    own = _own(form.get("client_id", ""), secret)
    if own and not hmac.compare_digest(form.get("client_secret", ""), own["secret"]):
        return None, {"error": "invalid_client", "error_description": "Wrong client secret."}
    code = unsign("code", form.get("code", ""), secret)
    if not code or code["c"] != form.get("client_id") or code["r"] != form.get("redirect_uri", code["r"]):
        return None, {"error": "invalid_grant", "error_description": "The code is invalid, expired or not for this client."}
    verifier = form.get("code_verifier", "")
    if not verifier or not hmac.compare_digest(_b64(hashlib.sha256(verifier.encode()).digest()), code["p"]):
        return None, {"error": "invalid_grant", "error_description": "PKCE verification failed."}
    return code["u"], client(code["c"], secret) or {"name": "MCP client"}


def token_response(access_token: str) -> dict:
    return {"access_token": access_token, "token_type": "Bearer", "scope": SCOPE}


# ── sign-in page ────────────────────────────────────────────────────────────

def client_page(app: str, mcp_url: str, creds: dict | None, fields: list[tuple[str, str, str]], error: str = "") -> str:
    """/oauth/client: sign in, then see the URL, client ID and secret to paste into Claude."""
    e = html.escape
    if creds:
        rows = "".join(f"<label>{e(k)}<input readonly value=\"{e(v)}\" onclick='this.select()'></label>"
                       for k, v in (("Remote MCP server URL", mcp_url), ("OAuth client ID", creds["client_id"]),
                                    ("OAuth client secret", creds["client_secret"])))
        body = (f"<h1>{e(app)} connector</h1><p>In Claude: Settings → Connectors → Add custom connector. Paste the URL, "
                "open Advanced → Use your own OAuth client, and paste the ID and secret. Keep the secret private.</p>"
                f"{rows}<p>Or leave the OAuth fields empty — Claude registers itself and you just sign in.</p>")
        return _page(app, body)
    inputs = "".join(f'<label>{e(label)}<input name="{e(name)}" type="{e(kind)}" required></label>' for name, label, kind in fields)
    body = (f"<h1>{e(app)} connector details</h1><p>Sign in to see the client ID and secret for Claude.</p>"
            f"{f'<p class=err>{e(error)}</p>' if error else ''}<form method=post>{inputs}<button>Show details</button></form>")
    return _page(app, body)


def login_page(app: str, req: dict | None, fields: list[tuple[str, str, str]], error: str = "",
               action: str = "/oauth/authorize") -> str:
    """The sign-in page Claude opens. `fields` are (name, label, input type) for this app's login."""
    e = html.escape
    if req is None:
        body = f"<h1>Can't connect</h1><p class=err>{e(error)}</p>"
    else:
        hidden = "".join(f'<input type=hidden name="{k}" value="{e(str(v))}">'
                         for k, v in {"client_id": req["client_id"], "redirect_uri": req["redirect_uri"],
                                      "state": req["state"], "code_challenge": req["code_challenge"],
                                      "response_type": "code", "code_challenge_method": "S256"}.items())
        inputs = "".join(f'<label>{e(label)}<input name="{e(name)}" type="{e(kind)}" required '
                         f'autocomplete="{"current-password" if kind == "password" else "username"}"></label>'
                         for name, label, kind in fields)
        body = (f"<h1>Connect {e(req['client_name'])} to {e(app)}</h1>"
                f"<p>Sign in to let <b>{e(req['client_name'])}</b> use {e(app)}'s tools for you. "
                f"You can revoke it any time with your other MCP keys in {e(app)}'s settings.</p>"
                f"{f'<p class=err>{e(error)}</p>' if error else ''}"
                f'<form method=post action="{e(action)}">{hidden}{inputs}<button>Sign in and connect</button></form>')
    return _page(app, body)


def _page(app: str, body: str) -> str:
    e = html.escape
    return ("<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Connect to {e(app)}</title><style>"
            "body{font-family:system-ui,sans-serif;background:#f6f5f2;color:#1f1f1e;margin:0;display:flex;min-height:100vh;"
            "align-items:center;justify-content:center;padding:16px}main{background:#fff;border:1px solid #e5e4e1;"
            "border-radius:16px;padding:24px;max-width:380px;width:100%}h1{font-size:20px;margin:0 0 8px}p{font-size:14px;"
            "line-height:1.5;color:#555}.err{color:#c4453a}label{display:block;font-size:13px;font-weight:600;margin:12px 0 4px}"
            "input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ddd;border-radius:10px;font-size:15px;margin-top:4px}"
            "button{margin-top:16px;width:100%;padding:12px;border:1.5px solid #c9a227;border-radius:10px;background:#181818;"
            "color:#fff;font-size:15px;font-weight:600}@media(prefers-color-scheme:dark){body{background:#111;color:#eee}"
            "main{background:#1b1b1b;border-color:#333}p{color:#aaa}input{background:#222;color:#eee;border-color:#444}}"
            f"</style></head><body><main>{body}</main></body></html>")
