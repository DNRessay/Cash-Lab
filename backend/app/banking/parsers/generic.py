import logging
import re
from datetime import datetime

from .common import make_txn, to_float

log = logging.getLogger(__name__)

PATTERNS = [
    (r"(\d{2}/\d{2}/\d{4})\s*\|\s*([^|]+?)\s*\|\s*(-?R?[\d,]+\.\d{2})", "%d/%m/%Y"),
    (r"(\d{2}/\d{2}/\d{4})\s+([^\d\-+$R]+?)\s+(-?R?[\d,]+\.\d{2})", "%d/%m/%Y"),
    (r"(\d{4}-\d{2}-\d{2})\s+([^\d\-+$R]+?)\s+(-?R?[\d,]+\.\d{2})", "%Y-%m-%d"),
    (r"(\d{2}\s+\w{3}\s+\d{4})\s+([^\d\-+$R]+?)\s+(-?R?[\d,]+\.\d{2})", "%d %b %Y"),
]


class GenericParser:
    """Best-effort fallback for banks without a dedicated parser."""

    def parse(self, text):
        out = []
        for pattern, fmt in PATTERNS:
            for day_s, desc, amt_s in re.findall(pattern, text, re.MULTILINE):
                try:
                    day = datetime.strptime(day_s.strip(), fmt).date()
                except ValueError:
                    continue
                amount = to_float(amt_s.replace("$", ""))
                if len(desc.strip()) < 3 or not amount:
                    continue
                out.append(make_txn(day, desc, amount, "debit" if amount < 0 else "credit", "GEN", len(out)))
            if out:
                break
        log.info("Generic: %d transactions", len(out))
        return out
