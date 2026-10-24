# Bank statements from Gmail: find statement emails with PDF attachments, read every transaction,
# keep each account's latest balance. Uses the same "Sign in with Google" token as the EasyEquities mail reader.
import base64
import hashlib
import logging
import re
from collections import defaultdict
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..invest.ee import gmail
from ..invest.ee.models import EEConnection, EESetting
from ..security import unseal
from . import parsers
from .models import BankAccount, BankStatement, BankTxn, Liability

log = logging.getLogger(__name__)

# sender domain -> (bank key, display name). Parsers exist for capitec, tymebank, gotyme; others use the generic one.
BANKS = {
    "capitecbank.co.za": ("capitec", "Capitec"),
    "tymebank.co.za": ("tymebank", "TymeBank"),
    "gotyme.co.za": ("gotyme", "GoTyme"),
    "fnb.co.za": ("fnb", "FNB"),
    "absa.co.za": ("absa", "Absa"),
    "standardbank.co.za": ("standardbank", "Standard Bank"),
    "nedbank.co.za": ("nedbank", "Nedbank"),
    "discovery.co.za": ("discovery", "Discovery Bank"),
    "investec.co.za": ("investec", "Investec"),
    "investec.com": ("investec", "Investec"),
    "africanbank.co.za": ("africanbank", "African Bank"),
    "bankzero.co.za": ("bankzero", "Bank Zero"),
}
NAMES = {k: v for k, v in BANKS.values()}
QUERY = "has:attachment filename:pdf (statement OR statements) from:(" + " OR ".join(BANKS) + ")"

ACCOUNT_RE = re.compile(r"(?:account|acc|card)\s*(?:number|no\.?|nr\.?|#)?\s*[:.]?\s*((?:\d[\d \-*x]{5,}\d))", re.I)
CLOSING_RE = re.compile(r"closing\s+balance[^\d\-R]{0,30}(-?R?\s?-?[\d ,]+\.\d{2})\s*(cr|dr)?", re.I)
# A credit card statement shows a credit limit AND a minimum payment / amount due; a debit (bank) account statement
# can mention "credit card" in a fee table, so one phrase alone isn't enough.
CREDIT_LIMIT_RE = re.compile(r"credit\s+limit|available\s+credit", re.I)
CREDIT_DUE_RE = re.compile(r"minimum\s+(amount\s+|payment\s+)*(due|payable)|total\s+amount\s+due|payment\s+due\s+date", re.I)
LOAN_RE = re.compile(r"personal\s+loan|loan\s+account|home\s+loan|vehicle\s+finance|instal+ment\s+sale", re.I)
LOAN_DUE_RE = re.compile(r"instal+ment|outstanding\s+(capital|balance)|settlement\s+amount", re.I)

KNOWN = {"Income", "Savings", "Withdrawal", "Transfer", "Payments", "Cellphone", "Investments", "Fees", "Interest"}


def refresh_token(db: Session, user_id: int) -> str:
    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user_id))
    if not conn or not conn.mail_password.startswith(gmail.PREFIX):
        return ""
    return unseal(conn.mail_password[len(gmail.PREFIX):])


def passwords(db: Session, user_id: int):
    row = db.scalar(select(EESetting).where(EESetting.user_id == user_id))
    return (unseal(row.pdf_password),) if row and row.pdf_password else ()


def bank_for(sender: str, text: str = ""):
    sender = sender.lower()
    for domain, (key, _) in BANKS.items():
        if domain in sender:
            return key
    low = text.lower()
    for _, (key, name) in BANKS.items():
        if name.lower() in low:
            return key
    return "other"


def account_label(bank: str, text: str) -> str:
    m = ACCOUNT_RE.search(text)
    digits = re.sub(r"\D", "", m.group(1)) if m else ""
    return f"{NAMES.get(bank, bank.title())} ••{digits[-4:]}" if len(digits) >= 4 else NAMES.get(bank, bank.title())


