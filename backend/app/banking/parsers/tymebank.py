import logging
import re
from datetime import datetime

from .common import make_txn

log = logging.getLogger(__name__)

# TymeBank prints every amount with cents; a bare number is a card or reference number, never money.
AMT = r"(?:\d{1,3}(?:,\d{3})*|\d+)\.\d{2}"
SLOT = rf"(-|{AMT})"
LAST = rf"({AMT})"
DATE_START = re.compile(r"^(\d{1,2}\s+\w{3}\s+\d{4})\s+(.+)")
NEXT_DATE = re.compile(r"^\d{1,2}\s+\w{3}\s+\d{4}")
FOUR_COL = re.compile(rf"^{SLOT}\s+{SLOT}\s+{SLOT}\s+{LAST}\s*$")
INLINE = re.compile(rf"^(.+?)\s+{SLOT}\s+{SLOT}\s+{SLOT}\s+{LAST}\s*$")
LONG_NUMBER = re.compile(r"^\d{10,}$")


def _amt(s):
    if not s or s == "-":
        return 0.0
    try:
        v = float(s.replace(",", ""))
        return v if v <= 10_000_000 else 0.0
    except ValueError:
        return 0.0


class TymeBankLegacyParser:
    """Older TymeBank layout: Date | Description | Fees | Money Out | Money In | Balance."""

    def parse(self, text):
        out = []
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            m = DATE_START.match(lines[i].strip())
            if not m:
                i += 1
                continue
            try:
                day = datetime.strptime(m.group(1), "%d %b %Y").date()
            except ValueError:
                i += 1
                continue

            rest = m.group(2).strip()
            parts, cols = [rest], None
            j = i + 1
            while j < len(lines) and j < i + 6:
                nxt = lines[j].strip()
                if NEXT_DATE.match(nxt):
                    break
                if (m4 := FOUR_COL.match(nxt)):
                    cols = m4.groups()[:3]
                    i = j
                    break
                if (mi := INLINE.match(nxt)):
                    parts.append(mi.group(1))
                    cols = mi.groups()[1:4]
                    i = j
                    break
                if nxt and not LONG_NUMBER.match(nxt) and not nxt.startswith("-"):
                    parts.append(nxt)
                j += 1

            if cols is None and (mi := INLINE.match(rest)):
                parts = [mi.group(1)]
                cols = mi.groups()[1:4]

            if cols:
                description = " ".join(" ".join(parts).split())
                if len(description) >= 3 and "Description" not in description and "Money Out" not in description:
                    fees, money_out, money_in = (_amt(c) for c in cols)
                    if money_in > 0:
                        amount, kind = money_in, "credit"
                    elif money_out > 0:
                        amount, kind = money_out, "debit"
                    elif fees > 0:
                        amount, kind, description = fees, "debit", description + " (Fee)"
                    else:
                        amount = 0
                    if amount > 0:
                        out.append(make_txn(day, description, amount, kind, "TYME", len(out)))
            i += 1
        log.info("TymeBank legacy: %d transactions", len(out))
        return out
