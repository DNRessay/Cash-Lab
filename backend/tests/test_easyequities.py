import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.invest.ee import mail, platform, properties, sync
from app.invest.ee.models import EEConnection
from app.main import app
from tests.test_invest import fake_fetch, register

api = TestClient(app)
FIXTURES = Path(__file__).parent / "fixtures"
RealPlatform = platform.Platform

ACC = "Test User Account: {acc} Acc. number: EE0000001-{num} Trader: Test User"

SELL = ("EasyEquities Trade Tax Invoice for Afrimat Limited SHARES: (0) FSRs: (.2982) TRADE PRICE: 37.8300 "
        "Afrimat Limited TRADED 1 : SHARES FSRs (0) (.2982) TRADE PRICE: R 37.8300 "
        + ACC.format(acc="EasyEquities ZAR", num="1000001") +
        " First World Trader t/a EasyEquities INVOICE NUMBER: #89473742 SUBMISSION DATE: 2025-09-08 07:10:55 "
        "CASH SETTLEMENT DATE: 2 2025-09-15 DETAIL ZAR BROKER COMMISSION 0.02 SETTLEMENT AND ADMINISTRATION 3 0.01 "
        "VALUE-ADDED TAX ON COSTS (VAT) 0.01 EASYMONEY CREDIT ( How do I earn credit? ) (EM 0.00) "
        "GROSS AMOUNT DUE TO YOU 11.28 LESS COSTS 0.05 NET AMOUNT DUE TO YOU 11.23")
USD_SELL = SELL.replace("Afrimat Limited", "Tesla Inc").replace("R 37.8300", "$ 353.6100") \
    .replace("EasyEquities ZAR", "EasyEquities USD").replace("37.8300", "353.6100").replace("#89473742", "#89473755")
BID = ("Hi, System The Edge YOUR BID WAS SHARES: BID PRICE: 87.206897 <= R 1.16 TRADED 1 SHARES: FSRs: TRADE PRICE: "
       "87 .2068 R 1.00 " + ACC.format(acc="EasyProperties ZAR", num="2000002") +
       " INVOICE NUMBER: 80678386 SUBMISSION DATE: Mon Jan 27 09:03:10 UTC 2025 SETTLEMENT DATE: 2 2025-02-06T00:00:00Z "
       "DETAIL ZAR SETTLEMENT AND ADMINISTRATION 3 0.07 Auction Brokerage Commission Buy Charge 1.31 "
       "VALUE-ADDED TAX ON COSTS (VAT) 0.21 TOTAL TRANSACTION COST 1.59 TRADE VALUE 87.21 TOTAL COST 88.80 "
       "AMOUNT RESERVED 103.00 AMOUNT RETURNED TO YOU * 14.2")
ORDER = ("Confirmation of Open Order Hyde Park House Congrats, your order has been successfully placed. Hyde Park House "
         "OFFERING 1 SHARES: FSRs OFFER PRICE (98) (.220) >= R 1 " + ACC.format(acc="EasyProperties ZAR", num="2000002")
         + " APPLICATION NUMBER: 93949378 SUBMISSION DATE: Thu Dec 18 07:10:56 UTC 2025 DETAIL ZAR "
         "Auction Brokerage Commission Sell Charge 1.47 SETTLEMENT AND ADMINISTRATION 3 0.08 "
         "VALUE-ADDED TAX ON COSTS (VAT) 0.23 GROSS EST. AMOUNT DUE TO YOU 98.22 LESS EST. COSTS 1.78 "
         "NET EST. AMOUNT DUE TO YOU 96.44")
DEPOSIT = ("High Five Test! You legend! Currency: EasyEquities ZAR Account number: EE0000001-1000001 Action Deposit "
           "Amount 1,676.00 Date and time: Fri May 09 15:35:30 UTC 2025 Up next: It's time to invest!")
WITHDRAWAL = ("High Five Test! The below withdrawal has been processed as requested. Currency: TFSA Account number: "
              "EE0000001-1000002 Action Withdrawal Amount 37.00 Date and time: Wed Sep 17 21:28:42 UTC 2025")


def test_html_to_text_and_real_buy_email():
    html = (FIXTURES / "ee_trade_buy.html").read_text()
    p = mail.parse("info@easyequities.co.za", "Confirmation of your transaction", mail.html_to_text(html), html)
    assert p["kind"] == "trade" and p["parsed"]
    assert (p["instrument"], p["contract_code"], p["side"]) == ("Adcock Ingram Holdings Limited", "EQU.ZA.AIP", "buy")
    assert (p["quantity"], p["price"], p["value"], p["costs"], p["total"]) == (1.0068, 49.66, 50.0, 0.3, 50.3)
    assert (p["account"], p["reference"], p["currency"]) == ("EasyEquities ZAR", "84809776", "ZAR")
    assert p["date"] == datetime(2025, 5, 12, 7, 10, 5)


