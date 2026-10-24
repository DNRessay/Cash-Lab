import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import User
from .security import read_token


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Authentication credentials were not provided.")
    try:
        payload = read_token(header[7:].strip(), "access")
    except jwt.PyJWTError:
        raise HTTPException(401, "Token is invalid or expired.")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or inactive.")
    return user
