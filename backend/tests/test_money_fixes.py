from datetime import date
from types import SimpleNamespace as T

from app.banking import reader
from app.banking.parsers.tymebank import TymeBankLegacyParser


def txn(i, d, amount, desc, account="A", category="Other"):
    return T(id=i, date=d, amount=amount, description=desc, account=account, category=category)


def test_pocket_moves_are_internal_and_people_are_named():
    txns = [
        txn(1, date(2026, 5, 1), -100, "Money transferred in for GoalSave 123"),
        txn(2, date(2026, 5, 1), 100, "Money transferred in for GoalSave 123"),
        txn(3, date(2026, 5, 2), -50, "Transfer to Current account"),
        txn(4, date(2026, 5, 3), 346.71, "PayShap - Pay by ShapID, M NYOBOL", category="Transfers"),
        txn(5, date(2026, 5, 4), 200, "Transfer from Capitec", account="B"),
        txn(6, date(2026, 5, 4), -200, "Send money", account="C"),
    ]
    internal = reader.internal_pairs(txns)
    assert {1, 2, 3, 5, 6} <= internal and 4 not in internal
    assert reader.income_category(txns[3]) == "Once-off payments"
    assert reader.income_category(txn(7, date(2026, 5, 5), 9.0, "Interest_applied", category="Interest")) == "Interest"


def test_statement_summary_lines_are_not_payments():
    rows = reader.rows_from([
        {"date": date(2026, 10, 1), "description": "Summary Opening balance", "amount": 668.34, "type": "credit"},
        {"date": date(2026, 10, 1), "description": "Closing Balance", "amount": 10.0, "type": "credit"},
        {"date": date(2026, 10, 2), "description": "Purchase at Boxer", "amount": 50.0, "type": "debit"},
    ], "bank")
    assert [r["description"] for r in rows] == ["Purchase at Boxer"]


def test_fee_lines_are_bank_fees_whatever_the_merchant_rules_say():
    rules = [("mabopane square", "Shopping")]
    assert reader.categorise("Fee: ATM Withdrawal at MABOPANE SQUARE 2 PRETORIA ZA", amount=-10, rules=rules) == "Bank fees"
    assert reader.categorise("Purchase at MABOPANE SQUARE", amount=-50, rules=rules) == "Shopping"


def test_tymebank_ignores_reference_numbers_posing_as_amounts():
    text = "\n".join([
        "18 Mar 2026 Purchase at S2SSlovosupermarket5 Pretoria ZA - 4",
        "19 Mar 2026 Purchase at MAMA PRINCESS TUCK SHOP Winterveld ZA",
        "- 54.00 - 1,234.56",
        "20 Mar 2026 PayShap - Pay by ShapID, M NYOBOL - - 346.71 1,581.27",
    ])
    rows = TymeBankLegacyParser().parse(text)
    assert [(r["description"][:20], r["amount"], r["type"]) for r in rows] == [
        ("Purchase at MAMA PRI", 54.0, "debit"), ("PayShap - Pay by Sha", 346.71, "credit")]


def test_easyequities_cash_never_shows_a_wallet_hundreds_overdrawn():
    from app.invest.ee import sync

    conn = T(snapshot={"taken_at": "2026-10-02", "accounts": [
        {"id": 1, "name": "EasyEquities ZAR", "value": 14.24, "holdings": [
            {"contract_code": "STX40", "name": "Satrix 40", "current_value": 125.0, "purchase_value": 100.0, "current_price": 1.0}]},
        {"id": 2, "name": "TFSA", "value": 3.18, "holdings": [
            {"contract_code": "STXNDQ", "name": "Satrix Nasdaq", "current_value": 3.04, "purchase_value": 2.0, "current_price": 1.0}]},
    ]})
    original = sync.rand_rate
    sync.rand_rate = lambda db, cur: 1.0
    try:
        view, _ = sync.platform_view(None, conn)
    finally:
        sync.rand_rate = original
    cash = {a["name"]: round(a["cash_zar"], 2) for a in view["accounts"]}
    assert cash == {"EasyEquities ZAR": 0.0, "TFSA": 0.14}


