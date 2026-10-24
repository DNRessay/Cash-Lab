import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import User, utcnow
from ..services.mailer import send_mail
from . import prices
from .models import WatchItem

log = logging.getLogger(__name__)


def check_all(db: Session):
    """Nightly: refresh watchlist prices and email anyone whose price target was crossed (max once a day each)."""
    items = list(db.scalars(select(WatchItem).where(
        (WatchItem.alert_above.is_not(None)) | (WatchItem.alert_below.is_not(None)))))
    sent = 0
    for w in items:
        q = prices.quote(db, w.symbol, force=True)
        if not q or q.price is None:
            continue
        if w.last_alert_at and utcnow() - w.last_alert_at < timedelta(hours=20):
            continue
        price = q.price
        hit = ("above", w.alert_above) if w.alert_above is not None and price >= w.alert_above else \
              ("below", w.alert_below) if w.alert_below is not None and price <= w.alert_below else None
        if not hit:
            continue
        user = db.get(User, w.user_id)
        if not user or not user.email:
            continue
        send_mail(user.email, f"Price alert: {w.symbol} is {hit[0]} R{hit[1]}",
                  f"{w.name or q.name} ({w.symbol}) is now R{price:.2f}, {hit[0]} your alert of R{hit[1]}.\n\n"
                  "This is an automated price alert from C-Lab, not investment advice.\n")
        w.last_alert_at = utcnow()
        db.commit()
        sent += 1
    return sent
