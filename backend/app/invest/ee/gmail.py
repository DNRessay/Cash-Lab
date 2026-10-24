# "Sign in with Google" for the EasyEquities email reader: OAuth with read-only Gmail access, then the Gmail API.
# The refresh token is stored sealed in EEConnection.mail_password as "oauth:<sealed token>" (no password involved).
import base64
import email
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import requests

from ...config import settings
from . import mail

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = "openid email https://www.googleapis.com/auth/gmail.readonly"
QUERY = "from:easyequities.co.za -from:noreply@easyequities.co.za"
PREFIX = "oauth:"


def configured():
    return bool(settings.google_client_id and settings.google_client_secret)


def auth_url(redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.google_client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "state": state,
    })


def exchange(code: str, redirect_uri: str) -> dict:
    """Code -> {'refresh_token', 'access_token', 'email'}. Raises mail.MailError."""
    r = requests.post(TOKEN_URL, data={"client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                                       "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri}, timeout=20)
    data = r.json() if r.content else {}
    if not r.ok or not data.get("access_token"):
        raise mail.MailError(f"Google sign-in failed: {data.get('error_description') or data.get('error') or r.status_code}")
    if not data.get("refresh_token"):
        raise mail.MailError("Google didn't grant offline access. Remove C-Lab under your Google account's "
                             "third-party access and sign in again.")
    info = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {data['access_token']}"}, timeout=20).json()
    return {"refresh_token": data["refresh_token"], "access_token": data["access_token"], "email": info.get("email", "")}


def access_token(refresh_token: str) -> str:
    r = requests.post(TOKEN_URL, data={"client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                                       "refresh_token": refresh_token, "grant_type": "refresh_token"}, timeout=20)
    data = r.json() if r.content else {}
    if not r.ok or not data.get("access_token"):
        reason = data.get("error_description") or data.get("error") or r.status_code
        raise mail.MailError(f"Google access expired or was revoked ({reason}). Sign in with Google again.")
    return data["access_token"]


def fetch(refresh_token: str, since_uid=0, uidvalidity="", limit=150):
    """Same contract as mail.fetch: (messages, last_seen_epoch_seconds, 'gmail')."""
    token = access_token(refresh_token)
    headers = {"Authorization": f"Bearer {token}"}
    since = since_uid if uidvalidity == "gmail" else 0
    q = QUERY + (f" after:{since}" if since else "")
    ids, page = [], None
    while len(ids) < 2000:
        params = {"q": q, "maxResults": 100, **({"pageToken": page} if page else {})}
        r = requests.get(f"{API}/messages", headers=headers, params=params, timeout=30)
        if not r.ok:
            raise mail.MailError(f"Gmail search failed (HTTP {r.status_code})")
        data = r.json()
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    out, newest = [], since
    for mid in reversed(ids[:limit] if since else ids[-limit:]):  # Gmail lists newest first; take oldest unseen first
        r = requests.get(f"{API}/messages/{mid}", headers=headers, params={"format": "raw"}, timeout=30)
        if not r.ok:
            continue
        body = r.json()
        raw = base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4))
        msg = email.message_from_bytes(raw, policy=default_policy)
        html, plain = mail._bodies(msg)
        try:
            received = parsedate_to_datetime(msg.get("Date")).astimezone().replace(tzinfo=None)
        except Exception:
            received = None
        newest = max(newest, int(body.get("internalDate", 0)) // 1000)
        out.append({"uid": mid, "message_id": str(msg.get("Message-ID", "")).strip() or f"gmail-{mid}",
                    "sender": str(msg.get("From", "")).lower(), "subject": str(msg.get("Subject", "")), "received": received,
                    "html": html, "text": mail.html_to_text(html) if html else " ".join(plain.split())})
    # Next time only ask for mail after the newest one seen (minus a day for Gmail's day-granular 'after:').
    return out, max(newest - 86400, since), "gmail"
