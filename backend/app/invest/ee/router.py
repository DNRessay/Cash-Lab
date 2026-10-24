from typing import Optional

from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...deps import current_user, get_db
from ...models import User
from ...config import settings
from ...security import make_token, read_token, seal, unseal
from . import gmail, mail, platform, reader
from . import sync
from .models import EEConnection, EEMail

router = APIRouter(prefix="/api/ee", tags=["easyequities"])


class PlatformIn(BaseModel):
    username: str
    password: str


class MailIn(BaseModel):
    address: str
    app_password: str


class MailPatch(BaseModel):
    done: Optional[bool] = None


def connection(db: Session, user: User, create=False) -> Optional[EEConnection]:
    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user.id))
    if not conn and create:
        conn = EEConnection(user_id=user.id, snapshot={})
        db.add(conn)
        db.flush()
    return conn


def _dt(v):
    return v.isoformat(timespec="seconds") if v else None


def status(db: Session, conn: Optional[EEConnection]):
    view, _ = sync.platform_view(db, conn) if conn else ({"accounts": [], "value_zar": 0.0, "taken_at": None}, {})
    return {
        "platform": {
            "connected": bool(conn and conn.username),
            "username": conn.username if conn else "",
            "status": conn.platform_status if conn else "",
            "error": conn.platform_error if conn else "",
            "error_stage": conn.platform_error_stage if conn else "",
            "synced_at": _dt(conn.platform_synced_at) if conn else None,
            "tried_at": _dt(conn.platform_tried_at) if conn else None,
        },
        "mail": {
            "connected": bool(conn and conn.mail_address),
            "address": conn.mail_address if conn else "",
            "status": conn.mail_status if conn else "",
            "error": conn.mail_error if conn else "",
            "synced_at": _dt(conn.mail_synced_at) if conn else None,
            "method": ("google" if conn and conn.mail_password.startswith(gmail.PREFIX) else
                       "app_password" if conn and conn.mail_password else ""),
            "google_available": gmail.configured(),
        },
        **view,
    }


@router.get("")
def get_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return status(db, connection(db, user))