def account_kind(text: str) -> str:
    head = text[:5000]
    if CREDIT_LIMIT_RE.search(head) and CREDIT_DUE_RE.search(head):
        return "credit"
    if LOAN_RE.search(head) and LOAN_DUE_RE.search(head):
        return "loan"
    return "bank"


def redetect_kinds(db: Session, user_id: int):
    """Re-check every account's type from all its statements (most say what it is), unless you chose it yourself."""
    from collections import Counter

    votes = defaultdict(Counter)
    for st in db.scalars(select(BankStatement).where(BankStatement.user_id == user_id, BankStatement.status == "ok")):
        kind = account_kind(st.body)
        st.kind = kind
        votes[st.account][kind] += 1
    changed = 0
    for acc in db.scalars(select(BankAccount).where(BankAccount.user_id == user_id, BankAccount.kind_set.is_(False))):
        kind = votes[acc.account].most_common(1)[0][0] if votes.get(acc.account) else "bank"
        if kind != acc.kind:
            acc.kind = kind
            changed += 1
    db.commit()
    return changed


# A statement's own summary lines ("Opening balance" etc.) — not money that moved.
SUMMARY_RE = re.compile(r"\b(opening|closing|brought forward|carried forward|b/f|c/f)\s*balance\b|^\s*summary\b", re.I)
FEE_LINE_RE = re.compile(r"^\s*fee\s*:|\(fee\)\s*$", re.I)
# Moves between pockets of the same account (GoalSave, "Transfer to Current account"): never income or spending.
OWN_MOVE_RE = re.compile(r"goalsave|savings pocket|\btransfer (?:to|from) (?:current|[\w ]*savings) account\b|own account|"
                         r"between (?:your |my )?accounts|round-?up|first savings|"
                         r"banking app transfer (?:to|received from) [^:]+: transfer", re.I)  # Capitec's savings pockets
# Money that came from (or went to) another person.
PEOPLE_RE = re.compile(r"payshap|pay by shapid|pay by account|send ?money|cash ?send|\beft\b|received from|"
                       r"immediate payment|instant payment|pay beneficiary|payment from", re.I)


def categorise(description: str, parsed: str = "", amount: float = 0.0, rules=()) -> str:
    """Bank fee lines first, then your rules, then the bank's own label (Capitec prints one), then the keyword lists."""
    from .categorize import categorise as by_keywords

    if FEE_LINE_RE.search(description or ""):
        return "Bank fees"
    low = " " + (description or "").lower() + " "
    for keyword, category in rules:
        if keyword and keyword in low:
            return category
    parsed = next((w for w in reversed(parsed.split()) if w in KNOWN), "") if parsed else ""
    if parsed and parsed in KNOWN - {"Payments", "Withdrawal"}:
        return {"Transfer": "Transfers", "Investments": "Transfers", "Cellphone": "Airtime & data", "Savings": "Transfers",
                "Fees": "Bank fees"}.get(parsed, parsed)
    return by_keywords(description, amount)


def closing_balance(text: str):
    m = None
    for m in CLOSING_RE.finditer(text):
        pass
    if not m:
        return None
    v = float(re.sub(r"[^\d.\-]", "", m.group(1)) or 0)
    return -abs(v) if (m.group(2) or "").lower() == "dr" else v


def _key(user_id, account, row):
    raw = f"{user_id}|{account}|{row['date']}|{row['signed']:.2f}|{row.get('balance')}|{row['description'][:120].lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def rows_from(parsed, kind, rules=()):
    out = []
    for r in parsed:
        if SUMMARY_RE.search(r["description"] or ""):
            continue  # "Summary Opening balance" is the statement's own total, not a payment
        amount = r["amount"] if r["type"] == "credit" else -r["amount"]
        fee = float(r.get("fee") or 0)
        desc = r["description"]
        if not fee and "(fee)" in desc.lower():
            fee = abs(amount)
        out.append({"date": r["date"], "description": desc, "signed": amount, "fee": fee,
                    "balance": r.get("balance"), "category": categorise(desc, r.get("category") or "", amount, rules),
                    "account_number": r.get("account_number") or ""})
    return out


