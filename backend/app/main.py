import logging
import re
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mangum import Mangum
from sqlalchemy import text

from . import auth
from .config import settings
from .db import SessionLocal, init_db
from .invest import markets
from .ai.router import router as ai_router
from .banking.router import router as bank_router
from .onboarding import router as onboarding_router
from .reports import router as reports_router
from .invest.ee.router import router as ee_router
from .invest.router import router as invest_router
from .mcp import keys_router as mcp_keys_router, router as mcp_router
from .mcp_connect import router as mcp_connect_router

logging.getLogger().setLevel(logging.INFO)

if settings.auto_create_tables:
    init_db()

app = FastAPI(title="C-Lab API", redirect_slashes=False, docs_url="/docs" if settings.debug else None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_origin_regex=settings.cors_origin_regex,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def strip_trailing_slash(request: Request, call_next):
    path = request.scope["path"]
    if len(path) > 1 and path.endswith("/"):
        request.scope["path"] = path.rstrip("/")
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = {}
    for e in exc.errors():
        field = ".".join(str(p) for p in e["loc"] if p not in ("body", "query", "path", "form"))
        errors.setdefault(field or "non_field_errors", []).append(e["msg"])
    return JSONResponse({"detail": errors}, status_code=400)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    logging.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Internal server error."}, status_code=500)


for r in (auth.router, invest_router, markets.router, ee_router, bank_router, ai_router, onboarding_router, reports_router,
          mcp_keys_router, mcp_router, mcp_connect_router):
    app.include_router(r)


@app.get("/api/health")
def health():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        ok = True
    except Exception:
        ok = False
    finally:
        db.close()
    return {"status": "ok" if ok else "degraded", "database": ok}


_asgi = Mangum(app, lifespan="off")


# Lambda entrypoint: app.main.handler. The nightly EventBridge schedule calls it too:
# EasyEquities sync first (so prices are fresh), then price alerts.
def _numbers_only(v, depth=0):
    """Valuation JSON with keys and numbers kept, text shortened and account numbers masked, for diagnosis."""
    if depth > 4:
        return "…"
    if isinstance(v, dict):
        return {k: _numbers_only(x, depth + 1) for k, x in list(v.items())[:40] if "number" not in k.lower()}
    if isinstance(v, list):
        return [_numbers_only(x, depth + 1) for x in v[:12]]
    if isinstance(v, str):
        return re.sub(r"\d{6,}", "#", v)[:60]
    return v


def admin(event):
    """Direct Lambda invokes only (AWS credentials needed; a Function URL request can't produce this event shape)."""
    from sqlalchemy import func, select

    from .invest.ee import reader
    from .invest.ee.models import EESetting
    from .models import User
    from .security import seal

    db = SessionLocal()
    try:
        user = db.get(User, int(event["user_id"])) if event.get("user_id") else \
            db.scalar(select(User).where(func.lower(User.email) == str(event.get("email", "")).lower()))
        if not user:
            return {"ok": False, "error": "no such user"}
        if event.get("action") == "set_pdf_password":
            row = db.scalar(select(EESetting).where(EESetting.user_id == user.id)) or EESetting(user_id=user.id)
            row.pdf_password = seal(str(event.get("password", "")).strip())
            db.add(row)
            db.commit()
            return {"ok": True, **{k: v for k, v in reader.status(db, user.id).items() if k != "last_read"}}
        if event.get("action") == "reparse_statements":
            return {"ok": True, **reader.reparse(db, user.id)}
        if event.get("action") == "read_statements":
            from .invest.ee.models import EEConnection

            conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user.id))
            return {"ok": True, **reader.read_batch(db, conn, limit=int(event.get("limit", 25)))}
        if event.get("action") == "statement_shape":
            # Layout only (letters -> a, digits -> 9) of a stored statement, plus which lines the parser took.
            from .invest.ee.models import EEStatementDoc

            docs = list(db.scalars(select(EEStatementDoc).where(EEStatementDoc.user_id == user.id,
                                                                EEStatementDoc.status == "ok").order_by(EEStatementDoc.id)))
            kind = event.get("kind")
            docs = [d for d in docs if not kind or d.kind == kind]
            if not docs:
                return {"ok": False, "error": "no statements read"}
            doc = docs[min(int(event.get("index", 0)), len(docs) - 1)]
            lines = [ln.rstrip() for ln in doc.body.splitlines() if ln.strip()]
            start, count = int(event.get("start", 0)), int(event.get("count", 120))
            taken = {r["line_no"] for r in reader.parse_lines(doc.body)}
            numbered = [n for n, ln in enumerate(doc.body.splitlines()) if ln.strip()]
            # Table column titles only: lines with no digits, no @ or dots (no emails/urls) and 3+ spaced columns.
            headers = [re.sub(r"\s{2,}", " | ", ln.strip()) for ln in lines
                       if not re.search(r"[\d@.]", ln) and len(re.split(r"\s{3,}", ln.strip())) >= 3]
            return {"ok": True, "kind": doc.kind, "period": doc.period, "total_lines": len(lines), "headers": headers,
                    "lines_found": doc.lines_found,
                    "shape": [("* " if numbered[i] in taken else "  ") + s for i, s in
                              enumerate(reader.shape(doc.body, max_lines=10_000))][start:start + count]}
        if event.get("action") == "blog_shape":
            # What the blog reader sees: listing links, and the newest dividend post's lines with what was parsed.
            from .invest import blog

            links = []
            for listing in blog.LISTINGS:
                try:
                    links += [u for u in blog.post_links(blog._get(listing)) if u not in links]
                except Exception as e:
                    links.append(f"error {listing}: {e}")
            url = event.get("url") or next((u for u in links if "dividend" in u.lower()), None)
            if not url:
                return {"ok": False, "links": links}
            title, published, main = blog.content(blog._get(url))
            lines = blog.lines_of(main)
            start, count = int(event.get("start", 0)), int(event.get("count", 80))
            return {"ok": True, "links": links[:40], "url": url, "title": title, "published": str(published),
                    "lines": lines[start:start + count], "total_lines": len(lines),
                    "parsed": [{**i, "ldt": str(i["ldt"]), "pay_date": str(i["pay_date"])} for i in blog.parse_dividends(lines)][:30]}
        if event.get("action") == "flows_check":
            # Money-in/out as C-Lab sees it, and the labels behind transfer/other lines (digits masked).
            from collections import Counter

            from .invest.ee.models import EEConnection
            from .invest.ee.sync import statement_rows
            from .invest.portfolio import statement_flows

            conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user.id))
            rows = statement_rows(db, conn)
            flows = statement_flows(rows)
            labels = Counter()
            for r in rows:
                if r["category"] in ("transfer", "other", "withdrawal", "deposit"):
                    text = re.sub(r"\d", "9", f'{r["category"]}: {r.get("action", "")} | {r.get("comment", "")}')[:90]
                    labels[text] += 1
            return {"ok": True, "deposits": float(sum(a for _, a in flows if a > 0)),
                    "withdrawals": float(sum(-a for _, a in flows if a < 0)), "lines": len(rows),
                    "labels": labels.most_common(40)}
        if event.get("action") == "ee_check":
            # Account values vs their holdings, as EasyEquities sent them (no personal details).
            from .invest.ee.models import EEConnection

            conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user.id))
            out = []
            for a in (conn.snapshot or {}).get("accounts", []):
                top = (a.get("valuation") or {}).get("TopSummary") if isinstance(a.get("valuation"), dict) else None
                out.append({"name": a.get("name"), "value": a.get("value"), "holdings_value": a.get("holdings_value"),
                            "purchase_value": a.get("purchase_value"), "warnings": a.get("warnings"),
                            "top_summary": {k: v for k, v in (top or {}).items() if not isinstance(v, (list, dict))},
                            "account_values": (top or {}).get("AccountValues"),
                            "valuation": _numbers_only(a.get("valuation")),
                            "holdings": [{k: h.get(k) for k in ("name", "contract_code", "shares", "quantity", "current_value",
                                                                 "purchase_value", "current_price")} for h in a.get("holdings", [])][:30]})
            return {"ok": True, "taken_at": (conn.snapshot or {}).get("taken_at"), "accounts": out}
        if event.get("action") == "blog_view":
            from .invest import blog
            from .invest.models import PriceCache, WatchItem

            v = blog.view(db, [], list(db.scalars(select(WatchItem).where(WatchItem.user_id == user.id))))
            syms = [(s.instrument, s.symbol) for s in db.scalars(select(blog.BlogSymbol))][:60]
            cached = {c.symbol: (float(c.price) if c.price is not None else None, c.currency, c.error[:60])
                      for c in db.scalars(select(PriceCache).where(PriceCache.symbol.in_([x for _, x in syms if x])))}
            return {"ok": True, "rows": [{k: d[k] for k in ("instrument", "account", "ticker", "price", "state")}
                                         for d in v["upcoming"][:25]], "symbols": syms, "cached": cached}
        if event.get("action") == "refresh_blog":
            from .invest import blog

            if event.get("retry_tickers"):
                db.query(blog.BlogSymbol).filter(blog.BlogSymbol.symbol == "").delete()
                db.commit()
            if event.get("reread_empty"):
                db.query(blog.BlogPost).filter(blog.BlogPost.items == 0, blog.BlogPost.kind != "news").delete()
                db.commit()
            return {"ok": True, **blog.refresh(db, limit=int(event.get("limit", 12)))}
        if event.get("action") == "bank_shape":
            from .banking import reader as bank_reader

            return {"ok": True, **bank_reader.shape(db, user.id, int(event.get("index", 0)), int(event.get("start", 0)),
                                                    int(event.get("count", 120)))}
        if event.get("action") == "reparse_bank":
            from .banking import reader as bank_reader

            return {"ok": True, **bank_reader.reparse(db, user.id, only_empty=bool(event.get("only_empty")))}
        if event.get("action") == "rewarm_ai":
            # Numbers changed: drop this person's saved suggestions and chat answers, then make fresh ones.
            from .ai.router import AIChatCache, AISuggestion, warm_all

            db.query(AISuggestion).filter(AISuggestion.user_id == user.id).delete()
            db.query(AIChatCache).filter(AIChatCache.user_id == user.id).delete()
            db.commit()
            return {"ok": True, "made": warm_all(db, deadline=time.time() + 240)}
        if event.get("action") == "bank_accounts":
            from .banking.models import BankAccount
            from .banking.reader import redetect_kinds

            changed = redetect_kinds(db, user.id) if event.get("redetect") else 0
            if event.get("all_debit"):  # the person says every account is a debit/bank account
                for a in db.scalars(select(BankAccount).where(BankAccount.user_id == user.id)):
                    if a.kind != "bank":
                        a.kind, a.kind_set = "bank", True
                        changed += 1
                db.commit()
            return {"ok": True, "changed": changed, "accounts": [
                {"account": a.account[-8:], "bank": a.bank, "kind": a.kind, "kind_set": a.kind_set, "hidden": a.hidden,
                 "balance": a.balance, "balance_date": str(a.balance_date)} for a in db.scalars(select(BankAccount).where(BankAccount.user_id == user.id))]}
        if event.get("action") == "tidy_bank":
            from .banking.categorize import tidy

            return {"ok": True, **tidy(db, user.id)}
        if event.get("action") == "read_bank":
            from .banking import reader as bank_reader

            return bank_reader.read_batch(db, user.id, limit=int(event.get("limit", 15)))
        return {"ok": False, "error": "unknown action"}
    finally:
        db.close()


