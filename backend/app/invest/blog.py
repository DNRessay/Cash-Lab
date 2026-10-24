# EasyEquities' public blog: the Weekly Dividends Update and the monthly Dividends List. Read nightly into
# shared tables (the same for every user); each user's view marks what they hold or watch.
import logging
import re
from datetime import date, datetime, timedelta
from collections import defaultdict
from typing import Optional

from bs4 import BeautifulSoup
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..db import Base
from ..models import BigId, created, pk, text, utcnow
from . import prices
from .prices import HEADERS, HTTP_KW, http

log = logging.getLogger(__name__)

BLOG = "https://blogs.easyequities.co.za"
LISTINGS = [f"{BLOG}/topic/dividends-update", f"{BLOG}/topic/dividends-update/page/2", f"{BLOG}/", f"{BLOG}/page/2"]
SKIP_PATHS = ("/topic/", "/author/", "/page/", "/tag/", "/rss", "/all")

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE = r"(\d{1,2})(?:st|nd|rd|th)?[\s\-/]+([A-Za-z]{3,9})[\s\-/,]+(\d{4})|(\d{4})[-/](\d{2})[-/](\d{2})|(\d{1,2})/(\d{1,2})/(\d{4})"
LDT_RE = re.compile(r"(?:last\s+(?:day|date)\s+(?:to|of)\s+trad(?:e|ing)|last\s+trading\s+(?:day|date)|\bLDT\b)\s*[:\-•]?\s*(" + DATE + ")", re.I)
PAY_RE = re.compile(r"(?:pay(?:ment)?\s+date|paid\s+on|\bpay\b)\s*[:\-•]?\s*(" + DATE + ")", re.I)
AMOUNT_RE = re.compile(r"(?<![\w.])(R|ZAR|US\$|\$|USD|A\$|AUD|£|GBP|€|EUR)\s?(\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?|\d*\.\d+)"
                       r"|(\d+(?:\.\d+)?)\s?(c|cents|ZAc|cps)\b", re.I)
CURRENCY = {"r": "ZAR", "zar": "ZAR", "$": "USD", "us$": "USD", "usd": "USD", "a$": "AUD", "aud": "AUD", "£": "GBP",
            "gbp": "GBP", "€": "EUR", "eur": "EUR"}
ACCOUNT_RE = re.compile(r"\b(ZAR|USD|AUD|GBP|EUR|TFSA)\b[^\n]{0,20}\baccount", re.I)
NOISE = re.compile(r"[☀-➿\U0001F000-\U0001FAFF️▪•·|‍]+")
SUFFIXES = re.compile(r"\b(ltd|limited|inc|incorporated|plc|corp|corporation|co|company|holdings?|group|n\.?v|s\.?a|ag|se|"
                      r"the|class [a-z]|ordinary shares?|ord|reit|etf|ametf|feeder)\b\.?", re.I)


class BlogPost(Base):
    __tablename__ = "ee_blog_posts"
    id: Mapped[int] = pk()
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = text(300)
    kind: Mapped[str] = text(20)  # weekly_dividends | monthly_dividends | news
    published: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    summary: Mapped[str] = text()
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = text(20)
    error: Mapped[str] = text()
    fetched_at: Mapped[datetime] = created()


class BlogDividend(Base):
    __tablename__ = "ee_blog_dividends"
    __table_args__ = (UniqueConstraint("post_id", "account", "instrument"),)
    id: Mapped[int] = pk()
    post_id: Mapped[int] = mapped_column(BigId, ForeignKey("ee_blog_posts.id", ondelete="CASCADE"), nullable=False, index=True)
    account: Mapped[str] = text(10)
    instrument: Mapped[str] = text(200)
    amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = text(3)
    ldt: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    pay_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    line: Mapped[str] = text()


