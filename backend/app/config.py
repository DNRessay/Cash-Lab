import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


def _env(key, default=""):
    return os.environ.get(key, default).strip()


def _flag(key, default="false"):
    return _env(key, default).lower() in ("1", "true", "yes", "on")


def _list(key, default=""):
    return [v.strip() for v in _env(key, default).split(",") if v.strip()]


class Settings:
    def __init__(self):
        self.secret_key = _env("SECRET_KEY", "dev-insecure-change-me")
        self.debug = _flag("DEBUG")
        self.database_url = _env("DATABASE_URL", "sqlite:///./clab.db")
        self.auto_create_tables = _flag("AUTO_CREATE_TABLES", "true")

        self.frontend_url = _env("FRONTEND_URL", "http://localhost:8789").rstrip("/")
        self.cors_origins = _list("CORS_ALLOWED_ORIGINS") or [self.frontend_url]
        host = self.frontend_url.split("://", 1)[-1]
        default_regex = rf"https://([a-z0-9-]+\.)?{re.escape(host)}" if host.endswith(".pages.dev") else ""
        self.cors_origin_regex = _env("CORS_ORIGIN_REGEX", default_regex) or None

        # Personal app: after the first account exists, only these emails may sign up (comma-separated).
        self.signup_emails = [e.lower() for e in _list("SIGNUP_EMAILS")]

        self.access_token_minutes = int(_env("ACCESS_TOKEN_MINUTES", "60"))
        self.refresh_token_days = int(_env("REFRESH_TOKEN_DAYS", "30"))

        self.email_host = _env("EMAIL_HOST")
        self.email_port = int(_env("EMAIL_PORT", "587"))
        self.email_user = _env("EMAIL_HOST_USER")
        self.email_password = _env("EMAIL_HOST_PASSWORD")
        self.ses_from = _env("SES_FROM")  # e.g. "C-Lab <noreply@example.com>" (a verified SES identity)
        self.ses_region = _env("SES_REGION", "eu-west-1")
        self.email_use_tls = _flag("EMAIL_USE_TLS", "true")
        self.default_from_email = _env("DEFAULT_FROM_EMAIL", "C-Lab <noreply@example.com>")

        # "Sign in with Google" for the EasyEquities email reader (read-only Gmail).
        self.google_client_id = _env("GOOGLE_CLIENT_ID")
        self.google_client_secret = _env("GOOGLE_CLIENT_SECRET")
        # AI: Cohere writes suggestions, Groq answers chat (several keys, comma separated, rotate on rate limits).
        self.cohere_api_key = _env("COHERE_API_KEY")
        self.cohere_model = _env("COHERE_MODEL", "command-a-03-2025")
        self.groq_api_keys = _list("GROQ_API_KEYS") or _list("GROQ_API_KEY")
        self.groq_model = _env("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.serpapi_keys = _list("SERPAPI_KEYS") or _list("SERPAPI_KEY")  # real news for the market pulse
        self.api_url = _env("API_URL").rstrip("/")  # public URL of this API; defaults to the request's own
        # Heavy scraping runs on GitHub Actions (worker.yml), started from here with a token that may only run workflows.
        self.gh_dispatch_token = _env("GH_DISPATCH_TOKEN")
        self.gh_repo = _env("GH_REPO")  # "owner/C-Lab"
        self.gh_workflow = _env("GH_WORKFLOW", "worker.yml")
        self.gh_ref = _env("GH_REF", "main")

        self.mysql_ssl_ca = _env("MYSQL_SSL_CA")


settings = Settings()
