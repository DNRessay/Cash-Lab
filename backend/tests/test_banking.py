import json
import io
from datetime import date

from fastapi.testclient import TestClient

from app.banking import reader
from app.db import SessionLocal
from app.invest.ee import gmail
from app.invest.ee.models import EEConnection, EESetting
from app.main import app
from app.models import User
from app.security import seal
from tests.test_invest import register

api = TestClient(app)


def capitec_pdf(lines, password=None):
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in ["Capitec Bank", "Account Number: 1234 5678 9012", "Transaction History", *lines]:
        c.drawString(40, y, line)
        y -= 18
    c.save()
    if not password:
        return buf.getvalue()
    w = PdfWriter()
    for p in PdfReader(io.BytesIO(buf.getvalue())).pages:
        w.add_page(p)
    w.encrypt(password)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


AUG = ["01/08/2026 Payment Received ACME Salary Income 20,000.00 20,500.00",
       "03/08/2026 Checkers Sandton Groceries -1,250.00 -1.50 19,248.50",
       "05/08/2026 Monthly Account Admin Fee -7.50 19,241.00"]
SEP = ["05/08/2026 Monthly Account Admin Fee -7.50 19,241.00",  # overlaps August: must not count twice
       "02/09/2026 Uber Trip Transport -180.00 19,061.00"]


def test_bank_statements_from_gmail(monkeypatch):
    h = register("banker")
    db = SessionLocal()
    uid = db.query(User).filter(User.email == "banker@inv.example.com").one().id
    assert api.post("/api/bank/sync", headers=h).status_code == 400  # Google not signed in yet
    db.add(EEConnection(user_id=uid, snapshot={}, mail_address="b@gmail.com", mail_password=gmail.PREFIX + seal("rt")))
    db.commit()

    mails = {"m1": ("Capitec <statements@capitecbank.co.za>", "Your statement", capitec_pdf(AUG, "0101015800080")),
             "m2": ("Capitec <statements@capitecbank.co.za>", "Your statement", capitec_pdf(SEP, "0101015800080"))}
    monkeypatch.setattr(gmail, "access_token", lambda rt: "at")
    monkeypatch.setattr(reader, "search", lambda token: list(mails))
    monkeypatch.setattr(reader, "attachments", lambda token, mid: (
        {"from": mails[mid][0], "subject": mails[mid][1], "date": "Mon, 07 Sep 2026 08:00:00 +0200"},
        [(f"{mid}.pdf", mails[mid][2])]))

    r = api.post("/api/bank/sync", headers=h).json()
    assert r["status"]["locked"] == 2 and r["status"]["transactions"] == 0  # no PDF password yet

    db.add(EESetting(user_id=uid, pdf_password=seal("0101015800080")))
    db.commit()
    r = api.post("/api/bank/sync", headers=h).json()
    assert r["read_now"] == 2 and r["status"]["ok"] == 2 and r["status"]["locked"] == 0
    rows = api.get("/api/bank/transactions", headers=h).json()
    assert len(rows) == 4  # the admin fee on both statements is stored once
    checkers = next(t for t in rows if "Checkers" in t["description"])
    assert (checkers["amount"], checkers["fee"], checkers["category"]) == (-1250.0, 1.5, "Groceries")
    assert next(t for t in rows if "Admin Fee" in t["description"])["category"] == "Bank fees"
    assert next(t for t in rows if "ACME" in t["description"])["category"] == "Income"  # Capitec's "Salary Income"

    o = api.get("/api/bank", headers=h).json()
    acc = o["accounts"][0]
    assert (acc["account"], acc["kind"], acc["balance"], acc["balance_date"]) == ("Capitec ••9012", "bank", 19061.0, "2026-09-02")
    assert acc["bank_name"] == "Capitec" and acc["last"]["description"].startswith("Uber")
    assert acc["fees_12m"] == 9.0
    assert o["cash"] == 19061.0 and o["fees_12m"] == 9.0  # 1.50 on the Checkers line + 7.50 admin fee
    aug = next(m for m in o["months"] if m["month"] == "2026-08")
    assert aug["in"] == 20000.0 and aug["fees"] == 9.0
    assert o["categories"]["Groceries"] == 1250.0

    # Debt: a typed-in car loan, and a card that's switched to credit.
    assert api.post("/api/bank/liabilities", headers=h, json={"name": "Car", "kind": "vehicle", "balance": 150000}).status_code == 201
    s = api.get("/api/invest/summary", headers=h).json()
    assert s["banking"]["cash"] == 19061.0 and s["banking"]["debt"] == 150000.0
    assert round(s["net_worth"], 2) == round(s["value"] + 19061.0 - 150000.0, 2)
    assert api.patch(f"/api/bank/accounts/{acc['id']}", headers=h, json={"kind": "credit"}).json()["kind"] == "credit"
    assert api.get("/api/bank", headers=h).json()["debt"] == 169061.0

    mp = api.get("/api/invest/money", headers=h).json()
    aug = next(r for r in mp["months"] if r["month"] == "2026-08")
    sep = next(r for r in mp["months"] if r["month"] == "2026-09")
    # The account was switched to a card above, so its month-end balances count as debt.
    assert (aug["bank"], aug["debt"], sep["debt"]) == (0.0, 19241.0 + 150000.0, 19061.0 + 150000.0)
    assert aug["money_in"] == 20000.0
    assert mp["income_12m"]["income"] == 20000.0 and mp["income_12m"]["fees"] == 9.0
    assert mp["months"][-1]["month"] == date.today().strftime("%Y-%m")

    other = register("nosy")
    assert api.get(f"/api/bank/statements/1/text", headers=other).status_code == 404
    first = next(s for s in api.get("/api/bank/statements", headers=h).json() if s["filename"] == "m1.pdf")
    assert "Checkers" in api.get(f"/api/bank/statements/{first['id']}/text", headers=h).json()["text"]
    # Other test modules expect a single EasyEquities connection/setting row.
    db.query(EEConnection).filter(EEConnection.user_id == uid).delete()
    db.query(EESetting).filter(EESetting.user_id == uid).delete()
    db.commit()
    db.close()


