from datetime import date

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.invest import blog
from app.main import app
from tests.test_invest import market, register  # noqa: F401

api = TestClient(app)

LISTING = """<html><body>
<a href="https://blogs.easyequities.co.za/dividend-20twentysix-september-three">Weekly Dividends Update</a>
<a href="/september-2026-dividends-list">September 2026 Dividends List</a>
<a href="https://blogs.easyequities.co.za/topic/dividends-update">topic</a>
<a href="https://blogs.easyequities.co.za/author/easyresearch">author</a>
</body></html>"""

WEEKLY = """<html><head><title>Weekly Dividends Update</title>
<meta property="article:published_time" content="2026-09-14T08:00:00Z"></head><body><nav>menu</nav>
<div class="post-body">
<p>Here are the dividends this week.</p>
<h3>\U0001F1FF\U0001F1E6 ZAR Account \U0001F1FF\U0001F1E6</h3>
<p>▪️ Prescient Income Provider Feeder AMETF • R0.05 • Last trading date 15 September 2026 • Payment date 21 September 2026</p>
<p>▪️ Sirius Real Estate Ltd • 5.90c • Last day to trade 29 September 2026 • Pay date 20 October 2026</p>
<h3>\U0001F1FA\U0001F1F8 USD Account \U0001F1FA\U0001F1F8</h3>
<p>▪️ Hewlett Packard Enterprise Company • $0.14 • Last trading date 30 September 2026 • Payment date 16 October 2026</p>
</div></body></html>"""

MONTHLY = """<html><head><meta property="og:title" content="September 2026 Dividends List"></head><body>
<div class="post-body"><p>All dividends this month.</p>
<table><tr><th>Company</th><th>Amount</th><th>Last Day to Trade</th><th>Pay Date</th></tr>
<tr><td>Growthpoint Properties Ltd (GRT)</td><td>R0.62</td><td>30/09/2026</td><td>06/10/2026</td></tr>
<tr><td>Vodacom Group</td><td>R3.10</td><td>2026-10-02</td><td>2026-10-06</td></tr></table></div></body></html>"""


def test_parse_weekly_and_monthly():
    title, published, main = blog.content(WEEKLY)
    assert (title, published) == ("Weekly Dividends Update", date(2026, 9, 14))
    rows = blog.parse_dividends(blog.lines_of(main))
    assert [(r["account"], r["instrument"], r["amount"], r["currency"], r["ldt"], r["pay_date"]) for r in rows] == [
        ("ZAR", "Prescient Income Provider Feeder AMETF", 0.05, "ZAR", date(2026, 9, 15), date(2026, 9, 21)),
        ("ZAR", "Sirius Real Estate Ltd", 0.059, "ZAR", date(2026, 9, 29), date(2026, 10, 20)),
        ("USD", "Hewlett Packard Enterprise Company", 0.14, "USD", date(2026, 9, 30), date(2026, 10, 16))]
    _, _, main = blog.content(MONTHLY)
    rows = blog.parse_dividends(blog.lines_of(main))
    assert [(r["instrument"], r["amount"], r["ldt"], r["pay_date"]) for r in rows] == [
        ("Growthpoint Properties Ltd (GRT)", 0.62, date(2026, 9, 30), date(2026, 10, 6)),
        ("Vodacom Group", 3.1, date(2026, 10, 2), date(2026, 10, 6))]
    assert blog.kind_of("September 2026 Dividends List", "") == "monthly_dividends"
    assert blog.kind_of("Weekly Dividends Update", "") == "weekly_dividends"
    assert blog.post_links(LISTING) == ["https://blogs.easyequities.co.za/dividend-20twentysix-september-three",
                                        "https://blogs.easyequities.co.za/september-2026-dividends-list"]


def test_blog_on_overview_and_watchlist(market, monkeypatch):
    pages = {blog.LISTINGS[0]: LISTING, "https://blogs.easyequities.co.za/dividend-20twentysix-september-three": WEEKLY,
             "https://blogs.easyequities.co.za/september-2026-dividends-list": MONTHLY}
    monkeypatch.setattr(blog, "_get", lambda url: pages.get(url, "<html></html>"))
    monkeypatch.setattr(blog, "utcnow", lambda: __import__("datetime").datetime(2026, 9, 25))
    db = SessionLocal()
    assert blog.refresh(db) == {"links": 2, "read": 2}
    assert blog.refresh(db)["read"] == 0  # nothing new the same day
    db.close()

    h = register("blogger")
    monkeypatch.setattr(blog, "search_symbol", lambda name, account="ZAR": "GRT.JO" if "Growthpoint" in name else None)
    r = api.post("/api/invest/blog/watch", headers=h, json={"instrument": "Growthpoint Properties Ltd (GRT)", "account": "ZAR"})
    assert r.status_code == 201 and r.json()["symbol"] == "GRT.JO"
    assert api.post("/api/invest/blog/watch", headers=h, json={"instrument": "Nothing Real"}).status_code == 404

    v = api.get("/api/invest/blog", headers=h).json()
    assert {p["kind"] for p in v["posts"]} == {"weekly_dividends", "monthly_dividends"}
    names = {u["instrument"]: u for u in v["upcoming"]}
    assert names["Prescient Income Provider Feeder AMETF"]["state"] == "paid"  # last day 15 Sept, paid 21 Sept
    assert names["Sirius Real Estate Ltd"]["state"] == "open"
    grt = names["Growthpoint Properties Ltd (GRT)"]
    assert (grt["mine"], grt["symbol"], grt["can_buy"]) == ("watch", "GRT.JO", True)
    assert grt["price"] == 16.0 and grt["this_pct"] == round(0.62 / 16, 5)  # one unit costs R16; this pays 3.9% of it
    assert (grt["season"], grt["pays_in"]) == ("Spring", ["Oct"])
    assert grt["amount_zar"] is None
    hpe = names["Hewlett Packard Enterprise Company"]
    assert hpe["amount_zar"] == round(0.14 * 18.0, 4)  # $0.14 at R18 to the dollar
    s = api.get("/api/invest/summary", headers=h).json()
    assert s["blog"]["upcoming"] and s["blog"]["posts"]


