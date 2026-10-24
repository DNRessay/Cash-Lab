import io
import logging
import re
from datetime import datetime

from .common import make_txn

log = logging.getLogger(__name__)

# Column x-boundaries (points) on the GoTyme statement layout.
COLUMNS = [(72, "date"), (326, "detail"), (368, "credit"), (411, "debit")]
DATE_RE = re.compile(r"^\d{2} [A-Za-z]{3} \d{4}$")
SIGNALS = ("GoTyme", "GoalSave", "Credits (+)", "Running Balance", "Debits (-)")
ROW_BUCKET = 4


def is_gotyme(text):
    return any(s in text for s in SIGNALS)


def _column(x):
    return next((name for edge, name in COLUMNS if x < edge), "balance")


def _num(s):
    s = s.replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


class GoTymeParser:
    """Character-level parser. Rotated 'PAID' style stamps are dropped via the text matrix."""

    def parse(self, pdf_bytes, password=None):
        import pdfplumber

        rows = []
        with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
            account = None
            for page in pdf.pages:
                lines = self._lines(page)
                for words in lines.values():
                    joined = " ".join(w["text"] for w in words)
                    if (m := re.search(r"Account Number:\s*(\d+)", joined)):
                        account = m.group(1)
                for r in self._rows(lines):
                    r["account_number"] = account
                    rows.append(r)

        out = []
        for r in rows:
            try:
                day = datetime.strptime(r["date"], "%d %b %Y").date()
            except ValueError:
                continue
            if r["credit"]:
                amount, kind = r["credit"], "credit"
            elif r["debit"]:
                amount, kind = r["debit"], "debit"
            else:
                continue
            out.append(make_txn(day, r["details"], amount, kind, "GOTYME", len(out), balance=r["balance"],
                                account_number=r["account_number"]))
        log.info("GoTyme: %d transactions", len(out))
        return out

    def _lines(self, page):
        chars = sorted(
            (c for c in page.chars if c.get("matrix", (1, 0))[1] == 0),
            key=lambda c: (round(c["top"] / ROW_BUCKET) * ROW_BUCKET, c["x0"]),
        )
        words, current = [], []
        for c in chars:
            if current and (abs(c["top"] - current[-1]["top"]) >= 4 or c["x0"] - current[-1]["x1"] >= 4):
                words.append(current)
                current = []
            current.append(c)
        if current:
            words.append(current)

        lines = {}
        for w in words:
            text = "".join(ch["text"] for ch in w).strip()
            if text:
                y = round(w[0]["top"] / ROW_BUCKET) * ROW_BUCKET
                lines.setdefault(y, []).append({"text": text, "x0": w[0]["x0"]})
        return {y: sorted(ws, key=lambda w: w["x0"]) for y, ws in sorted(lines.items())}

    def _rows(self, lines):
        current_date = None
        for words in lines.values():
            cols = {"date": [], "detail": [], "credit": [], "debit": [], "balance": []}
            for w in words:
                cols[_column(w["x0"])].append(w["text"])
            date_s, detail, credit_s, debit_s, balance_s = (" ".join(cols[k]).strip() for k in cols)

            if DATE_RE.match(date_s):
                current_date = date_s
            if detail == "Details" or not (current_date and detail and balance_s):
                continue
            balance, credit, debit = _num(balance_s), _num(credit_s), _num(debit_s)
            if balance is None:
                continue
            if credit is None and debit is None and credit_s != "-" and debit_s != "-":
                continue
            yield {"date": current_date, "details": detail, "credit": credit, "debit": debit, "balance": balance}


TEXT_ROW = re.compile(r"^(\d{2} [A-Za-z]{3} \d{4})\s+(.+?)\s+(-|[\d,]+(?:\.\d+)?)\s+(-|[\d,]+(?:\.\d+)?)\s+(-?[\d,]+(?:\.\d+)?)\s*$")
ACCOUNT_LINE = re.compile(r"Account Number:\s*(\d+)")


class GoTymeTextParser:
    """GoTyme from the statement's text (what C-Lab stores), so saved statements can be re-read without the PDF:
    'DD Mon YYYY  Details  Credits(+)  Debits(-)  Running Balance', one account (or GoalSave pocket) per
    'Account Number:' block. Summary lines ('Opening balance R0') don't have this shape and are never rows."""

    def parse(self, text):
        out, account = [], None
        for raw in text.split("\n"):
            line = raw.strip()
            if (m := ACCOUNT_LINE.search(line)):
                account = m.group(1)
                continue
            m = TEXT_ROW.match(line)
            if not m:
                continue
            try:
                day = datetime.strptime(m.group(1), "%d %b %Y").date()
            except ValueError:
                continue
            credit, debit, balance = _num(m.group(3)), _num(m.group(4)), _num(m.group(5))
            if credit:
                amount, kind = credit, "credit"
            elif debit:
                amount, kind = debit, "debit"
            else:
                continue
            out.append(make_txn(day, m.group(2), amount, kind, "GOTYME", len(out), balance=balance,
                                account_number=account))
        log.info("GoTyme (text): %d transactions", len(out))
        return out
