# Market pulse for South Africa: real news (SerpAPI) with sentiment, the calendar that moves the rand and the JSE,
# technicals from cached daily prices, and a short outlook. Shared by all users; refreshed nightly (or on demand).
import json
import logging
import math
import re
from datetime import date, datetime, timedelta

import requests
from sqlalchemy import JSON, DateTime, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..config import settings
from ..db import Base
from ..models import pk, text, utcnow
from . import prices

log = logging.getLogger(__name__)

SERP_URL = "https://serpapi.com/search.json"
TOPICS = [("jse", "JSE shares"), ("rand", "rand dollar exchange rate"), ("sarb", "SARB interest rate repo"),
          ("economy", "South Africa economy inflation")]
MARKETS = [("ZAR=X", "USD/ZAR"), ("STX40.JO", "JSE Top 40 (Satrix 40)"), ("STXPRO.JO", "SA property (Satrix Property)"),
           ("GC=F", "Gold")]
MAX_AGE = timedelta(hours=3)
POS = re.compile(r"\b(gain|gains|rise|rises|rally|surge|jump|strong|firm|record high|beat|upgrade|growth|cut rates?|"
                 r"rate cut|eases?|recover|boost|optimis|higher)\w*", re.I)
NEG = re.compile(r"\b(fall|falls|drop|slump|plunge|weak|slide|loss|losses|miss|downgrade|recession|hike|rate hike|"
                 r"load.?shedding|strike|fears?|concern|pressure|sell.?off|lower|decline)\w*", re.I)


class MarketPulse(Base):
    __tablename__ = "market_pulse"
    id: Mapped[int] = pk()
    key: Mapped[str] = text(20)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# ── technicals (pure Python over daily closes) ──────────────────────────────

def sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema(xs, n):
    k, out, prev = 2 / (n + 1), [], None
    for x in xs:
        prev = x if prev is None else x * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(xs, n=14):
    out, gain, loss = [None] * len(xs), 0.0, 0.0
    for i in range(1, len(xs)):
        ch = xs[i] - xs[i - 1]
        g, l = max(ch, 0), max(-ch, 0)
        if i <= n:
            gain += g / n
            loss += l / n
            if i < n:
                continue
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + l) / n
        out[i] = 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def macd(xs, fast=12, slow=26, signal=9):
    line = [a - b for a, b in zip(ema(xs, fast), ema(xs, slow))]
    sig = ema(line, signal)
    return line, sig, [a - b for a, b in zip(line, sig)]


def bollinger(xs, n=20, k=2):
    mid, up, lo = sma(xs, n), [], []
    for i, m in enumerate(mid):
        if m is None:
            up.append(None)
            lo.append(None)
            continue
        window = xs[i - n + 1:i + 1]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / n)
        up.append(m + k * sd)
        lo.append(m - k * sd)
    return up, mid, lo


def technical(history, days=180):
    """Indicators on the last `days` of a [[date, close], ...] history, plus a plain-words read."""
    if len(history) < 60:
        return None
    dates, closes = [d for d, _ in history], [c for _, c in history]
    r, (ml, ms, mh), (bu, bm, bl) = rsi(closes), macd(closes), bollinger(closes)
    s50, s200 = sma(closes, 50), sma(closes, 200)
    last = closes[-1]
    reads = []
    if r[-1] is not None:
        reads.append("overbought (RSI above 70)" if r[-1] > 70 else "oversold (RSI below 30)" if r[-1] < 30 else
                     f"RSI {r[-1]:.0f}, neutral")
    flat = abs(ml[-1] - ms[-1]) < 1e-9
    reads.append("MACD flat" if flat else "MACD above its signal (momentum up)" if ml[-1] > ms[-1] else
                 "MACD below its signal (momentum down)")
    if s200[-1]:
        reads.append("above its 200-day average (long trend up)" if last > s200[-1] else "below its 200-day average (long trend down)")
    if bu[-1] and last > bu[-1]:
        reads.append("above the upper Bollinger band (stretched)")
    elif bl[-1] and last < bl[-1]:
        reads.append("below the lower Bollinger band (stretched down)")
    score = (0 if flat else 1 if ml[-1] > ms[-1] else -1) + \
            (1 if s200[-1] and last > s200[-1] else -1 if s200[-1] and last < s200[-1] else 0) + \
            (-1 if r[-1] and r[-1] > 70 else 1 if r[-1] and r[-1] < 30 else 0)
    cut = slice(-days, None)
    rnd = lambda xs: [round(x, 4) if x is not None else None for x in xs[cut]]  # noqa: E731
    return {"last": round(last, 4), "rsi": round(r[-1], 1) if r[-1] is not None else None,
            "macd": round(ml[-1], 4), "signal": round(ms[-1], 4), "sma50": round(s50[-1], 4) if s50[-1] else None,
            "sma200": round(s200[-1], 4) if s200[-1] else None,
            "change_1m": round(last / closes[-22] - 1, 4) if len(closes) > 22 else None,
            "bias": "up" if score > 0 else "down" if score < 0 else "mixed", "reads": reads,
            "series": {"dates": dates[cut], "close": rnd(closes), "bb_upper": rnd(bu), "bb_lower": rnd(bl),
                       "sma50": rnd(s50), "rsi": rnd(r), "macd_hist": rnd(mh)}}