def test_bank_helpers():
    assert reader.bank_for("alerts@fnb.co.za") == "fnb"
    assert reader.account_kind("Credit Card Statement  Credit limit R 20 000  Minimum payment due R 500") == "credit"
    assert reader.account_kind("Fees: Credit card replacement R 50 · Debit card") == "bank"  # debit account fee table
    assert reader.account_kind("Credit limit R 5 000") == "bank"  # a limit alone isn't a card statement
    assert reader.closing_balance("Opening balance 10.00\nClosing Balance   R 1 234.56 Dr") == -1234.56
    assert reader.categorise("POS Purchase Engen Garage") == "Transport"
    assert reader.categorise("Unknown thing", amount=50) == "Other income"
    assert date(2026, 1, 1)


def test_parser_fallback_when_bank_parser_finds_nothing():
    from app.banking import parsers

    # A TymeBank statement mentions GoalSave, which looks like GoTyme; the GoTyme parser finds no rows,
    # so the text parsers get a go and TymeBank's wins.
    pdf = capitec_pdf(["TymeBank GoalSave", "05 Sep 2025 Woolworths Food", "- 99.90 - 400.10",
                       "06 Sep 2025 Salary", "- - 5,000.00 5,400.10"])
    rows, text = parsers.parse_pdf(pdf, "tymebank")
    assert [(r["type"], r["amount"]) for r in rows] == [("debit", 99.9), ("credit", 5000.0)]
    assert parsers.parse_text("Transaction History\n01/09/2025 Payment Received J Smith Other Income 1,500.00 2,500.00", "fnb")[0]["amount"] == 1500.0


