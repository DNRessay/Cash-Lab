# Bank transaction categories: your rules first, then keyword lists (whole-word
# matches), then Groq for what's left. Confident Groq answers and your own fixes become rules for next time.
import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import llm
from ..config import settings
from .models import BankCategoryRule, BankTxn

log = logging.getLogger(__name__)

# (category, applies to "in" | "out" | "any", keywords). Order matters: first match wins.
DEFAULTS = [
    ("Bank fees", "out", "service fee,bank charge,atm fee,monthly fee,admin fee,monthly account admin,account maintenance,"
                         "sms notification,sms payment notification,sms fee,card fee,statement fee,print statement,"
                         "stop payment,unpaid debit,dishonour,penalty fee,returned item,debit order fee,dispute fee,"
                         "insufficient funds,debicheck insufficient,branch card replacement,cash withdrawal fee,fee"),
    ("Transfers", "any", "transfer to,transfer from,own account,internal transfer,transfer between accounts,goalsave,"
                         "savings pocket,live better,round-up,round up,savings round,easyequities,first world trader,"
                         "investment,tfsa"),
    ("Interest", "in", "earned interest,interest earned,interest credited,interest paid,savings interest,interest"),
    # Cash put in at an ATM or branch is usually your own money going back in, not new income.
    ("Cash deposit", "in", "atm deposit,cash deposit,deposit at atm,atm cash deposit,notes deposit,cash dep"),
    ("Income", "in", "salary,payroll,wages,commission,bonus,dividend,refund,payshap payment received,payment received,"
                     "received from,deposit"),
    # Before the shop and fuel lists: "ATM Withdrawal at ENGEN WINTERVD" is cash from an ATM at a garage, not fuel.
    ("Cash", "out", "atm withdrawal,atm cash,cash withdrawal,cash sent,atm"),
    ("Groceries", "out", "supermarket,checkers,woolworths,pick n pay,pnp,spar,shoprite,food lover,usave,makro,boxer,"
                         "spaza,tuck shop,liquor,tops"),
    ("Transport", "out", "caltex,shell,sasol,engen,totalenergies,bp,astron,fuel,petrol,uber,bolt,taxi,gautrain,"
                         "metrobus,intercape,greyhound,toll,sanral,parking"),
    ("Eating out", "out", "nando,nandos,kfc,mcdonalds,mcdonald,steers,wimpy,debonairs,fishaways,chicken licken,burger king,"
                          "romans,pizza,restaurant,cafe,bakery,coffee,mugg,ocean basket,spur,uber eats,mr d"),
    ("Airtime & data", "out", "vodacom,mtn,telkom,cell c,airtime,data bundle,prepaid,recharge,rain,afrihost,webafrica"),
    ("Subscriptions", "out", "netflix,showmax,dstv,spotify,apple music,apple.com,youtube,amazon prime,disney,google,"
                             "microsoft,openai,anthropic,claude,playstation,xbox"),
    ("Utilities", "out", "eskom,city power,municipality,rates,water,electricity,prepaid electricity,tshwane,joburg,"
                         "ekurhuleni,city of cape town"),
    ("Health", "out", "dischem,dis-chem,pharmacy,clicks,clinic,hospital,doctor,dentist,optometrist,medirite,"
                      "discovery health,bonitas,momentum health"),
    ("Insurance", "out", "insurance,assurance,sanlam,old mutual,outsurance,miway,hollard,funeral,liberty,king price"),
    ("Debt", "out", "loan,credit card,repayment,instalment,finance,rcs,edgars account,truworths account,mr price money"),
    ("Shopping", "out", "takealot,amazon,mr price,ackermans,pep,jet,h&m,zara,edgars,truworths,foschini,sportsmans,"
                        "builders,leroy merlin,game,incredible,temu,shein"),
    ("Payments", "out", "immediate payment,capitec pay,snapscan,zapper,payfast,ewallet,send money,payshap"),
]
NAMES = [n for n, _, _ in DEFAULTS] + ["Other", "Other income"]
_compiled = [(name, side, re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(k.strip()) for k in kws.split(",") if k.strip())
                                      + r")(?![a-z0-9])")) for name, side, kws in DEFAULTS]
AI_BATCH = 40
MIN_CONFIDENCE = 0.6


def rules_for(db: Session, user_id: int):
    return [(r.keyword, r.category) for r in db.scalars(select(BankCategoryRule).where(BankCategoryRule.user_id == user_id)
                                                        .order_by(BankCategoryRule.source.desc(), BankCategoryRule.id.desc()))]


def categorise(description: str, amount: float = 0.0, rules=()) -> str:
    """Your rules ('you' before 'ai'), then the keyword lists; 'Other' / 'Other income' when nothing fits."""
    low = " " + re.sub(r"\s+", " ", (description or "").lower()) + " "
    for keyword, category in rules:
        if keyword and keyword in low:
            return category
    side = "in" if amount > 0 else "out"
    for name, applies, rx in _compiled:
        if applies in (side, "any") and rx.search(low):
            return name
    return "Other income" if amount > 0 else "Other"