@pytest.mark.parametrize("subject,text,expect", [
    ("Confirmation of your transaction", SELL,
     {"kind": "trade", "side": "sell", "instrument": "Afrimat Limited", "quantity": 0.2982, "price": 37.83,
      "value": 11.28, "costs": 0.05, "total": 11.23, "reference": "89473742", "currency": "ZAR"}),
    ("Confirmation of your transaction", USD_SELL,
     {"kind": "trade", "side": "sell", "instrument": "Tesla Inc", "currency": "USD", "account": "EasyEquities USD"}),
    ("Confirmation of your transaction", BID,
     {"kind": "trade", "side": "buy", "instrument": "The Edge", "quantity": 87.2068, "price": 1.0, "value": 87.21,
      "costs": 1.59, "total": 88.8, "account": "EasyProperties ZAR", "reference": "80678386"}),
    ("Confirmation of Open Order Hyde Park House", ORDER,
     {"kind": "order", "side": "sell", "instrument": "Hyde Park House", "quantity": 98.22, "value": 98.22,
      "costs": 1.78, "total": 96.44, "reference": "93949378"}),
    ("Confirmation of a EFT deposit.", DEPOSIT,
     {"kind": "deposit", "value": 1676.0, "account": "EasyEquities ZAR", "currency": "ZAR"}),
    ("Confirmation of a Withdrawal", WITHDRAWAL, {"kind": "withdrawal", "value": 37.0, "account": "TFSA"}),
    ("Sirius Real Estate Limited (SRE) - DRIP DEC2025", "Hi Test! You hold shares in Sirius",
     {"kind": "corporate_action", "instrument": "Sirius Real Estate Limited"}),
    ("Scheduled Maintenance from 21-22 March", "We'll be offline", {"kind": "notice", "parsed": False}),
])
def test_parse_email_kinds(subject, text, expect):
    p = mail.parse("info@easyequities.co.za", subject, text, received=datetime(2025, 1, 1))
    for k, v in expect.items():
        assert p[k] == pytest.approx(v) if isinstance(v, float) else p[k] == v, (k, p[k], v)
    if expect["kind"] != "notice":
        assert p["parsed"]


def test_symbols_and_classes():
    assert sync.symbol_for("EQU.ZA.AIP") == "AIP.JO"
    assert sync.symbol_for("EQU.US.TSLA") == "TSLA"
    assert sync.symbol_for("EQU.ZA.PROP51", account="EasyProperties ZAR") == "EE:PROP51"
    assert sync.symbol_for("", "The Edge") == "EE:THEEDGE"
    assert sync.asset_class_for("EasyProperties ZAR", "The Edge") == "easyproperties"
    assert sync.asset_class_for("TFSA", "Satrix Global Balanced Fund of Funds ETF") == "etf"


# ── Platform ────────────────────────────────────────────────────────────────

OVERVIEW = """<html><body>My Investments <a href="/Statements/Index">Statements</a>
<div data-id="111" data-tradingcurrencyid="2"><span id="trust-account-types">EasyEquities ZAR</span></div>
<div data-id="222" data-tradingcurrencyid="2"><span id="trust-account-types">EasyProperties ZAR</span></div>
<div data-id="999" data-tradingcurrencyid="2"><span id="trust-account-types">Demo ZAR</span></div>
</body></html>"""


def holding_html(name, code, cost, value, price):
    return f"""<div class="holding-inner-container">
      <img class="instrument" src="https://resources.easyequities.co.za/logos/{code}.png">
      <div class="equity-image-as-text">{name}</div>
      <div class="purchase-value-cell">R{cost}</div><div class="current-value-cell">R{value}</div>
      <div class="current-price-cell">R{price}</div>
      <div class="collapse-container"><span data-detailviewurl="/AccountOverview/GetInstrumentDetailAction/?IsinCode=ZAE1"></span></div>
    </div>"""


HOLDINGS = {
    "111": "<div class='holding-inner-container'>header</div>" + holding_html("Adcock Ingram", "EQU.ZA.AIP", "50.00", "1 050.00", "52.00"),
    "222": "<div class='holding-inner-container'>header</div>" + holding_html("The Edge", "EQU.ZA.PROP9", "87.21", "95.00", "1.09"),
}


class FakeResponse:
    def __init__(self, status=200, text="", data=None, headers=None):
        self.status_code, self.text, self._data, self.headers = status, text, data, headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    def __init__(self, login_status=302, overview=OVERVIEW):
        self.login_status, self.overview, self.account = login_status, overview, None

    def post(self, url, data=None, **kw):
        if url.endswith(platform.SIGN_IN):
            return FakeResponse(self.login_status, "sign in page", headers={"location": "/Dashboard"})
        self.account = data["trustAccountId"]
        return FakeResponse(200)

    def get(self, url, **kw):
        if url.endswith(platform.OVERVIEW):
            return FakeResponse(200, self.overview)
        if "GetHoldingsView" in url:
            return FakeResponse(200, HOLDINGS[self.account])
        if "GetInstrumentDetailAction" in url:
            return FakeResponse(200, "<div>#Shares</div><div>1</div><div>#FSR</div><div>.0068</div>")
        if "Valuations" in url:
            return FakeResponse(200, data='{"TopSummary": {"AccountValue": "R1 100.00"}}')
        if url.endswith("/Statement"):
            return FakeResponse(200, STATEMENT_PAGE)
        if "/Statement/Download" in url:
            r = FakeResponse(200)
            r.content = PDF
            return r
        if "GetTransactions" in url:
            if self.account != "111":
                return FakeResponse(200, data=[])
            return FakeResponse(200, data=[
                {"TransactionId": 1, "Action": "Buy", "Comment": "Bought Adcock", "DebitCredit": -50.3,
                 "TransactionDate": "/Date(1747033805000)/"},
                {"TransactionId": 2, "Action": "Deposit", "Comment": "EFT Deposit", "DebitCredit": 1676.0,
                 "TransactionDate": "2025-05-09T15:35:30"},
                {"TransactionId": 3, "Action": "Dividend", "Comment": "Adcock Ingram dividend", "DebitCredit": 2.5,
                 "TransactionDate": "2025-06-02T00:00:00"},
                {"TransactionId": 4, "Action": "", "Comment": "Monthly custody fee", "DebitCredit": -1.15,
                 "TransactionDate": "2025-06-30T00:00:00"},
            ])
        return FakeResponse(404)


