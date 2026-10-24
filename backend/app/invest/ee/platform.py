# Unofficial EasyEquities platform client, adapted from easy-equities-client 0.5.0 (MIT, see LICENSE file here).
# Kept in the repo on purpose: when EasyEquities changes their site we patch this file instead of waiting on PyPI.
# Every failure is raised as PlatformError with the step that broke, so the UI can say exactly what stopped working.
import json
import logging
import re
import urllib.parse

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as http
    SESSION_KW = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    import requests as http
    SESSION_KW = {}

BASE_URL = "https://platform.easyequities.io"
EP_BASE_URL = "https://platform.easyproperties.co.za"  # EasyProperties runs its own platform; same login
EP_PAGES = ["", "/AccountOverview", "/Portfolio", "/MyProperties", "/Invest/MyProperties", "/Properties/MyProperties"]
SIGN_IN = "/Account/SignIn"
OVERVIEW = "/AccountOverview"
SWITCH_ACCOUNT = "/Menu/UpdateCurrency"
VALUATIONS = "/AccountOverview/GetTrustAccountValuations"
HOLDINGS = "/AccountOverview/GetHoldingsView?stockViewCategoryId=12"
TRANSACTIONS = "/TransactionHistory/GetTransactions"
STATEMENTS = "/Statement"


log = logging.getLogger(__name__)


class PlatformError(Exception):
    def __init__(self, stage, message, page=""):
        super().__init__(f"{stage}: {message}")
        self.stage, self.message = stage, message
        self.page = (page or "")[:50_000]  # kept for debugging a changed page layout


def money(text):
    """'R2 000.00' / '$353.61' / '-R12.30' -> float, or None."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)  # already a number (JSON); str() could give 3.4e-05, which the regex below would misread
    s = str(text).replace("\xa0", "").replace(" ", "").replace(",", "")
    m = re.search(r"(-?)[^\d-]*(\d+(?:\.\d+)?)", s)
    return float(m.group(1) + m.group(2)) if m else None


def _text(el):
    return el.get_text(" ", strip=True) if el else ""


def parse_accounts(page: str):
    soup = BeautifulSoup(page, "html.parser")
    accounts = []
    for div in soup.find_all(attrs={"id": "trust-account-types"}):
        parent = div.parent
        if parent and parent.attrs.get("data-tradingcurrencyid") and parent.attrs.get("data-id"):
            accounts.append({"id": parent["data-id"].strip(), "name": _text(div),
                             "currency_id": parent["data-tradingcurrencyid"].strip()})
    if not accounts:  # fallback: any element carrying both data attributes
        for el in soup.find_all(attrs={"data-tradingcurrencyid": True, "data-id": True}):
            accounts.append({"id": el["data-id"].strip(), "name": _text(el)[:80],
                             "currency_id": el["data-tradingcurrencyid"].strip()})
    seen, out = set(), []
    for a in accounts:
        if a["id"] not in seen:
            seen.add(a["id"])
            out.append(a)
    return out


def _cell(div, cls):
    return _text(div.find(attrs={"class": cls}))


def parse_holdings(page):
    soup = BeautifulSoup(page, "html.parser")
    out, seen = [], set()
    for div in soup.find_all(attrs={"class": "holding-inner-container"}):
        name = _cell(div, "equity-image-as-text")
        if not name or name in seen:
            continue
        img = div.find(attrs={"class": "instrument"})
        src = img.attrs.get("src", "") if img else ""
        code = re.search(r"/([A-Z0-9.]+)\.png", src)
        span = div.find(attrs={"data-detailviewurl": True})
        view = span.attrs["data-detailviewurl"] if span else ""
        seen.add(name)
        out.append({
            "name": name,
            "contract_code": code.group(1) if code else "",
            "purchase_value": money(_cell(div, "purchase-value-cell")),
            "current_value": money(_cell(div, "current-value-cell")),
            "current_price": money(_cell(div, "current-price-cell")),
            "view_url": view,
            "isin": view.split("=")[-1] if "=" in view else "",
        })
    return out


def parse_shares(page):
    """Whole shares + fractional share rights from a holding's detail page."""
    text = _text(BeautifulSoup(page, "html.parser"))
    whole = re.search(r"#\s*Shares\s*([\d ,]+)", text)
    frac = re.search(r"#\s*FSR\w*\s*(\.?\d+)", text)
    if not whole and not frac:
        return None
    w = float(whole.group(1).replace(" ", "").replace(",", "") or 0) if whole else 0.0
    f = frac.group(1) if frac else ""
    return w + (float(f if f.startswith(".") else "0." + f) if f else 0.0)