def test_categoriser_rules_and_ai(monkeypatch):
    from app.ai import llm
    from app.banking import categorize as c
    from app.config import settings

    assert c.categorise("POS Purchase Checkers Sandton", -100) == "Groceries"
    assert c.categorise("BP Garage Rivonia", -500) == "Transport"
    assert c.categorise("BPAY something", -5) == "Other"  # whole words only
    assert c.categorise("Salary ACME", 20000) == "Income"
    assert c.categorise("Monthly Account Admin Fee", -7.5) == "Bank fees"
    assert c.categorise("Kwik Spar", -40, rules=[(" kwik ", "Eating out")]) == "Eating out"
    assert c.merchant_key("POS Purchase Kauai Rosebank 1234") == "kauai rosebank"

    h = register("banker")
    db = SessionLocal()
    uid = db.query(User).filter(User.email == "banker@inv.example.com").one().id
    rows = api.get("/api/bank/transactions", headers=h).json()
    uber = next(t for t in rows if "Uber" in t["description"])
    r = api.patch(f"/api/bank/transactions/{uber['id']}", headers=h, json={"category": "Eating out"}).json()
    assert r["rule"] == "uber trip" and r["category"] == "Eating out"
    assert api.patch(f"/api/bank/transactions/{uber['id']}", headers=h, json={"category": "Nope"}).status_code == 400
    cats = api.get("/api/bank/categories", headers=h).json()
    assert {"keyword": "uber trip", "category": "Eating out", "source": "you"}.items() <= cats["rules"][0].items()

    # Groq names the leftovers; confident answers stick and teach a rule.
    monkeypatch.setattr(settings, "groq_api_keys", ["g"])
    other = [t for t in api.get("/api/bank/transactions", headers=h).json() if t["category"] in ("Other", "Other income")]
    monkeypatch.setattr(llm, "groq", lambda messages, max_tokens=900, json_mode=False: json.dumps(
        {"results": [{"i": i, "category": "Income", "confidence": 0.9, "keyword": "acme"} for i in range(len(other))]}))
    from app.banking.categorize import learn
    from app.banking.models import BankCategoryRule

    learn(db, uid, "slovosupermarket5", "Groceries", "ai")
    learn(db, uid, "slovosupermarket", "Groceries", "ai")  # same keyword twice in one batch
    learn(db, uid, "payment", "Transfers", "ai")  # too generic
    db.commit()
    kws = {r.keyword for r in db.query(BankCategoryRule).filter(BankCategoryRule.user_id == uid)}
    assert "slovosupermarket" in kws and "payment" not in kws
    out = api.post("/api/bank/categorise", headers=h).json()
    assert out["ai"] == len(other) and out["left"] == 0
    db.close()


def test_internal_transfers_are_not_income_or_spending():
    from types import SimpleNamespace as T

    from app.banking.reader import INVEST_RE, internal_pairs

    txns = [T(id=1, account="TymeBank ••1", date=date(2026, 9, 1), amount=-500.0),   # to own GoTyme
            T(id=2, account="GoTyme ••2", date=date(2026, 9, 2), amount=500.0),
            T(id=3, account="TymeBank ••1", date=date(2026, 9, 3), amount=-500.0),   # paid someone, no match
            T(id=4, account="TymeBank ••1", date=date(2026, 9, 3), amount=500.0),    # same account: not a move
            T(id=5, account="Capitec ••3", date=date(2026, 9, 20), amount=500.0)]   # too late for id 3
    assert internal_pairs(txns) == {1, 2}
    assert INVEST_RE.search("Payment to EasyEquities ref EE123") and not INVEST_RE.search("Checkers")


def test_atm_withdrawals_at_garages_and_shops_are_cash():
    from app.banking.categorize import categorise

    assert categorise("ATM Withdrawal at ENGEN WINTERVD DDU MABOPANE ZA 619014174187", -600) == "Cash"
    assert categorise("ATM Withdrawal at Boxer Spr Mabopane", -200) == "Cash"
    assert categorise("ATM Withdrawal at 21 Molete Makinta Road Mabopane", -400) == "Cash"
    assert categorise("ATM Withdrawal Fee", -10) == "Bank fees"
    assert categorise("Purchase at ENGEN WINTERVELD", -500) == "Transport"  # fuel is still transport


def test_atm_deposits_are_cash_deposits_not_income():
    from types import SimpleNamespace

    from app.banking.categorize import NAMES, categorise
    from app.banking.reader import income_category

    assert "Cash deposit" in NAMES
    for line in ("ATM Deposit at ENGEN WINTERVD DDU MABOPANE", "Cash Deposit at Boxer Mabopane", "ATM Cash Deposit 21 Molete Rd"):
        assert categorise(line, 500) == "Cash deposit"
        assert income_category(SimpleNamespace(description=line, category="Cash deposit")) == "Cash deposits"
    assert categorise("Salary deposit ACME", 9000) == "Income"
    assert categorise("ATM Withdrawal at ENGEN WINTERVD", -600) == "Cash"