# ── calendar ────────────────────────────────────────────────────────────────

def _first_weekday(y, m, weekday):
    d = date(y, m, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def rule_calendar(today: date, days=45):
    """Events with a fixed rule: US jobs report (first Friday), Absa PMI (first business day)."""
    out = []
    for k in range(3):
        y, m = (today.year + (today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1)
        nfp = _first_weekday(y, m, 4)
        pmi = date(y, m, 1)
        while pmi.weekday() > 4:
            pmi += timedelta(days=1)
        out += [{"date": nfp.isoformat(), "event": "US jobs report (NFP)", "country": "US", "rule": True,
                 "why": "Strong US jobs lift the dollar and usually weaken the rand; weak jobs do the opposite."},
                {"date": pmi.isoformat(), "event": "Absa manufacturing PMI", "country": "ZA", "rule": True,
                 "why": "Above 50 means factories are growing: good for SA growth shares."}]
    end = today + timedelta(days=days)
    return sorted([e for e in out if today <= date.fromisoformat(e["date"]) <= end], key=lambda e: e["date"])


# ── news ────────────────────────────────────────────────────────────────────

_serp_next = 0


def serp_news(query, num=8):
    """Google News results via SerpAPI (keys rotate). [{title, source, date, link, snippet}]"""
    global _serp_next
    keys = settings.serpapi_keys
    if not keys:
        return []
    for i in range(len(keys)):
        key = keys[(_serp_next + i) % len(keys)]
        try:
            r = requests.get(SERP_URL, timeout=25, params={"engine": "google_news", "q": query, "gl": "za", "hl": "en",
                                                           "api_key": key})
        except requests.RequestException:
            continue
        if r.status_code in (401, 403, 429):
            continue
        if not r.ok:
            return []
        _serp_next = (_serp_next + i + 1) % len(keys)
        items = []
        for it in r.json().get("news_results", []):
            for x in [it, *(it.get("stories") or [])]:
                if x.get("title") and x.get("link"):
                    items.append({"title": x["title"][:220], "source": ((x.get("source") or {}).get("name") or "")[:60],
                                  "date": _news_date(x.get("date", "")), "link": x["link"], "snippet": (x.get("snippet") or "")[:300]})
        seen, out = set(), []
        for x in sorted(items, key=lambda x: x["date"] or "", reverse=True):
            if x["title"] not in seen:
                seen.add(x["title"])
                out.append(x)
        return out[:num]
    return []


def _news_date(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None


def lexicon_sentiment(text):
    p, n = len(POS.findall(text)), len(NEG.findall(text))
    return "positive" if p > n else "negative" if n > p else "neutral"


# ── build ───────────────────────────────────────────────────────────────────

def build(db: Session, holdings=()):
    from ..ai import llm

    today = utcnow().date()
    news = {}
    for key, q in TOPICS:
        news[key] = serp_news(q)
    for h in list(holdings)[:4]:
        name = re.sub(r"\b(limited|ltd|plc|holdings)\b", "", h, flags=re.I).strip()
        if name:
            news[f"h:{name}"] = serp_news(f"{name} shares", 4)
    flat = [a for items in news.values() for a in items]
    for a in flat:
        a["sentiment"] = lexicon_sentiment(f"{a['title']} {a['snippet']}")

    tech = {}
    for sym, label in MARKETS:
        row = prices.quote(db, sym)
        t = technical(row.history) if row and row.history else None
        if t:
            tech[sym] = {"label": label, **t}

    outlook, events = None, []
    if settings.groq_api_keys and flat:
        heads = "\n".join(f"{i}. [{a['date'] or '?'}] {a['title']} — {a['snippet'][:160]}" for i, a in enumerate(flat[:40]))
        techs = {v["label"]: {k: v[k] for k in ("last", "rsi", "bias", "change_1m", "reads")} for v in tech.values()}
        system = ("You are a South African markets analyst writing for a private investor. Using ONLY the headlines and "
                  "technicals given, reply as JSON: {\"sentiment\": [{\"i\": index, \"s\": \"positive|negative|neutral\"}], "
                  "\"events\": [{\"date\": \"YYYY-MM-DD\", \"event\": short, \"country\": \"ZA|US|...\", \"expect\": "
                  "\"what is expected, with the number if a headline gives it\", \"from\": index}] (only events after "
                  f"{today.isoformat()} whose date a headline states: SARB rate decision, CPI, GDP, unemployment, "
                  "retail sales, budget...), \"outlook\": {\"rand\": 1-2 sentences, \"jse\": 1-2 sentences, "
                  "\"mood\": \"positive|negative|mixed\", \"watch\": [up to 3 short things to watch]}}. "
                  "An outlook is an expectation, not advice; say what could change it.")
        try:
            data = llm.parse_json(llm.groq([{"role": "system", "content": system},
                                            {"role": "user", "content": f"Headlines:\n{heads}\n\nTechnicals:\n{json.dumps(techs)}"}],
                                           max_tokens=1800, json_mode=True)) or {}
            for sres in data.get("sentiment", []) if isinstance(data, dict) else []:
                try:
                    i = int(sres.get("i"))
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(flat) and sres.get("s") in ("positive", "negative", "neutral"):
                    flat[i]["sentiment"] = sres["s"]
            for e in data.get("events", []) if isinstance(data, dict) else []:
                try:
                    d = date.fromisoformat(str(e.get("date"))[:10])
                    src = flat[int(e.get("from"))] if e.get("from") is not None and 0 <= int(e.get("from")) < len(flat) else None
                except (TypeError, ValueError):
                    continue
                if today <= d <= today + timedelta(days=60) and src:
                    events.append({"date": d.isoformat(), "event": str(e.get("event", ""))[:80], "country": str(e.get("country", ""))[:3],
                                   "expect": str(e.get("expect", ""))[:240], "source": src["source"], "link": src["link"], "rule": False})
            outlook = data.get("outlook") if isinstance(data, dict) and isinstance(data.get("outlook"), dict) else None
        except llm.AIError as e:
            log.warning("Pulse outlook skipped: %s", e)
    cal = sorted(rule_calendar(today) + events, key=lambda e: e["date"])
    counts = {s: sum(1 for a in flat if a["sentiment"] == s) for s in ("positive", "negative", "neutral")}
    return {"built_at": utcnow().isoformat(), "news": news, "sentiment": counts, "calendar": cal, "outlook": outlook,
            "technical": {k: {kk: vv for kk, vv in v.items() if kk != "series"} for k, v in tech.items()},
            "sources": {"news": bool(settings.serpapi_keys), "outlook": bool(settings.groq_api_keys)}}


def get(db: Session, holdings=(), refresh=False):
    row = db.scalar(select(MarketPulse).where(MarketPulse.key == "za").order_by(MarketPulse.id.desc()))
    if row and not refresh and utcnow() - row.created_at < timedelta(hours=26):
        return row.data
    if row and refresh and utcnow() - row.created_at < MAX_AGE and row.data.get("sources", {}).get("news") == bool(settings.serpapi_keys):
        return row.data  # don't burn API calls: at most one rebuild every few hours
    data = build(db, holdings)
    row = row or MarketPulse(key="za")
    row.data, row.created_at = data, utcnow()
    db.add(row)
    db.commit()
    return data


def chart(db: Session, symbol: str, days=180):
    row = prices.quote(db, symbol)
    t = technical(row.history, days) if row and row.history else None
    return {"symbol": symbol, "name": row.name if row else symbol, **(t or {})}