REAL_WEEKLY = """<html><head><title>Weekly Dividends Update</title></head><body><div class="post-body">
<p>Published on: Apr 13, 2026, 8:00:00 AM</p>
<p>Dividend Aristocrats have historically stood out for their consistency and long-term dividend growth.</p>
<p>South Africa</p>
<p>The JSE Group Limited will be paying R10.61 (R9.61 Ordinary and R1.00 Special) per share.</p>
<p>Last trading date - 14 April 2026</p><p>Payment date - 20 April 2026</p>
<p>Standard Bank Group Limited will be paying R3.93 per second preference share (SBPP).</p>
<p>Last trading date - 14 April 2026</p><p>Payment date - 20 April 2026</p>
<p>Choppies Enterprises Limited will be paying 1.0 thebe per share.</p>
<p>Exchange rate date - 13 April 2026</p><p>Last trading date - 14 April 2026</p><p>Payment date - 29 April 2026</p>
<p>TBI Global Targeted Yield ETF will be paying R0.33 per share.</p>
<p>Last payment date -10 March 2026</p><p>Payment date - 16 March 2026</p>
<p>United States</p>
<p>Freeport-McMoRan Inc will be paying $0.15 ( $0.075 Base and $0.075 Variable) per share.</p>
<p>Last trading date - 14 April 2026</p><p>Payment date - 01 May 2026</p>
</div></body></html>"""


def test_parse_real_layout():
    _, _, main = blog.content(REAL_WEEKLY)
    lines = blog.lines_of(main)
    assert blog.published_from(lines) == date(2026, 4, 13)
    assert blog.highlights(lines) == ["Dividend Aristocrats have historically stood out for their consistency and long-term dividend growth."]
    rows = blog.parse_dividends(lines)
    assert [(r["account"], r["instrument"], r["amount"], r["currency"], r["ldt"], r["pay_date"]) for r in rows] == [
        ("ZAR", "The JSE Group Limited", 10.61, "ZAR", date(2026, 4, 14), date(2026, 4, 20)),
        ("ZAR", "Standard Bank Group Limited (second preference share (SBPP))", 3.93, "ZAR", date(2026, 4, 14), date(2026, 4, 20)),
        ("ZAR", "Choppies Enterprises Limited", 0.01, "BWP", date(2026, 4, 14), date(2026, 4, 29)),
        ("ZAR", "TBI Global Targeted Yield ETF", 0.33, "ZAR", date(2026, 3, 10), date(2026, 3, 16)),
        ("USD", "Freeport-McMoRan Inc", 0.15, "USD", date(2026, 4, 14), date(2026, 5, 1))]
    assert blog.kind_of("September 2026 Dividends Update: JSE-Listed Companies", "") == "monthly_dividends"
    assert blog.kind_of("March 2026 ETF Dividends List: Satrix", "") == "monthly_dividends"
    assert blog.kind_of("Sanlam's latest results are in", "") == "news"
    m = blog.matcher([("hold", "SBK.JO", "Standard Bank Group Ltd"), ("watch", "GRT.JO", "Growthpoint Properties Ltd")])
    assert m("Standard Bank Group Limited") == ("hold", "SBK.JO")
    assert m("Growthpoint Properties Limited") == ("watch", "GRT.JO")
    assert m("Grindrod Limited") is None
    assert m("Standard Bank Group Limited (second preference share (SBPP))") is None


def test_name_variants_and_search(monkeypatch):
    assert blog.name_variants("Northam Holdings Limited") == ["Northam Holdings Limited", "Northam Holdings", "Northam"]
    assert blog.name_variants("Gold Field Limited")[1] == "Gold Fields"
    seen = []
    results = {"Spur": [{"symbol": "SUR.JO"}], "Spur Corporation Limited": [{"symbol": "SPUR"}]}
    monkeypatch.setattr(blog, "_yahoo_search", lambda q: seen.append(q) or results.get(q, []))
    assert blog.search_symbol("Spur Corporation Limited") == "SUR.JO"  # a US hit first, the JSE one on the shorter name
    assert blog.search_symbol("Nothing Here Limited") is None
