# Reads EasyEquities / EasyProperties emails over IMAP (Gmail app password) and turns them into structured rows.
# The normalised text of every email is stored, so a parser fix can be re-run over old mail ("Re-read emails").
import email
import html as htmllib
import imaplib
import re
from datetime import datetime
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime

SENDER_DOMAIN = "easyequities.co.za"
SKIP_SENDERS = ("noreply@",)  # newsletters and marketing
CURRENCY_SIGNS = {"R": "ZAR", "$": "USD", "€": "EUR", "£": "GBP"}
WHOLE = r"\(?-?[\d,]+\)?"  # whole shares, e.g. 17 or (98)
FRAC = r"\(?\.\d+\)?"  # fractional share rights, e.g. .3638 or (.220)


def html_to_text(html: str) -> str:
    t = re.sub(r"(?is)<(style|script|head)\b.*?</\1>|<!--.*?-->", " ", html or "")
    t = re.sub(r"(?i)<br\s*/?>|</(td|tr|p|div|table)>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = htmllib.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def num(s):
    """'1,676.00' -> 1676.0; '(98)' -> 98.0 (brackets mark a sell, handled by the caller)."""
    if s is None:
        return None
    s = s.strip().strip("()").replace(",", "").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def _qty(whole, frac):
    w = num(whole) or 0.0
    f = (frac or "").strip("() ")
    f = float(f if f.startswith(".") else "0." + f) if f.strip(".") else 0.0
    return w + f


def _date(text, fallback=None):
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)", text or "")
    if m:
        return datetime.fromisoformat(f"{m.group(1)} {m.group(2)}")
    m = re.search(r"\w{3} (\w{3}) (\d{1,2}) (\d{2}:\d{2}:\d{2}) UTC (\d{4})", text or "")
    if m:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)} {m.group(4)}", "%b %d %H:%M:%S %Y")
    return fallback


def account_currency(account: str) -> str:
    m = re.search(r"\b(USD|EUR|GBP|AUD)\b", account or "")
    return m.group(1) if m else "ZAR"


def _find(pattern, text, group=1, flags=re.I):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else ""


def _amount(label, text):
    return num(_find(label + r"\s*(?:\d\s+)?\*?\s*\(?(-?[\d,]+\.\d+)", text))


def classify(sender: str, subject: str, text: str) -> str:
    s, sub = (sender or "").lower(), (subject or "").lower()
    if "open order" in sub:
        return "order"
    if "bundle transaction" in sub:
        return "bundle"
    if "confirmation of your transaction" in sub or "tax invoice for" in text.lower()[:400]:
        return "trade"
    if "withdrawal" in sub:
        return "withdrawal"
    if "deposit" in sub:
        return "deposit"
    if "corporateaction" in s or re.search(r"dividend|drip|scheme of arrangement|amalgam|rights offer|odd.lot|"
                                           r"take.?over|delist|consolidation|bundle is changing", sub):
        return "corporate_action"
    return "notice"


