# Background work that is too heavy for the API (reading articles, back-filling a year of blog posts) runs on GitHub
# Actions. The API only records a job row, starts the workflow and lets the page poll the row; the worker writes
# its results into the shared tables, so every user benefits from one run.
import hashlib
import logging
import os
import threading
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..config import settings
from ..db import Base
from ..models import created, pk, text, utcnow

log = logging.getLogger(__name__)

KINDS = ("deals", "calendar")
COOLDOWN = {"deals": timedelta(minutes=10), "calendar": timedelta(hours=1)}
STALE = timedelta(minutes=25)  # a job that never reported back (runner died) stops blocking new ones


class BgJob(Base):
    __tablename__ = "bg_jobs"
    id: Mapped[int] = pk()
    kind: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    status: Mapped[str] = text(12, "queued")  # queued | running | done | failed
    message: Mapped[str] = text(300)
    done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = created()
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class DealArticle(Base):
    """One news/blog article about a company deal, read once and shared by all users: what the extractor found in it."""

    __tablename__ = "deal_articles"
    id: Mapped[int] = pk()
    url_key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    url: Mapped[str] = text()
    title: Mapped[str] = text(300)
    source: Mapped[str] = text(80)
    published: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = text(10, "pending")  # pending | done | failed
    method: Mapped[str] = text(10)  # article (full text read) | snippet (paywalled or unreachable: headline text only)
    facts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str] = text(300)
    updated_at: Mapped[datetime] = created()


def url_key(url: str) -> str:
    return hashlib.sha1((url or "").strip().encode()).hexdigest()


def article_facts(db: Session, urls) -> dict:
    """{url: DealArticle} for the urls already read."""
    keys = {url_key(u): u for u in urls if u}
    if not keys:
        return {}
    rows = db.scalars(select(DealArticle).where(DealArticle.url_key.in_(list(keys))))
    return {keys[r.url_key]: r for r in rows}


# ── jobs ────────────────────────────────────────────────────────────────────

def latest(db: Session, kind: str) -> Optional[BgJob]:
    job = db.scalar(select(BgJob).where(BgJob.kind == kind).order_by(BgJob.id.desc()))
    if job and job.status in ("queued", "running") and utcnow() - job.created_at > STALE:
        job.status, job.message, job.finished_at = "failed", "The background job never finished.", utcnow()
        db.commit()
    return job


def view(job: Optional[BgJob]) -> dict:
    if not job:
        return {"status": "idle"}
    return {"id": job.id, "status": job.status, "message": job.message, "done": job.done, "total": job.total,
            "active": job.status in ("queued", "running"),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None}


def start(db: Session, kind: str):
    """(job, started): the running job when there is one, else a new one with its workflow dispatched."""
    job = latest(db, kind)
    if job and job.status in ("queued", "running"):
        return job, False
    if job and job.status == "done" and job.finished_at and utcnow() - job.finished_at < COOLDOWN[kind]:
        return job, False  # just updated: another click wouldn't find anything new
    job = BgJob(kind=kind, status="queued", message="Waiting for a worker…")
    db.add(job)
    db.commit()
    try:
        how = dispatch(kind)
    except Exception as e:
        job.status, job.message, job.finished_at = "failed", f"Couldn't start the worker: {e}"[:300], utcnow()
        db.commit()
        return job, False
    if how == "none":
        job.status, job.message, job.finished_at = "failed", "No background worker is set up (GH_DISPATCH_TOKEN and GH_REPO).", utcnow()
        db.commit()
        return job, False
    return job, True


def dispatch(kind: str) -> str:
    """Start the worker: 'github' (a workflow_dispatch run), 'local' (a thread, for running the API on your own
    machine) or 'none' (nothing configured)."""
    if settings.gh_dispatch_token and settings.gh_repo:
        r = requests.post(
            f"https://api.github.com/repos/{settings.gh_repo}/actions/workflows/{settings.gh_workflow}/dispatches",
            headers={"Authorization": f"Bearer {settings.gh_dispatch_token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"ref": settings.gh_ref, "inputs": {"job": kind}}, timeout=15)
        if r.status_code != 204:
            raise RuntimeError(f"GitHub answered {r.status_code}: {r.text[:150]}")
        return "github"
    if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):  # a Lambda is frozen once it has replied: a thread would just die
        from . import worker

        threading.Thread(target=worker.run, args=(kind,), daemon=True).start()
        return "local"
    return "none"