class BlogSymbol(Base):
    """Ticker for a company named in the blog (looked up once), so its price and yield can be shown."""

    __tablename__ = "ee_blog_symbols"
    __table_args__ = (UniqueConstraint("key"),)
    id: Mapped[int] = pk()
    key: Mapped[str] = text(220)  # account|normalised name
    instrument: Mapped[str] = text(200)
    symbol: Mapped[str] = text(30)  # "" when nothing was found
    checked_at: Mapped[datetime] = created()


# South African seasons, by the month the dividend is paid.
SEASONS = {12: "Summer", 1: "Summer", 2: "Summer", 3: "Autumn", 4: "Autumn", 5: "Autumn",
           6: "Winter", 7: "Winter", 8: "Winter", 9: "Spring", 10: "Spring", 11: "Spring"}
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def symbol_key(account, instrument):
    return (account + "|" + norm(re.sub(r"\(.*?\)", " ", instrument)))[:220]


def resolve_symbols(db: Session, days=150, limit=80):
    """Look up tickers for recent blog companies (not preference shares), then refresh their prices."""
    from . import prices

    since = utcnow().date() - timedelta(days=days)
    week_ago = utcnow() - timedelta(days=7)
    stale = db.scalars(select(BlogSymbol).where(BlogSymbol.symbol == "", BlogSymbol.checked_at < week_ago)).all()
    for row in stale:  # nothing found last time: try again (names and Yahoo's index change)
        db.delete(row)
    db.commit()
    known = {k: sym for k, sym in db.execute(select(BlogSymbol.key, BlogSymbol.symbol))}
    looked = 0
    for account, instrument in db.execute(select(BlogDividend.account, BlogDividend.instrument).distinct()
                                          .where(BlogDividend.ldt >= since)):
        key = symbol_key(account, instrument)
        if key in known or re.search(r"preference|debenture|note\b", instrument, re.I):
            continue
        if looked >= limit:
            break
        name = re.sub(r"\(.*?\)", " ", instrument)
        known[key] = search_symbol(name, account) or ""
        db.add(BlogSymbol(key=key, instrument=instrument[:200], symbol=known[key][:30]))
        db.commit()
        looked += 1
    priced = 0
    for sym in sorted({v for v in known.values() if v}):
        try:
            prices.quote(db, sym)
            priced += 1
        except Exception:
            db.rollback()
    return {"looked_up": looked, "priced": priced}


def _get(url):
    r = http.get(url, headers=HEADERS, timeout=25, **HTTP_KW)
    r.raise_for_status()
    return r.text


def _date(groups) -> Optional[date]:
    g = [x for x in groups]
    try:
        if g[0]:
            month = MONTHS.get(g[1][:3].lower())
            return date(int(g[2]), month, int(g[0])) if month else None
        if g[3]:
            return date(int(g[3]), int(g[4]), int(g[5]))
        if g[6]:
            return date(int(g[8]), int(g[7]), int(g[6]))
    except ValueError:
        return None
    return None


def kind_of(title: str, url: str) -> str:
    t = f"{title} {url}".lower()
    if "dividend" in t and ("list" in t or re.search(r"(january|february|march|april|may|june|july|august|september|"
                                                     r"october|november|december) (\d{4} )?(etf |etn |etn-etf )?dividends", t)):
        return "monthly_dividends"
    if "dividend" in t:
        return "weekly_dividends"
    return "news"


def post_links(html: str):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].split("#")[0].split("?")[0].rstrip("/")
        if href.startswith("/"):
            href = BLOG + href
        if not href.startswith(BLOG + "/") or any(s in href for s in SKIP_PATHS) or href.count("/") != 3:
            continue
        if href not in out:
            out.append(href)
    return out


