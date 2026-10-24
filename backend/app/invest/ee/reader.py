# Statement reader: downloads EasyEquities' printable statements (PDF) a batch at a time, keeps their text, and saves
# every money line to the database to the cent. The text stays, so a better parser can be re-run without downloading.
import io
import logging
import re
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...security import unseal
from . import mail, platform
from .models import (EEConnection, EEHoldingMonth, EEMail, EEMonthFigure, EESetting, EEStatementDoc,
                     EEStatementLine)

log = logging.getLogger(__name__)
BATCH = 25

MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DATE_RES = [
    (re.compile(r"^(\d{4})[-/](\d{2})[-/](\d{2})\b"), lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b"), lambda m: date(int(m[3]), int(m[2]), int(m[1]))),
    (re.compile(r"^(\d{1,2})[ -]([A-Za-z]{3})[a-z]*[ -](\d{4})\b"), lambda m: date(int(m[3]), MONTHS[m[2].lower()], int(m[1]))),
]
# A money value: optional sign/brackets/currency, thousands separated by space or comma, any number of decimals.
AMOUNT = re.compile(r"\(?-?\s?(?:R|\$|£|€|ZAR|USD|GBP)?\s?-?\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?\)?|\(?-?\d+\.\d+\)?")


def pdf_text(data: bytes, extra_passwords=()):
    """(text, pages). Uses pypdf's layout mode so table columns stay on one line."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # EasyEquities locks its statements; usually only against editing, so a blank password opens them.
        for password in ("", *extra_passwords):
            try:
                if reader.decrypt(password):
                    break
            except Exception:
                continue
        else:
            raise ValueError("statement is password-protected (blank password didn't open it)")
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            pages.append(page.extract_text() or "")
    return "\n".join(pages), len(reader.pages)


def _to_float(token: str):
    t = token.strip()
    neg = t.startswith("(") and t.endswith(")") or "-" in t
    digits = re.sub(r"[^\d.]", "", t)
    if not digits or digits == ".":
        return None
    try:
        v = float(digits)
    except ValueError:
        return None
    return -v if neg else v


def _date(line: str):
    for rx, make in DATE_RES:
        m = rx.match(line)
        if m:
            try:
                return make(m), line[m.end():]
            except (ValueError, KeyError):
                return None, line
    return None, line


def parse_lines(body: str):
    """Rows of (date, description, amount, balance) from statement text. Lines without a leading date are skipped."""
    out = []
    for n, raw in enumerate(body.splitlines()):
        line = raw.strip()
        d, rest = _date(line)
        if not d:
            continue
        tokens = [(m.start(), m.group(0)) for m in AMOUNT.finditer(rest) if re.search(r"\d", m.group(0))]
        # Only numbers at the end of the line are money columns (the description can contain numbers, e.g. "@15%").
        tail, cursor = [], len(rest)
        for start, tok in reversed(tokens):
            gap = rest[start + len(tok):cursor]
            if gap.strip():
                break
            tail.insert(0, tok)
            cursor = start
        values = [v for v in (_to_float(t) for t in tail) if v is not None]
        if not values:
            continue
        desc = re.sub(r"\s{2,}", " ", rest[:cursor]).strip(" -|")
        amount, balance = (values[-2], values[-1]) if len(values) >= 2 else (values[0], None)
        out.append({"line_no": n, "date": d, "description": desc[:400], "amount": amount, "balance": balance})
    return out


MONTH_COL = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{4})\b")
FIGURE_KINDS = [  # 12-month summary row label -> what it is (first match wins)
    ("opening", r"opening"),
    ("closing", r"closing"),
    ("costs", r"withholding|tax|charge|fee|cost|expense|vat|levy"),
    ("income", r"dividend|interest|distribution|income"),
    ("money_in", r"contribution|deposit|transfer.?in|funds? in|top.?up"),
    ("money_out", r"withdraw|transfer.?out|funds? out|payment"),
    ("growth", r"gain|loss|growth|movement"),
]


def _cols(line: str):
    return [c for c in re.split(r"\s{2,}", line.strip()) if c]


def _num(cell: str):
    cell = cell.strip()
    if not re.fullmatch(r"\(?-?(?:R|\$|£|€)?\s?-?[\d ,]*\.?\d+%?\)?", cell):
        return None
    v = _to_float(cell.rstrip("%"))
    return v


def figure_kind(label: str) -> str:
    low = label.lower()
    for kind, pattern in FIGURE_KINDS:
        if re.search(pattern, low):
            return kind
    return "other"


def parse_monthly(body: str):
    """A monthly statement's 12-month summary and month-end holdings table."""
    lines = [ln.rstrip() for ln in body.splitlines()]
    months, figures, holdings = [], [], []
    in_holdings = False
    for ln in lines:
        text = ln.strip()
        if not text:
            continue
        found = MONTH_COL.findall(text)
        if len(found) >= 6:  # the summary's month header: "Sep 2025  Oct 2025 ..."
            months = [f"{y}-{MONTHS[m.lower()]:02d}" for m, y in found]
            continue
        cols = _cols(text)
        if re.search(r"\bInstrument\b", text) and re.search(r"Curr\.? ?Value|Value", text):
            in_holdings = True
            continue
        if in_holdings:
            if re.match(r"(?i)total\b", text):
                in_holdings = False
                continue
            nums = [_num(c) for c in cols[1:]]
            if len(cols) >= 7 and all(n is not None for n in nums):
                # read from the end, so a missing column on the left can't shift the important ones
                weight, value, price, cost_price, cost, qty = nums[-1], nums[-2], nums[-3], nums[-4], nums[-5], nums[-6]
                left = nums[:-6]
                row = {"instrument": cols[0][:200], "qty": qty, "cost": cost, "price": price, "value": value,
                       "weight": weight, "buy_qty": None, "buy_cost": None, "sell_qty": None, "sell_proceeds": None,
                       "profit": None}
                if len(left) >= 7:  # opening qty/cost, purchases qty/cost, sales qty/proceeds/profit
                    row.update(buy_qty=left[2], buy_cost=left[3], sell_qty=left[4], sell_proceeds=left[5], profit=left[6])
                holdings.append(row)
            elif holdings and len(cols) == 1 and not re.search(r"\d", text):
                holdings[-1]["instrument"] = (holdings[-1]["instrument"] + " " + text)[:200]  # wrapped name
            continue
        if months and len(cols) == len(months) + 1 and not re.search(r"\d", cols[0]):
            values = [_num(c) for c in cols[1:]]
            if all(v is not None for v in values):
                figures += [{"month": m, "label": cols[0][:80], "kind": figure_kind(cols[0]), "value": v}
                            for m, v in zip(months, values)]
    return {"months": months, "figures": figures, "holdings": holdings}


def shape(body: str, max_lines=40):
    """The statement's layout with every letter as 'a' and digit as '9': shows the format, not the contents."""
    lines = [re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", ln.rstrip())) for ln in body.splitlines() if ln.strip()]
    return lines[:max_lines]


def accounts_by_number(db: Session, user_id: int):
    return dict(db.execute(select(EEMail.account_number, EEMail.account).where(
        EEMail.user_id == user_id, EEMail.account_number != "", EEMail.account != "")).all())


def save_doc(db: Session, conn: EEConnection, item: dict, info: dict, body: str, pages: int, error: str = ""):
    from .sync import categorise

    doc = db.scalar(select(EEStatementDoc).where(EEStatementDoc.user_id == conn.user_id, EEStatementDoc.name == item["name"]))
    if not doc:
        doc = EEStatementDoc(user_id=conn.user_id, name=item["name"][:300])
        db.add(doc)
    doc.account, doc.account_number, doc.kind, doc.period = info["account"][:80], info["account_number"], info["kind"], info["period"]
    doc.body, doc.pages, doc.status, doc.error = body, pages, "error" if error else "ok", error[:500]
    db.flush()
    currency = mail.account_currency(doc.account)
    for model in (EEStatementLine, EEHoldingMonth, EEMonthFigure):
        db.execute(delete(model).where(model.doc_id == doc.id))
    rows = []
    if body and doc.kind == "monthly":
        m = parse_monthly(body)
        month = doc.period if re.fullmatch(r"\d{4}-\d{2}", doc.period or "") else (m["months"][-1] if m["months"] else "")
        for h in m["holdings"]:
            db.add(EEHoldingMonth(user_id=conn.user_id, doc_id=doc.id, account=doc.account[:80], currency=currency,
                                  month=month, **h))
        for f in m["figures"]:
            db.add(EEMonthFigure(user_id=conn.user_id, doc_id=doc.id, account=doc.account[:80], currency=currency,
                                 period=month, **f))
        doc.lines_found = len(m["holdings"]) + len(m["figures"])
        return doc
    rows = parse_lines(body) if body else []
    for r in rows:
        db.add(EEStatementLine(user_id=conn.user_id, doc_id=doc.id, account=doc.account[:80], currency=currency,
                               category=categorise("", r["description"]), **r))
    doc.lines_found = len(rows)
    return doc


def read_batch(db: Session, conn: EEConnection, limit=BATCH, client=None):
    """Read up to `limit` statements not read yet (newest first). Returns counts."""
    from .router import statement_info

    items = (conn.snapshot or {}).get("statements") or []
    done = set(db.scalars(select(EEStatementDoc.name).where(EEStatementDoc.user_id == conn.user_id,
                                                             EEStatementDoc.status == "ok")))
    accounts = accounts_by_number(db, conn.user_id)
    todo = [(it, statement_info(it["name"], accounts)) for it in items if it["name"] not in done]
    # monthly statements first (they hold the day-to-day lines), newest first
    todo = sorted(todo, key=lambda x: (x[1]["kind"] == "monthly", x[1]["period"]), reverse=True)[:limit]
    if not todo:
        return {"read_now": 0, "left": 0, "lines_now": 0}
    setting = db.scalar(select(EESetting).where(EESetting.user_id == conn.user_id))
    passwords = [unseal(setting.pdf_password)] if setting and setting.pdf_password else []
    p = client or platform.Platform()
    p.login(conn.username, unseal(conn.password))
    read = lines = 0
    for it, info in todo:
        try:
            body, pages = pdf_text(p.download(it["url"]), extra_passwords=passwords)
            doc = save_doc(db, conn, it, info, body, pages)
            if read == 0:  # the format only, to tune the parser without exposing the contents
                log.info("Statement layout (%s, %s): %s", info["kind"], info["period"], shape(body))
        except Exception as e:
            doc = save_doc(db, conn, it, info, "", 0, error=str(e))
            log.warning("Statement %s failed: %s", it["name"][:60], e)
        db.commit()
        read += 1
        lines += doc.lines_found
    left = len([1 for it in items if it["name"] not in done]) - read
    log.info("Statement reader: %d read, %d lines, %d left", read, lines, left)
    return {"read_now": read, "left": max(left, 0), "lines_now": lines}


def reparse(db: Session, user_id: int):
    """Re-run the (improved) parser over every stored statement text."""
    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user_id))
    docs = list(db.scalars(select(EEStatementDoc).where(EEStatementDoc.user_id == user_id, EEStatementDoc.status == "ok")))
    total = 0
    for doc in docs:
        info = {"account": doc.account, "account_number": doc.account_number, "kind": doc.kind, "period": doc.period}
        total += save_doc(db, conn, {"name": doc.name}, info, doc.body, doc.pages).lines_found
    db.commit()
    return {"statements": len(docs), "lines": total}


