"""Sign-in for Claude's custom connectors (and any MCP client) — see mcp_oauth.py. Signing in with the
C-Lab email and password gives the client an ordinary MCP key, listed and revocable under Settings."""
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import mcp_oauth
from .config import settings
from .deps import get_db
from .mcp import KEY_PREFIX, McpKey, _hash
from .models import User
from .security import verify_password

router = APIRouter(tags=["mcp"])
APP, SLUG = "C-Lab", "c-lab"
FIELDS = [("email", "Email", "email"), ("password", "Password", "password")]


def base_url(request: Request) -> str:
    if settings.api_url:
        return settings.api_url
    return f"{request.headers.get('x-forwarded-proto', 'https')}://{request.headers.get('host', '')}"


def _login(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == (email or "").strip().lower()))
    return user if user and user.is_active and verify_password(password or "", user.password) else None


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(request: Request):
    return mcp_oauth.resource_metadata(base_url(request))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
def authorization_server(request: Request):
    return mcp_oauth.server_metadata(base_url(request))


@router.post("/oauth/register")
async def register(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    status, data = mcp_oauth.register(body, settings.secret_key)
    return JSONResponse(data, status)


@router.get("/oauth/authorize")
def authorize_page(request: Request):
    req, error = mcp_oauth.check_authorize(dict(request.query_params), settings.secret_key)
    return HTMLResponse(mcp_oauth.login_page(APP, req, FIELDS, error), 200 if req else 400)


@router.post("/oauth/authorize")
async def authorize(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    req, error = mcp_oauth.check_authorize(form, settings.secret_key)
    if not req:
        return HTMLResponse(mcp_oauth.login_page(APP, None, FIELDS, error), 400)
    user = _login(db, form.get("email"), form.get("password"))
    if not user:
        return HTMLResponse(mcp_oauth.login_page(APP, req, FIELDS, "Invalid email or password."), 401)
    return RedirectResponse(mcp_oauth.issue_code(user.id, req, settings.secret_key), 302)


@router.post("/oauth/token")
async def token(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    user_id, info = mcp_oauth.redeem(form, settings.secret_key, request.headers.get("authorization", ""))
    if not user_id:
        return JSONResponse(info, 401 if info.get("error") == "invalid_client" else 400)
    user = db.get(User, int(user_id))
    if not user or not user.is_active:
        return JSONResponse({"error": "invalid_grant"}, 400)
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    db.add(McpKey(user_id=user.id, name=f"{info['name']} (connector)"[:80], key_hash=_hash(key), hint=key[-4:]))
    db.commit()
    return JSONResponse(mcp_oauth.token_response(key), headers={"Cache-Control": "no-store"})


@router.get("/oauth/client")
def client_page():
    return HTMLResponse(mcp_oauth.client_page(APP, "", None, FIELDS))


@router.post("/oauth/client")
async def client_details(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    if not _login(db, form.get("email"), form.get("password")):
        return HTMLResponse(mcp_oauth.client_page(APP, "", None, FIELDS, "Invalid email or password."), 401)
    return HTMLResponse(mcp_oauth.client_page(APP, base_url(request) + "/mcp", mcp_oauth.own_client(SLUG, settings.secret_key), FIELDS),
                        headers={"Cache-Control": "no-store"})
