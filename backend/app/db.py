from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

LOCAL_HOSTS = {None, "", "localhost", "127.0.0.1"}


def build_engine(url: str):
    url = url.strip()
    if url.startswith("mysql://"):
        url = "mysql+pymysql://" + url[len("mysql://"):]
    elif url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]

    u = make_url(url)
    connect_args = {}

    if u.drivername.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        return create_engine(u, connect_args=connect_args)

    if u.drivername.startswith("mysql"):
        # Aiven-style URLs carry ?ssl-mode=REQUIRED which PyMySQL doesn't understand.
        u = u.difference_update_query(["ssl-mode", "ssl_mode", "sslmode"])
        if u.host not in LOCAL_HOSTS:
            ssl = {"ca": settings.mysql_ssl_ca} if settings.mysql_ssl_ca else {"check_hostname": False}
            connect_args["ssl"] = ssl

    if u.drivername.startswith("postgresql"):
        # Neon's free tier sleeps when idle; give the first connection time to wake it.
        connect_args["connect_timeout"] = 15

    return create_engine(u, connect_args=connect_args, pool_pre_ping=True, pool_recycle=280)


engine = build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db():
    from . import models  # noqa: F401
    from .invest import models as invest_models  # noqa: F401
    from .invest.ee import models as ee_models  # noqa: F401
    from .banking import models as bank_models  # noqa: F401
    from .invest import blog, jobs, pulse  # noqa: F401
    from .ai import router as ai  # noqa: F401
    from . import onboarding  # noqa: F401
    from . import mcp  # noqa: F401

    Base.metadata.create_all(engine)