def save(db: Session, st: BankStatement, text: str, parsed):
    """Store the statement's rows (skipping ones already stored from an overlapping statement) and update the account."""
    st.kind = account_kind(text)
    st.account = account_label(st.bank, text)
    from .categorize import rules_for

    rows = rows_from(parsed, st.kind, rules_for(db, st.user_id))
    # One PDF can hold several accounts (GoTyme's GoalSave pockets each have their own number and balance).
    for r in rows:
        digits = re.sub(r"\D", "", r["account_number"])
        r["account"] = f"{NAMES.get(st.bank, st.bank.title())} ••{digits[-4:]}" if len(digits) >= 4 else st.account
    db.flush()  # rows added for an earlier statement in this session must count as already stored
    db.query(BankTxn).filter(BankTxn.statement_id == st.id).delete()
    have = set(db.scalars(select(BankTxn.key).where(BankTxn.user_id == st.user_id)))
    seen_here = defaultdict(int)
    for r in rows:
        k = _key(st.user_id, r["account"], r)
        seen_here[k] += 1
        if seen_here[k] > 1:  # two identical lines on one statement (two R5 airtime buys) are both real
            k = k[:58] + f"-{seen_here[k]:05d}"
        if k in have:
            continue
        have.add(k)
        db.add(BankTxn(user_id=st.user_id, statement_id=st.id, key=k, bank=st.bank, account=r["account"], date=r["date"],
                       description=r["description"][:400], amount=round(r["signed"], 2), balance=r["balance"],
                       category=r["category"], fee=round(r["fee"], 2)))
    st.rows = len(rows)
    dated = [r for r in rows if r["balance"] is not None]
    main = [r for r in dated if r["account"] == st.account] or dated
    last = max(main, key=lambda r: r["date"]) if main else None
    st.closing_balance = last["balance"] if last else closing_balance(text)
    st.closing_date = last["date"] if last else (max(r["date"] for r in rows) if rows else
                                                 (st.received_at.date() if st.received_at else None))
    closing = {}
    for r in sorted(dated, key=lambda r: r["date"]):
        closing[r["account"]] = (r["balance"], r["date"])  # last line of each account on this statement
    if not closing and st.closing_balance is not None:
        closing[st.account] = (st.closing_balance, st.closing_date)
    for account, (balance, when) in closing.items():
        acc = db.scalar(select(BankAccount).where(BankAccount.user_id == st.user_id, BankAccount.account == account))
        if not acc:
            acc = BankAccount(user_id=st.user_id, bank=st.bank, account=account, name=account, kind=st.kind)
            db.add(acc)
            db.flush()
        if not acc.kind_set:
            acc.kind = st.kind
        if acc.balance_date is None or (when and when >= acc.balance_date):
            acc.balance, acc.balance_date = balance, when


def _walk(part):
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)


def search(token: str, limit_ids=500):
    headers = {"Authorization": f"Bearer {token}"}
    ids, page = [], None
    while len(ids) < limit_ids:
        r = requests.get(f"{gmail.API}/messages", headers=headers, timeout=30,
                         params={"q": QUERY, "maxResults": 100, **({"pageToken": page} if page else {})})
        if not r.ok:
            raise gmail.mail.MailError(f"Gmail search failed (HTTP {r.status_code})")
        data = r.json()
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    return ids


