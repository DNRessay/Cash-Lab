# EasyProperties: an Angular app backed by an API. Login is OAuth2 code + PKCE through EasyID (identity.openeasy.io),
# the same flow the EasyProperties site runs in the browser. Values below come from that site's public app bundle.
import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from .platform import SESSION_KW, PlatformError, http, money

API = "https://apigateway.openeasy.io/property-client-rest/v1"
IDP = "https://identity.openeasy.io"
CLIENT_ID = "33e0a18d2e654488bfb950170d258988"
SCOPE = "openid platform profile ep_userinfo api_gateway auction_rest_api account_api properties_api referrals_api"
REDIRECT = "https://platform.easyproperties.co.za/account/authorize"
EP_CURRENCY_ID = 66  # the EasyProperties rand account, as the EasyProperties app picks it


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(username, password, session=None):
    """Returns (session, access_token). Raises PlatformError naming the step that failed."""
    s = session or http.Session(**SESSION_KW)
    verifier, challenge = _pkce()
    query = urlencode({"client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPE,
                       "code_challenge": challenge, "code_challenge_method": "S256",
                       "state": secrets.token_urlsafe(16), "nonce": secrets.token_urlsafe(16)})
    try:
        r = s.get(f"{IDP}/connect/authorize?{query}", timeout=30)
    except Exception as e:
        raise PlatformError("easyproperties login page", f"request failed ({e})")
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", id="loginForm") or soup.find("form")
    if not form:
        raise PlatformError("easyproperties login page", "no login form found", r.text)
    data = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
    # "button=login" is what the Login button submits; without it EasyID treats the form as cancelled (access_denied).
    data.update({"Username": username, "Password": password, "IsUsernameProvided": "true", "button": "login"})
    page_url = str(getattr(r, "url", "") or f"{IDP}/Account/Login")
    action = urljoin(page_url, form.get("action") or page_url)

    url, method, body = action, "post", data
    for _ in range(8):  # follow redirects by hand until EasyID sends us back with ?code=
        try:
            r = s.post(url, data=body, allow_redirects=False, timeout=30) if method == "post" else \
                s.get(url, allow_redirects=False, timeout=30)
        except Exception as e:
            raise PlatformError("easyproperties login", f"request failed ({e})")
        location = r.headers.get("location") or ""
        if location.startswith(REDIRECT):
            parsed = urlparse(location)
            params = {**parse_qs(parsed.fragment), **parse_qs(parsed.query)}  # code may come in the query or fragment
            code = params.get("code", [""])[0]
            if not code:
                detail = {k: v[0][:120] for k, v in params.items() if k in ("error", "error_description", "error_uri")}
                raise PlatformError("easyproperties login",
                                    f"EasyID returned without a code ({detail or 'keys: ' + ','.join(params)})")
            break
        if r.status_code in (301, 302, 303, 307, 308) and location:
            url, method, body = urljoin(url, location), "get", None
            continue
        page = r.text or ""
        hint = "captcha or extra verification required" if re.search(r"captcha|recaptcha|one.time|otp", page, re.I) \
            else "username/password not accepted"
        raise PlatformError("easyproperties login", f"EasyID did not sign in (HTTP {r.status_code}: {hint})", page)
    else:
        raise PlatformError("easyproperties login", "too many redirects")

    try:
        r = s.post(f"{API}/user/accesstoken", json={"authorizationCode": code, "codeVerifier": verifier,
                                                    "redirectUri": REDIRECT}, timeout=30)
        tokens = r.json()
    except Exception as e:
        raise PlatformError("easyproperties token", f"token exchange failed ({e})")
    token = _first(tokens, ("accessToken", "access_token", "AccessToken", "token"))
    if not token:
        raise PlatformError("easyproperties token", f"no access token in reply (keys: {shape(tokens, 1)})")
    return s, token