STATEMENT_PAGE = """<html><title>Statements</title><div class="panel"><h4>EasyEquities ZAR</h4>
<button class="statementDownloadBtn" data-url="/Statement/Download?id=1" data-filename="EE ZAR Aug 2026.pdf">Download</button>
<button class="statementDownloadBtn" data-url="/Statement/Download?id=2" data-filename="EE ZAR Jul 2026.pdf">Download</button>
</div></html>"""
PDF = b"%PDF-1.4 fake statement"

# The EasyProperties API as the EasyProperties app uses it (prices in cents).
import base64 as _b64, json as _json  # noqa: E402
TOKEN = "h." + _b64.urlsafe_b64encode(_json.dumps({"userid": 4242}).encode()).decode().rstrip("=") + ".sig"
EP_ACCOUNTS = [{"trustAccountId": 111, "tradingCurrencyId": 2}, {"trustAccountId": 555, "tradingCurrencyId": 66}]
EP_ACCOUNT = {"trustAccountId": 555, "trustAccountValue": 316.0, "properties": [
    {"property": {"id": 9}, "quantity": 87.2068, "vwap": 100.0, "rentalIncomeTotal": 1.23456},
    {"property": {"id": 12}, "quantity": 150, "vwap": 98.21},
]}
EP_CATALOGUE = [
    {"id": 9, "name": "The Edge", "contractCode": "EQU.ZA.PROP9", "financialInfo": {"sharePrice": 135.0, "rentalYieldPercentage": 0.44}},
    {"id": 12, "name": "Four on O - Sea Point", "contractCode": "EQU.ZA.PROP12",
     "financialInfo": {"sharePrice": 132.007, "rentalYieldPercentage": 0.89}},
    {"id": 99, "name": "Not mine", "financialInfo": {"sharePrice": 100}},
]
LOGIN_PAGE = """<html><title>Log in | EasyID</title><form id="loginForm" method="post" action="">
<input name="ReturnUrl" value="/connect/authorize/callback?x=1"><input name="ClientIdForProperties" value="">
<input name="Response" value=""><input name="Username"><input name="IsUsernameProvided" value="false">
<input name="Password"><input name="__RequestVerificationToken" value="csrf"></form></html>"""


class FakeIdpSession:
    """EasyID (OAuth code + PKCE) and the EasyProperties API."""

    def __init__(self, accept=True):
        self.accept, self.sent = accept, {}

    def get(self, url, headers=None, **kw):
        if "/connect/authorize?" in url:
            return FakeResponse(200, LOGIN_PAGE)
        if "/connect/authorize/callback" in url:
            return FakeResponse(302, headers={"location": properties.REDIRECT + "?code=abc&state=x"})
        if url.startswith(properties.API):
            assert headers["Authorization"] == f"Bearer {TOKEN}"
            if url.endswith("/user/accounts/4242"):
                return FakeResponse(200, data=EP_ACCOUNTS)
            if url.endswith("/property/all"):
                return FakeResponse(200, data=EP_CATALOGUE)
            return FakeResponse(403, "forbidden")
        return FakeResponse(404)

    def post(self, url, data=None, json=None, **kw):
        if url.endswith("/user/accesstoken"):
            self.sent["token"] = json
            return FakeResponse(200, data={"access_token": TOKEN, "id_token": "i", "refresh_token": "r"})
        if url.endswith("/user/account"):
            self.sent["account"] = json
            return FakeResponse(200, data=EP_ACCOUNT)
        self.sent["login"] = data
        if not self.accept:
            return FakeResponse(200, "<form id='loginForm'>Invalid username or password</form>")
        return FakeResponse(302, headers={"location": "/connect/authorize/callback?client_id=x"})


REAL_EP_FETCH = properties.fetch


def fake_ep_fetch(u, p):
    return REAL_EP_FETCH(u, p, session=FakeIdpSession())


def fake_platform(login_status=302):
    return lambda base_url=platform.BASE_URL: RealPlatform(base_url, FakeSession(login_status=login_status))


def test_easyproperties_login_and_holdings():
    idp = FakeIdpSession()
    ep = REAL_EP_FETCH("me", "pw", session=idp)
    assert idp.sent["login"]["Username"] == "me" and idp.sent["login"]["__RequestVerificationToken"] == "csrf"
    assert idp.sent["login"]["button"] == "login"  # otherwise EasyID answers access_denied
    assert idp.sent["token"]["authorizationCode"] == "abc" and idp.sent["token"]["codeVerifier"]
    assert idp.sent["account"] == {"userId": 4242, "trustAccountId": "555"}  # the rand (66) account
    edge, four = ep["holdings"]
    assert (edge["name"], edge["shares"], edge["contract_code"]) == ("The Edge", 87.2068, "EQU.ZA.PROP9")
    assert edge["current_value"] == pytest.approx(117.73, abs=0.01) and edge["purchase_value"] == pytest.approx(87.21, abs=0.01)
    assert four["rental_yield"] == pytest.approx(0.0089)
    assert edge["rental_income"] == 1.23456
    assert ep["shapes"]["user/account"]["properties"][1] == "x2"  # keys only, no values
    with pytest.raises(platform.PlatformError) as e:
        REAL_EP_FETCH("me", "wrong", session=FakeIdpSession(accept=False))
    assert e.value.stage == "easyproperties login"