@router.put("/platform")
def connect_platform(body: PlatformIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not body.username.strip() or not body.password:
        raise HTTPException(400, "Enter your EasyEquities username and password.")
    conn = connection(db, user, create=True)
    conn.username, conn.password = body.username.strip(), seal(body.password)
    db.commit()
    sync.sync_platform(db, conn)
    return status(db, conn)


@router.delete("/platform")
def disconnect_platform(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = connection(db, user)
    if conn:
        conn.username = conn.password = conn.platform_status = conn.platform_error = conn.platform_debug = ""
        conn.snapshot = {}
        db.commit()
    return status(db, conn)


@router.put("/mail")
def connect_mail(body: MailIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if "@" not in body.address or not body.app_password.strip():
        raise HTTPException(400, "Enter the Gmail address and an app password.")
    conn = connection(db, user, create=True)
    if conn.mail_address.lower() != body.address.strip().lower():
        conn.mail_last_uid, conn.mail_uidvalidity = 0, ""
    conn.mail_address, conn.mail_password = body.address.strip(), seal(body.app_password.strip())
    db.commit()
    sync.sync_mail(db, conn)
    return status(db, conn)


def _api_base(request: Request):
    base = settings.api_url or str(request.base_url).rstrip("/")
    return base if "localhost" in base or "127.0.0.1" in base else base.replace("http://", "https://", 1)


def _callback_url(request: Request):
    return f"{_api_base(request)}/api/ee/google/callback"


@router.get("/google/start")
def google_start(request: Request, user: User = Depends(current_user)):
    """URL of Google's sign-in screen (read-only Gmail) for the EasyEquities email reader."""
    if not gmail.configured():
        raise HTTPException(400, "Google sign-in isn't set up yet (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
    state = make_token("google", user.id, timedelta(minutes=10))
    return {"url": gmail.auth_url(_callback_url(request), state)}


def _back(result: str, detail: str = ""):
    from urllib.parse import quote

    return RedirectResponse(f"{settings.frontend_url}/?google={result}{'&detail=' + quote(detail[:200]) if detail else ''}"
                            "#google", status_code=302)


@router.get("/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)):
    """Google sends the browser back here after sign-in; no login token in this request, the state carries the user."""
    try:
        user_id = int(read_token(state, "google")["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return _back("error", "The sign-in link expired. Try again.")
    if error or not code:
        return _back("error", "Google sign-in was cancelled." if error == "access_denied" else f"Google said: {error}")
    user = db.get(User, user_id)
    if not user:
        return _back("error", "Unknown user.")
    try:
        tokens = gmail.exchange(code, _callback_url(request))
    except mail.MailError as e:
        return _back("error", str(e))
    conn = connection(db, user, create=True)
    if conn.mail_address.lower() != tokens["email"].lower() or not conn.mail_password.startswith(gmail.PREFIX):
        conn.mail_last_uid, conn.mail_uidvalidity = 0, ""
    conn.mail_address, conn.mail_password = tokens["email"], gmail.PREFIX + seal(tokens["refresh_token"])
    db.commit()
    sync.sync_mail(db, conn)
    return _back("ok" if conn.mail_status == "ok" else "error", conn.mail_error)


@router.delete("/mail")
def disconnect_mail(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = connection(db, user)
    if conn:
        conn.mail_address = conn.mail_password = conn.mail_status = conn.mail_error = ""
        db.commit()
    return status(db, conn)


@router.post("/sync")
def sync_now(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = connection(db, user)
    if not conn:
        raise HTTPException(400, "Connect EasyEquities or Gmail first.")
    added = 0
    if conn.username and conn.password:
        sync.sync_platform(db, conn)
    if conn.mail_address and conn.mail_password:
        added = sync.sync_mail(db, conn)
    return {**status(db, conn), "new_emails": added}


@router.get("/transactions")
def platform_transactions(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = connection(db, user)
    rows = sync.statement_rows(db, conn) if conn else []
    return {"rows": rows, "totals": sync.statement_totals(rows), **sync.statement_breakdown(db, rows),
            "reader": reader.status(db, user.id, conn)}


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def statement_info(name: str, accounts: dict):
    """Account, kind and period from a statement file name like 'EE1720926-7814224 Name - Monthly Statement ...'."""
    import re

    num = re.search(r"EE\d+-\d+", name)
    kind = "tax" if re.search(r"tax", name, re.I) else "monthly" if re.search(r"monthly", name, re.I) else "other"
    period = ""
    m = re.search(r"(20\d{2})[-_ /]?(0[1-9]|1[0-2])(?:[-_ /]?(\d{2}))?", name)
    if m:
        period = f"{m.group(1)}-{m.group(2)}"
    else:
        m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ _-]*(20\d{2}|\d{2})(?!\d)", name, re.I)
        if m:
            year = m.group(2) if len(m.group(2)) == 4 else "20" + m.group(2)  # "Aug26" -> 2026
            period = f"{year}-{MONTHS[m.group(1).lower()]:02d}"
        else:
            m = re.search(r"(20\d{2})[_/-](20\d{2})", name)  # tax year "2025_2026"
            if m:
                period = f"{m.group(1)}/{m.group(2)[2:]}"
            else:
                m = re.search(r"(20\d{2})", name)
                period = m.group(1) if m else ""
    number = num.group(0) if num else ""
    return {"account": accounts.get(number) or number or "Other", "account_number": number, "kind": kind, "period": period}


@router.get("/statements")
def statements(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = connection(db, user)
    items = ((conn.snapshot or {}).get("statements") or []) if conn else []
    # EasyEquities account numbers -> names, learnt from the emails (EE1720926-7814224 = EasyEquities ZAR).
    accounts = dict(db.execute(select(EEMail.account_number, EEMail.account).where(
        EEMail.user_id == user.id, EEMail.account_number != "", EEMail.account != "")).all())
    from .models import EEStatementDoc

    docs = {d.name: d for d in db.scalars(select(EEStatementDoc).where(EEStatementDoc.user_id == user.id))}
    out = [{"id": i, "name": x["name"], **statement_info(x["name"], accounts),
            "read": x["name"] in docs and docs[x["name"]].status == "ok",
            "lines": docs[x["name"]].lines_found if x["name"] in docs else 0} for i, x in enumerate(items)]
    return sorted(out, key=lambda r: (r["account"], r["kind"], r["period"]), reverse=False)


@router.get("/statements/reader")
def reader_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return reader.status(db, user.id, connection(db, user))


class PdfPasswordIn(BaseModel):
    password: str


@router.put("/statements/password")
def set_pdf_password(body: PdfPasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The password EasyEquities locks statement PDFs with. Stored sealed; failed statements are retried with it."""
    from .models import EESetting, EEStatementDoc

    row = db.scalar(select(EESetting).where(EESetting.user_id == user.id))
    if not row:
        row = EESetting(user_id=user.id)
        db.add(row)
    row.pdf_password = seal(body.password.strip()) if body.password.strip() else ""
    db.query(EEStatementDoc).filter(EEStatementDoc.user_id == user.id, EEStatementDoc.status == "error").delete()
    db.commit()
    return reader.status(db, user.id, connection(db, user))


@router.post("/statements/read")
def read_statements(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Read the next batch of statement PDFs into the database."""
    conn = connection(db, user)
    if not conn or not conn.username:
        raise HTTPException(400, "Connect EasyEquities first.")
    try:
        result = reader.read_batch(db, conn)
    except platform.PlatformError as e:
        raise HTTPException(502, f"EasyEquities: {e}")
    return {**result, **reader.status(db, user.id, conn)}


@router.post("/statements/reparse")
def reparse_statements(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return reader.reparse(db, user.id)


@router.get("/statements/{idx}/text")
def statement_text(idx: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """What C-Lab read from a statement: its text and the money lines found in it."""
    from .models import EEStatementDoc, EEStatementLine

    conn = connection(db, user)
    items = ((conn.snapshot or {}).get("statements") or []) if conn else []
    if not 0 <= idx < len(items):
        raise HTTPException(404, "Statement not found.")
    doc = db.scalar(select(EEStatementDoc).where(EEStatementDoc.user_id == user.id, EEStatementDoc.name == items[idx]["name"]))
    if not doc:
        raise HTTPException(404, "Not read yet. Press Read statements.")
    lines = db.scalars(select(EEStatementLine).where(EEStatementLine.doc_id == doc.id).order_by(EEStatementLine.line_no))
    return {"name": doc.name, "status": doc.status, "error": doc.error, "pages": doc.pages, "text": doc.body,
            "lines": [{"date": x.date.isoformat() if x.date else None, "description": x.description, "amount": x.amount,
                       "balance": x.balance, "category": x.category} for x in lines]}


@router.get("/statements/{idx}")
def download_statement(idx: int, inline: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Logs in to EasyEquities and passes the PDF through; nothing is stored."""
    conn = connection(db, user)
    items = ((conn.snapshot or {}).get("statements") or []) if conn else []
    if not conn or not conn.username or not 0 <= idx < len(items):
        raise HTTPException(404, "Statement not found. Sync EasyEquities first.")
    p = platform.Platform()
    try:
        p.login(conn.username, unseal(conn.password))
        pdf = p.download(items[idx]["url"])
    except platform.PlatformError as e:
        raise HTTPException(502, f"EasyEquities: {e}")
    name = items[idx]["name"].replace('"', "") or "statement.pdf"
    how = "inline" if inline else "attachment"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'{how}; filename="{name}"'})


@router.post("/reparse")
def reparse(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return sync.reparse(db, user.id)


def mail_out(m: EEMail, body=False):
    out = {k: getattr(m, k) for k in ("id", "kind", "parsed", "account", "instrument", "contract_code", "side",
                                      "quantity", "price", "currency", "value", "costs", "total", "reference",
                                      "done", "txn_id", "subject", "sender")}
    out["date"] = m.date.isoformat() if m.date else None
    out["received_at"] = _dt(m.received_at)
    if body:
        out["body"] = m.body
    return out


@router.get("/mails")
def mails(kind: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(EEMail).where(EEMail.user_id == user.id)
    if kind:
        q = q.where(EEMail.kind == kind)
    return [mail_out(m) for m in db.scalars(q.order_by(EEMail.received_at.desc(), EEMail.id.desc()).limit(500))]


def own_mail(db, mail_id, user):
    m = db.get(EEMail, mail_id)
    if not m or m.user_id != user.id:
        raise HTTPException(404, "Not found.")
    return m


@router.get("/mails/{mail_id}")
def mail_detail(mail_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return mail_out(own_mail(db, mail_id, user), body=True)


@router.patch("/mails/{mail_id}")
def update_mail(mail_id: int, body: MailPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = own_mail(db, mail_id, user)
    if body.done is not None:
        m.done = body.done
    db.commit()
    return mail_out(m)
