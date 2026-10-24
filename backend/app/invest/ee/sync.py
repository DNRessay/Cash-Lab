import logging
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import utcnow
from ...security import unseal
from .. import prices
from ..models import InvestTxn
from . import mail, platform
from .models import EEConnection, EEMail

log = logging.getLogger(__name__)

TXN_KINDS = ("trade", "deposit", "withdrawal")
KEEP_PLATFORM_TXNS = 3000  # the account statement: dividends, fees, interest, trades, deposits


def symbol_for(code: str, name: str = "", account: str = "") -> str:
    """EasyEquities contract code -> the symbol C-Lab prices it by. EQU.ZA.AIP -> AIP.JO, EQU.US.TSLA -> TSLA."""
    if code:
        parts = code.split(".")
        market, tick = (parts[1] if len(parts) > 2 else ""), parts[-1]
        if "properties" in (account or "").lower() or tick.startswith("PROP"):
            return f"EE:{tick}"
        if market == "ZA":
            return f"{tick}.JO"
        if market == "US":
            return tick
        return f"EE:{tick}"
    return "EE:" + re.sub(r"[^A-Z0-9]+", "", (name or "").upper())[:24]


def asset_class_for(account: str, name: str) -> str:
    if "properties" in (account or "").lower():
        return "easyproperties"
    if re.search(r"\b(ETF|ETN)\b|Satrix|CoreShares|Sygnia|1nvest|Ashburton", name or "", re.I):
        return "etf"
    if re.search(r"crypto|bitcoin|ethereum", name or "", re.I):
        return "crypto"
    return "share"


def rand_rate(db: Session, currency: str, day: date = None):
    if currency in ("", "ZAR"):
        return 1.0
    q = prices.quote(db, "ZAR=X" if currency == "USD" else f"{currency}ZAR=X")
    if not q or q.price is None:
        return None
    if day and q.history:
        return prices.price_on(q.history, day)
    return float(q.price)


# ── Platform ────────────────────────────────────────────────────────────────

def sync_platform(db: Session, conn: EEConnection, client=None, ep_fetch=None):
    conn.platform_tried_at = utcnow()
    try:
        snap = platform.snapshot(conn.username, unseal(conn.password), client=client, ep_fetch=ep_fetch)
    except platform.PlatformError as e:
        conn.platform_status, conn.platform_error, conn.platform_error_stage = "error", e.message, e.stage
        conn.platform_debug = e.page
        log.warning("EasyEquities sync failed for user %s at %s: %s shape=%s", conn.user_id, e.stage, e.message,
                    platform.page_shape(e.page))
    except Exception as e:  # never lose the last good snapshot to an unexpected crash
        conn.platform_status, conn.platform_error, conn.platform_error_stage = "error", str(e)[:500], "unexpected"
        log.exception("EasyEquities sync crashed for user %s", conn.user_id)
    else:
        for acc in snap["accounts"]:
            if isinstance(acc.get("transactions"), list):
                acc["transactions"] = acc["transactions"][-KEEP_PLATFORM_TXNS:]
        snap["taken_at"] = utcnow().isoformat(timespec="seconds")
        conn.snapshot = snap
        conn.platform_status, conn.platform_error, conn.platform_error_stage, conn.platform_debug = "ok", "", "", ""
        conn.platform_synced_at = utcnow()
        log.info("EasyEquities sync ok for user %s: %s", conn.user_id,
                 [(a.get("name"), len(a.get("holdings", [])), a.get("warnings", [])) for a in snap["accounts"]])
        log.info("EasyEquities statement links: %s", snap.get("statement_links"))
        log.info("EasyProperties: %s", snap.get("easyproperties"))
        zero = [(type(t.get("DebitCredit")).__name__, repr(t.get("DebitCredit"))[:20]) for a in snap["accounts"]
                for t in (a.get("transactions") or []) if isinstance(t, dict) and platform.money(t.get("DebitCredit")) == 0][:8]
        log.info("EasyEquities zero-amount statement lines (type, raw): %s", zero)
        log.info("EasyEquities statements: %d listed %s", len(snap.get("statements") or []), snap.get("statements_error", ""))
        log.info("EasyEquities account switch: %s",
                 {a.get("name"): a.get("switch_debug") for a in snap["accounts"] if a.get("switch_debug")})
    db.commit()
    return conn.platform_status == "ok"


