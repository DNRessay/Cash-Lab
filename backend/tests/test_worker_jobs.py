from datetime import date, datetime, timedelta

import pytest

from app.db import SessionLocal
from app.invest import blog, deal_facts, deals, div_calendar as cal, jobs, worker
from app.invest.models import DividendHistory
from tests.test_invest import api, register

# An article laid out like a delisting story: the offer and the quoted price share one sentence.
BUYOUT = ("Acme has announced plans to buy out its shareholders and delist from the Johannesburg Stock Exchange. "
          "The company is offering shareholders 17 cents per Acme share, with the company currently trading at around "
          "15 cents per share at the time of writing. The share price is down 70% over five years, and down more than "
          "90% from its 2021 peak of R1.54 per share. Acme will make an offer to repurchase its remaining shares for a "
          "cash offer consideration of 17 cents. The offer is for a total maximum consideration of R19.75 million. "
          "Acme will attempt to conclude this transaction no later than Monday, 30 November 2026.")
PAYOUT = ("Zed declared a special dividend of 120 cents per share from the sale. The last day to trade is 11 November 2026 "
          "and the payment date is 19 November 2026.")


@pytest.fixture(params=["regex", "spacy"])
def engine(request, monkeypatch):
    if request.param == "regex":
        monkeypatch.setattr(deal_facts, "_NLP", None)
    elif deal_facts.nlp() is None:
        pytest.skip("spaCy's small model isn't installed here")
    return request.param


def test_offer_and_quoted_price_are_told_apart(engine):
    f = deal_facts.extract("Company set to delist from the JSE", BUYOUT)
    assert (f["company"], f["kind"], f["amount"], f["price"]) == ("Acme", "delisting", 0.17, 0.15)
    assert f["deadline"] == "2026-11-30" and "17 cents per Acme share" in f["evidence"]
    # the old headline reader took the trading price for the offer
    assert deals.parse_amount("offering shareholders 17 cents per Acme share, with the company trading at around 15 cents per share") == 0.15


def test_special_payout_dates_and_totals_are_not_amounts(engine):
    f = deal_facts.extract("Zed declares special dividend", PAYOUT)
    assert (f["kind"], f["amount"], f["ldt"], f["pay_date"]) == ("special_distribution", 1.2, "2026-11-11", "2026-11-19")
    f = deal_facts.extract("Quu to return capital", "Quu will return R7bn to shareholders, a total of R7.5 billion.")
    assert f["amount"] is None  # totals, not per share


def test_unreadable_page_falls_back_to_the_snippet(monkeypatch):
    def boom(url):
        raise deal_facts.Unreadable("paywall")
    monkeypatch.setattr(deal_facts, "read_url", boom)
    f, method = deal_facts.read({"title": "Foo buyout", "snippet": "Foo offers 250 cents per share in cash", "link": "https://x/y"})
    assert method == "snippet" and f["amount"] == 2.5 and "paywall" in f["note"]


def test_a_ticker_must_carry_the_company_name():
    assert deals.same_company("Zeder Investments", "ZEDER INVESTMENTS LTD")
    assert not deals.same_company("South African company run by the same family", "COLLINS PROPERTY GROUP")


def _db_with_article(monkeypatch, link="https://news.example/acme", read=True):
    db = SessionLocal()
    art = {"title": "South African company run by the same family set to delist", "snippet": "A family firm will delist.",
           "link": link, "source": "Daily Investor", "date": "2026-06-26"}
    monkeypatch.setattr(deals, "serp_news", lambda q, n=8: [])
    monkeypatch.setattr(deals.settings, "groq_api_keys", [])
    monkeypatch.setattr("app.invest.blog.search_symbol", lambda name, account="ZAR": None)
    if read:
        facts = deal_facts.extract(art["title"], BUYOUT)
        db.merge(jobs.DealArticle(url_key=jobs.url_key(link), url=link, title=art["title"], status="done", method="article",
                                  facts=facts))
        db.commit()
    return db, art


def test_read_articles_fix_the_company_amount_and_price(monkeypatch):
    db, art = _db_with_article(monkeypatch)
    out = deals.build(db, [art])
    d = out["deals"][0]
    assert (d["company"], d["kind"], d["amount"], d["price"], d["price_source"]) == ("Acme", "delisting", 0.17, 0.15, "article")
    assert d["tone"] == "info" and "No live price" in d["verdict"] and d["symbol"] is None  # an old quote isn't a buy signal
    assert d["deadline"] == "2026-11-30" and d["read"] == "article"
    db.close()


def test_refresh_answers_at_once_then_the_worker_fills_it_in(monkeypatch):
    h = register("jobs")
    db, art = _db_with_article(monkeypatch, "https://news.example/job-acme", read=False)
    deals.save(db, {"built_at": "2026-10-24T06:00:00", "deals": [
        {"company": "Fam Co", "kind": "delisting", "kind_label": "Delisting offer", "tone": "info", "verdict": "x", "amount": None,
         "price": None, "link": art["link"], "title": art["title"], "source": "Daily Investor", "date": "2026-06-26", "detail": []}],
        "sources": {"news": False, "ai": False}})
    calls = []
    monkeypatch.setattr(jobs, "dispatch", lambda kind: calls.append(kind) or "github")
    monkeypatch.setitem(jobs.COOLDOWN, "deals", timedelta(0))

    r = api.post("/api/invest/deals/refresh", headers=h).json()
    assert r["started"] and calls == ["deals"] and r["job"]["active"]
    assert r["deals"][0]["company"] == "Fam Co" and r["deals"][0]["reading"]  # the old figures show straight away
    again = api.post("/api/invest/deals/refresh", headers=h).json()
    assert not again["started"] and calls == ["deals"]  # one run at a time

    monkeypatch.setattr(deals, "gather", lambda db: [art])
    monkeypatch.setattr(deal_facts, "read", lambda a: (deal_facts.extract(a["title"], BUYOUT), "article"))
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    worker.run("deals")

    r = api.get("/api/invest/deals", headers=h).json()
    assert r["job"]["status"] == "done" and not r["reading"]
    assert r["deals"][0]["company"] == "Acme" and r["deals"][0]["amount"] == 0.17 and not r["deals"][0]["reading"]
    db.close()


