import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from .config import settings

ALGO = "HS256"
PBKDF2_ITERATIONS = 600_000

COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "qwertyuiop", "iloveyou", "11111111", "abc12345", "letmein1",
    "admin123", "welcome1", "passw0rd", "football", "baseball", "sunshine",
}


# Django-compatible "pbkdf2_sha256$iter$salt$hash" so accounts from the old app keep working.
def hash_password(raw: str) -> str:
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${base64.b64encode(digest).decode()}"


def verify_password(raw: str, encoded: str) -> bool:
    try:
        algo, iterations, salt, expected = (encoded or "").split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(base64.b64encode(digest).decode(), expected)


def password_problems(password: str, username: str = "", email: str = "") -> list[str]:
    problems = []
    if len(password) < 8:
        problems.append("This password is too short. It must contain at least 8 characters.")
    if password.isdigit():
        problems.append("This password is entirely numeric.")
    if password.lower() in COMMON_PASSWORDS:
        problems.append("This password is too common.")
    for attr in (username, (email or "").split("@")[0]):
        if attr and len(attr) >= 3 and attr.lower() in password.lower():
            problems.append("The password is too similar to your personal details.")
            break
    return problems


def _now():
    return datetime.now(timezone.utc)


def make_token(kind: str, subject, ttl: timedelta, **extra) -> str:
    payload = {"type": kind, "sub": str(subject), "iat": _now(), "exp": _now() + ttl, "jti": uuid.uuid4().hex, **extra}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGO)


def read_token(token: str, kind: str) -> dict:
    """Raises jwt.PyJWTError on bad/expired tokens or wrong type."""
    payload = jwt.decode(token, settings.secret_key, algorithms=[ALGO])
    if payload.get("type") != kind:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def token_pair(user_id: int) -> dict:
    return {
        "access": make_token("access", user_id, timedelta(minutes=settings.access_token_minutes)),
        "refresh": make_token("refresh", user_id, timedelta(days=settings.refresh_token_days)),
    }


def password_fingerprint(encoded_password: str) -> str:
    # Embedded in reset tokens so they die as soon as the password changes.
    return hashlib.sha256(f"{settings.secret_key}:{encoded_password}".encode()).hexdigest()[:24]



def _fernet():
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(f"seal:{settings.secret_key}".encode()).digest())
    return Fernet(key)


def seal(value: str) -> str:
    """Encrypt a secret for storage (EasyEquities and Gmail app passwords)."""
    if not value or value.startswith("enc:"):
        return value or ""
    return "enc:" + _fernet().encrypt(value.encode()).decode()


def unseal(value: str) -> str:
    if not value or not value.startswith("enc:"):
        return value or ""
    return _fernet().decrypt(value[4:].encode()).decode()
