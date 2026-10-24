"""C-Lab as a read-only MCP server (Streamable HTTP, stateless JSON replies),
so an assistant such as assistant can answer questions about this account's
money. Nothing here can change data or move money.

Access is by MCP key: made under Settings, shown once, stored hashed, and
revocable one at a time. Connect with the API URL + "/mcp" and the key as a
Bearer token — or add the URL as a custom connector in Claude and sign in
(mcp_connect.py), which makes the key for you."""
import hashlib
import json
import secrets
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base
from .deps import current_user, get_db
from .models import BigId, User, created, pk, utcnow

KEY_PREFIX = "clab_"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class McpKey(Base):
    __tablename__ = "mcp_keys"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(BigId, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    hint: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    created_at: Mapped[datetime] = created()
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def key_user(request: Request, db: Session = Depends(get_db)) -> User:
    header = request.headers.get("authorization", "")
    key = header[7:].strip() if header.lower().startswith("bearer ") else ""
    row = db.scalar(select(McpKey).where(McpKey.key_hash == _hash(key))) if key.startswith(KEY_PREFIX) else None
    user = db.get(User, row.user_id) if row else None
    if not user or not user.is_active:
        # The header points MCP clients (e.g. Claude's custom connectors) at the sign-in (mcp_connect.py).
        from .mcp_connect import base_url
        from .mcp_oauth import www_authenticate
        raise HTTPException(401, "This MCP key was revoked or doesn't exist." if key else
                            "Sign in, or use an MCP key from Settings → Connect apps.",
                            headers={"WWW-Authenticate": www_authenticate(base_url(request))})
    row.last_used_at = utcnow()
    db.commit()
    return user


# ── Key management (normal login) ───────────────────────────────────────────

keys_router = APIRouter(prefix="/api/mcp/keys", tags=["mcp"])


class KeyIn(BaseModel):
    name: str = Field(default="assistant", max_length=80)


def _key_out(k: McpKey):
    return {"id": k.id, "name": k.name, "hint": k.hint, "created_at": k.created_at.isoformat(),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}


@keys_router.get("")
def list_keys(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_key_out(k) for k in db.scalars(select(McpKey).where(McpKey.user_id == user.id).order_by(McpKey.id))]


@keys_router.post("", status_code=201)
def create_key(body: KeyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    row = McpKey(user_id=user.id, name=body.name.strip() or "assistant", key_hash=_hash(key), hint=key[-4:])
    db.add(row)
    db.commit()
    return {**_key_out(row), "key": key}


@keys_router.delete("/{key_id}", status_code=204)
def revoke_key(key_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(McpKey, key_id)
    if not row or row.user_id != user.id:
        raise HTTPException(404, "Not found.")
    db.delete(row)
    db.commit()


# ── Tools (read-only) ───────────────────────────────────────────────────────

_S = {"type": "string"}
_I = {"type": "integer"}


def _tool(name, title, description, props=None, required=None):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": required or []},
            "annotations": {"readOnlyHint": True}}


TOOLS = [
    _tool("overview", "Money overview", "Net worth, investments, property, bank, debt, 12 months month by month, "
          "spending by category and upcoming dividends — the best starting point. Amounts in rand."),
    _tool("portfolio", "Portfolio", "Every holding with value, weight, gain, dividends; allocation; cash; "
          "and what the money would be worth in Satrix 40 / Satrix Property / US dollars instead."),
    _tool("markets", "Markets board", "USD/ZAR, EUR/ZAR, GBP/ZAR, JSE Top 40, SA property, gold and S&P 500 with day, month and year moves, plus "
          "company deals (buyouts, payouts, unbundlings) with whether buying now makes money."),
    _tool("quote", "Share quote", "Price, moves, dividend yield and 52-week range for one symbol (JSE shares end in .JO, e.g. GRT.JO).",
          {"symbol": _S}, ["symbol"]),
    _tool("watchlist", "Watchlist", "Watched shares with moves, yields and price alerts."),
    _tool("properties", "Property", "Each property: value, bond, equity, loan-to-value, yields and monthly cash flow."),
    _tool("bank_spending", "Bank spending", "Money in and out per month, fees, money in by category (incl. received from people) and spending by category from bank statements.",
          {"months": _I}),
    _tool("bank_transactions", "Bank transactions", "Recent bank transactions, optionally filtered by text or category.",
          {"limit": _I, "search": _S, "category": _S}),
    _tool("investment_transactions", "Investment transactions", "Buys, sells, dividends, deposits and fees, newest first.",
          {"limit": _I, "symbol": _S}),
    _tool("report", "Period report", "Net-worth change, investment returns, bank in/out and insights for a date range "
          "(YYYY-MM-DD; defaults to this month).", {"start": _S, "end": _S}),
]


def _jsonable(value):
    return json.loads(json.dumps(value, default=lambda v: float(v) if isinstance(v, Decimal) else
                                 v.isoformat() if isinstance(v, (date, datetime)) else str(v)))


def call_tool(name: str, args: dict, user: User, db: Session):
    from .ai.context import build
    from .banking.models import BankTxn
    from .banking.reader import charts as bank_charts
    from .invest import markets, portfolio
    from .invest import router as invest
    from .reports import report

    if name == "overview":
        return build(db, user.id)
    if name == "portfolio":
        s = portfolio.summary(db, user.id)
        keep = ("value", "invested", "gain", "return_pct", "cash", "allocation", "holdings", "benchmarks", "net_worth")
        return {k: s.get(k) for k in keep}
    if name == "markets":
        from .invest import deals

        try:
            company_deals = deals.get(db)["deals"]
        except Exception:
            company_deals = []
        return {**markets.board(user=user, db=db), "company_deals": company_deals}
    if name == "quote":
        return invest.quote(str(args.get("symbol", "")).strip().upper(), user=user, db=db)
    if name == "watchlist":
        return invest.watchlist(user=user, db=db)
    if name == "properties":
        return invest.properties(user=user, db=db)
    if name == "bank_spending":
        months = max(1, min(int(args.get("months") or 12), 36))
        c = bank_charts(db, user.id, months)
        return {"months": c.get("months"), "money_in_by_category": c.get("income_categories"),
                "spending_by_category": c.get("categories")}
    if name == "bank_transactions":
        q = select(BankTxn).where(BankTxn.user_id == user.id)
        if args.get("category"):
            q = q.where(BankTxn.category == args["category"])
        if args.get("search"):
            q = q.where(BankTxn.description.ilike(f"%{args['search']}%"))
        rows = db.scalars(q.order_by(BankTxn.date.desc(), BankTxn.id.desc()).limit(max(1, min(int(args.get("limit") or 50), 500))))
        return [{"date": t.date, "description": t.description, "amount": t.amount, "fee": t.fee,
                 "category": t.category, "bank": t.account} for t in rows]
    if name == "investment_transactions":
        rows = invest.list_txns(user=user, db=db)
        if args.get("symbol"):
            rows = [t for t in rows if (t["symbol"] or "").upper() == str(args["symbol"]).upper()]
        return rows[: max(1, min(int(args.get("limit") or 50), 500))]
    if name == "report":
        r = report(start=args.get("start") or "", end=args.get("end") or "", user=user, db=db)
        return {k: v for k, v in r.items() if k != "ai"}
    raise KeyError(name)


# ── JSON-RPC endpoint ───────────────────────────────────────────────────────

router = APIRouter(tags=["mcp"])


def _handle(msg, user: User, db: Session):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "c-lab", "title": "C-Lab", "version": "1.0"},
                  "instructions": "Read-only view of one person's money in South Africa (rand). Start with `overview`. "
                                  "This is a tracker, not financial advice."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        if name not in {t["name"] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        try:
            data = _jsonable(call_tool(name, params.get("arguments") or {}, user, db))
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": False}
        except HTTPException as e:
            result = {"content": [{"type": "text", "text": str(e.detail)}], "isError": True}
        except Exception as e:  # a failed tool is a result the assistant can read, not a protocol error
            db.rollback()
            result = {"content": [{"type": "text", "text": f"{name} failed: {str(e)[:300]}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


@router.post("/mcp")
async def mcp(request: Request, user: User = Depends(key_user), db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
    msgs = payload if isinstance(payload, list) else [payload]
    replies = [r for r in (_handle(m, user, db) for m in msgs) if r is not None]
    if not replies:
        return Response(status_code=202)
    return JSONResponse(replies if isinstance(payload, list) else replies[0])


@router.get("/mcp")
def mcp_no_stream():
    return Response(status_code=405, headers={"Allow": "POST"})