def attachments(token: str, msg_id: str):
    """(headers dict, [(filename, bytes)]) for every PDF attached to the message."""
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{gmail.API}/messages/{msg_id}", headers=headers, params={"format": "full"}, timeout=30)
    r.raise_for_status()
    msg = r.json()
    head = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    out = []
    for part in _walk(msg.get("payload", {})):
        name = part.get("filename") or ""
        if not name.lower().endswith(".pdf"):
            continue
        body = part.get("body", {})
        data = body.get("data")
        if not data and body.get("attachmentId"):
            a = requests.get(f"{gmail.API}/messages/{msg_id}/attachments/{body['attachmentId']}", headers=headers, timeout=60)
            a.raise_for_status()
            data = a.json().get("data")
        if data:
            out.append((name, base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))))
    return head, out


def read_batch(db: Session, user_id: int, limit=15):
    """Read up to `limit` new statement emails. Locked ones are retried (the PDF password may have been set since)."""
    rt = refresh_token(db, user_id)
    if not rt:
        return {"ok": False, "error": "Sign in with Google first (Account ▸ Google).", "read_now": 0, "left": 0}
    token = gmail.access_token(rt)
    ids = search(token)
    done = set(db.scalars(select(BankStatement.gmail_id).where(BankStatement.user_id == user_id,
                                                               BankStatement.status.in_(("ok", "error")))))
    todo = [i for i in ids if i not in done]
    pws = passwords(db, user_id)
    read_now = rows_now = 0
    for msg_id in todo[:limit]:
        try:
            head, files = attachments(token, msg_id)
        except Exception as e:
            log.warning("Bank statement %s download failed: %s", msg_id, e)
            continue
        try:
            received = parsedate_to_datetime(head.get("date")).astimezone().replace(tzinfo=None)
        except Exception:
            received = None
        for filename, data in files:
            st = db.scalar(select(BankStatement).where(BankStatement.user_id == user_id, BankStatement.gmail_id == msg_id,
                                                       BankStatement.filename == filename[:300]))
            if not st:
                st = BankStatement(user_id=user_id, gmail_id=msg_id, filename=filename[:300])
                db.add(st)
            st.subject, st.received_at = head.get("subject", "")[:400], received
            st.bank = bank_for(head.get("from", ""))
            db.flush()
            try:
                parsed, text = parsers.parse_pdf(data, st.bank, pws)
                st.bank = st.bank if st.bank != "other" else bank_for("", text)
                st.body = text
                save(db, st, text, parsed)
                st.status, st.error = "ok", ""
                rows_now += st.rows
            except ValueError as e:
                st.status, st.error = ("locked" if "password" in str(e) else "error"), str(e)[:500]
            except Exception as e:
                log.exception("Bank statement %s (%s) failed", msg_id, filename)
                st.status, st.error = "error", f"{type(e).__name__}: {e}"[:500]
            db.commit()
        read_now += 1
    left = max(0, len(todo) - read_now)
    log.info("Bank reader user %s: read %d emails, %d rows, %d left", user_id, read_now, rows_now, left)
    return {"ok": True, "found": len(ids), "read_now": read_now, "rows_now": rows_now, "left": left}


def _read_from_text(db: Session, st: BankStatement):
    """True when a GoTyme statement's rows came from its text, which loses the PDF's columns (wrapped
    reference numbers become rows, the '-' of an empty column lands in the description, signs flip)."""
    descs = db.scalars(select(BankTxn.description).where(BankTxn.statement_id == st.id))
    return any(d.endswith(" -") or d.strip().isdigit() for d in descs)


def reparse(db: Session, user_id: int, only_empty=False):
    """Re-run the text parsers over stored statements. GoTyme needs the PDF's column positions, so a GoTyme
    statement that was read from text is queued to be downloaded again by the next 'Read bank statements'."""
    n = queued = 0
    for st in db.scalars(select(BankStatement).where(BankStatement.user_id == user_id, BankStatement.status == "ok")):
        if only_empty and st.rows:
            continue
        if st.bank == "gotyme":
            if _read_from_text(db, st):
                st.status = "reread"
                queued += 1
            continue
        save(db, st, st.body, parsers.parse_text(st.body, st.bank))
        n += 1
    db.commit()
    return {"reparsed": n, "to_download": queued, **{k: v for k, v in status(db, user_id).items() if k != "last_read"}}