CAPITEC = """Main Account Statement
  MR THABO JAMES MOLOI
Transaction History
Date Description Category Money In Money Out Fee* Balance
01/10/2024 Recurring Transfer Insufficient Funds of R1 000.00 (16916070)
21/10/2024 Payment Received: 1070143456004 Vault M Other Income 58.00 73.54
21/10/2024 Banking App External Payment: Tyme Savings -43.00 -2.00 28.54
31/10/2024 Payment Received: Acme Learnership Octpayment
2066267452
Other Income 3 465.00 3 493.54
31/10/2024 Banking App External PayShap Payment: Thabo James M
(081 000 0000)
Digital Payments -2 000.00 -6.00 1 487.54
* Includes VAT at 15%
*
 0860 10 20 43  ClientCare@capitecbank.co.za  capitecbank.co.za24hr Client Care Centre E W
Date Description Category Money In Money Out Fee* Balance
07/11/2025 Banking App Prepaid Purchase: Vodacom Cellphone -5.00 -0.50 1 482.04
07/11/2025 Banking App Correction: Prepaid Purchase Cellphone 5.00 0.50 1 487.54
30/11/2025 Monthly Account Admin Fee Fees -7.50 1 480.04
* Includes VAT at 15%
Pending Card Transactions
*
-R55.0026/09/2026 Tuck Shop Winterveld (Card 5997)
"""


def test_capitec_reads_every_row_and_follows_the_balance():
    from app.banking.parsers.capitec import CapitecParser

    rows = CapitecParser().parse(CAPITEC)
    got = [(r["description"][:30], r["amount"], r["type"], r["fee"], r["balance"]) for r in rows]
    assert got == [
        ("Payment Received: 107014345600", 58.0, "credit", 0.0, 73.54),
        ("Banking App External Payment: ", 43.0, "debit", 2.0, 28.54),
        ("Payment Received: Acme Learner", 3465.0, "credit", 0.0, 3493.54),  # "3 465.00", wrapped over 3 lines
        ("Banking App External PayShap P", 2000.0, "debit", 6.0, 1487.54),
        ("Banking App Prepaid Purchase: ", 5.0, "debit", 0.5, 1482.04),
        ("Banking App Correction: Prepai", 5.5, "credit", 0.0, 1487.54),  # the fee came back too
        ("Monthly Account Admin Fee", 7.5, "debit", 0.0, 1480.04),        # last row before "Pending"
    ]
    assert rows[2]["category"] == "Income"


def test_payments_between_your_own_accounts_by_name():
    own = reader.own_name_pattern([CAPITEC])
    assert own.search("Banking App External PayShap Payment: Thabo James M")
    assert own.search("PayShap - Pay by ShapID, T MOLOI")
    assert not own.search("PayShap - Pay by Account, C MOKOENA")
    assert not own.search("PayShap - Pay by ShapID, S MOLOI")  # a relative, not you
    txns = [txn(1, date(2026, 5, 1), -2000, "Banking App External PayShap Payment: Thabo James M"),
            txn(2, date(2026, 5, 9), 500, "PayShap - Pay by ShapID, C MOKOENA", category="Transfers")]
    assert reader.internal_pairs(txns, own=own) == {1}


GOTYME = """GoalSave Account
Account Number: 50348541894
Summary
Opening balance R0
Total Credit R500.33
Closing balance R0
Date Details Credits (+) Debits (-) Running Balance
06 Jul 2026 Transfer from Current account 500 - 500
10 Jul 2026 Earned interest 0.33 - 500.33
10 Jul 2026 Transfer to Current account - 500.33 0
GoalSave Account
Account Number: 50268212678
Date Details Credits (+) Debits (-) Running Balance
06 Sep 2026 Transfer from Current account 1,200 - 1,200
"""


def test_gotyme_text_reader_keeps_each_pocket_apart():
    from app.banking.parsers.gotyme import GoTymeTextParser

    rows = GoTymeTextParser().parse(GOTYME)
    assert [(r["description"], r["amount"], r["type"], r["balance"], r["account_number"][-4:]) for r in rows] == [
        ("Transfer from Current account", 500.0, "credit", 500.0, "1894"),
        ("Earned interest", 0.33, "credit", 500.33, "1894"),
        ("Transfer to Current account", 500.33, "debit", 0.0, "1894"),
        ("Transfer from Current account", 1200.0, "credit", 1200.0, "2678"),
    ]


