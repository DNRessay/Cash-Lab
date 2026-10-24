# Facts out of one deal article: who, what kind, the offer or payout per share, the price the article quotes, dates.
# Runs on the CI worker, where spaCy's small English model is installed (sentences and company names); without it the
# same rules run on plain regex, so the API and the tests never need spaCy.
import logging
import re
from collections import Counter
from datetime import date

from bs4 import BeautifulSoup

from . import blog, prices
from .deals import KIND_RE, PROPERTY_RE

log = logging.getLogger(__name__)

_NLP = False  # False = not tried yet, None = not available


def nlp():
    global _NLP
    if _NLP is False:
        try:
            import spacy

            _NLP = spacy.load("en_core_web_sm", disable=["parser", "lemmatizer", "tagger", "attribute_ruler"])
            _NLP.add_pipe("sentencizer", first=True)
        except Exception as e:  # not installed: fine, the regex rules do the work
            log.info("spaCy not available (%s); using regex rules", e)
            _NLP = None
    return _NLP


MONEY = re.compile(r"\bR\s?(?P<r>\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)(?:\s?(?P<mult>million|billion|bn|m)\b)?"
                   r"|(?P<c>\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)\s?(?:cents?|c|ZAc)\b", re.I)
SPLIT = re.compile(r";|,\s*(?:with|while|whereas|but|and)\b|\s+(?:while|whereas|but)\s+", re.I)
OFFER = re.compile(r"offer|consideration|buy.?out|buy out|repurchase|acquir|take.?over|scheme|per\s+(?:\w+\s+){0,2}share|"
                   r"\bpay|receive|special (?:dividend|distribution)|distribution|return of capital|cash", re.I)
PRICE = re.compile(r"trad(?:ing|es|ed)\s+(?:at|around|near|for)|share price|closed at|currently|last traded|market price|"
                   r"at the time of writing", re.I)
NOT_UNIT = re.compile(r"market cap|peak|high of|low of|\bdown\b|\bup\b|fell|rose|gained|loss|profit|revenue|turnover|"
                      r"maximum|aggregate|worth|valued|guarantee|funded|facility", re.I)
CLOSING = re.compile(r"conclude|complet|clos(?:e|es|ing)|effective|implement|no later than|expected to|finali[sz]", re.I)
IGNORE_ORGS = {"jse", "johannesburg stock exchange", "nsx", "a2x", "sens", "sarb", "sars", "fsca", "cipc", "trp",
               "takeover regulation panel", "daily investor", "easyequities", "moneyweb", "business day", "reuters",
               "bloomberg", "google", "google news", "facebook", "whatsapp", "linkedin", "login", "register"}
# "last day to trade is 11 November 2026", "the payment date will be 19 November": a few words may sit before the date
LDT_WORDS = re.compile(r"last\s+(?:day|date)\s+(?:to|of)\s+trad(?:e|ing)\b[^.\d]{0,30}?(" + blog.DATE + ")", re.I)
PAY_WORDS = re.compile(r"\bpa(?:y|id|yment)\s+date\b[^.\d]{0,30}?(" + blog.DATE + ")", re.I)
ORG_RX = re.compile(r"\b([A-Z][\w&.\-]+(?:\s+[A-Z][\w&.\-]+){0,2})\s+(?:has|have|had|will|is|are|plans?|announced|"
                    r"received|offers?|says|agreed|to|shareholders)\b")


def to_rand(m):
    """Rand per share from a MONEY match, or None for totals ('R19.75 million')."""
    if m.group("r"):
        if m.group("mult"):
            return None
        s = m.group("r")
        s = re.sub(r"[ ,](?=\d{3}\b)", "", s) if re.search(r"[ ,]\d{3}\b", s) else s.replace(",", ".")
        return float(s)
    s = m.group("c")
    s = re.sub(r"[ ,](?=\d{3}\b)", "", s) if re.search(r"[ ,]\d{3}\b", s) else s.replace(",", ".")
    return round(float(s) / 100, 4)


def sentences(text, doc=None):
    if doc is not None:
        return [s.text.strip() for s in doc.sents if s.text.strip()]
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"'R0-9])", re.sub(r"\s+", " ", text)) if s.strip()]


