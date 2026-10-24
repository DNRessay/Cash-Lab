# Company deals on the JSE: buyouts, delisting offers, special payouts, rights offers, unbundlings and property-fund
# mergers, from EasyEquities' blog first and the news (SerpAPI) second, each checked against today's share price: does buying now make money or not?
# Shared by all users; cached in the market_pulse table and refreshed nightly (or on demand).
import json
import logging
import re
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import utcnow
from . import prices
from .pulse import MAX_AGE, MarketPulse, serp_news

log = logging.getLogger(__name__)

KEY = "deals"
QUERIES = ["JSE scheme of arrangement offer cents per share", "JSE delisting offer shareholders per share",
           "JSE special distribution per share last day to trade", "JSE mandatory offer minority shareholders",
           "JSE rights offer cents per share", "JSE unbundling shareholders", "JSE REIT property fund offer per share",
           "JSE property merger share swap"]
KINDS = {"buyout": "Buyout offer", "delisting": "Delisting offer", "merger": "Merger / share swap",
         "special_distribution": "Special payout", "rights_offer": "Rights offer", "unbundling": "Unbundling",
         "other": "Company change"}
DIVIDEND_TAX = 0.20
FEES = 0.01  # about what buying and selling a share costs on EasyEquities, both ways together
KIND_RE = [("special_distribution", r"special (?:dividend|distribution)|capital (?:return|distribution)|final distribution"),
           ("delisting", r"delist"), ("rights_offer", r"rights offer|rights issue"), ("unbundling", r"unbundl"),
           ("merger", r"merger|share swap|amalgamat"),
           ("buyout", r"scheme of arrangement|buyout|take.?over|acquisition|mandatory offer|offer to (?:buy|acquire)|bid for")]
AMOUNT_RE = re.compile(r"R\s?(\d+(?:[.,]\d+)?)\s*(?:a|per)\s+(?:ordinary\s+)?share|"
                       r"(\d{1,3}(?:[ ,]\d{3})+|\d+(?:[.,]\d+)?)\s*(?:cents|c)\s*(?:a|per)\s+(?:ordinary\s+)?share", re.I)
AMOUNT_SEARCH = {"special_distribution": "special dividend cents per share", "buyout": "offer cents per share",
                 "delisting": "offer cents per share"}
MAX_AMOUNT_SEARCHES = 5  # extra news searches per rebuild, for deals whose headline has no per-share amount
PROPERTY_RE = re.compile(r"\breit\b|property|properties|real estate|reit", re.I)


def regex_deal(article):
    """A deal from a headline without AI: the kind from keywords, the amount when it says '450 cents per share'."""
    text = f"{article['title']} {article['snippet']}"
    kind = next((k for k, rx in KIND_RE if re.search(rx, text, re.I)), None)
    if not kind:
        return None
    amount = parse_amount(text)
    company = re.split(r"\s+(?:receives|gets|to|offers?|announces|says|plans|agrees|in|faces|bid|shareholders)\b|[:,–-]",
                       article["title"], maxsplit=1)[0].strip()
    return {"company": company[:80], "kind": kind, "amount": amount, "ldt": None, "property": bool(PROPERTY_RE.search(text))}


def parse_amount(text):
    """Rand per share from 'R29.50 a share' or '2 950 cents per share' / '2,950c per share' (thousands separators in
    cents are not decimals). None when the text doesn't say."""
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    if m.group(1):
        return float(m.group(1).replace(",", "."))
    cents = m.group(2)
    cents = re.sub(r"[ ,](?=\d{3}\b)", "", cents) if re.search(r"[ ,]\d{3}\b", cents) else cents.replace(",", ".")
    return round(float(cents) / 100, 4)


def find_amount(company, kind):
    """The per-share amount when the headline only gave a total ('to return R7bn'): one news search for the cents per
    share, read by the same never-guess rule (the first explicit amount near the company's name)."""
    if kind not in AMOUNT_SEARCH:
        return None, None
    for a in serp_news(f"{company} {AMOUNT_SEARCH[kind]}", 6):
        text = f"{a['title']} {a['snippet']}"
        if company.split()[0].lower() in text.lower() and (amount := parse_amount(text)):
            return amount, a["link"]
    return None, None


def since_news(row, day):
    """How far the share price has moved since the news came out (0.10 = up 10%), or None."""
    if not row or row.price is None or not row.history or not day:
        return None
    try:
        then = prices.price_on(row.history, date.fromisoformat(day))
    except ValueError:
        return None
    return round(float(row.price) / then - 1, 4) if then else None