def test_the_worker_reads_each_article_once(monkeypatch):
    db, art = _db_with_article(monkeypatch, "https://news.example/once", read=False)
    monkeypatch.setattr(deals, "gather", lambda db: [art])
    seen = []
    monkeypatch.setattr(deal_facts, "read", lambda a: seen.append(a["link"]) or (deal_facts.extract(a["title"], BUYOUT), "article"))
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    for _ in range(2):
        job = jobs.BgJob(kind="deals")
        db.add(job)
        db.commit()
        worker.deals_job(db, job)
    assert seen == [art["link"]]  # the second run found it in the shared table
    db.close()


def test_a_worker_that_cannot_start_says_so(monkeypatch):
    db = SessionLocal()
    monkeypatch.setitem(jobs.COOLDOWN, "calendar", timedelta(0))
    monkeypatch.setattr(jobs, "dispatch", lambda kind: (_ for _ in ()).throw(RuntimeError("GitHub answered 401")))
    job, started = jobs.start(db, "calendar")
    assert not started and job.status == "failed" and "401" in job.message
    monkeypatch.setattr(jobs, "dispatch", lambda kind: "none")
    job, started = jobs.start(db, "calendar")
    assert not started and "GH_DISPATCH_TOKEN" in job.message
    stuck = jobs.BgJob(kind="deals", status="running", created_at=datetime(2020, 1, 1))
    db.add(stuck)
    db.commit()
    assert jobs.latest(db, "deals").status == "failed"  # a runner that died doesn't block the next run
    db.close()


def post_html(title, day):
    return (f'<html><head><meta property="og:title" content="{title}"><meta property="article:published_time" '
            f'content="{day}T08:00:00"></head><body><article><p>{title}</p></article></body></html>')


def test_blog_backfill_reads_back_to_the_start_date_and_caches(monkeypatch):
    base = blog.BLOG
    pages = {f"{base}/topic/dividends-update": f'<a href="{base}/new-post">x</a><a href="{base}/old-post">y</a>',
             f"{base}/topic/dividends-update/page/2": f'<a href="{base}/older-post">z</a>',
             f"{base}/new-post": post_html("Weekly dividends update", "2026-03-02"),
             f"{base}/old-post": post_html("Weekly dividends update early 2025", "2025-02-03"),
             f"{base}/older-post": post_html("Weekly dividends update 2024", "2024-05-03"), base: ""}
    fetched = []

    def fake(url):
        fetched.append(url)
        if url not in pages:
            raise RuntimeError("404")
        return pages[url]
    monkeypatch.setattr(blog, "_get", fake)
    monkeypatch.setattr("time.sleep", lambda s: None)
    db = SessionLocal()
    got = blog.backfill(db, date(2025, 1, 1))
    assert got["read"] >= 2 and f"{base}/new-post" in fetched
    n = len(fetched)
    again = blog.backfill(db, date(2025, 1, 1))
    assert again["read"] == 0 and not [u for u in fetched[n:] if u.endswith("-post")]  # known posts aren't fetched again
    db.close()


def test_calendar_shows_last_year_this_year_and_what_was_received(monkeypatch):
    monkeypatch.setattr(cal, "utcnow", lambda: datetime(2026, 10, 5))
    h = register("history")
    api.post("/api/invest/transactions", headers=h, json={"date": "2024-01-02", "kind": "buy", "symbol": "STX40.JO",
                                                         "asset_class": "etf", "quantity": 100, "price": 10, "amount": 1000})
    api.post("/api/invest/transactions", headers=h, json={"date": "2026-03-20", "kind": "dividend", "symbol": "STX40.JO",
                                                         "name": "Satrix 40", "amount": 65})
    db = SessionLocal()
    db.merge(DividendHistory(symbol="STX40.JO", events=[["2025-03-12", 0.6], ["2025-10-08", 0.62], ["2026-03-11", 0.65]]))
    db.commit()
    db.close()
    r = api.get("/api/invest/calendar", headers=h).json()
    assert len(r["months"]) == 12 and r["months"][0]["label"] == "Oct 2026"  # the upcoming part is unchanged
    hist = {m["month"]: m for m in r["history"]}
    assert r["history"][0]["month"] == "2025-01" and r["history"][-1]["month"] == "2026-09"
    assert hist["2025-03"]["period"] == "last_year" and hist["2026-03"]["period"] == "this_year"
    paid = hist["2026-03"]["items"][0]
    assert (paid["status"], paid["amount"], paid["ldt"]) == ("paid", 0.65, "2026-03-10") and paid["estimate"] is None
    assert abs(paid["change"] - (0.65 / 0.6 - 1)) < 1e-3  # against March last year
    assert hist["2026-03"]["received"] == 65.0 and hist["2026-03"]["received_items"][0]["instrument"] == "Satrix 40"
    assert r["received_this_year"] == 65.0 and "status" in r["job"]