def amounts(sents):
    """(offer, price, evidence): the per-share offer or payout, and the share price the article quotes.
    Each sentence is cut into clauses first, because 'offering 17 cents a share, with the price at 15 cents' says both."""
    offers, prices_, evidence = [], [], {}
    for s in sents:
        for clause in SPLIT.split(s):
            vals = [v for m in MONEY.finditer(clause) if (v := to_rand(m)) is not None and v > 0]
            if not vals:
                continue
            if PRICE.search(clause):
                prices_.extend(vals)
            elif NOT_UNIT.search(clause):
                continue
            elif OFFER.search(clause):
                for v in vals:
                    offers.append(v)
                    evidence.setdefault(v, clause.strip()[:220])
    offer = Counter(offers).most_common(1)[0][0] if offers else None
    price = Counter(prices_).most_common(1)[0][0] if prices_ else None
    return offer, price, evidence.get(offer)


def company_of(title, text, doc=None):
    names = Counter()
    # spaCy's organisations plus the subject of "X has announced…", "X will…": either alone misses names now and then
    found = [m.group(1) for s in sentences(text) for m in ORG_RX.finditer(s)]
    if doc is not None:
        found += [e.text for e in doc.ents if e.label_ == "ORG"]
    for n in found:
        n = re.sub(r"^(?:the|a)\s+|[’']s$", "", n.strip(), flags=re.I).strip(" .,")
        if re.search(r"[A-Za-z]{3}", n) and not re.match(r"(?:R|US\$|\$)?\s?\d", n) and n.lower() not in IGNORE_ORGS:
            names[n] += 1
    if not names:
        return ""
    head = f"{title} {text[:400]}".lower()
    best = max(names, key=lambda n: names[n] + (2 if n.lower() in head else 0))
    return best[:80]


def first_date(sent):
    m = re.search(blog.DATE, sent)
    return blog._date(m.groups()) if m else None


def extract(title, text, snippet=""):
    """The facts one article gives. Never guesses: a field is None when the text doesn't say."""
    body = re.sub(r"[ \t]+", " ", text or "").strip()
    whole = f"{title}. {body}" if body else f"{title}. {snippet}"
    doc = None
    if nlp() and len(whole) < 60000:
        doc = nlp()(whole[:30000])
    sents = sentences(whole, doc)
    offer, price, evidence = amounts(sents)
    lead = whole[:2500]
    kind = next((k for k, rx in KIND_RE if re.search(rx, lead, re.I)), None)

    ldt = pay = deadline = None
    if m := (blog.LDT_RE.search(whole) or LDT_WORDS.search(whole)):
        ldt = blog._date(m.groups()[1:])
    if m := (blog.PAY_RE.search(whole) or PAY_WORDS.search(whole)):
        pay = blog._date(m.groups()[1:])
    for s in sents:
        if CLOSING.search(s) and (d := first_date(s)):
            deadline = d
            break
    return {"company": company_of(title, body or snippet, doc), "kind": kind, "amount": offer, "price": price,
            "ldt": ldt.isoformat() if ldt else None, "pay_date": pay.isoformat() if pay else None,
            "deadline": deadline.isoformat() if deadline else None, "property": bool(PROPERTY_RE.search(lead)),
            "evidence": evidence}


# ── reading the page ────────────────────────────────────────────────────────

class Unreadable(Exception):
    pass


def page_text(html):
    """The article's own paragraphs: not menus, share buttons or 'latest stories'."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer", "aside", "form", "header", "noscript"]):
        tag.decompose()
    main = soup.select_one("[class*=article-body], [class*=post-body], [class*=entry-content], [itemprop=articleBody]") \
        or soup.select_one("article") or soup.select_one("main") or soup
    paras = [p.get_text(" ", strip=True) for p in main.find_all("p")]
    text = "\n".join(p for p in paras if len(p) > 25)
    if len(text) < 300:
        raise Unreadable("the page has no readable article text (paywall or script-built page)")
    return text[:15000]


def read_url(url):
    r = prices.http.get(url, headers=prices.HEADERS, timeout=20, **prices.HTTP_KW)
    r.raise_for_status()
    return page_text(r.text)


def read(article):
    """(facts, method) for one article dict {title, snippet, link}: the full page, else the headline and snippet."""
    try:
        return extract(article["title"], read_url(article["link"]), article.get("snippet", "")), "article"
    except Exception as e:
        log.info("Couldn't read %s: %s", article["link"], e)
        facts = extract(article["title"], "", article.get("snippet", ""))
        facts["note"] = f"{type(e).__name__}: {e}"[:200]
        return facts, "snippet"