def platform_view(db: Session, conn: EEConnection):
    """Last good snapshot in rand (demo accounts left out), plus per-symbol prices the portfolio can use."""
    snap = (conn.snapshot or {}) if conn else {}
    accounts, symbol_prices, total = [], {}, 0.0
    for acc in snap.get("accounts", []):
        if platform.is_demo(acc.get("name")):
            continue
        cur = mail.account_currency(acc.get("name", ""))
        rate = rand_rate(db, cur) or 0.0
        rows = []
        for h in acc.get("holdings", []):
            sym = symbol_for(h.get("contract_code", ""), h.get("name", ""), acc.get("name", ""))
            value = (h.get("current_value") or 0) * rate
            cost = (h.get("purchase_value") or 0) * rate
            if h.get("current_price") is not None:
                symbol_prices[sym] = h["current_price"] * rate
            rows.append({**h, "symbol": sym, "account": acc.get("name"), "currency": cur,
                         "asset_class": asset_class_for(acc.get("name", ""), h.get("name", "")),
                         "price_zar": (h["current_price"] * rate) if h.get("current_price") is not None else None,
                         "value_zar": value, "cost_zar": cost, "gain_zar": value - cost})
        value = (acc.get("value") or 0) * rate
        holdings_value = sum(r["value_zar"] for r in rows)
        if value < holdings_value - max(2.0, 0.02 * holdings_value):
            # EasyEquities' account value can be stale or not the total (seen: R14 for an account holding R125);
            # a wallet isn't R100 overdrawn, so count no cash rather than a large negative.
            value = holdings_value
        total += value
        accounts.append({"id": acc.get("id"), "name": acc.get("name"), "currency": cur, "rate": rate,
                         "value": acc.get("value"), "value_zar": value, "holdings": rows,
                         # EasyEquities' account value = holdings + "Funds to Invest", which goes negative when
                         # costs are owed; keep the sign so worth matches EasyEquities.
                         "cash_zar": value - holdings_value,
                         "cost_zar": sum(r["cost_zar"] for r in rows), "warnings": acc.get("warnings", [])})
    return {"accounts": accounts, "value_zar": total, "taken_at": snap.get("taken_at")}, symbol_prices


def _field(t, *keys):
    for k in keys:
        if t.get(k) not in (None, ""):
            return t[k]
    return None


# First match wins, so "dividend withholding tax" is tax and "broker commission" is a fee.
CATEGORIES = [
    ("tax", r"withholding|dividend tax|\bdwt\b|\bwht\b|securities transfer tax|\bstt\b"),
    ("dividend", r"dividend|distribution|\bdiv\b|drip"),
    ("interest", r"interest"),
    ("fee", r"fee|custody|admin|charge|commission|vat|levy|subscription|thrive|cost"),
    ("deposit", r"deposit|\beft\b|card funding|funded|top.?up"),
    ("withdrawal", r"withdraw"),
    ("transfer", r"transfer|conversion|exchange|fx|forex"),
    ("trade", r"\bbuy\b|\bsell\b|bought|sold|purchase|trade|allocation|redemption"),
]


def categorise(action: str, comment: str) -> str:
    text = f"{action} {comment}".lower()
    for name, pattern in CATEGORIES:
        if re.search(pattern, text):
            return name
    return "other"


def statement_totals(rows):
    """Money per category, per account and year. Fees/tax are shown as positive amounts paid."""
    out = {}
    for r in rows:
        if r["amount"] is None:
            continue
        key = (r["account"], (r["date"] or "")[:4])
        t = out.setdefault(key, {"account": r["account"], "year": key[1], "currency": r["currency"]})
        t[r["category"]] = round(t.get(r["category"], 0.0) + r["amount"], 5)  # tiny dividends are fractions of a cent
    return sorted(out.values(), key=lambda t: (t["year"], t["account"] or ""), reverse=True)


COST_TYPES = [
    ("Dividend tax", r"withholding|dividend tax|\bdwt\b|\bwht\b"),
    ("VAT", r"\bvat\b|value.added"),
    ("Brokerage", r"brokerage|commission|broker"),
    ("Taxes and levies", r"securities transfer|\bstt\b|levy|\bipl\b|investor protection|sec fee|finra"),
    ("Account fees", r"custody|admin|platform|account fee|subscription|thrive|monthly fee|settlement"),
]