def content(html: str):
    """(title, published date, main-content soup)."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.find("meta", property="og:title") or {}).get("content") or (soup.title.string if soup.title else "")
    published = None
    for key in ("article:published_time", "og:published_time"):
        m = soup.find("meta", property=key)
        if m and m.get("content"):
            try:
                published = datetime.fromisoformat(m["content"][:10]).date()
            except ValueError:
                pass
    main = (soup.select_one("[class*=post-body]") or soup.select_one("article") or soup.select_one("main") or soup.body or soup)
    for tag in main.find_all(["script", "style", "nav", "footer", "form"]):
        tag.decompose()
    return (title or "").strip(), published, main


def lines_of(main) -> list:
    """Table rows as ' | '-joined cells, everything else as text lines (block elements split lines)."""
    out = []
    for table in main.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if any(cells):
                out.append(" | ".join(cells))
        table.replace_with(BeautifulSoup("<p></p>", "html.parser"))
    for br in main.find_all("br"):
        br.replace_with("\n")
    for block in main.find_all(["p", "li", "h1", "h2", "h3", "h4", "h5", "div"]):
        block.insert_after("\n")
    return out + [ln.strip() for ln in main.get_text("").splitlines() if ln.strip()]


def clean_name(s: str) -> str:
    s = NOISE.sub(" ", s)
    s = re.sub(r"^\s*[-–—*\d.)]+\s+", "", s)
    return re.sub(r"\s{2,}", " ", s).strip(" :-–—|,")


REGIONS = {"south africa": "ZAR", "united states": "USD", "usa": "USD", "australia": "AUD", "united kingdom": "GBP",
           "uk": "GBP", "europe": "EUR", "germany": "EUR", "netherlands": "EUR", "france": "EUR", "botswana": "ZAR"}
PAYING_RE = re.compile(r"^(?P<name>.+?)\s+will\s+be\s+paying\s+(?P<amt>(?:R|US\$|\$|A\$|£|€)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*"
                       r"(?:thebe|cents?|c|pula|usd|zar))\s*(?:\([^)]*\))?\s*per\s+(?P<unit>.+?)\.?$", re.I)
DATED_RE = re.compile(r"^(last\s+(?:trading|payment|day\s+to\s+trade)\s*(?:date|day)?|ldt|payment\s+date|pay\s+date|record\s+date|"
                      r"exchange\s+rate\s+date)\s*[-:–]\s*(" + DATE + ")", re.I)
PUBLISHED_RE = re.compile(r"published\s+on:?\s*([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})", re.I)


def published_from(lines):
    for ln in lines[:15]:
        m = PUBLISHED_RE.search(ln)
        if m and MONTHS.get(m.group(1)[:3].lower()):
            return date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    return None


def highlights(lines):
    """The intro above the list: what EasyEquities thinks matters this week/month."""
    start = next((i + 1 for i, ln in enumerate(lines[:15]) if PUBLISHED_RE.search(ln)), 0)
    out = []
    for ln in lines[start:]:
        if PAYING_RE.match(ln) or ln.lower().rstrip(":") in REGIONS or re.match(r"here.s (the full update|how much)", ln, re.I):
            break
        if len(ln) > 12:
            out.append(ln)
    return out[:12]


def parse_blocks(lines):
    """EasyEquities' layout: 'X will be paying R1.23 per share.' then 'Last trading date - 14 April 2026' and
    'Payment date - 20 April 2026' lines, grouped under region headings (South Africa, United States...)."""
    account, found, cur = "ZAR", [], None
    for ln in lines:
        low = ln.lower().strip(" :")
        if low in REGIONS:
            account = REGIONS[low]
            continue
        acc = ACCOUNT_RE.search(ln)
        if acc and len(ln) < 40:
            account = acc.group(1).upper()
            continue
        m = PAYING_RE.match(ln)
        if m:
            amt = m.group("amt")
            num = float(re.sub(r"[^\d.]", "", amt.replace(",", "")) or 0)
            sym = amt.strip()[:2].upper()
            if re.search(r"thebe", amt, re.I):
                value, currency = num / 100, "BWP"
            elif re.search(r"\d\s*(cents?|c)\s*$", amt, re.I):
                value, currency = round(num / 100, 6), "ZAR"
            else:
                currency = ("USD" if "$" in amt and not sym.startswith("A") else "AUD" if sym.startswith("A$") else
                            "GBP" if "£" in amt else "EUR" if "€" in amt else "ZAR")
                value = num
            unit = m.group("unit").strip()
            name = m.group("name").strip()
            if not re.fullmatch(r"(ordinary\s+)?(share|unit|participatory interest)s?", unit, re.I):
                name = f"{name} ({unit})"
            cur = {"account": account, "instrument": clean_name(name), "amount": value, "currency": currency,
                   "ldt": None, "pay_date": None, "line": ln[:500]}
            found.append(cur)
            continue
        d = DATED_RE.match(ln)
        if d and cur:
            when = _date(d.groups()[2:])
            label = d.group(1).lower()
            if label.startswith("payment") or label.startswith("pay"):
                cur["pay_date"] = when
            elif label.startswith("last"):  # 'Last payment date' is their typo for last trading date
                cur["ldt"] = when
    return found


def parse_dividends(lines, year_hint=None):
    blocks = parse_blocks(lines)
    if blocks:
        return blocks
    return parse_lines(lines)


def parse_lines(lines):
    """Each line naming an instrument with an amount and dates. Account sections (ZAR/USD Account headings) carry over;
    table rows whose dates sit in separate cells work too."""
    account, found, header = "ZAR", [], None
    for raw in lines:
        line = NOISE.sub(" • ", raw)
        acc = ACCOUNT_RE.search(line)
        if acc and not AMOUNT_RE.search(line):
            account = acc.group(1).upper()
            continue
        cells = [c.strip() for c in raw.split(" | ")] if " | " in raw else None
        if cells and not AMOUNT_RE.search(raw) and any(re.search(r"last|ldt|pay", c, re.I) for c in cells):
            header = [c.lower() for c in cells]
            continue
        amt = AMOUNT_RE.search(line)
        if not amt:
            continue
        ldt = LDT_RE.search(line)
        pay = PAY_RE.search(line)
        ldt_d = _date(ldt.groups()[1:]) if ldt else None
        pay_d = _date(pay.groups()[1:]) if pay else None
        if cells and header and len(cells) == len(header) and not (ldt_d and pay_d):
            for h, c in zip(header, cells):
                m = re.search(DATE, c)
                if m and ("last" in h or "ldt" in h) and not ldt_d:
                    ldt_d = _date(m.groups())
                elif m and "pay" in h and not pay_d:
                    pay_d = _date(m.groups())
        if not ldt_d and not pay_d:
            continue
        name = clean_name(cells[0] if cells else line[:amt.start()])
        if len(name) < 2 or len(name) > 160:
            continue
        if amt.group(2):
            value, cur = float(amt.group(2).replace(",", "").replace(" ", "")), CURRENCY.get(amt.group(1).lower(), "")
        else:
            value, cur = round(float(amt.group(3)) / 100, 6), "ZAR"
        found.append({"account": account, "instrument": name, "amount": value, "currency": cur or
                      ("USD" if account == "USD" else "ZAR"), "ldt": ldt_d, "pay_date": pay_d, "line": raw[:500]})
    return found


def save_post(db: Session, url: str, html: str):
    title, published, main = content(html)
    post = db.scalar(select(BlogPost).where(BlogPost.url == url)) or BlogPost(url=url)
    db.add(post)
    lines = lines_of(main)
    post.title, post.kind = title[:300], kind_of(title, url)
    post.body = "\n".join(lines)
    hl = highlights(lines)
    post.summary = "\n".join(hl)[:2000] if hl else next((ln for ln in lines if len(ln) > 80), "")[:600]
    items = parse_dividends(lines) if post.kind != "news" else []
    published = published or published_from(lines)
    if not published:
        dates = [d for i in items for d in (i["ldt"], i["pay_date"]) if d]
        published = min(dates) - timedelta(days=3) if dates else utcnow().date()
    post.published = published
    db.flush()
    db.query(BlogDividend).filter(BlogDividend.post_id == post.id).delete()
    seen = set()
    for i in items:
        key = (i["account"], i["instrument"][:200])
        if key in seen:
            continue
        seen.add(key)
        db.add(BlogDividend(post_id=post.id, account=i["account"], instrument=i["instrument"][:200], amount=i["amount"],
                            currency=i["currency"], ldt=i["ldt"], pay_date=i["pay_date"], line=i["line"]))
    post.items, post.status, post.error, post.fetched_at = len(seen), "ok", "", utcnow()
    return post


def refresh(db: Session, limit=12):
    """New posts from the listings (plus re-reading the newest dividend post, which EasyEquities sometimes updates)."""
    links = []
    for listing in LISTINGS:
        try:
            for u in post_links(_get(listing)):
                if u not in links:
                    links.append(u)
        except Exception as e:
            log.warning("Blog listing %s failed: %s", listing, e)
    known = {u: (s, f) for u, s, f in db.execute(select(BlogPost.url, BlogPost.status, BlogPost.fetched_at))}
    day_ago = utcnow() - timedelta(days=1)
    newest_div = next((u for u in links if "dividend" in u.lower()), None)
    empty = set(db.scalars(select(BlogPost.url).where(BlogPost.items == 0, BlogPost.kind != "news")))
    todo = [u for u in links if u not in known or (known[u][1] < day_ago and (known[u][0] != "ok" or u == newest_div))
            or (u in empty and known[u][1] < day_ago)]
    done = 0
    for url in todo[:limit]:
        try:
            save_post(db, url, _get(url))
            done += 1
        except Exception as e:
            post = db.scalar(select(BlogPost).where(BlogPost.url == url)) or BlogPost(url=url, title=url.rsplit("/", 1)[-1])
            post.status, post.error, post.fetched_at = "error", f"{type(e).__name__}: {e}"[:400], utcnow()
            db.add(post)
        db.commit()
    try:
        syms = resolve_symbols(db)
    except Exception:
        db.rollback()
        log.exception("Blog ticker lookup failed")
        syms = {}
    log.info("EasyEquities blog: %d links, %d read, %s", len(links), done, syms)
    return {"links": len(links), "read": done}


def backfill(db: Session, since: date, max_pages=40, max_posts=400, progress=None):
    """Walk the blog's older listing pages and read every post not read yet, until the posts are older than `since`.
    Posts are keyed by url, so a second run only reads what is new: this is the shared cache for every user."""
    import time

    seen, read, failed, pages = set(), 0, 0, 0
    known = {u: s for u, s in db.execute(select(BlogPost.url, BlogPost.status))}
    for base in (f"{BLOG}/topic/dividends-update", BLOG):
        for n in range(1, max_pages + 1):
            try:
                links = post_links(_get(base if n == 1 else f"{base}/page/{n}"))
            except Exception as e:  # past the last page the blog answers 404
                log.info("Blog listing %s page %d stopped: %s", base, n, e)
                break
            pages += 1
            links = [u for u in links if u not in seen]
            if not links:
                break
            seen.update(links)
            newest_old = 0
            for url in links:
                if known.get(url) == "ok":
                    post = db.scalar(select(BlogPost).where(BlogPost.url == url))
                    newest_old += bool(post and post.published and post.published < since)
                    continue
                if read >= max_posts:
                    break
                try:
                    post = save_post(db, url, _get(url))
                    read += 1
                    newest_old += bool(post.published and post.published < since)
                except Exception as e:
                    failed += 1
                    post = db.scalar(select(BlogPost).where(BlogPost.url == url)) or BlogPost(url=url, title=url.rsplit("/", 1)[-1])
                    post.status, post.error, post.fetched_at = "error", f"{type(e).__name__}: {e}"[:400], utcnow()
                    db.add(post)
                db.commit()
                time.sleep(0.3)
                if progress:
                    progress(f"Read {read} posts ({base.rsplit('/', 1)[-1] or 'home'}, page {n})")
            if newest_old == len(links) or read >= max_posts:
                break  # a whole page of posts from before `since`
    return {"pages": pages, "read": read, "failed": failed}


def norm(s: str) -> str:
    s = SUFFIXES.sub(" ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def matcher(names_and_symbols):
    """[(kind, symbol, name)] -> function(instrument) -> (kind, symbol) or None."""
    keys = []
    for kind, symbol, name in names_and_symbols:
        n = norm(name)
        base = (symbol or "").upper().split(".")[0]
        keys.append((kind, symbol, n, base))

    def match(instrument):
        if re.search(r"preference|debenture|note\b", instrument, re.I):
            return None
        n = norm(instrument)
        tokens = set(re.findall(r"\b[A-Z0-9]{2,6}\b", instrument))
        for kind, symbol, key, base in keys:
            if key and len(key) >= 4 and (n == key or n.startswith(key + " ") or key.startswith(n + " ") and len(n) >= 6):
                return kind, symbol
            if base and len(base) >= 3 and base in tokens:
                return kind, symbol
        return None
    return match


def fx_rates(db: Session, currencies) -> dict:
    """Rand per unit of each currency (from the cached Yahoo FX quotes)."""
    fx = {"ZAR": 1.0}
    for cur in {(c or "").upper() for c in currencies} - {"ZAR", ""}:
        try:
            q = prices.quote(db, f"{cur}ZAR=X" if cur != "USD" else "ZAR=X")
            if q and q.price:
                fx[cur] = float(q.price)
        except Exception:
            pass
    return fx


def view(db: Session, holdings, watch, days_back=45):
    """Latest posts, and the dividends declared in them (still to come, or from the last few weeks), each marked if
    you hold or watch it; for holdings, roughly what you'll get (units x amount)."""
    match = matcher([("hold", h.get("symbol"), h.get("name")) for h in holdings] +
                    [("watch", w.symbol, w.name) for w in watch])
    units = {h.get("symbol"): h.get("quantity") for h in holdings}
    today = utcnow().date()
    posts = list(db.scalars(select(BlogPost).where(BlogPost.status == "ok").order_by(BlogPost.published.desc()).limit(12)))
    rows = db.execute(select(BlogDividend, BlogPost).join(BlogPost, BlogDividend.post_id == BlogPost.id)
                      .where((BlogDividend.ldt >= today - timedelta(days=days_back)) | (BlogDividend.pay_date >= today))
                      .order_by(BlogDividend.ldt.desc())).all()
    from .models import PriceCache

    symbols = dict(db.execute(select(BlogSymbol.key, BlogSymbol.symbol)).all())
    wanted = {v for v in symbols.values() if v} | {w.symbol for w in watch} | {h.get("symbol") for h in holdings if h.get("symbol")}
    cache = {c.symbol: c for c in db.scalars(select(PriceCache).where(PriceCache.symbol.in_(wanted)))}
    pay_months = defaultdict(set)
    for account, instrument, paid in db.execute(select(BlogDividend.account, BlogDividend.instrument, BlogDividend.pay_date)):
        if paid:
            pay_months[symbol_key(account, instrument)].add(paid.month)
    fx = fx_rates(db, {d.currency for d, _ in rows})
    seen, out = set(), []
    for d, p in rows:
        key = (d.account, norm(d.instrument), d.ldt)
        if key in seen:
            continue
        seen.add(key)
        m = match(d.instrument)
        state = ("open" if d.ldt and d.ldt >= today else "paying" if d.pay_date and d.pay_date >= today else "paid")
        qty = float(units.get(m[1]) or 0) if m and m[0] == "hold" else None
        key_s = symbol_key(d.account, d.instrument)
        sym = (m[1] if m else "") or symbols.get(key_s, "")
        pc = cache.get(sym)
        price = float(pc.price) if pc and pc.price is not None else None
        same = pc is not None and (pc.currency or "").upper() == (d.currency or "").upper()
        rate = fx.get((d.currency or "").upper())
        in_zar = round(d.amount * rate, 4) if rate and d.amount is not None else None
        if not same and pc is not None and (pc.currency or "").upper() == "ZAR" and in_zar is not None:
            same, amt = True, in_zar
        else:
            amt = d.amount
        paid_month = (d.pay_date or d.ldt).month if (d.pay_date or d.ldt) else None
        out.append({"price": price, "price_currency": pc.currency if pc else "", "ticker": sym,
                    "this_pct": round(amt / price, 5) if price and amt is not None and same else None,
                    "yield_12m": round(float(pc.dividends_12m) / price, 5) if price and pc.dividends_12m else None,
                    "season": SEASONS.get(paid_month, ""),
                    "pays_in": [MONTH_NAMES[i - 1] for i in sorted(pay_months.get(key_s, ()))],
                    "instrument": d.instrument, "account": d.account, "amount": d.amount, "currency": d.currency,
                    "amount_zar": in_zar if (d.currency or "").upper() != "ZAR" else None,
                    "ldt": d.ldt.isoformat() if d.ldt else None, "pay_date": d.pay_date.isoformat() if d.pay_date else None,
                    "mine": m[0] if m else "", "symbol": m[1] if m else "", "post": p.url, "state": state,
                    "can_buy": state == "open", "units": qty,
                    "estimate": round(qty * d.amount, 2) if qty and d.amount is not None else None})
    order = {"open": 0, "paying": 1, "paid": 2}
    out.sort(key=lambda x: (not x["mine"], order[x["state"]], x["ldt"] or ""))
    return {"posts": [{"title": p.title, "url": p.url, "kind": p.kind, "published": p.published.isoformat() if p.published else None,
                       "summary": p.summary, "items": p.items} for p in posts],
            "upcoming": out}