def shape(db: Session, user_id: int, index=0, start=0, count=120):
    """Layout of one stored statement (letters -> a, digits -> 9) and counts per bank, to tune the parsers."""
    from ..invest.ee.reader import shape as mask

    rows = list(db.scalars(select(BankStatement).where(BankStatement.user_id == user_id).order_by(BankStatement.id)))
    per_bank = defaultdict(lambda: {"statements": 0, "empty": 0, "locked": 0, "error": 0})
    for st in rows:
        c = per_bank[st.bank]
        c["statements"] += 1
        c["empty"] += st.status == "ok" and not st.rows
        c["locked"] += st.status == "locked"
        c["error"] += st.status == "error"
    ok = [st for st in rows if st.status == "ok"]
    if not ok:
        return {"per_bank": per_bank, "errors": list({st.error for st in rows if st.error})[:5]}
    st = ok[min(index, len(ok) - 1)]
    lines = mask(st.body, max_lines=10_000)
    return {"per_bank": per_bank, "bank": st.bank, "kind": st.kind, "rows": st.rows, "total_lines": len(lines),
            "headers": [re.sub(r"\s{2,}", " | ", ln.strip()) for ln in st.body.splitlines()
                        if not re.search(r"[\d@]", ln) and re.search(r"\b(date|description|details|amount|balance|fees?|"
                                                                     r"money|debit|credit|transaction|reference|in|out)\b", ln, re.I)
                        and len(ln.split()) <= 10][:40],
            "shape": lines[start:start + count]}


def position(db: Session, user_id: int):
    """Money in banks, debt (bank credit/loan accounts + typed-in liabilities) and fees, for net worth."""
    accounts = list(db.scalars(select(BankAccount).where(BankAccount.user_id == user_id).order_by(BankAccount.account)))
    manual = list(db.scalars(select(Liability).where(Liability.user_id == user_id).order_by(Liability.name)))
    cash = debt = 0.0
    for a in accounts:
        if a.hidden or a.balance is None:
            continue
        if a.kind == "bank" and a.balance >= 0:
            cash += a.balance
        else:
            debt += abs(a.balance)  # overdrawn bank account, card or loan balance
    debt += sum(max(0.0, m.balance) for m in manual)
    since = date.today() - timedelta(days=365)
    fees = db.scalar(select(func.coalesce(func.sum(BankTxn.fee), 0.0)).where(BankTxn.user_id == user_id,
                                                                            BankTxn.date >= since)) or 0.0
    fee_lines = db.scalar(select(func.coalesce(func.sum(-BankTxn.amount), 0.0)).where(
        BankTxn.user_id == user_id, BankTxn.date >= since, BankTxn.category == "Bank fees", BankTxn.fee == 0,
        BankTxn.amount < 0)) or 0.0
    return {"cash": round(cash, 2), "debt": round(debt, 2), "fees_12m": round(fees + fee_lines, 2),
            "accounts": accounts, "liabilities": manual}


def status(db: Session, user_id: int):
    rows = db.execute(select(BankStatement.status, func.count()).where(BankStatement.user_id == user_id)
                      .group_by(BankStatement.status)).all()
    counts = {s: n for s, n in rows}
    last = db.scalar(select(BankStatement).where(BankStatement.user_id == user_id, BankStatement.status != "ok")
                     .order_by(BankStatement.read_at.desc()))
    return {"google": bool(refresh_token(db, user_id)), "statements": sum(counts.values()), "ok": counts.get("ok", 0),
            "locked": counts.get("locked", 0), "failed": counts.get("error", 0),
            "transactions": db.scalar(select(func.count()).select_from(BankTxn).where(BankTxn.user_id == user_id)),
            "last_error": last.error if last else "", "pdf_password_set": bool(passwords(db, user_id)),
            "last_read": (db.scalar(select(func.max(BankStatement.read_at)).where(BankStatement.user_id == user_id)) or
                          None)}