def parse(sender: str, subject: str, text: str, html: str = "", received: datetime = None) -> dict:
    """Structured fields for one email. Unknown formats come back with parsed=False and kind 'notice'."""
    kind = classify(sender, subject, text)
    out = {"kind": kind, "parsed": False, "account": "", "account_number": "", "instrument": "", "contract_code": "",
           "side": "", "quantity": None, "price": None, "currency": "", "value": None, "costs": None, "total": None,
           "reference": "", "date": received}
    acc = re.search(r"Account:\s*(.+?)\s+Acc\. number:\s*(EE[\d-]+)", text)
    if acc:
        out["account"], out["account_number"] = acc.group(1).strip(), acc.group(2)
    code = re.search(r"logos/(EQU\.[A-Z]{2}\.[A-Z0-9]+)\.png", html or "")
    if code:
        out["contract_code"] = code.group(1)

    if kind == "trade":
        name = _find(r"Tax Invoice for (.+?) SHARES:", text) or \
            _find(r"Hi,? [\w ]+?\s+(.+?)\s+YOUR (?:BID|OFFER) WAS", text)
        q = re.search(rf"SHARES:\s*({WHOLE})\s*FSRs:\s*({FRAC})\s*TRADE PRICE:\s*([\d.,]+)", text) or \
            re.search(rf"TRADED\s*1?\s*:?\s*SHARES:?\s*FSRs:?\s*TRADE PRICE:\s*({WHOLE})\s*({FRAC})\s*[R$€£]\s*([\d.,]+)", text)
        sign = _find(r"TRADE PRICE:\s*([R$€£])\s*[\d.,]+", text)
        if q:
            out["quantity"] = _qty(q.group(1), q.group(2))
            out["price"] = num(q.group(3))
        sold = bool(q and "(" in q.group(1)) or bool(re.search(r"DUE TO YOU|Sell Charge", text, re.I))
        out["side"] = "sell" if sold else "buy"
        out["currency"] = CURRENCY_SIGNS.get(sign) or account_currency(out["account"])
        out["value"] = _amount(r"TRADE VALUE", text) or _amount(r"GROSS (?:EST\. )?AMOUNT DUE (?:TO|BY|FROM) YOU", text)
        out["costs"] = _amount(r"TOTAL TRANSACTION COST", text) or _amount(r"LESS (?:EST\. )?COSTS", text)
        out["total"] = _amount(r"TOTAL COST", text) or _amount(r"NET (?:EST\. )?AMOUNT DUE (?:TO|BY|FROM) YOU", text)
        out["reference"] = _find(r"INVOICE NUMBER:\s*#?\s*(\d+)", text)
        out["date"] = _date(_find(r"SUBMISSION DATE:\s*(.{10,32}?)\s+(?:CASH )?SETTLEMENT", text), received)
        out["instrument"] = re.sub(r"\s+IPO$", "", (name or "").strip())
        out["parsed"] = bool(out["instrument"] and out["quantity"] and out["value"] is not None)

    elif kind == "order":
        out["instrument"] = re.sub(r"(?i)^confirmation of open order\s*", "", subject or "").strip()
        q = re.search(rf"SHARES:?\s*FSRs:?\s*(?:OFFER|BID) PRICE:?\s*({WHOLE})\s*({FRAC})\s*[<>]?=?\s*[R$]\s*([\d.,]+)", text)
        if q:
            out["quantity"] = _qty(q.group(1), q.group(2))
            out["price"] = num(q.group(3))
        out["side"] = "sell" if re.search(r"Sell Charge|DUE TO YOU|OFFER PRICE", text, re.I) else "buy"
        out["currency"] = account_currency(out["account"])
        out["value"] = _amount(r"GROSS (?:EST\. )?AMOUNT DUE (?:TO|BY|FROM) YOU", text) or _amount(r"TRADE VALUE", text)
        out["costs"] = _amount(r"LESS (?:EST\. )?COSTS", text) or _amount(r"TOTAL TRANSACTION COST", text)
        out["total"] = _amount(r"NET (?:EST\. )?AMOUNT DUE (?:TO|BY|FROM) YOU", text) or _amount(r"TOTAL COST", text)
        out["reference"] = _find(r"APPLICATION NUMBER:\s*(\d+)", text)
        out["date"] = _date(_find(r"SUBMISSION DATE:\s*(.{10,32}?UTC \d{4})", text), received)
        out["parsed"] = bool(out["instrument"] and out["quantity"])

    elif kind in ("deposit", "withdrawal"):
        m = re.search(r"Currency:\s*(.+?)\s+Account number:\s*(EE[\d-]+)\s+Action:?\s*(\w[\w ]*?)\s+Amount:?\s*"
                      r"(-?[\d,]+\.\d+)\s+Date and time:\s*(.+?UTC \d{4})", text)
        if m:
            out["account"], out["account_number"] = m.group(1).strip(), m.group(2)
            out["value"] = out["total"] = num(m.group(4))
            out["date"] = _date(m.group(5), received)
            out["currency"] = account_currency(out["account"])
            out["parsed"] = True
        out["side"] = "in" if kind == "deposit" else "out"

    elif kind == "corporate_action":
        out["instrument"] = re.split(r"\s+[–-]\s+|\s*\(", subject or "")[0].strip()
        out["parsed"] = True

    return out


# ── IMAP ────────────────────────────────────────────────────────────────────

class MailError(Exception):
    pass


def _all_mail_box(m):
    typ, boxes = m.list()
    for raw in boxes or []:
        line = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
        if "\\All" in line:
            return line.rsplit(' "/" ', 1)[-1].strip()
    return "INBOX"


def _bodies(msg):
    html, plain = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        if ctype == "text/html" and not html:
            html = content
        elif ctype == "text/plain" and not plain:
            plain = content
    return html, plain


def fetch(address, app_password, since_uid=0, uidvalidity="", limit=150, host="imap.gmail.com"):
    """New EasyEquities emails since `since_uid`. Returns (messages, last_uid, uidvalidity)."""
    try:
        m = imaplib.IMAP4_SSL(host, timeout=30)
    except Exception as e:
        raise MailError(f"could not reach {host} ({e})")
    try:
        try:
            m.login(address, app_password.replace(" ", ""))
        except imaplib.IMAP4.error as e:
            raise MailError(f"Gmail refused the login ({e}). Use a Gmail app password, and check IMAP is enabled.")
        box = _all_mail_box(m)
        typ, data = m.select(box, readonly=True)
        if typ != "OK":
            raise MailError(f"could not open mailbox {box}")
        validity = ""
        typ, resp = m.response("UIDVALIDITY")
        if resp and resp[0]:
            validity = resp[0].decode() if isinstance(resp[0], bytes) else str(resp[0])
        if validity != uidvalidity:
            since_uid = 0
        typ, data = m.uid("search", None, "UID", f"{since_uid + 1}:*", "FROM", f'"{SENDER_DOMAIN}"')
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > since_uid)[:limit]
        out = []
        for uid in uids:
            typ, parts = m.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw, policy=default_policy)
            sender = str(msg.get("From", "")).lower()
            html, plain = _bodies(msg)
            try:
                received = parsedate_to_datetime(msg.get("Date")).astimezone().replace(tzinfo=None)
            except Exception:
                received = None
            out.append({"uid": uid, "message_id": str(msg.get("Message-ID", "")).strip() or f"uid-{validity}-{uid}",
                        "sender": sender, "subject": str(msg.get("Subject", "")), "received": received,
                        "html": html, "text": html_to_text(html) if html else re.sub(r"\s+", " ", plain).strip()})
        return out, (uids[-1] if uids else since_uid), validity
    finally:
        try:
            m.logout()
        except Exception:
            pass


def wanted(sender: str) -> bool:
    return SENDER_DOMAIN in sender and not any(s in sender for s in SKIP_SENDERS)
