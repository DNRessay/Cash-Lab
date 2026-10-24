import re

import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .deps import current_user, get_db
from .models import User, utcnow
from .security import hash_password, password_problems, read_token, token_pair, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=150)
    last_name: str = ""
    email: str
    password: str
    id_number: str = ""  # SA ID: checked here, stored sealed, reused for statement PDFs


class LoginIn(BaseModel):
    email: str
    password: str


class RefreshIn(BaseModel):
    refresh: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


def user_out(u: User):
    return {"id": u.id, "email": u.email, "first_name": u.first_name, "last_name": u.last_name}


@router.post("/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    first = not db.scalar(select(func.count()).select_from(User))
    # Private app: the first person to sign up owns it; after that only allow-listed emails.
    if not first and email not in settings.signup_emails:
        raise HTTPException(403, "Sign-up is closed. Ask the owner to add your email.")
    errors = {}
    if not EMAIL_RE.match(email):
        errors["email"] = ["Enter a valid email address."]
    elif db.scalar(select(User.id).where(User.email == email)):
        errors["email"] = ["An account with this email already exists."]
    if problems := password_problems(body.password, "", email):
        errors["password"] = problems
    if body.id_number.strip():
        from .onboarding import check_sa_id

        try:
            check_sa_id(body.id_number)
        except ValueError as e:
            errors["id_number"] = [str(e)]
    if errors:
        raise HTTPException(400, errors)
    user = User(email=email, first_name=body.first_name.strip(), last_name=body.last_name.strip(),
                password=hash_password(body.password))
    db.add(user)
    db.commit()
    if body.id_number.strip():
        from .onboarding import save_id

        save_id(db, user.id, body.id_number)
    return {"user": user_out(user), **token_pair(user.id)}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not user or not user.is_active or not verify_password(body.password, user.password):
        raise HTTPException(401, "Invalid email or password.")
    user.last_login = utcnow()
    db.commit()
    return {"user": user_out(user), **token_pair(user.id)}


@router.post("/token/refresh")
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    try:
        payload = read_token(body.refresh, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(401, "Token is invalid or expired.")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or inactive.")
    return {"access": token_pair(user.id)["access"]}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


@router.post("/change-password")
def change_password(body: PasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.old_password, user.password):
        raise HTTPException(400, {"old_password": ["Your current password is wrong."]})
    if problems := password_problems(body.new_password, "", user.email):
        raise HTTPException(400, {"new_password": problems})
    user.password = hash_password(body.new_password)
    db.commit()
    return {"message": "Password changed."}
