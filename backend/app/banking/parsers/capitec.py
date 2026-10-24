import logging
import re
from datetime import datetime

from .common import make_txn

log = logging.getLogger(__name__)

# Capitec's Transaction History: Date | Description | Category | Money In | Money Out | Fee* | Balance.
# Money out and fees are printed negative, thousands are separated by spaces ("3 465.00", "-36 800.16"; older
# statements use commas, "20,000.00"),
# and a description can wrap over several lines with the amounts on the last one.
MONEY = r"-?\d{1,3}(?:[ ,]\d{3})*\.\d{2}"
TAIL = re.compile(rf"((?:\s+{MONEY})+)\s*$")
NUMBER = re.compile(MONEY)
ROW = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.*)$")
NOISE = ("Includes VAT", "Client Care Centre", "Capitec Bank is an authorised", "Unique Document No",
         "Date Description Category", "Transaction History", "Page ")
# Capitec's own categories, longest first so "Other Income" wins over "Income".
CATEGORIES = sorted([
    "Other Income", "Investment Income", "Digital Payments", "Card Payments", "Cash Withdrawal", "Debit Orders",
    "Card Subscriptions", "Digital Subscriptions", "Online Store", "Clothing & Shoes", "Personal Care",
    "Home Improvements", "Takeaways", "Restaurants", "Groceries", "Fuel", "Transport", "Cellphone", "Prepaid",
    "Vouchers", "Transfer", "Fees", "Interest", "Pension", "Refund", "Savings", "Insurance", "Loans", "Medical",
    "Entertainment", "Education", "Holiday", "Gifts", "Alcohol", "Furniture", "Electronics", "Uncategorised",
    "Send Cash", "Salary", "Income", "Payments", "Withdrawal", "Investments",
], key=len, reverse=True)
# A few of Capitec's names, mapped to what the rest of C-Lab calls them.
CATEGORY_MAP = {"Transfer": "Transfer", "Cash Withdrawal": "Withdrawal", "Prepaid": "Cellphone", "Fees": "Fees",
                "Interest": "Interest", "Investment Income": "Income", "Other Income": "Income", "Salary": "Income",
                "Pension": "Income", "Refund": "Income"}


def _money(s):
    return float(s.replace(" ", "").replace(",", ""))


def split_category(text):
    """'Payment Received: ... Other Income' -> ('Payment Received: ...', 'Other Income')."""
    for cat in CATEGORIES:
        if text.endswith(" " + cat) or text == cat:
            return text[: -len(cat)].strip(), cat
    return text, None


class CapitecParser:
    def parse(self, text):
        entries, current = [], None
        for raw in text.split("\n"):
            line = raw.strip()
            if line.startswith("Pending Card Transactions"):
                current = None  # not on the account yet; they appear as real rows on the next statement
                continue
            if not re.search(r"[A-Za-z0-9]", line) or any(n in line for n in NOISE):
                continue  # page furniture, and the lone "*" of the VAT footnote under a page's last row
            m = ROW.match(line)
            if m:
                current = [m.group(1), m.group(2)]
                entries.append(current)
            elif current is not None:
                current[1] += " " + line

        out, previous = [], None
        for day_s, body in entries:
            try:
                day = datetime.strptime(day_s, "%d/%m/%Y").date()
            except ValueError:
                continue
            body = " ".join(body.split())
            tail = TAIL.search(body)
            if not tail:
                continue  # "Insufficient Funds" notices carry no money
            numbers = [_money(n) for n in NUMBER.findall(tail.group(1))]
            if len(numbers) < 2:
                continue
            balance, values = numbers[-1], numbers[:-1][-2:]
            amount, fee = values[0], 0.0
            if len(values) == 2:
                amount, fee = values
                if previous is not None and abs(previous + values[1] - balance) < 0.005 and \
                        abs(previous + sum(values) - balance) >= 0.005:
                    amount, fee = values[1], 0.0  # the first number was part of the description
            if fee > 0:
                amount, fee = amount + fee, 0.0  # a fee refunded (Capitec "Correction" lines print it positive)
            description, category = split_category(body[: tail.start()].strip())
            if previous is not None and abs(previous + amount + fee - balance) >= 0.005:
                log.info("Capitec row doesn't follow the running balance: %s %s", day_s, description[:60])
            previous = balance
            if abs(amount) < 0.005 and abs(fee) >= 0.005:
                amount, fee, description = fee, 0.0, description + " (Fee)"
            if abs(amount) < 0.005 or len(description) < 3:
                continue
            out.append(make_txn(day, description, amount, "credit" if amount > 0 else "debit", "CAP", len(out),
                                category=CATEGORY_MAP.get(category, category), fee=abs(fee), balance=balance))
        log.info("Capitec: %d transactions", len(out))
        return out