def lines_as_statement_rows(db: Session, user_id: int):
    """Statement lines in the same shape as sync.platform_transactions (so charts and totals can use either)."""
    rows = db.scalars(select(EEStatementLine).where(EEStatementLine.user_id == user_id)
                      .order_by(EEStatementLine.date.desc(), EEStatementLine.line_no))
    return [{"account": r.account, "currency": r.currency, "date": r.date.isoformat() if r.date else "",
             "action": "", "comment": r.description, "amount": r.amount, "contract_code": "", "id": f"pdf-{r.id}",
             "category": r.category, "balance": r.balance, "source": "pdf"} for r in rows]


def status(db: Session, user_id: int, conn: EEConnection = None):
    docs = list(db.scalars(select(EEStatementDoc).where(EEStatementDoc.user_id == user_id)))
    total = len(((conn.snapshot or {}).get("statements") or [])) if conn else 0
    setting = db.scalar(select(EESetting).where(EESetting.user_id == user_id))
    return {"total": total, "done": sum(1 for d in docs if d.status == "ok"),
            "pdf_password_set": bool(setting and setting.pdf_password),
            "last_error": next((d.error for d in sorted(docs, key=lambda d: d.read_at, reverse=True) if d.error), ""),
            "failed": sum(1 for d in docs if d.status == "error"), "lines": sum(d.lines_found for d in docs),
            "last_read": max((d.read_at for d in docs), default=None)}



def month_series(db: Session, user_id: int):
    """Per account and month, each summary kind (money_in, income, closing, ...) from the newest statement covering it."""
    rows = db.scalars(select(EEMonthFigure).where(EEMonthFigure.user_id == user_id)
                      .order_by(EEMonthFigure.period))  # later statements overwrite earlier ones
    out = {}
    for r in rows:
        key = (r.account, r.currency, r.month)
        cell = out.setdefault(key, {})
        cell.setdefault(r.period, {})
        cell[r.period][r.kind] = cell[r.period].get(r.kind, 0.0) + (r.value or 0.0)
    result = {}
    for (account, currency, month), by_period in out.items():
        result[(account, currency, month)] = by_period[max(by_period)]
    return result