def cost_type(comment: str, action: str = "") -> str:
    text = f"{action} {comment}".lower()
    for name, pattern in COST_TYPES:
        if re.search(pattern, text):
            return name
    return "Other fees"


def statement_breakdown(db: Session, rows):
    """For the pie and bar charts: income and costs per account, and costs by type. All in rand."""
    rates, by_account, types = {}, {}, {}
    for r in rows:
        if r["amount"] is None or r["category"] not in ("dividend", "interest", "fee", "tax"):
            continue
        if r["currency"] not in rates:
            rates[r["currency"]] = rand_rate(db, r["currency"]) or 0.0
        v = abs(r["amount"]) * rates[r["currency"]]
        acc = by_account.setdefault(r["account"], {"account": r["account"], "income": 0.0, "costs": 0.0})
        if r["category"] in ("dividend", "interest"):
            acc["income"] += v
        else:
            acc["costs"] += v
            t = cost_type(r["comment"], r["action"])
            types[t] = types.get(t, 0.0) + v
    return {"by_account": [{**a, "income": round(a["income"], 5), "costs": round(a["costs"], 5)}
                           for a in sorted(by_account.values(), key=lambda a: -(a["income"] + a["costs"]))],
            "cost_types": [{"type": k, "amount": round(v, 5)} for k, v in sorted(types.items(), key=lambda kv: -kv[1])]}


def platform_transactions(conn: EEConnection):
    """EasyEquities' own transaction history from the last snapshot, newest first."""
    out = []
    for acc in ((conn.snapshot or {}) if conn else {}).get("accounts", []):
        if platform.is_demo(acc.get("name")) or not isinstance(acc.get("transactions"), list):
            continue
        for t in acc["transactions"]:
            if not isinstance(t, dict):
                continue
            raw_date = str(_field(t, "TransactionDate", "Date", "CreatedDate") or "")
            m = re.search(r"/Date\((\d+)", raw_date)  # .NET style /Date(1757315681000)/
            day = date.fromtimestamp(int(m.group(1)) / 1000).isoformat() if m else raw_date[:10]
            out.append({"account": acc.get("name"), "currency": mail.account_currency(acc.get("name", "")),
                        "date": day, "action": _field(t, "Action", "TransactionType") or "",
                        "comment": _field(t, "Comment", "Description") or "",
                        "amount": platform.money(_field(t, "DebitCredit", "Amount")),
                        "contract_code": _field(t, "ContractCode") or "", "id": _field(t, "TransactionId", "LogId")})
            out[-1]["category"] = categorise(out[-1]["action"], out[-1]["comment"])
    return sorted(out, key=lambda r: r["date"] or "", reverse=True)


# ── Email ───────────────────────────────────────────────────────────────────

def apply_parse(row: EEMail, html: str = ""):
    p = mail.parse(row.sender, row.subject, row.body, html, row.received_at)
    for field in ("kind", "account", "account_number", "instrument", "side", "currency", "reference"):
        setattr(row, field, p[field] or "")
    for field in ("parsed", "quantity", "price", "value", "costs", "total"):
        setattr(row, field, p[field])
    row.contract_code = p["contract_code"] or row.contract_code or ""
    d = p["date"] or row.received_at
    row.date = d.date() if d else None


def importable(row: EEMail) -> bool:
    if not row.parsed or row.kind not in TXN_KINDS or row.txn_id:
        return False
    if row.kind in ("deposit", "withdrawal") and row.currency not in ("", "ZAR"):
        return False  # foreign wallets are funded from the rand wallet, not new money
    return True