def test_platform_parsers():
    assert [a["name"] for a in platform.parse_accounts(OVERVIEW)] == ["EasyEquities ZAR", "EasyProperties ZAR", "Demo ZAR"]
    rows = platform.parse_holdings(HOLDINGS["111"])
    assert rows[0]["contract_code"] == "EQU.ZA.AIP" and rows[0]["current_value"] == 1050.0
    assert platform.parse_shares("<div>#Shares</div><div>12</div><div>#FSR</div><div>.5</div>") == 12.5
    assert platform.money("-R2 000.50") == -2000.5
    assert platform.money(3.4e-05) == 3.4e-05 and platform.money(0.0034) == 0.0034  # numbers stay numbers


def test_snapshot_and_login_failure():
    snap = platform.snapshot("u", "p", client=platform.Platform(session=FakeSession()), ep_fetch=fake_ep_fetch)
    eq, prop = snap["accounts"]  # the demo account is skipped
    assert snap["statement_links"] == ["/Statements/Index"]
    assert eq["value"] == 1100.0 and eq["holdings"][0]["shares"] == 1.0068
    # EasyProperties holdings come from the EasyProperties site; the account = wallet cash + properties.
    assert [h["name"] for h in prop["holdings"]] == ["The Edge", "Four on O - Sea Point"]
    assert prop["holdings"][1]["purchase_value"] == pytest.approx(150 * 0.9821)
    assert prop["value"] == pytest.approx(1100.0 - 95.0 + 87.2068 * 1.35 + 150 * 1.32007)
    assert snap["easyproperties"]["source"] == "api"
    assert [x["name"] for x in snap["statements"]] == ["EE ZAR Aug 2026.pdf", "EE ZAR Jul 2026.pdf"]
    assert snap["statements"][0]["account"] == "EasyEquities ZAR"
    with pytest.raises(platform.PlatformError) as e:
        platform.snapshot("u", "bad", client=platform.Platform(session=FakeSession(login_status=200)))
    assert e.value.stage == "login"
    with pytest.raises(platform.PlatformError) as e:
        platform.snapshot("u", "p", client=platform.Platform(session=FakeSession(overview="<html>new layout</html>")))
    assert e.value.stage == "accounts" and "new layout" in e.value.page


def test_account_switch_that_does_not_stick_is_not_copied(monkeypatch):
    monkeypatch.setitem(HOLDINGS, "222", HOLDINGS["111"])
    broken_ep = lambda u, p: REAL_EP_FETCH(u, p, session=FakeIdpSession(accept=False))  # noqa: E731
    snap = platform.snapshot("u", "p", client=platform.Platform(session=FakeSession()), ep_fetch=broken_ep)
    assert snap["easyproperties"]["error"].startswith("easyproperties login")
    prop = snap["accounts"][1]
    assert prop["holdings"] == [] and "previous account" in prop["warnings"][0]


# ── End to end through the API ──────────────────────────────────────────────

def fake_messages():
    buy = (FIXTURES / "ee_trade_buy.html").read_text()
    msgs = [
        ("m1", "Confirmation of your transaction", "", buy, datetime(2025, 5, 12, 7, 11)),
        ("m2", "Confirmation of a EFT deposit.", DEPOSIT, "", datetime(2025, 5, 9, 15, 36)),
        ("m3", "Confirmation of your transaction", BID, "", datetime(2025, 1, 27, 9, 4)),
        ("m4", "Confirmation of Open Order Hyde Park House", ORDER, "", datetime(2025, 12, 18, 7, 11)),
        ("m5", "Sirius Real Estate Limited (SRE) - DRIP DEC2025", "You hold shares in Sirius", "",
         datetime(2025, 12, 11, 3, 32)),
    ]
    out = [{"uid": i + 1, "message_id": mid, "sender": "info@easyequities.co.za", "subject": s, "received": r,
            "html": h, "text": t or mail.html_to_text(h)} for i, (mid, s, t, h, r) in enumerate(msgs)]
    out.append({"uid": 9, "message_id": "promo", "sender": "noreply@easyequities.co.za", "subject": "SpaceX!",
                "received": datetime(2026, 6, 1), "html": "", "text": "buy now"})
    return out


@pytest.fixture
def market(monkeypatch):
    from app.invest import prices

    monkeypatch.setattr(prices, "fetch", fake_fetch)
    monkeypatch.setattr(mail, "fetch", lambda *a, **k: (fake_messages(), 9, "1"))
    monkeypatch.setattr(platform, "Platform", fake_platform())
    monkeypatch.setattr(properties, "fetch", fake_ep_fetch)