INVEST_RE = re.compile(r"easy\s?equities|easygrp|ee_rfnd|first world trader|easyproperties|easy\s?properties|\bfwt\b|easycrypto|"
                       r"\bee withdrawal\b|\bsatrix\b|etfsa|\b10x\b|sygnia|allan gray|coronation|tfsa", re.I)


HOLDER_RE = re.compile(r"\b(?:MR|MRS|MS|MISS|DR|PROF)\.?[ ]+([A-Z][A-Za-z'-]+(?:[ ]+[A-Z][A-Za-z'-]+){1,3})[ ]*$", re.M)


def own_name_re(db: Session, user_id: int):
    """Your name as banks print it on payments between your own accounts: 'Miguel Kudakashe N…' (first + middle)
    or 'M Nyobol…' (initial + surname), read from the statements' address block. None if no name is found."""
    return own_name_pattern(db.scalars(select(BankStatement.body).where(BankStatement.user_id == user_id).limit(40)))


def own_name_pattern(bodies):
    names = set()
    for body in bodies:
        if (m := HOLDER_RE.search(body or "")):
            names.add(m.group(1).upper())
    pats = []
    for full in names:
        parts = full.split()
        first, surname = parts[0], parts[-1]
        pats.append(rf"\b{re.escape(first)}\s+{re.escape(parts[1])}" if len(parts) > 2 else rf"\b{re.escape(full)}")
        pats.append(rf"\b{re.escape(first[0])}\.?\s+{re.escape(surname[:5])}")
        pats.append(rf"payshap payment received:?\s*{re.escape(first)}\s*$")  # your first name as the reference
    return re.compile("|".join(pats), re.I) if pats else None


WORDS = frozenset((Path(__file__).with_name("words.txt")).read_text().split())
PAYSHAP_REF_RE = re.compile(r"payshap payment received:?\s*(.*)", re.I)


def own_reference(description: str, own=None) -> bool:
    """A PayShap you sent yourself from another bank: the reference is an ordinary word ('Main', 'Update',
    'Ohkay') or your own name. People paying you leave their name, the default reference on most banks."""
    m = PAYSHAP_REF_RE.search(description or "")
    if not m:
        return False
    if own is not None and (own.search(m.group(1)) or own.search(description)):
        return True
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", m.group(1))]
    return all(len(w) < 2 or w in WORDS for w in words)


def internal_pairs(txns, days=3, own=None):
    """Ids of money moved between your own accounts: an amount leaving one account and the same amount arriving
    in another account within a few days, plus moves between pockets of one account (GoalSave and the like).
    Those aren't income or spending."""
    used = {t.id for t in txns if OWN_MOVE_RE.search(getattr(t, "description", "") or "")
            or (own is not None and own.search(getattr(t, "description", "") or ""))
            or (t.amount > 0 and own_reference(getattr(t, "description", ""), own))}
    ins = defaultdict(list)
    for t in txns:
        if t.amount > 0 and t.id not in used:
            ins[round(t.amount, 2)].append(t)
    for o in sorted((t for t in txns if t.amount < 0 and t.id not in used), key=lambda t: (t.date, t.id)):
        for t in sorted(ins.get(round(-o.amount, 2), []), key=lambda t: t.date):
            if t.id not in used and t.account != o.account and 0 <= (t.date - o.date).days <= days:
                used.update((t.id, o.id))
                break
    return used


SALARY_RE = re.compile(r"salary|salaries|wages?\b|payroll|learnership|stipend|internship", re.I)
# Crypto platforms paying out. Paxful's payouts carry only a code like 'E025lr01n0' as the reference.
CRYPTO_RE = re.compile(r"paxful|\bluno\b|\bvalr\b|binance|bybit|altcoin ?trader|\bovex\b|noones|"
                       r"received:?\s*e0\d{2}[a-z0-9]{6}\b", re.I)