def import_txn(db: Session, row: EEMail):
    """Create the matching portfolio transaction. Returns it, or None if skipped."""
    if not importable(row):
        return None
    rate = rand_rate(db, row.currency, row.date)
    if rate is None:
        return None
    if row.kind == "trade":
        kind = "sell" if row.side == "sell" else "buy"
        txn = InvestTxn(user_id=row.user_id, date=row.date, kind=kind,
                        symbol=symbol_for(row.contract_code, row.instrument, row.account), name=row.instrument[:200],
                        asset_class=asset_class_for(row.account, row.instrument), quantity=row.quantity,
                        price=round(row.price * rate, 4) if row.price is not None else None,
                        amount=round((row.value or 0) * rate, 2), fees=round((row.costs or 0) * rate, 2))
    else:
        txn = InvestTxn(user_id=row.user_id, date=row.date, kind=row.kind, amount=round(row.value or 0, 2), fees=0)
    fx = f" at R{rate:.4f}/{row.currency}" if row.currency not in ("", "ZAR") else ""
    txn.notes = f"EasyEquities {row.account} {('#' + row.reference) if row.reference else ''}{fx}".strip()
    txn.source = "easyequities"
    db.add(txn)
    db.flush()
    row.txn_id = txn.id
    return txn


def sync_mail(db: Session, conn: EEConnection, fetcher=None):
    from . import gmail

    try:
        if conn.mail_password.startswith(gmail.PREFIX):  # signed in with Google
            messages, last_uid, validity = (fetcher or gmail.fetch)(
                unseal(conn.mail_password[len(gmail.PREFIX):]), since_uid=conn.mail_last_uid,
                uidvalidity=conn.mail_uidvalidity)
        else:  # older app-password connections keep working over IMAP
            messages, last_uid, validity = (fetcher or mail.fetch)(
                conn.mail_address, unseal(conn.mail_password), since_uid=conn.mail_last_uid,
                uidvalidity=conn.mail_uidvalidity)
    except mail.MailError as e:
        conn.mail_status, conn.mail_error = "error", str(e)[:500]
        db.commit()
        return 0
    except Exception as e:
        conn.mail_status, conn.mail_error = "error", f"unexpected: {e}"[:500]
        log.exception("Mail sync crashed for user %s", conn.user_id)
        db.commit()
        return 0
    known = set(db.scalars(select(EEMail.message_id).where(EEMail.user_id == conn.user_id)))
    added = 0
    for m in messages:
        if not mail.wanted(m["sender"]) or m["message_id"] in known:
            continue
        row = EEMail(user_id=conn.user_id, message_id=m["message_id"][:400], received_at=m["received"],
                     sender=m["sender"][:200], subject=m["subject"][:400], body=m["text"])
        apply_parse(row, m.get("html", ""))
        db.add(row)
        db.flush()
        import_txn(db, row)
        known.add(row.message_id)
        added += 1
    conn.mail_last_uid, conn.mail_uidvalidity = last_uid, validity
    conn.mail_status, conn.mail_error, conn.mail_synced_at = "ok", "", utcnow()
    log.info("Mail sync ok for user %s: %d fetched, %d new", conn.user_id, len(messages), added)
    db.commit()
    return added


def reparse(db: Session, user_id: int):
    """Re-run the (possibly fixed) parsers over every stored email and import anything new."""
    rows = list(db.scalars(select(EEMail).where(EEMail.user_id == user_id).order_by(EEMail.received_at)))
    imported = 0
    for row in rows:
        apply_parse(row)
        if import_txn(db, row):
            imported += 1
    db.commit()
    return {"emails": len(rows), "parsed": sum(1 for r in rows if r.parsed), "imported": imported}


def statement_rows(db: Session, conn: EEConnection):
    """Statement lines for charts and totals: the PDF statements (exact, full history) where read, plus the website's
    recent lines for anything newer than the last PDF of that account."""
    from .reader import lines_as_statement_rows

    web = platform_transactions(conn)
    pdf = lines_as_statement_rows(db, conn.user_id) if conn else []
    if not pdf:
        return web
    last = {}
    for r in pdf:
        last[r["account"]] = max(last.get(r["account"], ""), r["date"] or "")
    newer = [r for r in web if r["account"] not in last or (r["date"] or "") > last[r["account"]]]
    return sorted(pdf + newer, key=lambda r: r["date"] or "", reverse=True)


def sync_all(db: Session):
    """Nightly: every connected user."""
    from . import reader

    done = 0
    for conn in db.scalars(select(EEConnection)):
        if conn.username and conn.password:
            if sync_platform(db, conn):
                try:
                    reader.read_batch(db, conn)
                except Exception:
                    db.rollback()
                    log.exception("Statement reader failed for user %s", conn.user_id)
        if conn.mail_address and conn.mail_password:
            sync_mail(db, conn)
        done += 1
    return done