def test_gotyme_read_from_text_is_queued_for_download_not_reparsed():
    from app.banking.models import BankStatement, BankTxn
    from app.db import SessionLocal
    from app.models import User

    db = SessionLocal()
    user = User(email="gotext@money.example.com", first_name="Go", last_name="Text", password="x")
    db.add(user)
    db.flush()
    uid = user.id
    bad = BankStatement(user_id=uid, gmail_id="g1", filename="a.pdf", bank="gotyme", status="ok", body="x", rows=2)
    good = BankStatement(user_id=uid, gmail_id="g2", filename="b.pdf", bank="gotyme", status="ok", body="y", rows=1)
    db.add_all([bad, good])
    db.flush()
    db.add_all([BankTxn(user_id=uid, statement_id=bad.id, key="k1", bank="gotyme", account="GoTyme ••1", date=date(2026, 7, 6),
                        description="Money transferred in for GoalSave 50883810179 -", amount=500.0, category="Transfers"),
                BankTxn(user_id=uid, statement_id=good.id, key="k2", bank="gotyme", account="GoTyme ••1", date=date(2026, 7, 6),
                        description="Money transferred in for GoalSave 50883810179", amount=-500.0, category="Transfers")])
    db.commit()
    out = reader.reparse(db, uid)
    assert out["to_download"] == 1 and out["reparsed"] == 0
    db.refresh(bad), db.refresh(good)
    assert (bad.status, good.status) == ("reread", "ok")
    assert db.query(BankTxn).filter(BankTxn.user_id == uid).count() == 2  # kept until the PDF is read again
    db.close()


def test_money_in_is_grouped_by_where_it_came_from():
    d = date(2026, 5, 5)
    pays = [txn(10 + i, date(2026, m, 5), 3000.0, "PayShap - Pay by ShapID, M NYOBOL -", category="Payments")
            for i, m in enumerate((3, 4, 5))]
    regular = reader.regular_payers(pays)
    assert regular == {"M Nyobol"}
    assert reader.income_category(pays[0], regular) == "M Nyobol (regular)"
    assert reader.income_category(txn(20, d, 500, "Payment Received: T Moshuwe", category="Income"), regular) == "Once-off payments"
    assert reader.income_category(txn(21, d, 2000, "Payment Received: Tcps Gcra Learnership Octpayment 12345", category="Income")) == "Salary & stipends"
    assert reader.income_category(txn(22, d, 370, "SASSA SRD grant", category="Income")) == "Grants"
    assert reader.income_category(txn(23, d, 60, "Purchase at Checkers", category="Shopping")) == "Refunds & reversals"
    assert reader.income_category(txn(24, d, 29, "Banking App Correction: Prepaid Purchase", category="Airtime & data")) == "Refunds & reversals"
    assert reader.income_category(txn(25, d, 300, "PayShap Payment Received: 45", category="Income")) == "From people (no name)"
    assert reader.payer("Payment Received: Paypal Xhvw9sj9 Transfer 1234567890") == "PayPal"


def test_payshap_references_tell_your_own_transfers_from_people():
    import re

    own = re.compile(r"\bMIGUEL\s+KUDAKASHE|\bM\.?\s+NYOBO|payshap payment received:?\s*MIGUEL\s*$", re.I)
    for ref in ("Ohkay", "Main", "Update", "Create", "1", "Miguel"):
        assert reader.own_reference(f"PayShap Payment Received: {ref}", own), ref
    for ref in ("Tumelo", "Mokone", "T Moshuwe", "Sthe"):
        assert not reader.own_reference(f"PayShap Payment Received: {ref}", own), ref
    assert reader.payer("PayShap Payment Received: Tumelo") == "Tumelo"
    d = date(2026, 5, 5)
    txns = [txn(1, d, 300, "PayShap Payment Received: Ohkay", category="Income"),
            txn(2, d, 300, "PayShap Payment Received: Tumelo", category="Income"),
            txn(3, d, 500, "Banking App Transfer from Live Better Savings Account (1234567890)", category="Transfers")]
    assert reader.internal_pairs(txns, own=own) == {1, 3}
    assert reader.income_category(txn(4, d, 570, "Payment Received: Nrmlsassa Nw 1456961942 Pen", category="Income")) == "Grants"


def test_crypto_payouts_are_named():
    d = date(2026, 1, 6)
    assert reader.income_category(txn(1, d, 2319.71, "Payment Received: E026ad01q0 Transfer 2908220041", category="Income")) == "Crypto sales"
    assert reader.income_category(txn(2, d, 500, "Luno payout", category="Income")) == "Crypto sales"