GRANT_RE = re.compile(r"sassa|\bsrd\b|grant|nsfas|\buif\b", re.I)
PAYER_RES = [re.compile(r"pay by (?:shapid|account),\s*([^-]+)", re.I),
             re.compile(r"(?:payment )?received(?: from)?:?\s*(.+)", re.I),
             re.compile(r"payment from\s*(.+)", re.I)]
NOT_MONEY_IN = {"Transfers", "Other income", "Payments", "Income", "Other", "Interest"}


def payer(description: str) -> str:
    """Who paid: 'PayShap - Pay by ShapID, M NYOBOL -' -> 'M Nyobol'; 'Payment Received: Paypal Xhv Transfer 12' -> 'PayPal'."""
    d = description or ""
    if re.search(r"paypal", d, re.I):
        return "PayPal"
    if (m := PAYSHAP_REF_RE.search(d)):
        return " ".join(re.findall(r"[A-Za-z]{2,}", m.group(1))[:3]).title()  # the sender's name, their bank's default reference
    for rx in PAYER_RES:
        if (m := rx.search(d)):
            words = [w for w in re.split(r"[\s:,]+", m.group(1)) if w and not re.search(r"\d", w)
                     and w.lower() not in ("transfer", "payment", "ref", "reference", "-")]
            if words:
                return " ".join(words[:3]).title()
    return ""


def income_category(t, regular=frozenset()) -> str:
    """Where money in came from: salary or stipend, grants, each regular payer by name, once-off payments,
    refunds (money back on a purchase), interest, or an unknown source when the bank doesn't say."""
    d = t.description or ""
    if SALARY_RE.search(d):
        return "Salary & stipends"
    if GRANT_RE.search(d):
        return "Grants"
    if CRYPTO_RE.search(d):
        return "Crypto sales"
    if t.category == "Interest":
        return "Interest"
    if t.category == "Cash deposit":
        return "Cash deposits"
    if t.category not in NOT_MONEY_IN or re.search(r"refund|reversal|correction", d, re.I):
        return "Refunds & reversals"  # money in on a shop, airtime or restaurant line is money back
    who = payer(d)
    if who and who in regular:
        return f"{who} (regular)"
    if who:
        return "Once-off payments"
    if PEOPLE_RE.search(d) or re.search(r"payshap", d, re.I):
        return "From people (no name)"  # a PayShap or transfer that only carries the sender's reference
    return "Unknown source"


def regular_payers(txns, months=3):
    """Payers seen in at least `months` different months: an employer, a grant, someone who supports you."""
    seen = defaultdict(set)
    for t in txns:
        if (who := payer(t.description)):
            seen[who].add(t.date.strftime("%Y-%m"))
    return frozenset(w for w, ms in seen.items() if len(ms) >= months)


