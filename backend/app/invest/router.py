import datetime as dt
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import current_user, get_db
from ..models import User
from . import importer, portfolio, prices
from .models import ASSET_CLASSES, KINDS, InvestTxn, ManualPrice, PropertyAsset, WatchItem

router = APIRouter(prefix="/api/invest", tags=["investments"])


def mine(db, model, obj_id, user):
    obj = db.get(model, obj_id)
    if not obj or obj.user_id != user.id:
        raise HTTPException(404, "Not found.")
    return obj


def _blank_none(v):
    return None if v in ("", None) else v


# ── Schemas ─────────────────────────────────────────────────────────────────

class TxnIn(BaseModel):
    date: dt.date
    kind: str
    symbol: str = ""
    name: str = ""
    asset_class: str = "share"
    quantity: Optional[Decimal] = None
    price: Optional[Decimal] = None
    amount: Optional[Decimal] = None
    fees: Decimal = Decimal(0)
    notes: str = ""

    _blank = field_validator("quantity", "price", "amount", "fees", mode="before")(_blank_none)

    @field_validator("kind")
    @classmethod
    def ok_kind(cls, v):
        if v not in KINDS:
            raise ValueError(f"must be one of {', '.join(KINDS)}")
        return v

    @field_validator("asset_class")
    @classmethod
    def ok_class(cls, v):
        return v if v in ASSET_CLASSES else "other"


class WatchIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    asset_class: str = "share"
    alert_above: Optional[Decimal] = None
    alert_below: Optional[Decimal] = None
    notes: str = ""

    _blank = field_validator("alert_above", "alert_below", mode="before")(_blank_none)


class WatchPatch(BaseModel):
    alert_above: Optional[Decimal] = None
    alert_below: Optional[Decimal] = None
    notes: Optional[str] = None

    _blank = field_validator("alert_above", "alert_below", mode="before")(_blank_none)


class PropertyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = "house"
    purchase_date: Optional[dt.date] = None
    purchase_price: Decimal = Decimal(0)
    valuation: Decimal = Decimal(0)
    valuation_date: Optional[dt.date] = None
    bond_balance: Decimal = Decimal(0)
    monthly_bond_payment: Decimal = Decimal(0)
    monthly_rent: Decimal = Decimal(0)
    monthly_costs: Decimal = Decimal(0)
    notes: str = ""

    @field_validator("purchase_date", "valuation_date", mode="before")
    @classmethod
    def blank_date(cls, v):
        return v or None

    @field_validator("purchase_price", "valuation", "bond_balance", "monthly_bond_payment", "monthly_rent",
                     "monthly_costs", mode="before")
    @classmethod
    def blank_zero(cls, v):
        return v if v not in ("", None) else 0


class ManualPriceIn(BaseModel):
    price: Decimal = Field(gt=0)


def txn_out(t: InvestTxn):
    return {"id": t.id, "date": t.date, "kind": t.kind, "symbol": t.symbol, "name": t.name,
            "asset_class": t.asset_class, "quantity": t.quantity, "price": t.price, "amount": t.amount,
            "fees": t.fees, "notes": t.notes, "source": t.source}


# ── Portfolio ───────────────────────────────────────────────────────────────