def _first(d, keys):
    if isinstance(d, dict):
        for k in keys:
            if d.get(k):
                return d[k]
        for v in d.values():
            if isinstance(v, dict):
                found = _first(v, keys)
                if found:
                    return found
    return None


def shape(obj, depth=3):
    """Keys only (no values) of a JSON reply, to learn its structure from the logs."""
    if depth <= 0:
        return "…"
    if isinstance(obj, dict):
        return {k: shape(v, depth - 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, list):
        return [shape(obj[0], depth - 1), f"x{len(obj)}"] if obj else []
    return type(obj).__name__


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    return money(v) if isinstance(v, str) else None


def _pick(d, *keys):
    for k in keys:
        for cand in (k, k[0].upper() + k[1:]):
            if isinstance(d, dict) and d.get(cand) not in (None, ""):
                return d[cand]
    return None


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def extract_holdings(*replies):
    """Find property holdings in whatever shape the API returns. Prices on EasyProperties are in cents."""
    out, seen = [], set()
    for reply in replies:
        for d in _walk(reply):
            holding = d.get("holding") if isinstance(d.get("holding"), dict) else None
            prop = d.get("property") if isinstance(d.get("property"), dict) else None
            if holding is None and prop is not None and _pick(d, "quantity", "units", "shares", "totalShares"):
                holding, source = d, prop
            elif holding is not None:
                source = prop or d
            else:
                continue
            name = _pick(source, "title", "name", "propertyName") or _pick(d, "title", "name")
            qty = _num(_pick(holding, "quantity", "units", "shares", "totalShares", "sharesHeld", "volume"))
            if not name or not qty or name in seen:
                continue
            value = _num(_pick(holding, "currentValue", "marketValue", "value", "totalValue"))
            price_c = _num(_pick(holding, "lastPrice", "currentPrice", "price") or _pick(source, "lastPrice", "currentPrice"))
            vwap_c = _num(_pick(holding, "vwap", "averagePrice", "avgPrice"))
            if value is None and price_c is not None:
                value = qty * price_c / 100
            cost = _num(_pick(holding, "purchaseValue", "costValue", "totalCost", "cost"))
            if cost is None and vwap_c is not None:
                cost = qty * vwap_c / 100
            fin = source.get("financialInfo") if isinstance(source.get("financialInfo"), dict) else {}
            yld = _num(_pick(source, "rentalYield", "dividendYield") or _pick(fin, "rentalYield", "dividendYield",
                                                                              "grossYield", "netYield"))
            seen.add(name)
            out.append({"name": str(name)[:200], "contract_code": str(_pick(source, "contractCode") or ""),
                        "shares": qty, "current_value": value, "purchase_value": cost,
                        "current_price": (value / qty) if value is not None and qty else None,
                        # Yields run 0-3%: 0.89 means 0.89%, while a fraction would be tiny (0.0089).
                        "rental_yield": (yld / 100 if yld is not None and yld >= 0.05 else yld),
                        "view_url": "", "isin": ""})
    return out


def jwt_claims(token):
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def _items(detail):
    """The account's property holdings: `properties` in the EasyProperties app, or any list of dicts naming a property."""
    if isinstance(detail, dict):
        for key in ("properties", "holdings", "propertyHoldings"):
            if isinstance(detail.get(key), list):
                return detail[key]
    for d in _walk(detail):
        for v in d.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and ("property" in v[0] or "propertyId" in v[0]):
                return v
    return []


def _cents_to_rand(v):
    # EasyProperties quotes share prices in cents (IPOs list at 100c = R1).
    return v / 100 if v is not None and v > 10 else v


def holdings_from_account(detail, catalogue):
    by_id = {str(p.get("id")): p for p in catalogue or [] if isinstance(p, dict)}
    out = []
    for item in _items(detail):
        prop = item.get("property") if isinstance(item.get("property"), dict) else {}
        pid = str(prop.get("id") or item.get("propertyId") or "")
        cat = by_id.get(pid, {})
        fin = cat.get("financialInfo") if isinstance(cat.get("financialInfo"), dict) else {}
        h = item.get("holding") if isinstance(item.get("holding"), dict) else item
        name = _pick(cat, "name", "title") or _pick(prop, "name", "title") or _pick(item, "name", "title")
        qty = _num(_pick(h, "quantity", "shares", "totalShares", "numberOfShares", "units", "sharesHeld", "volume",
                         "availableShares"))
        if not name or not qty:
            continue
        price = _cents_to_rand(_num(_pick(h, "lastPrice", "currentPrice", "price") or _pick(fin, "sharePrice")))
        value = _num(_pick(h, "currentValue", "marketValue", "value", "totalValue", "holdingValue"))
        if value is None and price is not None:
            value = qty * price
        cost = _num(_pick(h, "purchaseValue", "costValue", "totalCost", "cost", "investmentValue"))
        vwap = _cents_to_rand(_num(_pick(h, "vwap", "averagePrice", "avgPrice")))
        if cost is None and vwap is not None:
            cost = qty * vwap
        yld = _num(_pick(item, "rentalYield") or _pick(fin, "rentalYieldPercentage", "grossRentalYieldPercentage")
                   or _pick(prop, "rentalYield"))
        out.append({"name": str(name)[:200], "contract_code": str(_pick(cat, "contractCode") or _pick(prop, "contractCode") or ""),
                    "shares": qty, "current_value": value, "purchase_value": cost,
                    "current_price": value / qty if value is not None else price,
                    # Yields run 0-3%: 0.89 means 0.89%, while a fraction would be tiny (0.0089).
                    "rental_yield": (yld / 100 if yld is not None and yld >= 0.05 else yld), "view_url": "", "isin": "",
                    "rental_income": _num(_pick(item, "rentalIncomeTotal")),
                    "monthly_costs": _num(_pick(item, "monthlyCosts"))})
    return out


def fetch(username, password, session=None):
    """Your EasyProperties holdings, the way the EasyProperties app loads them. Also returns reply shapes (keys only)."""
    s, token = login(username, password, session)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    shapes = {}

    def call(method, path, **kw):
        try:
            r = (s.post if method == "post" else s.get)(API + path, headers=headers, timeout=30, **kw)
        except Exception as e:
            raise PlatformError("easyproperties api", f"{path}: request failed ({e})")
        if r.status_code != 200:
            shapes[path] = f"HTTP {r.status_code}"
            raise PlatformError("easyproperties api", f"{path}: HTTP {r.status_code}", r.text)
        data = r.json()
        shapes[path.split("/")[1] + "/" + path.split("/")[2] if path.count("/") > 1 else path] = shape(data)
        return data

    user_id = jwt_claims(token).get("userid")
    if not user_id:
        try:
            user_id = s.get(f"{IDP}/connect/userinfo", headers=headers, timeout=30).json().get("userid")
        except Exception:
            user_id = None
    if not user_id:
        raise PlatformError("easyproperties api", "no userid in the EasyID token or userinfo")
    accounts = call("get", f"/user/accounts/{user_id}")
    accounts = accounts if isinstance(accounts, list) else _pick(accounts, "trustaccounts", "accounts") or []
    ep = next((a for a in accounts if str(a.get("tradingCurrencyId")) == str(EP_CURRENCY_ID)), accounts[0] if accounts else None)
    if not ep:
        raise PlatformError("easyproperties api", "no EasyProperties account found")
    detail = call("post", "/user/account", json={"userId": user_id, "trustAccountId": str(ep.get("trustAccountId"))})
    try:
        catalogue = call("get", "/property/all")
    except PlatformError:
        catalogue = []
    holdings = holdings_from_account(detail, catalogue) or extract_holdings(detail)
    return {"holdings": holdings, "source": "api", "shapes": shapes,
            "account_value": _num(_pick(detail, "trustAccountValue")) if isinstance(detail, dict) else None}