def charts(db: Session, user_id: int, months=24):
    """Per month: income, spending (with fees), bank fees, money into / out of investments, money moved between
    your own accounts (left out of income and spending), and money that reached your accounts without a statement
    line saying where from ("unrecorded_in": the month-end balances rose by more than the statements explain).
    Money in and money out by category for the last 12 months."""
    start = (date.today().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    txns = list(db.scalars(select(BankTxn).where(BankTxn.user_id == user_id, BankTxn.date >= start - timedelta(days=5))))
    txns = [t for t in txns if not SUMMARY_RE.search(t.description or "")]
    internal = internal_pairs(txns, own=own_name_re(db, user_id))
    by_month = defaultdict(lambda: {"in": 0.0, "out": 0.0, "fees": 0.0, "invested": 0.0, "from_investments": 0.0,
                                    "internal": 0.0, "unrecorded_in": 0.0})
    cats, income_cats = defaultdict(float), defaultdict(float)
    year_ago = date.today() - timedelta(days=365)
    regular = regular_payers([t for t in txns if t.amount > 0 and t.date >= year_ago and t.id not in internal
                              and not INVEST_RE.search(t.description or "")])
    for t in txns:
        if t.date < start:
            continue
        m = by_month[t.date.strftime("%Y-%m")]
        to_invest = bool(INVEST_RE.search(t.description or ""))
        if t.id in internal:
            m["internal"] += abs(t.amount)
        elif t.amount >= 0:
            m["from_investments" if to_invest else "in"] += t.amount
            if not to_invest and t.date >= year_ago:
                income_cats[income_category(t, regular)] += t.amount
        elif to_invest:
            m["invested"] += -t.amount
        else:
            m["out"] += -t.amount
            if t.date >= year_ago:
                cats["Sent to people" if t.category == "Transfers" else t.category] += -t.amount
        fee = t.fee or (-t.amount if t.category == "Bank fees" and t.amount < 0 else 0.0)
        m["fees"] += fee
        if t.fee:
            m["out"] += t.fee
            if t.date >= year_ago:
                cats["Bank fees"] += t.fee
    # Balances are the truth: where they rose by more than the lines explain, money came in off the statements.
    balances = {b["month"]: b["cash"] - b["debt"] for b in month_balances(db, user_id, months + 1)}
    for k in sorted(by_month):
        y, mo = int(k[:4]), int(k[5:])
        prev = f"{y - 1:04d}-12" if mo == 1 else f"{y:04d}-{mo - 1:02d}"
        if k in balances and prev in balances and (balances[k] or balances[prev]):
            m = by_month[k]
            explained = m["in"] + m["from_investments"] - m["out"] - m["invested"]
            gap = (balances[k] - balances[prev]) - explained
            if gap > 1:
                m["unrecorded_in"] = gap
                if date(y, mo, 1) >= year_ago.replace(day=1):
                    income_cats["Unknown source"] += gap
    return {"months": [{"month": k, **{f: round(v, 2) for f, v in by_month[k].items()}} for k in sorted(by_month)],
            "categories": {k: round(v, 2) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])},
            "income_categories": {k: round(v, 2) for k, v in sorted(income_cats.items(), key=lambda kv: -kv[1])}}


def read_all(db: Session):
    """Nightly: every user signed in with Google."""
    n = 0
    from .categorize import tidy

    for conn in db.scalars(select(EEConnection).where(EEConnection.mail_password.startswith(gmail.PREFIX))):
        try:
            read_batch(db, conn.user_id, limit=25)
            tidy(db, conn.user_id)
            n += 1
        except Exception:
            db.rollback()
            log.exception("Bank reader failed for user %s", conn.user_id)
    return n


def month_balances(db: Session, user_id: int, months=24):
    """Month-end money in the bank and card/loan debt, from each account's last balance in (or before) the month."""
    accounts = {a.account: a for a in db.scalars(select(BankAccount).where(BankAccount.user_id == user_id))}
    last = {}  # (account, month) -> balance on the last line of that month
    for t in db.scalars(select(BankTxn).where(BankTxn.user_id == user_id, BankTxn.balance.is_not(None))
                        .order_by(BankTxn.date, BankTxn.id)):
        last[(t.account, t.date.strftime("%Y-%m"))] = t.balance
    end = date.today().replace(day=1)
    keys = []
    y, m = end.year, end.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    keys.reverse()
    first = {}
    for (acc, month) in last:
        first[acc] = min(first.get(acc, month), month)
    out, carry = [], {}
    earlier = sorted(k for k in last if k[1] < keys[0])
    for acc, month in earlier:
        carry[acc] = last[(acc, month)]
    for k in keys:
        cash = debt = 0.0
        for acc, a in accounts.items():
            if (acc, k) in last:
                carry[acc] = last[(acc, k)]
            if a.hidden or acc not in carry:
                continue
            bal = carry[acc]
            if a.kind == "bank" and bal >= 0:
                cash += bal
            else:
                debt += abs(bal)
        out.append({"month": k, "cash": round(cash, 2), "debt": round(debt, 2)})
    return out
