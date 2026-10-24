from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

BigId = BigInteger().with_variant(Integer, "sqlite")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def pk():
    return mapped_column(BigId, primary_key=True, autoincrement=True)


def text(length=None, default=""):
    return mapped_column(String(length) if length else Text, default=default, nullable=False)


def created():
    return mapped_column(DateTime, default=utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = pk()
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    first_name: Mapped[str] = text(150)
    last_name: Mapped[str] = text(150)
    password: Mapped[str] = text(128)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    date_joined: Mapped[datetime] = created()