def money(x):
    return ("–" if x < 0 else "") + f"R{abs(x):,.2f}".replace(",", " ")


def explain(kind, amount, price, moved=None, day=None):
    """The working behind the verdict, step by step, for the tap-to-open detail."""
    steps = []
    if kind == "special_distribution" and amount and price:
        after = max(price - amount, 0)
        net = amount * (1 - DIVIDEND_TAX)
        fees = price * FEES
        result = after + net - price - fees
        steps = [f"The payout is {money(amount)} a share. Buying today costs {money(price)} a share.",
                 f"After the last day to trade, the share price drops by about the payout: to roughly {money(after)}.",
                 f"You receive the {money(amount)} payout less {DIVIDEND_TAX:.0%} dividend tax: {money(net)}.",
                 f"So for {money(price)} paid (plus about {money(fees)} in fees) you end up with about {money(after)} in "
                 f"shares and {money(net)} in cash: {money(result)} a share ({result / price:+.1%}).",
                 "If it is paid as a return of capital instead of a dividend there is no 20% dividend tax, but it lowers "
                 "your base cost, so you pay capital gains tax on it later instead."]
    elif kind in ("buyout", "delisting") and amount and price:
        net = amount - price - price * FEES
        steps = [f"The offer is {money(amount)} a share; it trades at {money(price)}.",
                 f"If the deal goes through you get {money(amount)}: {money(net)} a share after fees ({net / price:+.1%}).",
                 "If the deal fails or is delayed, the price usually falls back to where it was before the offer."]
    if moved is not None and abs(moved) >= 0.02 and steps:
        when = f" on {day}" if day else ""
        steps.append(f"The price has moved {moved:+.1%} since the news{when}: "
                     + ("the market has already priced the payout in, so there is no bargain left."
                        if moved > 0 else "it has fallen since, so check what the market knows before buying."))
    return steps


def ai_deals(articles):
    """Groq reads the headlines: which name a specific JSE company, what kind of deal, the rand amount per share."""
    from ..ai import llm

    heads = "\n".join(f"{i}. [{a['date'] or '?'}] {a['title']} — {a['snippet'][:700 if a['source'] == 'EasyEquities blog' else 220]}"
                      for i, a in enumerate(articles))
    system = ("You read South African business headlines. For each one about a SPECIFIC JSE-listed company having a "
              "corporate action, reply as JSON {\"deals\": [{\"i\": index, \"company\": the listed company's name, "
              "\"kind\": \"buyout|delisting|merger|special_distribution|rights_offer|unbundling|other\", "
              "\"amount\": rand per share as a number (450 cents = 4.50) or null if the headline doesn't give it, "
              "\"ldt\": \"YYYY-MM-DD\" last day to trade if given else null, \"property\": true if it's a property "
              "company or REIT}]}. Skip headlines that aren't about one company's deal. Never guess an amount.")
    data = llm.parse_json(llm.groq([{"role": "system", "content": system}, {"role": "user", "content": heads}],
                                   max_tokens=1600, json_mode=True)) or {}
    out = {}
    for d in data.get("deals", []) if isinstance(data, dict) else []:
        try:
            i = int(d.get("i"))
            amount = float(d["amount"]) if d.get("amount") not in (None, "") else None
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(articles) and d.get("company"):
            out[i] = {"company": str(d["company"])[:80], "kind": d.get("kind") if d.get("kind") in KINDS else "other",
                      "amount": amount if amount and amount > 0 else None, "ldt": _date(d.get("ldt")),
                      "property": bool(d.get("property"))}
    return out