def _amount_after(label, text):
    m = re.search(label + r"\s*:?\s*(-?\s*R?\s*-?[\d ,]*\.?\d+)", text, re.I)
    return money(m.group(1)) if m else None


def parse_property_cards(page):
    """'My Properties' cards: name, CURRENT VALUE R x, RENTAL YIELD y%, VALUATION CHG R z (text-based, layout-proof)."""
    text = _text(BeautifulSoup(page, "html.parser"))
    parts = re.split(r"(?i)current value", text)
    cards = []
    for i in range(1, len(parts)):
        before, body = parts[i - 1], parts[i]
        # The card name is whatever sits just before "CURRENT VALUE": after the previous card's numbers/headings.
        tail = re.split(r"(?i)valuation chg\s*:?\s*-?\s*R?\s*-?[\d ,]*\.?\d+%?|my properties|rental yield\s*[\d.]+%", before)[-1]
        name = re.sub(r"\s+", " ", tail).strip(" -|")[-80:].strip()
        value = _amount_after(r"^\s*(?:rental yield\s*[\d.]+\s*%)?", body)
        if value is None:
            value = money(re.search(r"R\s*[\d ,]*\.?\d+", body).group(0)) if re.search(r"R\s*[\d ,]*\.?\d+", body) else None
        yld = re.search(r"(?i)rental yield\s*:?\s*(-?[\d.]+)\s*%", body)
        chg = _amount_after(r"(?i)valuation chg", body)
        if name and value is not None:
            cards.append({"name": name, "contract_code": "", "current_value": value, "current_price": None,
                          "purchase_value": round(value - chg, 2) if chg is not None else None,
                          "rental_yield": float(yld.group(1)) / 100 if yld else None, "view_url": "", "isin": "",
                          "shares": None})
    return cards


def discover(page):
    """Links and API-looking paths on a page, for learning a changed/unknown layout. No values, just paths."""
    paths = set(re.findall(r"""(?:href|src|action)=["'](/[^"'#?]{1,120})""", page))
    paths |= set(re.findall(r"""["'](/api/[^"']{1,120})["']""", page))
    return sorted(p for p in paths if not re.search(r"\.(png|jpg|svg|css|ico|woff2?)$", p))[:60]


def parse_statements(page):
    soup = BeautifulSoup(page, "html.parser")
    out = []
    for b in soup.select(".statementDownloadBtn"):
        url = b.get("data-url") or ""
        if not url:
            continue
        # The account heading sits in an earlier row of the same dropdown section.
        section = b.find_parent(class_=re.compile("panel|account|dropDown|statement", re.I))
        heading = _text(section.find(re.compile("^h[1-6]$|^strong$"))) if section else ""
        out.append({"name": (b.get("data-filename") or _text(b) or "statement.pdf")[:200], "url": url[:500],
                    "account": heading[:80]})
    return out


def _redact(text):
    return re.sub(r"[A-Za-z0-9_\-]{24,}", "…", text or "")


def forms(page):
    """Form actions, methods and input names (never values), to learn how a site's sign-in or statement works."""
    soup = BeautifulSoup(page or "", "html.parser")
    out = []
    for f in soup.find_all("form")[:5]:
        out.append({"action": _redact(f.get("action", "")), "method": f.get("method", ""),
                    "inputs": [i.get("name") or i.get("id") or i.get("type") for i in f.find_all(["input", "select", "button"])][:20]})
    title = soup.title.get_text(strip=True) if soup.title else ""
    return {"title": title[:80], "forms": out, "links": [_redact(x) for x in discover(page or "")][:40],
            "scripts": [_redact(sc.get("src", "")) for sc in soup.find_all("script") if sc.get("src")][:15]}


def page_shape(page):
    """Structure of a page for the logs, never its content: title, size, markers and class names that mention holdings.
    Safe to ship to CloudWatch (no amounts, names or values), enough to tell an empty account from a changed layout."""
    page = page or ""
    soup = BeautifulSoup(page, "html.parser")
    classes = sorted({c for tag in soup.find_all(class_=True) for c in tag.get("class", [])
                      if re.search(r"holding|equity|instrument|empty|no-|portfolio|account", c, re.I)})[:25]
    return {"title": (soup.title.get_text(strip=True) if soup.title else "")[:80], "bytes": len(page),
            "has_container": "holding-inner-container" in page, "has_no_holdings": "No holdings" in page,
            "looks_signed_out": bool(re.search(r"(?i)sign\s?in|log\s?in|password", page[:5000])) and "holding" not in page.lower(),
            "classes": classes, "forms": len(soup.find_all("form")), "tables": len(soup.find_all("table"))}