def test_connect_sync_and_portfolio(market):
    h = register("eeuser")
    r = api.put("/api/ee/mail", headers=h, json={"address": "me@gmail.com", "app_password": "abcd efgh ijkl mnop"})
    assert r.status_code == 200, r.text
    assert r.json()["mail"]["status"] == "ok"

    mails = api.get("/api/ee/mails", headers=h).json()
    assert sorted(m["kind"] for m in mails) == ["corporate_action", "deposit", "order", "trade", "trade"]
    txns = api.get("/api/invest/transactions", headers=h).json()
    kinds = sorted((t["kind"], t["symbol"]) for t in txns)
    assert kinds == [("buy", "AIP.JO"), ("buy", "EE:THEEDGE"), ("deposit", "")]  # orders/notices aren't trades

    r = api.put("/api/ee/platform", headers=h, json={"username": "me", "password": "secret"})
    body = r.json()
    assert body["platform"]["status"] == "ok", body
    assert [a["name"] for a in body["accounts"]] == ["EasyEquities ZAR", "EasyProperties ZAR"]
    db = SessionLocal()
    conn = db.query(EEConnection).one()
    assert conn.password.startswith("enc:") and conn.mail_password.startswith("enc:")
    db.close()

    s = api.get("/api/invest/summary", headers=h).json()
    assert [x["symbol"] for x in s["holdings"]].count("AIP.JO") == 1  # not counted twice
    aip = next(x for x in s["holdings"] if x["symbol"] == "AIP.JO")
    assert aip["price"] == 52.0 and aip["price_source"].startswith("EasyEquities") and aip["account"] == "EasyEquities ZAR"
    assert aip["value"] == 1050.0 and aip["quantity"] == 1.0068
    ep_total = 87.2068 * 1.35 + 150 * 1.32007
    assert s["easyequities"]["value"] == pytest.approx(2200.0 - 95.0 + ep_total)
    assert s["cash"] == 2200.0 - 1050.0 - 95.0  # wallet cash = account value - holdings
    assert s["property_equity"] == pytest.approx(s["easyproperties_value"]) and s["easyproperties_value"] > 0
    assert s["history"][-1]["value"] == pytest.approx(s["value"], abs=0.01) and len(s["history"]) == 1
    # The Edge bought by email is the same holding as the EasyProperties card, so it's counted once.
    assert [x["name"] for x in s["holdings"]].count("The Edge") == 1
    # every holding comes from what EasyEquities holds now, not from old buy emails
    assert {x["account"] for x in s["holdings"]} <= {"EasyEquities ZAR", "EasyProperties ZAR"}
    assert s["value"] == pytest.approx(1050.0 + ep_total + s["cash"])
    assert s["invested"] == 1676.0  # the email deposit (Gmail history wins over the statement when both exist)

    assert s["income"] == {"dividend": 2.5, "interest": 0.0, "fee": 1.15, "tax": 0.0}

    c = api.get("/api/invest/charts", headers=h).json()
    i = c["months"].index("2025-05")
    assert c["money_in"][i] == 1676.0 and c["buys"][i] == pytest.approx(50.3)  # email deposit + Adcock buy
    j = c["months"].index("2025-06")
    assert c["income"][j] == pytest.approx(2.5) and c["costs"][j] == pytest.approx(1.15)  # statement dividend, fee
    assert c["months"][-1] == date.today().isoformat()[:7]

    st = api.get("/api/ee/statements", headers=h).json()
    assert {x["name"]: x["period"] for x in st} == {"EE ZAR Aug 2026.pdf": "2026-08", "EE ZAR Jul 2026.pdf": "2026-07"}
    assert api.get("/api/ee/statements/0?inline=true", headers=h).headers["content-disposition"].startswith("inline")
    pdf = api.get("/api/ee/statements/0", headers=h)
    assert pdf.status_code == 200 and pdf.content == PDF and pdf.headers["content-type"] == "application/pdf"
    assert api.get("/api/ee/statements/9", headers=h).status_code == 404

    t = api.get("/api/ee/transactions", headers=h).json()
    assert [r["category"] for r in t["rows"]] == ["fee", "dividend", "trade", "deposit"]  # newest first
    row = t["rows"][2]
    assert (row["action"], row["date"], row["amount"]) == ("Buy", "2025-05-12", -50.3)
    assert {"account": "EasyEquities ZAR", "year": "2025", "currency": "ZAR", "trade": -50.3, "deposit": 1676.0,
            "dividend": 2.5, "fee": -1.15} in t["totals"]
    assert t["by_account"] == [{"account": "EasyEquities ZAR", "income": 2.5, "costs": 1.15}]
    assert t["cost_types"] == [{"type": "Account fees", "amount": 1.15}]  # "Monthly custody fee"

    # A second sync adds nothing twice; re-reading emails is idempotent too.
    api.post("/api/ee/sync", headers=h)
    assert len(api.get("/api/invest/transactions", headers=h).json()) == 3
    assert api.post("/api/ee/reparse", headers=h).json() == {"emails": 5, "parsed": 5, "imported": 0}

    ca = next(m for m in mails if m["kind"] == "corporate_action")
    assert api.patch(f"/api/ee/mails/{ca['id']}", headers=h, json={"done": True}).json()["done"] is True
    assert "Sirius" in api.get(f"/api/ee/mails/{ca['id']}", headers=h).json()["body"]


@pytest.mark.parametrize("action,comment,cat", [
    ("Dividend", "Sirius Real Estate - Dividend", "dividend"),
    ("", "Dividend Withholding Tax @ 20%", "tax"),
    ("Custody Fee", "Monthly custody fee", "fee"),
    ("", "VAT on custody fee", "fee"),
    ("Deposit", "EFT deposit", "deposit"),
    ("Withdrawal", "Withdrawal to bank", "withdrawal"),
    ("", "Interest on cash", "interest"),
    ("Buy", "Bought Capitec", "trade"),
    ("", "Something new", "other"),
])
def test_statement_categories(action, comment, cat):
    assert sync.categorise(action, comment) == cat


