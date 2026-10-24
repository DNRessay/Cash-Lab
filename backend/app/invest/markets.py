from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..deps import current_user, get_db
from ..models import User
from . import prices

router = APIRouter(prefix="/api/markets", tags=["markets"])

# Yahoo symbols. FX pairs are quoted as rand per unit of foreign currency.
BOARD = [
    ("ZAR=X", "USD/ZAR", "fx"),
    ("EURZAR=X", "EUR/ZAR", "fx"),
    ("GBPZAR=X", "GBP/ZAR", "fx"),
    ("STX40.JO", "JSE Top 40 (Satrix 40)", "index"),
    ("STXPRO.JO", "SA listed property (Satrix Property)", "index"),
    ("GC=F", "Gold (USD/oz)", "commodity"),
    ("^GSPC", "S&P 500", "index"),
]


@router.get("")
def board(user: User = Depends(current_user), db: Session = Depends(get_db)):
    out = []
    for symbol, label, kind in BOARD:
        out.append({"symbol": symbol, "label": label, "kind": kind, **prices.stats(prices.quote(db, symbol))})
    return {"markets": out}
