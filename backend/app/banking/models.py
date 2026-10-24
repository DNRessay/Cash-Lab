import datetime as dt
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..invest.models import user_fk
from ..models import BigId, created, pk, text


class BankStatement(Base):
    """A bank statement PDF found in Gmail. Its text is kept so it can be re-parsed without downloading again."""

    __tablename__ = "bank_statements"
    __table_args__ = (UniqueConstraint("user_id", "gmail_id", "filename"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    gmail_id: Mapped[str] = text(40)
    filename: Mapped[str] = text(300)
    subject: Mapped[str] = text(400)
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    bank: Mapped[str] = text(30)
    account: Mapped[str] = text(40)  # "Capitec ••1234"
    kind: Mapped[str] = text(10)  # bank | credit | loan
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = text(20)  # ok | error | locked
    error: Mapped[str] = text()
    rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    closing_balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    closing_date: Mapped[Optional[dt.date]] = mapped_column(nullable=True)
    read_at: Mapped[datetime] = created()


class BankTxn(Base):
    __tablename__ = "bank_txns"
    __table_args__ = (UniqueConstraint("user_id", "key"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    statement_id: Mapped[int] = mapped_column(BigId, ForeignKey("bank_statements.id", ondelete="CASCADE"), nullable=False,
                                              index=True)
    key: Mapped[str] = text(64)  # same line in two overlapping statements is stored once
    bank: Mapped[str] = text(30)
    account: Mapped[str] = text(40)
    date: Mapped[dt.date] = mapped_column(nullable=False, index=True)
    description: Mapped[str] = text(400)
    amount: Mapped[float] = mapped_column(Float, nullable=False)  # + in, - out
    balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    category: Mapped[str] = text(40)
    fee: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # bank charge on this line (positive)


class BankAccount(Base):
    """Latest known balance per account. kind decides whether a balance counts as money or as debt."""

    __tablename__ = "bank_accounts"
    __table_args__ = (UniqueConstraint("user_id", "account"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    bank: Mapped[str] = text(30)
    account: Mapped[str] = text(40)
    name: Mapped[str] = text(80)
    kind: Mapped[str] = text(10)  # bank | credit | loan
    kind_set: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # user chose kind; don't guess over it
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # left out of net worth
    balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    balance_date: Mapped[Optional[dt.date]] = mapped_column(nullable=True)


class Liability(Base):
    """Debt typed in by hand (car finance, personal loan, money owed to someone)."""

    __tablename__ = "liabilities"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    name: Mapped[str] = text(120)
    kind: Mapped[str] = text(20)  # loan | credit | vehicle | store | personal | other
    balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # yearly interest, %
    monthly: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = text()
    updated_at: Mapped[datetime] = created()


class BankCategoryRule(Base):
    """'checkers' -> Groceries. From your own fixes ('you') or confident AI answers ('ai'); yours always win."""

    __tablename__ = "bank_category_rules"
    __table_args__ = (UniqueConstraint("user_id", "keyword"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    keyword: Mapped[str] = text(80)
    category: Mapped[str] = text(40)
    source: Mapped[str] = text(10)  # you | ai
    created_at: Mapped[datetime] = created()