def test_statement_file_names_and_cost_types():
    from app.invest.ee.router import statement_info

    accounts = {"EE1720926-7814224": "EasyEquities ZAR"}
    info = statement_info("EE1720926-7814224 Test User - Monthly Statement 2025-08-31.pdf", accounts)
    assert info == {"account": "EasyEquities ZAR", "account_number": "EE1720926-7814224", "kind": "monthly", "period": "2025-08"}
    assert statement_info("EE1720926-7814224 Test User -Monthly Statement Aug26.pdf", accounts)["period"] == "2026-08"
    assert statement_info("EE1720926-7814224 Test User - Tax Statement 2025_2026.pdf", accounts)["period"] == "2025/26"
    assert statement_info("EE1720926-11088540 Test User - Tax Statement 2025.pdf", accounts)["kind"] == "tax"
    assert statement_info("EE1720926-11088540 Test User - Tax Statement 2025.pdf", accounts)["account"] == "EE1720926-11088540"
    assert sync.cost_type("VAT on custody fee") == "VAT"
    assert sync.cost_type("Dividend Withholding Tax @ 20%") == "Dividend tax"
    assert sync.cost_type("Broker commission") == "Brokerage"


def test_sold_shares_from_emails_do_not_show_as_holdings(market):
    h = register("soldout")
    api.post("/api/invest/transactions", headers=h, json={"date": "2025-01-10", "kind": "buy", "symbol": "GRT.JO",
                                                          "quantity": 10, "price": 16, "asset_class": "reit"})
    db = SessionLocal()
    from app.invest.models import InvestTxn
    from app.models import User

    uid = db.query(User).filter(User.email == "soldout@inv.example.com").one().id
    db.add(InvestTxn(user_id=uid, date=date(2024, 5, 1), kind="buy", symbol="TRE.JO", name="Trencor Limited",
                     quantity=500, price=8, amount=4000, source="easyequities"))  # delisted since; no sell email
    db.commit()
    db.close()
    api.put("/api/ee/platform", headers=h, json={"username": "me", "password": "secret"})
    symbols = [x["symbol"] for x in api.get("/api/invest/summary", headers=h).json()["holdings"]]
    assert "TRE.JO" not in symbols  # gone from EasyEquities, so gone from holdings
    assert "GRT.JO" in symbols  # typed in by hand, still counts
    api.delete("/api/ee/platform", headers=h)


def test_failed_sync_keeps_last_snapshot(market, monkeypatch):
    h = register("eeuser")
    api.put("/api/ee/platform", headers=h, json={"username": "me", "password": "secret"})
    monkeypatch.setattr(platform, "Platform", fake_platform(login_status=200))
    body = api.post("/api/ee/sync", headers=h).json()
    assert body["platform"]["status"] == "error" and body["platform"]["error_stage"] == "login"
    assert len(body["accounts"]) == 2  # still showing the last good read


def test_statement_builds_history_without_gmail(market):
    h = register("nosy")
    api.put("/api/ee/platform", headers=h, json={"username": "me", "password": "secret"})
    s = api.get("/api/invest/summary", headers=h).json()
    assert s["invested"] == 1676.0 and s["since"] == "2025-05-09"  # money in, from the statement's deposit
    assert s["gain"] == pytest.approx(s["value"] - 1676.0)
    assert {b["symbol"] for b in s["benchmarks"]} == {"STX40.JO", "STXPRO.JO", "ZAR=X"}
    api.delete("/api/ee/platform", headers=h)


def test_other_users_cannot_see_mail(market):
    register("eeuser")
    other = register("nosy")
    assert api.get("/api/ee/mails", headers=other).json() == []
    assert api.get("/api/ee/mails/1", headers=other).status_code == 404