def _date(s):
    try:
        return date.fromisoformat(str(s)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def verdict(kind, amount, price):
    """(tone, what it means for someone buying today). tone: ok = buying can make money, warn = it loses, info = depends."""
    if kind in ("buyout", "delisting") and amount and price:
        gap = amount - price
        net = gap - price * FEES
        if net > 0:
            return "ok", (f"The offer is R{amount:.2f} a share and it trades at R{price:.2f}: about R{net:.2f} a share "
                          f"({net / price:+.1%}) after fees if the deal goes through. If it fails or is delayed, the price "
                          "usually falls back below where it is now.")
        return "warn", (f"It trades at R{price:.2f}, at or above the R{amount:.2f} offer: buying now loses about "
                        f"R{-net:.2f} a share after fees even if the deal goes through.")
    if kind == "special_distribution" and amount:
        lost = amount * DIVIDEND_TAX + (price or 0) * FEES
        return "warn", (f"After the last day to trade the price drops by about the R{amount:.2f} payout, and the payout is "
                        f"taxed {DIVIDEND_TAX:.0%}. Buying just to get it loses about R{lost:.2f} a share.")
    if kind == "special_distribution":
        return "warn", ("The price drops by about the payout after the last day to trade, and the payout is taxed "
                        f"{DIVIDEND_TAX:.0%}: buying just to get it loses money.")
    if kind == "rights_offer":
        return "info", ("Shareholders on the record date may buy new shares, usually below the market price. More shares "
                        "usually push the price down; only worth it if you'd want more of the company anyway.")
    if kind == "unbundling":
        return "info", ("Shareholders get shares in the company being split off and the price drops by about their value. "
                        "No free money; you end up holding two companies.")
    if kind == "merger":
        return "info", ("Shareholders get shares in the merged company. What it's worth depends on the swap ratio and "
                        "on the other company's price.")
    if kind in ("buyout", "delisting"):
        return "info", ("No price per share given yet. Compare the offer with the share price before buying: if the "
                        "price is already at or above the offer, there's nothing left to make.")
    return "info", "Read the announcement before buying: company changes can move the price either way."


def blog_articles(db: Session, days=120):
    """EasyEquities' blog posts about company deals (it announces buyouts, payouts and unbundlings to its clients)."""
    from .blog import BlogPost

    since = date.today() - timedelta(days=days)
    out = []
    for p in db.scalars(select(BlogPost).where(BlogPost.status == "ok", BlogPost.kind == "news", BlogPost.published >= since)):
        text = f"{p.title}\n{p.summary}\n{p.body[:3000]}"
        if any(re.search(rx, text, re.I) for _, rx in KIND_RE):
            out.append({"title": p.title, "snippet": re.sub(r"\s+", " ", f"{p.summary} {p.body[:1200]}")[:900], "link": p.url,
                        "source": "EasyEquities blog", "date": p.published.isoformat() if p.published else None})
    return out


def gather(db: Session):
    """Every article that might be about a deal: EasyEquities' blog first, then the news."""
    articles = blog_articles(db)
    seen = {a["link"] for a in articles}
    for q in QUERIES:
        for a in serp_news(q, 8):
            if a["link"] not in seen and (not a["date"] or a["date"] >= (date.today() - timedelta(days=120)).isoformat()):
                seen.add(a["link"])
                articles.append(a)
    return articles


def same_company(company, quote_name):
    """A ticker only counts when the quote's name carries the company's own name: Yahoo's search happily returns a
    different company for a headline fragment."""
    from .blog import norm

    words = [w for w in norm(company).split() if len(w) > 2][:2]
    name = norm(quote_name)
    return bool(words) and any(w in name for w in words)


def apply_facts(found, articles, read):
    """What the full article said beats what the headline hinted at: its company, kind, per-share amount and dates."""
    for i, a in enumerate(articles):
        row = read.get(a["link"])
        f = row.facts if row is not None and row.status == "done" else None
        if not f or not f.get("kind"):
            continue
        base = found.get(i) or {"company": "", "kind": f["kind"], "amount": None, "ldt": None, "property": False}
        full = row.method == "article"
        company = (f.get("company") if full else "") or base["company"] or f.get("company") or ""
        if not company:
            continue
        found[i] = {**base, "company": company[:80], "kind": f["kind"] if full or not base["company"] else base["kind"],
                    "amount": f["amount"] if f.get("amount") else base["amount"], "ldt": f.get("ldt") or base["ldt"],
                    "property": base["property"] or bool(f.get("property")), "deadline": f.get("deadline"),
                    "article_price": f.get("price"), "evidence": f.get("evidence"), "read": row.method}
    return found


def build(db: Session, articles=None):
    from ..ai import llm
    from .blog import search_symbol
    from .jobs import article_facts

    articles = gather(db) if articles is None else articles
    found = {}
    if settings.groq_api_keys and articles:
        try:
            found = ai_deals(articles[:50])
        except llm.AIError as e:
            log.warning("Deals: AI skipped: %s", e)
    if not found:
        found = {i: d for i, a in enumerate(articles) if (d := regex_deal(a))}
    found = apply_facts(found, articles, article_facts(db, [a["link"] for a in articles]))

    deals, done, searches = [], set(), 0
    blog_first = lambda kv: (articles[kv[0]]["source"] == "EasyEquities blog", articles[kv[0]]["date"] or "")
    for i, d in sorted(found.items(), key=blog_first, reverse=True):
        key = (d["company"].lower(), d["kind"])
        if key in done:
            continue  # the newest article about a deal wins
        done.add(key)
        a = articles[i]
        symbol = search_symbol(d["company"], "ZAR")
        row = prices.quote(db, symbol) if symbol and symbol.endswith(".JO") else None
        if row is not None and not same_company(d["company"], row.name):
            row = None
        price = float(row.price) if row and row.price is not None else None
        price_source = "market" if price is not None else None
        if price is None and d.get("article_price") and d["kind"] in ("buyout", "delisting"):
            price, price_source = d["article_price"], "article"
        amount_link = None
        if not d["amount"] and d["kind"] in AMOUNT_SEARCH and searches < MAX_AMOUNT_SEARCHES:
            searches += 1
            d["amount"], amount_link = find_amount(d["company"], d["kind"])
        moved = since_news(row, a["date"])
        tone, says = verdict(d["kind"], d["amount"], price)
        if price_source == "article":  # a price from the day the article was written is not today's price
            tone, says = "info", (f"The article quoted R{price:.2f} against the R{d['amount']:.2f} offer"
                                  + (f" on {a['date']}" if a["date"] else "") + ". No live price was found, so check "
                                  "today's price before buying: if it is at or above the offer there is nothing left to make.")
        if d.get("deadline") and d["deadline"] < date.today().isoformat():
            tone, says = "info", f"The article's closing date ({d['deadline']}) has passed: check whether the deal completed. " + says
        gap = round(d["amount"] - price, 2) if d["amount"] and price and d["kind"] in ("buyout", "delisting") else None
        deals.append({"company": d["company"], "symbol": symbol if row else None, "price_source": price_source,
                      "deadline": d.get("deadline"), "evidence": d.get("evidence"), "read": d.get("read"), "kind": d["kind"], "kind_label": KINDS[d["kind"]],
                      "property": d["property"], "amount": d["amount"], "price": price, "gap": gap,
                      "gap_pct": round(gap / price, 4) if gap is not None else None, "tone": tone, "verdict": says,
                      "ldt": d["ldt"], "title": a["title"], "link": a["link"], "source": a["source"], "date": a["date"],
                      "amount_link": amount_link, "since_news": moved,
                      "detail": explain(d["kind"], d["amount"], price, moved, a["date"]) if price_source != "article" else []})
    order = {"ok": 0, "warn": 1, "info": 2}
    deals.sort(key=lambda x: (order[x["tone"]], -(len(x["date"] or "") and int((x["date"] or "0").replace("-", "")))))
    return {"built_at": utcnow().isoformat(), "deals": deals[:25],
            "sources": {"news": bool(settings.serpapi_keys), "ai": bool(settings.groq_api_keys)}}


def save(db: Session, data):
    row = db.scalar(select(MarketPulse).where(MarketPulse.key == KEY).order_by(MarketPulse.id.desc())) or MarketPulse(key=KEY)
    row.data, row.created_at = json.loads(json.dumps(data)), utcnow()
    db.add(row)
    db.commit()


def annotate(db: Session, data, reading=False):
    """The cached deals plus, per deal, whether its article has been read yet and whether a worker is on it now."""
    from .jobs import article_facts

    read = article_facts(db, [d["link"] for d in data.get("deals", [])])
    out = dict(data)
    out["deals"] = [{**d, "read": (read[d["link"]].method if d["link"] in read and read[d["link"]].status == "done" else None),
                     "reading": reading and not (d["link"] in read and read[d["link"]].status == "done")}
                    for d in data.get("deals", [])]
    out["reading"] = reading
    return out


def get(db: Session, refresh=False):
    row = db.scalar(select(MarketPulse).where(MarketPulse.key == KEY).order_by(MarketPulse.id.desc()))
    if row and not refresh and utcnow() - row.created_at < timedelta(hours=26):
        return row.data
    if row and refresh and utcnow() - row.created_at < MAX_AGE and row.data.get("sources", {}).get("news") == bool(settings.serpapi_keys):
        return row.data  # at most one rebuild every few hours
    data = build(db)
    row = row or MarketPulse(key=KEY)
    row.data, row.created_at = json.loads(json.dumps(data)), utcnow()
    db.add(row)
    db.commit()
    return data