def statement_links(page):
    """Paths on the overview page that look like statements/reports (to learn where printable statements live)."""
    found = set(re.findall(r"""(?:href|action|data-[\w-]*url)=["']([^"']*(?:[Ss]tatement|[Rr]eport|[Tt]ax[Cc]ert)[^"']*)["']""", page))
    found |= set(re.findall(r"""["'](/[\w/-]*(?:Statement|Report|TaxCertificate)[\w/-]*)["']""", page))
    return sorted(p for p in found if len(p) < 200)[:20]


class Platform:
    def __init__(self, base_url=BASE_URL, session=None):
        self.base_url = base_url
        self.s = session or http.Session(**SESSION_KW)
        self.current = None
        self.statement_links = []
        self.landing = ""

    def _url(self, path):
        return self.base_url + path

    def _get(self, stage, path):
        try:
            r = self.s.get(self._url(path), timeout=30)
        except Exception as e:
            raise PlatformError(stage, f"request failed ({e})")
        if r.status_code != 200:
            raise PlatformError(stage, f"HTTP {r.status_code}", r.text)
        return r

    def login(self, username, password):
        data = (f"UserIdentifier={urllib.parse.quote(username)}&Password={urllib.parse.quote(password)}"
                "&ReturnUrl=&OneSignalGameId=&IsUsingNewLayoutSatrixOrEasyEquitiesMobileApp=False")
        try:
            r = self.s.post(self._url(SIGN_IN), data=data, allow_redirects=False, timeout=30,
                            headers={"Content-Type": "application/x-www-form-urlencoded",
                                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        except Exception as e:
            raise PlatformError("login", f"request failed ({e})")
        if r.status_code != 302:
            hint = "wrong username/password, or EasyEquities now asks for a one-time PIN"
            raise PlatformError("login", f"not accepted (HTTP {r.status_code}: {hint})", r.text)
        location = r.headers.get("location", "") or ""
        self.landing = location if location.startswith("/") else ""
        if "signin" in location.lower() or "otp" in location.lower() or "verify" in location.lower():
            raise PlatformError("login", f"redirected to {location} (password rejected or extra verification needed)")

    def accounts(self):
        r = self._get("accounts", OVERVIEW)
        self.statement_links = statement_links(r.text)
        accounts = parse_accounts(r.text)
        if not accounts:
            raise PlatformError("accounts", "no accounts found on the overview page (layout changed?)", r.text)
        return accounts

    def switch(self, account_id):
        if self.current == account_id:
            return
        try:
            r = self.s.post(self._url(SWITCH_ACCOUNT), data={"trustAccountId": account_id}, timeout=30)
        except Exception as e:
            raise PlatformError("switch account", f"request failed ({e})")
        if r.status_code != 200:
            raise PlatformError("switch account", f"HTTP {r.status_code}", r.text)
        self.current = account_id

    def statements(self):
        """Printable statements listed on /Statement: [{name, url, account}]."""
        r = self._get("statements", STATEMENTS)
        return parse_statements(r.text)

    def download(self, url):
        try:
            r = self.s.get(self._url(url) if url.startswith("/") else url, timeout=60)
        except Exception as e:
            raise PlatformError("statement download", f"request failed ({e})")
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            raise PlatformError("statement download", f"HTTP {r.status_code}, not a PDF")
        return r.content

    def switch_debug(self, account_id):
        """What EasyEquities answers when asked to use this account (EasyProperties seems to need something else)."""
        out = {}
        for name, path in (("can_use", "/Menu/CanUseSelectedAccount"), ("switch", SWITCH_ACCOUNT)):
            try:
                r = self.s.post(self._url(path), data={"trustAccountId": account_id}, timeout=30, allow_redirects=False)
                out[name] = {"status": r.status_code, "location": _redact(r.headers.get("location", "")),
                             "body": _redact(r.text[:300])}
            except Exception as e:
                out[name] = {"error": str(e)[:200]}
        return out

    def valuations(self, account_id):
        self.switch(account_id)
        r = self._get("valuations", VALUATIONS)
        try:
            data = r.json()
            return json.loads(data) if isinstance(data, str) else data
        except ValueError:
            raise PlatformError("valuations", "response is not JSON", r.text)

    def holdings(self, account_id, with_shares=True):
        self.switch(account_id)
        r = self._get("holdings", HOLDINGS)
        rows = parse_holdings(r.text)
        if not rows and "holding-inner-container" not in r.text and "No holdings" not in r.text:
            # Either an empty account or a changed layout; keep the page so we can tell which.
            raise PlatformError("holdings", "could not read the holdings table (layout changed?)", r.text)
        if with_shares:
            for h in rows:
                if not h["view_url"]:
                    continue
                try:
                    h["shares"] = parse_shares(self._get("holding detail", h["view_url"]).text)
                except PlatformError:
                    h["shares"] = None  # value still comes through; units are a nice-to-have
        return rows

    def transactions(self, account_id):
        self.switch(account_id)
        r = self._get("transactions", TRANSACTIONS)
        try:
            return r.json()
        except ValueError:
            raise PlatformError("transactions", "response is not JSON", r.text)


def is_demo(name):
    return "demo" in (name or "").lower()


def valuation_total(valuation):
    """Best effort 'account value' from the valuations JSON."""
    if not isinstance(valuation, dict):
        return None
    top = valuation.get("TopSummary") or {}
    for key in ("AccountValue", "TotalValue", "AccountValueNumeric"):
        if key in top:
            v = money(top[key])
            if v is not None:
                return v
    for item in top.get("AccountValues", []) if isinstance(top.get("AccountValues"), list) else []:
        v = money(item.get("Value"))
        if v is not None:
            return v
    return None


def snapshot(username, password, base_url=BASE_URL, client=None, ep_fetch=None):
    """Log in and read every account. Raises PlatformError; returns a JSON-safe dict."""
    p = client or Platform(base_url)
    p.login(username, password)
    out, previous = [], None
    for acc in p.accounts():
        if is_demo(acc["name"]):
            continue  # EasyEquities' practice accounts aren't real money
        item = {**acc, "holdings": [], "valuation": None, "transactions": [], "warnings": []}
        try:
            item["holdings"] = p.holdings(acc["id"])
        except PlatformError as e:
            # One account with an unrecognised holdings page (empty/new account, wallet-only, odd layout) must not
            # fail the whole connection: the login worked, so keep the account and note what we couldn't read.
            item["warnings"].append(str(e))
            log.warning("EasyEquities holdings unreadable: account=%r id=%s stage=%s msg=%s shape=%s",
                        acc.get("name"), acc.get("id"), e.stage, e.message, page_shape(e.page))
        names = sorted(h["name"] for h in item["holdings"])
        if names and names == previous or "properties" in acc["name"].lower():
            item["switch_debug"] = p.switch_debug(acc["id"])
        if names and names == previous:
            # EasyEquities kept showing the previous account (seen with EasyProperties); don't copy its holdings.
            item["holdings"] = []
            item["warnings"].append("holdings: EasyEquities showed the previous account's holdings instead of this one's")
        previous = names or previous
        for stage, fn in (("valuation", p.valuations), ("transactions", p.transactions)):
            try:
                item[stage] = fn(acc["id"])
            except PlatformError as e:
                item["warnings"].append(str(e))
        holdings_value = sum(h["current_value"] or 0 for h in item["holdings"])
        item["value"] = valuation_total(item["valuation"]) or holdings_value
        item["holdings_value"] = holdings_value
        item["purchase_value"] = sum(h["purchase_value"] or 0 for h in item["holdings"])
        out.append(item)
    snap = {"accounts": out, "statement_links": p.statement_links}
    try:
        snap["statements"] = p.statements()
    except PlatformError as e:
        snap["statements"] = []
        snap["statements_error"] = str(e)
    from . import properties

    try:
        ep = (ep_fetch or properties.fetch)(username, password)
    except PlatformError as e:
        ep = {"holdings": [], "error": str(e), "page": forms(e.page) if e.page else {}}
    except Exception as e:  # EasyProperties must never break the EasyEquities read
        ep = {"holdings": [], "error": f"unexpected: {str(e)[:300]}"}
    snap["easyproperties"] = {k: v for k, v in ep.items() if k != "holdings"}
    if ep["holdings"]:
        wallet = next((a for a in out if "properties" in a["name"].lower()), None)
        if wallet is None:
            wallet = {"id": "easyproperties", "name": "EasyProperties ZAR", "currency_id": "", "valuation": None,
                      "transactions": [], "warnings": [], "value": 0.0, "holdings_value": 0.0, "purchase_value": 0.0}
            out.append(wallet)
        cash = max((wallet.get("value") or 0) - (wallet.get("holdings_value") or 0), 0.0)
        wallet["holdings"] = ep["holdings"]
        wallet["warnings"] = [w for w in wallet["warnings"] if "previous account" not in w]
        wallet["holdings_value"] = sum(h["current_value"] or 0 for h in ep["holdings"])
        wallet["purchase_value"] = sum(h["purchase_value"] or 0 for h in ep["holdings"])
        wallet["value"] = wallet["holdings_value"] + cash
    return snap