def _yahoo_search(q):
    try:
        r = http.get("https://query2.finance.yahoo.com/v1/finance/search", headers=HEADERS, timeout=15,
                     params={"q": q[:60], "quotesCount": 10, "newsCount": 0}, **HTTP_KW)
        return [x for x in r.json().get("quotes", []) if x.get("symbol")]
    except Exception:
        return []


def name_variants(name: str):
    """'Northam Holdings Limited' -> itself, 'Northam Holdings', 'Northam'; fixes 'Gold Field' -> 'Gold Fields'."""
    base = re.sub(r"\s+", " ", re.sub(r"\(.*?\)", " ", name)).strip()
    base = re.sub(r"\bGold Field\b", "Gold Fields", base)
    short = re.sub(r"\b(limited|ltd|plc|inc|incorporated|corporation|corp|n\.?v\.?|s\.?a\.?|se|ag)\b\.?", " ", base, flags=re.I)
    short = re.sub(r"\s+", " ", short).strip(" ,")
    out = [base, short]
    words = short.split()
    if len(words) > 2:
        out.append(" ".join(words[:2]))
    if len(words) > 1 and len(words[0]) > 3:
        out.append(words[0])
    return list(dict.fromkeys(v for v in out if v))


def search_symbol(name: str, account: str = "ZAR"):
    """Yahoo's search: best ticker for a name, preferring the JSE for ZAR-account shares (tries shorter names too)."""
    fallback = None
    for q in name_variants(name):
        quotes = _yahoo_search(q)
        if not quotes:
            continue
        if account == "ZAR":
            jse = [x for x in quotes if x["symbol"].endswith(".JO")]
            if jse:
                return jse[0]["symbol"]
        elif account in ("USD", "AUD", "GBP", "EUR"):
            suffix = {"USD": "", "AUD": ".AX", "GBP": ".L", "EUR": ""}[account]
            pref = [x for x in quotes if (x["symbol"].endswith(suffix) if suffix else "." not in x["symbol"])]
            if pref:
                return pref[0]["symbol"]
        fallback = fallback or quotes[0]["symbol"]
    return fallback
