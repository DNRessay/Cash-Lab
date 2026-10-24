# Personal investing tracker. Everything here belongs to one user and is never shared with their team.
import datetime as dt
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import BigId, created, pk, text, utcnow

Qty = Numeric(18, 6)
Price = Numeric(18, 4)
Money = Numeric(15, 2)

KINDS = ("buy", "sell", "dividend", "deposit", "withdrawal", "fee", "interest")
ASSET_CLASSES = ("share", "etf", "reit", "easyproperties", "crypto", "other")


def user_fk():
    return mapped_column(BigId, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)


class InvestTxn(Base):
    __tablename__ = "invest_transactions"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    kind: Mapped[str] = text(20)
    symbol: Mapped[str] = text(30)  # Yahoo symbol, e.g. GRT.JO; empty for cash movements
    name: Mapped[str] = text(200)
    asset_class: Mapped[str] = text(20, "share")
    quantity: Mapped[Optional[Decimal]] = mapped_column(Qty, nullable=True)
    price: Mapped[Optional[Decimal]] = mapped_column(Price, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)  # rand value, always positive
    fees: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    notes: Mapped[str] = text()
    source: Mapped[str] = text(20, "manual")
    created_at: Mapped[datetime] = created()


class WatchItem(Base):
    __tablename__ = "invest_watchlist"
    __table_args__ = (UniqueConstraint("user_id", "symbol"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    symbol: Mapped[str] = text(30)
    name: Mapped[str] = text(200)
    asset_class: Mapped[str] = text(20, "share")
    alert_above: Mapped[Optional[Decimal]] = mapped_column(Price, nullable=True)
    alert_below: Mapped[Optional[Decimal]] = mapped_column(Price, nullable=True)
    last_alert_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = text()
    created_at: Mapped[datetime] = created()


class ManualPrice(Base):
    """For things Yahoo doesn't price, e.g. EasyProperties shares."""

    __tablename__ = "invest_manual_prices"
    __table_args__ = (UniqueConstraint("user_id", "symbol"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    symbol: Mapped[str] = text(30)
    price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    as_of: Mapped[dt.date] = mapped_column(Date, default=dt.date.today, nullable=False)


class PropertyAsset(Base):
    __tablename__ = "invest_properties"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    name: Mapped[str] = text(200)
    kind: Mapped[str] = text(20, "house")  # house | flat | land | commercial
    purchase_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    purchase_price: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    valuation: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    valuation_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    bond_balance: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    monthly_bond_payment: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    monthly_rent: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    monthly_costs: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)  # levies, rates, insurance, upkeep
    notes: Mapped[str] = text()
    created_at: Mapped[datetime] = created()


class PriceCache(Base):
    """Last Yahoo quote + ~5y daily closes per symbol, shared by all users (it's public market data)."""

    __tablename__ = "invest_price_cache"
    symbol: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = text(200)
    currency: Mapped[str] = text(10)
    price: Mapped[Optional[Decimal]] = mapped_column(Price, nullable=True)
    dividends_12m: Mapped[Decimal] = mapped_column(Price, default=0, nullable=False)
    history: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # [[iso_date, close], ...]
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class DividendHistory(Base):
    """Past dividends per symbol from Yahoo (~5 years of ex-dates), for the dividend calendar."""

    __tablename__ = "invest_dividend_history"
    symbol: Mapped[str] = mapped_column(String(30), primary_key=True)
    events: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # [[ex_date_iso, amount], ...]
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class PortfolioSnapshot(Base):
    """One row per user per day: what the portfolio was worth, for the 'worth over time' chart."""

    __tablename__ = "invest_snapshots"
    __table_args__ = (UniqueConstraint("user_id", "date"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    value: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    invested: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    net_worth: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