def merchant_key(description: str) -> str:
    """'POS Purchase Checkers Sandton 12345' -> 'checkers sandton': the words that name who it was."""
    words = re.sub(r"[^a-z& ]+", " ", description.lower()).split()
    noise = {"pos", "purchase", "payment", "card", "debit", "credit", "order", "online", "local", "international",
             "banking", "app", "to", "from", "ref", "the", "and", "for", "of", "pty", "ltd", "za", "sa", "fee", "via"}
    keep = [w for w in words if w not in noise and len(w) > 1]
    return " ".join(keep[:2])


def recategorise(db: Session, user_id: int):
    """Re-apply rules and keywords to every transaction (after rules change)."""
    rules = rules_for(db, user_id)
    changed = 0
    for t in db.scalars(select(BankTxn).where(BankTxn.user_id == user_id)):
        new = categorise(t.description, t.amount, rules)
        if new != t.category:
            t.category = new
            changed += 1
    db.commit()
    return changed


# Words that appear on many unrelated lines: never a rule on their own.
GENERIC = {"payment", "payments", "eft", "payshap", "transfer", "debit", "credit", "card", "pos", "purchase", "immediate",
           "online", "send", "sent", "received", "money", "cash", "deposit", "fee", "fees", "shop", "store", "order",
           "capitec", "tymebank", "gotyme", "fnb", "absa", "nedbank", "standard bank", "bank", "account", "ref"}


def learn(db: Session, user_id: int, keyword: str, category: str, source: str):
    keyword = re.sub(r"\d+$", "", keyword.strip().lower()).strip()  # 'slovosupermarket5' -> 'slovosupermarket'
    if len(keyword) < 4 or keyword in GENERIC or re.fullmatch(r"[\d\W]+", keyword):
        return
    row = db.scalar(select(BankCategoryRule).where(BankCategoryRule.user_id == user_id, BankCategoryRule.keyword == keyword))
    if row and row.source == "you" and source == "ai":
        return  # never override the person's own choice
    row = row or BankCategoryRule(user_id=user_id, keyword=keyword)
    row.category, row.source = category, source
    db.add(row)
    db.flush()  # so the next lookup in this batch sees it


def ai_pass(db: Session, user_id: int, max_batches=10):
    """Groq for 'Other'/'Other income' lines, one merchant at a time; confident answers become rules."""
    if not settings.groq_api_keys:
        return {"ai": 0, "left": None}
    left = list(db.scalars(select(BankTxn).where(BankTxn.user_id == user_id, BankTxn.category.in_(("Other", "Other income")))))
    groups = {}
    for t in left:
        groups.setdefault((merchant_key(t.description) or t.description.lower()[:40], t.amount > 0), []).append(t)
    keys = list(groups)
    done = 0
    system = ("You categorise South African personal bank transactions. Pick the single best category for each from the "
              "allowed list only, or \"Other\" if nothing fits. Also return the merchant keyword from the description "
              "(lowercase, copied verbatim) that justifies it. Reply as JSON: {\"results\": [{\"i\": <index>, "
              "\"category\": \"<name>\", \"confidence\": <0-1>, \"keyword\": \"<word>\"}]}")
    allowed = {n.lower(): n for n in NAMES}
    for b in range(0, min(len(keys), AI_BATCH * max_batches), AI_BATCH):
        batch = keys[b:b + AI_BATCH]
        lines = "\n".join(f"{i}. {'IN' if incoming else 'OUT'} {groups[k][0].description[:90]}"
                          for i, k in enumerate(batch) for incoming in [k[1]])
        try:
            data = llm.parse_json(llm.groq([{"role": "system", "content": system},
                                            {"role": "user", "content": f"Allowed: {json.dumps(NAMES)}\n\n{lines}"}],
                                           max_tokens=2500, json_mode=True)) or {}
        except llm.AIError as e:
            log.warning("AI categorise stopped: %s", e)
            break
        for r in data.get("results", []) if isinstance(data, dict) else []:
            try:
                i, conf = int(r.get("i")), float(r.get("confidence", 0))
            except (TypeError, ValueError):
                continue
            name = allowed.get(str(r.get("category", "")).lower())
            if not name or name in ("Other", "Other income") or conf < MIN_CONFIDENCE or not 0 <= i < len(batch):
                continue
            txns = groups[batch[i]]
            for t in txns:
                t.category = name
            done += len(txns)
            kw = str(r.get("keyword") or "").lower().strip()
            if kw and kw in txns[0].description.lower():
                learn(db, user_id, kw, name, "ai")
        db.commit()
    return {"ai": done, "left": len(left) - done}


def tidy(db: Session, user_id: int):
    from .reader import SUMMARY_RE, redetect_kinds

    # Statement summary lines ("Summary Opening balance") read as payments by older parsers.
    for t in db.scalars(select(BankTxn).where(BankTxn.user_id == user_id)):
        if SUMMARY_RE.search(t.description or ""):
            db.delete(t)
    db.flush()
    redetect_kinds(db, user_id)
    changed = recategorise(db, user_id)
    return {"rules_and_keywords": changed, **ai_pass(db, user_id)}