def test_sign_in_with_google(market, monkeypatch):
    from app.config import settings
    from app.invest.ee import gmail

    h = register("eeuser")
    assert api.get("/api/ee/google/start", headers=h).status_code == 400  # not configured yet
    monkeypatch.setattr(settings, "google_client_id", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "secret")
    from urllib.parse import unquote

    url = unquote(api.get("/api/ee/google/start", headers=h).json()["url"])
    assert "gmail.readonly" in url and "access_type=offline" in url and "/api/ee/google/callback" in url
    state = url.split("state=")[1].split("&")[0]

    seen = {}
    monkeypatch.setattr(gmail, "exchange", lambda code, redirect: seen.update(code=code, redirect=redirect) or
                        {"refresh_token": "rt-123", "access_token": "at", "email": "me@gmail.com"})
    monkeypatch.setattr(gmail, "fetch", lambda refresh, since_uid=0, uidvalidity="": (
        seen.update(refresh=refresh) or fake_messages(), 1790000000, "gmail"))
    r = api.get(f"/api/ee/google/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302 and "google=ok" in r.headers["location"] and r.headers["location"].endswith("#google")
    assert seen["code"] == "abc" and seen["refresh"] == "rt-123"  # the sealed token is unsealed for Gmail
    status = api.get("/api/ee", headers=h).json()["mail"]
    assert (status["method"], status["address"], status["status"]) == ("google", "me@gmail.com", "ok")
    db = SessionLocal()
    conn = db.query(EEConnection).filter(EEConnection.mail_address == "me@gmail.com").one()
    assert conn.mail_password.startswith("oauth:enc:") and "rt-123" not in conn.mail_password
    db.close()

    bad = api.get("/api/ee/google/callback?code=abc&state=forged", follow_redirects=False)
    assert "google=error" in bad.headers["location"]
    denied = api.get(f"/api/ee/google/callback?error=access_denied&state={state}", follow_redirects=False)
    assert "google=error" in denied.headers["location"] and "cancelled" in denied.headers["location"]


SAMPLE_STATEMENT = """EasyEquities ZAR                 Monthly Statement            August 2025
Date          Description                                          Amount        Balance
2025-08-04    Dividend Sirius Real Estate @ 5.9 cents               0.00314      12.34567
05/08/2025    Monthly custody fee                                  (1.15)         11.19567
12 Aug 2025   VAT on custody fee                                   -0.17          11.02567
2025-08-20    EFT Deposit                                        1 676.00      1 687.02567
Closing balance                                                                1 687.02567
"""


def test_encrypted_pdf_opens_with_blank_password():
    from pypdf import PdfWriter

    from app.invest.ee import reader

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.encrypt(user_password="", owner_password="owner-secret")
    buf = __import__("io").BytesIO()
    w.write(buf)
    text, pages = reader.pdf_text(buf.getvalue())
    assert pages == 1
    locked = PdfWriter()
    locked.add_blank_page(width=200, height=200)
    locked.encrypt(user_password="needs-this", owner_password="x")
    buf2 = __import__("io").BytesIO()
    locked.write(buf2)
    with pytest.raises(ValueError):
        reader.pdf_text(buf2.getvalue())
    assert reader.pdf_text(buf2.getvalue(), extra_passwords=["needs-this"])[1] == 1


def test_statement_line_parser():
    from app.invest.ee import reader

    rows = reader.parse_lines(SAMPLE_STATEMENT)
    assert [(r["date"].isoformat(), r["amount"], r["balance"]) for r in rows] == [
        ("2025-08-04", 0.00314, 12.34567), ("2025-08-05", -1.15, 11.19567),
        ("2025-08-12", -0.17, 11.02567), ("2025-08-20", 1676.0, 1687.02567)]
    assert rows[0]["description"] == "Dividend Sirius Real Estate @ 5.9 cents"  # numbers inside text stay text
    assert all(set(line) <= set("a9 -/.,():@%") for line in reader.shape(SAMPLE_STATEMENT))  # layout only


def test_statement_reader_saves_cents(market, monkeypatch):
    from app.invest.ee import reader

    monkeypatch.setattr(reader, "pdf_text", lambda data, extra_passwords=(): (SAMPLE_STATEMENT, 1))
    h = register("eeuser")
    api.put("/api/ee/platform", headers=h, json={"username": "me", "password": "secret"})
    r = api.post("/api/ee/statements/read", headers=h).json()
    assert (r["read_now"], r["left"], r["total"], r["done"]) == (2, 0, 2, 2)
    assert r["lines_now"] == 8 and r["lines"] == 8  # 4 lines x 2 statements
    assert api.post("/api/ee/statements/read", headers=h).json()["read_now"] == 0  # nothing twice

    t = api.get("/api/ee/transactions", headers=h).json()
    tiny = next(x for x in t["rows"] if x["comment"].startswith("Dividend Sirius"))
    assert tiny["amount"] == 0.00314 and tiny["category"] == "dividend" and tiny["source"] == "pdf"
    assert t["reader"]["done"] == 2

    doc = api.get("/api/ee/statements/0/text", headers=h).json()
    assert "Monthly custody fee" in doc["text"] and doc["lines"][1]["amount"] == -1.15
    assert api.post("/api/ee/statements/reparse", headers=h).json() == {"statements": 2, "lines": 8}


def test_statement_password_is_sealed_and_used(market, monkeypatch):
    from app.invest.ee import reader
    from app.invest.ee.models import EESetting

    seen = []
    monkeypatch.setattr(reader, "pdf_text", lambda data, extra_passwords=(): seen.append(list(extra_passwords)) or
                        (SAMPLE_STATEMENT, 1))
    h = register("nosy")
    api.put("/api/ee/platform", headers=h, json={"username": "me", "password": "secret"})
    r = api.put("/api/ee/statements/password", headers=h, json={"password": "8001015009087"}).json()
    assert r["pdf_password_set"] is True
    db = SessionLocal()
    row = db.query(EESetting).one()
    assert row.pdf_password.startswith("enc:") and "8001015009087" not in row.pdf_password
    db.close()
    api.post("/api/ee/statements/read", headers=h)
    assert seen and seen[0] == ["8001015009087"]
    api.delete("/api/ee/platform", headers=h)


MONTHLY_STATEMENT = """                                  Investor Details    Scheduled Transactions    Cumulative Account Summary    Performance Results
Cumulative Account Summary (last 12 months)
                        Mar 2026        Apr 2026        May 2026        Jun 2026        Jul 2026        Aug 2026
Opening Value              100             110             112             120             121             130
Contributions               10               0               5               0              10               0
Withdrawals                  0               0               0               0               0             -20
Dividends                    1               2               0               1               0               3
Dividend Withholding Tax     0              -1               0               0               0              -1
Platform Charges            -1              -1              -1              -1              -1              -1
Unrealised Gains/Losses      0               2               4               1              -1               9
Closing Value              110             112             120             121             130             120
                        Opening Balance              Purchases                      Sales                          Closing Balance
Instrument          Qty       Cost      Qty       Cost      Qty       Proceeds     Profit/Loss     Qty       Cost      Cost Price     Curr Price     Curr Value     Weight
Satrix 40         1.0000     90.00    0.5000     45.00    0.0000       0.00          0.00      1.5000    135.00      9 000.00       9 500.00        142.50      95.0%
Sirius Real      2.0000      5.00    0.0000      0.00    0.0000       0.00          0.00      2.0000      5.00        250.00         375.00          7.50       5.0%
Estate Ltd
Total                                                                                                    140.00                                    150.00     100.0%
"""


def test_monthly_statement_parser():
    from app.invest.ee import reader

    m = reader.parse_monthly(MONTHLY_STATEMENT)
    assert m["months"] == ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    kinds = {f["label"]: f["kind"] for f in m["figures"]}
    assert kinds == {"Opening Value": "opening", "Contributions": "money_in", "Withdrawals": "money_out",
                     "Dividends": "income", "Dividend Withholding Tax": "costs", "Platform Charges": "costs",
                     "Unrealised Gains/Losses": "growth", "Closing Value": "closing"}
    closing = [f["value"] for f in m["figures"] if f["kind"] == "closing"]
    assert closing == [110, 112, 120, 121, 130, 120]
    satrix, sirius = m["holdings"]
    assert (satrix["qty"], satrix["cost"], satrix["price"], satrix["value"], satrix["weight"]) == (1.5, 135.0, 9500.0, 142.5, 95.0)
    assert (satrix["buy_qty"], satrix["buy_cost"]) == (0.5, 45.0)
    assert sirius["instrument"] == "Sirius Real Estate Ltd"  # wrapped name joined


def test_monthly_statements_feed_history_and_charts(market, monkeypatch):
    from app.invest.ee import reader

    monkeypatch.setattr(reader, "pdf_text", lambda data, extra_passwords=(): (MONTHLY_STATEMENT, 2))
    monkeypatch.setattr(sys.modules[__name__], "STATEMENT_PAGE", STATEMENT_PAGE.replace("EE ZAR Aug 2026.pdf", "EE0000001-1000001 Test -Monthly Statement Aug26.pdf").replace("EE ZAR Jul 2026.pdf", "EE0000001-1000001 Test -Monthly Statement Jul26.pdf"))
    h = register("nosy")
    api.put("/api/ee/platform", headers=h, json={"username": "me", "password": "secret"})
    api.post("/api/ee/statements/read", headers=h)
    s = api.get("/api/invest/summary", headers=h).json()
    hist = s["statement_history"]
    assert [p["month"] for p in hist] == ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    assert [p["value"] for p in hist] == [110, 112, 120, 121, 130, 120]
    assert [p["invested"] for p in hist] == [110, 110, 115, 115, 125, 105]  # opening 100, then contributions/withdrawals
    c = api.get("/api/invest/charts", headers=h).json()
    i = c["months"].index("2026-04")
    assert c["income"][i] == 2 and c["costs"][i] == 2  # dividend; withholding tax + charges
    api.delete("/api/ee/platform", headers=h)


def test_snapshot_survives_unreadable_holdings_page(monkeypatch):
    """One account whose holdings page can't be read must not fail the whole connection."""
    class P(platform.Platform):
        def __init__(self): pass
        statement_links = []
        def login(self, u, p): pass
        def accounts(self): return [{"id": "1", "name": "TFSA ZAR"}]
        def holdings(self, account_id, with_shares=True):
            raise platform.PlatformError("holdings", "could not read the holdings table (layout changed?)", "<html/>")
        def valuations(self, account_id): return None
        def transactions(self, account_id): return []
        def statements(self): return []
    snap = platform.snapshot("u", "p", client=P(), ep_fetch=lambda u, p: {"holdings": []})
    assert snap["accounts"][0]["holdings"] == [] and "holdings table" in snap["accounts"][0]["warnings"][0]


def test_unreadable_holdings_is_logged_with_page_shape_only(caplog):
    """The CloudWatch line must say which account/stage failed and the page's structure, but never its content."""
    import logging

    page = "<html><title>Holdings</title><div class='holding-list'>SECRET-NAME R99 999.99</div></html>"

    class P(platform.Platform):
        def __init__(self): pass
        statement_links = []
        def login(self, u, p): pass
        def accounts(self): return [{"id": "7", "name": "TFSA ZAR"}]
        def holdings(self, account_id, with_shares=True):
            raise platform.PlatformError("holdings", "could not read the holdings table (layout changed?)", page)
        def valuations(self, account_id): return None
        def transactions(self, account_id): return []
        def statements(self): return []
    with caplog.at_level(logging.WARNING):
        platform.snapshot("u", "p", client=P(), ep_fetch=lambda u, p: {"holdings": []})
    line = next(r.getMessage() for r in caplog.records if "holdings unreadable" in r.getMessage())
    assert "TFSA ZAR" in line and "stage=holdings" in line and "holding-list" in line and "'bytes': " in line
    assert "SECRET-NAME" not in line and "99 999" not in line
    shape = platform.page_shape(page)
    assert shape["has_container"] is False and shape["title"] == "Holdings" and shape["classes"] == ["holding-list"]