@router.get("/summary")
def summary(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return portfolio.summary(db, user.id)


@router.get("/charts")
def charts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return portfolio.monthly(db, user.id)


@router.get("/money")
def money(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return portfolio.money_picture(db, user.id)


@router.get("/pulse")
def market_pulse(refresh: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import pulse

    names = [h.get("name") or h["symbol"] for h in sorted(portfolio.summary(db, user.id)["holdings"], key=lambda h: -h["value"])
             if h.get("asset_class") != "easyproperties"] if refresh else []
    return pulse.get(db, names, refresh)


def deals_view(db):
    from . import deals, jobs

    job = jobs.view(jobs.latest(db, "deals"))
    return {**deals.annotate(db, deals.get(db), reading=bool(job.get("active"))), "job": job}


@router.get("/deals")
def company_deals(refresh: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import deals

    if refresh:  # the old inline rebuild, kept for the nightly run and API users
        deals.get(db, refresh=True)
    return deals_view(db)


@router.post("/deals/refresh")
def refresh_deals(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Answer at once with the deals already known, and hand the slow part (reading each article) to the worker.
    The page polls GET /deals while `job.active`, and swaps in the finished figures."""
    from . import jobs

    _, started = jobs.start(db, "deals")
    return {**deals_view(db), "started": started}


@router.get("/technical/{symbol}")
def technical_chart(symbol: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import pulse

    return pulse.chart(db, symbol.strip().upper())


@router.get("/quote/{symbol}")
def quote(symbol: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = prices.quote(db, symbol)
    if not row or row.price is None:
        raise HTTPException(404, f"No price found for {symbol.upper()}. JSE shares end in .JO, e.g. GRT.JO.")
    return {"symbol": row.symbol, **prices.stats(row)}


@router.put("/prices/{symbol}")
def set_manual_price(symbol: str, body: ManualPriceIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Own price for something the market feed doesn't cover (e.g. EasyProperties)."""
    symbol = symbol.strip().upper()
    row = db.scalar(select(ManualPrice).where(ManualPrice.user_id == user.id, ManualPrice.symbol == symbol))
    row = row or ManualPrice(user_id=user.id, symbol=symbol)
    row.price, row.as_of = body.price, dt.date.today()
    db.add(row)
    db.commit()
    return {"symbol": symbol, "price": row.price, "as_of": row.as_of}


@router.delete("/prices/{symbol}", status_code=204)
def clear_manual_price(symbol: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.scalar(select(ManualPrice).where(ManualPrice.user_id == user.id, ManualPrice.symbol == symbol.upper()))
    if row:
        db.delete(row)
        db.commit()


# ── Transactions ────────────────────────────────────────────────────────────

@router.get("/transactions")
def list_txns(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(InvestTxn).where(InvestTxn.user_id == user.id)
                      .order_by(InvestTxn.date.desc(), InvestTxn.id.desc()))
    return [txn_out(t) for t in rows]


@router.post("/transactions", status_code=201)
def add_txn(body: TxnIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = body.model_dump()
    data["symbol"] = data["symbol"].strip().upper()
    if body.kind in ("buy", "sell"):
        if not data["symbol"] or not body.quantity:
            raise HTTPException(400, "A buy or sell needs a symbol and a quantity.")
        if body.amount is None and body.price is not None:
            data["amount"] = (body.quantity * body.price).quantize(Decimal("0.01"))
        if data["price"] is None and data["amount"] is not None:
            data["price"] = data["amount"] / body.quantity
        if not data["name"]:
            q = prices.quote(db, data["symbol"])
            data["name"] = q.name if q and q.price is not None else ""
    if data["amount"] is None:
        raise HTTPException(400, "Amount is required.")
    t = InvestTxn(user_id=user.id, **data)
    db.add(t)
    db.commit()
    return txn_out(t)


@router.delete("/transactions/{txn_id}", status_code=204)
def delete_txn(txn_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(mine(db, InvestTxn, txn_id, user))
    db.commit()


@router.post("/transactions/import")
async def import_txns(file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "File is larger than 5 MB.")
    try:
        rows, errors = importer.parse(data, user.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    existing = {(t.date, t.kind, t.symbol, t.amount) for t in
                db.scalars(select(InvestTxn).where(InvestTxn.user_id == user.id))}
    new = [r for r in rows if (r.date, r.kind, r.symbol, r.amount) not in existing]
    db.add_all(new)
    db.commit()
    return {"imported": len(new), "duplicates": len(rows) - len(new), "errors": errors[:20]}


@router.get("/transactions/template")
def template(user: User = Depends(current_user)):
    return Response(importer.TEMPLATE, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=investments_template.csv"})


# ── Watchlist ───────────────────────────────────────────────────────────────

def watch_out(db, w: WatchItem):
    return {"id": w.id, "symbol": w.symbol, "name": w.name, "asset_class": w.asset_class,
            "alert_above": w.alert_above, "alert_below": w.alert_below, "notes": w.notes,
            **prices.stats(prices.quote(db, w.symbol))}


@router.get("/watchlist")
def watchlist(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [watch_out(db, w) for w in db.scalars(select(WatchItem).where(WatchItem.user_id == user.id)
                                                 .order_by(WatchItem.symbol))]


@router.post("/watchlist", status_code=201)
def add_watch(body: WatchIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    symbol = body.symbol.strip().upper()
    if db.scalar(select(WatchItem.id).where(WatchItem.user_id == user.id, WatchItem.symbol == symbol)):
        raise HTTPException(400, f"{symbol} is already on your watchlist.")
    q = prices.quote(db, symbol)
    if not q or q.price is None:
        raise HTTPException(404, f"No price found for {symbol}. JSE shares end in .JO, e.g. GRT.JO.")
    w = WatchItem(user_id=user.id, symbol=symbol, name=q.name, asset_class=body.asset_class,
                  alert_above=body.alert_above, alert_below=body.alert_below, notes=body.notes)
    db.add(w)
    db.commit()
    return watch_out(db, w)


@router.patch("/watchlist/{item_id}")
def update_watch(item_id: int, body: WatchPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    w = mine(db, WatchItem, item_id, user)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(w, k, v if k != "notes" else (v or ""))
    w.last_alert_at = None
    db.commit()
    return watch_out(db, w)


@router.delete("/watchlist/{item_id}", status_code=204)
def delete_watch(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(mine(db, WatchItem, item_id, user))
    db.commit()


# ── Property ────────────────────────────────────────────────────────────────

@router.get("/properties")
def properties(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [portfolio.property_view(p) for p in db.scalars(select(PropertyAsset).where(PropertyAsset.user_id == user.id)
                                                           .order_by(PropertyAsset.name))]


@router.post("/properties", status_code=201)
def add_property(body: PropertyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = PropertyAsset(user_id=user.id, **body.model_dump())
    db.add(p)
    db.commit()
    return portfolio.property_view(p)


@router.put("/properties/{prop_id}")
def update_property(prop_id: int, body: PropertyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = mine(db, PropertyAsset, prop_id, user)
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return portfolio.property_view(p)


@router.delete("/properties/{prop_id}", status_code=204)
def delete_property(prop_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(mine(db, PropertyAsset, prop_id, user))
    db.commit()


# EasyEquities blog: weekly dividends, monthly dividends list
@router.get("/blog")
def blog_view(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import blog

    s = portfolio.summary(db, user.id)
    return blog.view(db, s["holdings"], list(db.scalars(select(WatchItem).where(WatchItem.user_id == user.id))))


@router.get("/calendar")
def dividend_calendar(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import div_calendar

    from . import jobs

    s = portfolio.summary(db, user.id)
    data = div_calendar.view(db, s["holdings"], list(db.scalars(select(WatchItem).where(WatchItem.user_id == user.id))), user.id)
    return {**data, "job": jobs.view(jobs.latest(db, "calendar"))}


@router.post("/calendar/refresh")
def refresh_calendar(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Hand the year-long blog read and dividend-history refresh to the worker; the page polls GET /calendar."""
    from . import jobs

    job, started = jobs.start(db, "calendar")
    return {"job": jobs.view(job), "started": started}


@router.post("/blog/refresh")
def blog_refresh(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import blog

    return blog.refresh(db, limit=6)


class BlogWatchIn(BaseModel):
    instrument: str
    account: str = "ZAR"


@router.post("/blog/watch", status_code=201)
def blog_watch(body: BlogWatchIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Put a blog dividend pick on the watchlist: the name is looked up to a ticker first."""
    from . import blog

    symbol = blog.search_symbol(body.instrument, body.account.upper())
    if not symbol:
        raise HTTPException(404, f"Couldn't find a ticker for {body.instrument}. Add it by symbol instead.")
    return add_watch(WatchIn(symbol=symbol, notes=f"From EasyEquities' dividends update: {body.instrument}"[:500]), user, db)
