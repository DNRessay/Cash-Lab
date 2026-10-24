# What the GitHub Actions worker runs:  python -m app.invest.worker deals|calendar
# It reads articles and fills the shared tables; the API only starts it and shows what it found.
import logging
import sys
import time
from datetime import date, timedelta

from sqlalchemy import select

from ..db import SessionLocal, init_db
from ..models import utcnow
from . import blog, deal_facts, deals, jobs, prices
from .models import InvestTxn, WatchItem

log = logging.getLogger("worker")

MAX_READ = 40  # articles read per run
SAVE_EVERY = 10  # rebuild the deals the page shows after this many articles, so it fills in while the job runs
RETRY_SNIPPETS = timedelta(days=3)  # an article that was unreadable gets another try after this


def set_status(db, job, **kw):
    for k, v in kw.items():
        setattr(job, k, v)
    db.commit()


def deals_job(db, job):
    articles = deals.gather(db)
    known = jobs.article_facts(db, [a["link"] for a in articles])
    now = utcnow()
    todo = [a for a in articles if a["link"] not in known or known[a["link"]].status != "done"
            or (known[a["link"]].method == "snippet" and now - known[a["link"]].updated_at > RETRY_SNIPPETS)][:MAX_READ]
    set_status(db, job, total=len(todo), done=0, message=f"Reading {len(todo)} articles…")
    for n, a in enumerate(todo, 1):
        facts, method = deal_facts.read(a)
        row = known.get(a["link"]) or jobs.DealArticle(url_key=jobs.url_key(a["link"]), url=a["link"])
        row.title, row.source, row.method, row.status, row.facts, row.error = \
            a["title"][:300], (a.get("source") or "")[:80], method, "done", facts, facts.get("note", "")
        row.published = date.fromisoformat(a["date"]) if a.get("date") else None
        row.updated_at = now
        db.add(row)
        set_status(db, job, done=n, message=f"Read {n} of {len(todo)} articles")
        if n % SAVE_EVERY == 0 and n < len(todo):
            deals.save(db, deals.build(db, articles))
        time.sleep(0.4)
    deals.save(db, deals.build(db, articles))
    return f"Read {len(todo)} articles, {len(articles)} in the list"


def calendar_job(db, job):
    today = utcnow().date()
    since = date(today.year - 1, 1, 1)
    say = lambda m: set_status(db, job, message=m)
    say("Reading the EasyEquities blog back to " + since.isoformat() + "…")
    got = blog.backfill(db, since, progress=say)
    say("Looking up tickers…")
    try:
        blog.resolve_symbols(db, days=(today - since).days + 30, limit=120)
    except Exception:
        db.rollback()
        log.exception("Ticker lookup failed")
    # Dividend history for everything anyone here holds or watches (Yahoo gives ~5 years of ex-dates).
    symbols = sorted({s for s in db.scalars(select(InvestTxn.symbol).where(InvestTxn.symbol != "").distinct())} |
                     {s for s in db.scalars(select(WatchItem.symbol).distinct())})
    set_status(db, job, total=len(symbols), done=0)
    for n, sym in enumerate(symbols, 1):
        try:
            prices.quote(db, sym, force=True)
        except Exception:
            db.rollback()
        set_status(db, job, done=n, message=f"Dividend history {n} of {len(symbols)}")
    return f"{got['read']} blog posts read ({got['failed']} failed), {len(symbols)} symbols refreshed"


JOBS = {"deals": deals_job, "calendar": calendar_job}


def run(kind):
    db = SessionLocal()
    try:
        job = jobs.latest(db, kind)
        if not job or job.status not in ("queued", "running"):  # started by hand from GitHub: make the row ourselves
            job = jobs.BgJob(kind=kind)
            db.add(job)
        set_status(db, job, status="running", message="Starting…")
        try:
            message = JOBS[kind](db, job)
            set_status(db, job, status="done", message=message[:300], finished_at=utcnow())
        except Exception as e:
            db.rollback()
            log.exception("Worker %s failed", kind)
            set_status(db, job, status="failed", message=f"{type(e).__name__}: {e}"[:300], finished_at=utcnow())
            raise
    finally:
        db.close()


def fail(kind):
    """The workflow died before or outside run() (install failed, runner cancelled): don't leave the page waiting."""
    db = SessionLocal()
    try:
        job = jobs.latest(db, kind)
        if job and job.status in ("queued", "running"):
            set_status(db, job, status="failed", message="The worker run failed: see the Worker run on GitHub Actions.",
                       finished_at=utcnow())
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = sys.argv[1:]
    if args[:1] == ["fail"] and args[1:2] and args[1] in JOBS:
        fail(args[1])
    elif len(args) == 1 and args[0] in JOBS:
        init_db()
        run(args[0])
    else:
        sys.exit(f"usage: python -m app.invest.worker {'|'.join(JOBS)}")