def handler(event, context):
    if isinstance(event, dict) and event.get("source") == "clab.admin":
        return admin(event)
    if isinstance(event, dict) and event.get("source") == "aws.events":
        from .invest.alerts import check_all
        from .invest.ee.sync import sync_all
        from .invest.portfolio import record_all

        db = SessionLocal()
        try:
            result = {}
            try:
                result["easyequities_synced"] = sync_all(db)
            except Exception:
                logging.exception("Nightly EasyEquities sync failed")
                db.rollback()
            try:
                from .banking.reader import read_all

                result["bank_statements"] = read_all(db)
            except Exception:
                logging.exception("Nightly bank statement read failed")
                db.rollback()
            try:
                from .invest import pulse

                pulse.get(db, refresh=True)
            except Exception:
                logging.exception("Nightly market pulse failed")
                db.rollback()
            try:
                from .invest.blog import refresh

                result["blog"] = refresh(db)
            except Exception:
                logging.exception("Nightly blog read failed")
                db.rollback()
            try:
                from .invest import deals

                deals.get(db, refresh=True)
            except Exception:
                logging.exception("Nightly company deals failed")
                db.rollback()
            try:
                from .ai.router import warm_all

                left = context.get_remaining_time_in_millis() / 1000 if context else 240
                result["ai_suggestions"] = warm_all(db, deadline=time.time() + left - 45)
            except Exception:
                logging.exception("Nightly AI suggestions failed")
                db.rollback()
            result["alerts_sent"] = check_all(db)
            result["snapshots"] = record_all(db)
            return result
        finally:
            db.close()
    return _asgi(event, context)
