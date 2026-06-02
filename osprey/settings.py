"""
Django settings for the OSPREY demo.

Configuration is driven by environment variables; see `.env.example`.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(int(default))).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-changeme-only-for-dev",
)

DEBUG = _env_bool("DJANGO_DEBUG", default=True)

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.orcid",
    # Local apps
    "core",
    "people",
    "projects",
    "api",
    "feedback",
    "notifications",
    "wiki",
    "use_reports",
    "conversations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "osprey.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "core" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "notifications.context_processors.unread_notification_count",
            ],
        },
    },
]

WSGI_APPLICATION = "osprey.wsgi.application"


# Database. Postgres via DATABASE_URL when running under docker compose;
# falls back to SQLite so `manage.py check` is runnable without a DB.
if os.environ.get("DATABASE_URL"):
    from urllib.parse import urlparse

    url = urlparse(os.environ["DATABASE_URL"])
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": url.path.lstrip("/"),
            "USER": url.username or "",
            "PASSWORD": url.password or "",
            "HOST": url.hostname or "",
            "PORT": str(url.port or ""),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
_core_static = BASE_DIR / "core" / "static"
STATICFILES_DIRS = [_core_static] if _core_static.exists() else []

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Allow up to 512 MiB project archive uploads. Anything over 2.5 MiB
# spills to a temp file on disk rather than being held in memory, so
# raising the cap doesn't blow up worker RSS.
DATA_UPLOAD_MAX_MEMORY_SIZE = 512 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"
ACCOUNT_SIGNUP_REDIRECT_URL = "/people/me/welcome/"

SITE_ID = int(os.environ.get("DJANGO_SITE_ID", "1"))

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

ACCOUNT_ADAPTER = "people.adapters.ClosedBetaAccountAdapter"
SOCIALACCOUNT_ADAPTER = "people.adapters.OrcidSocialAccountAdapter"
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_STORE_TOKENS = False

ORCID_USE_SANDBOX = _env_bool("ORCID_USE_SANDBOX", default=False)
SOCIALACCOUNT_PROVIDERS = {
    "orcid": {
        "BASE_DOMAIN": "sandbox.orcid.org" if ORCID_USE_SANDBOX else "orcid.org",
        "APP": {
            "client_id": os.environ.get("ORCID_CLIENT_ID", ""),
            "secret": os.environ.get("ORCID_CLIENT_SECRET", ""),
            "key": "",
        },
    }
}

ZENODO_USE_SANDBOX = _env_bool("ZENODO_USE_SANDBOX", default=True)
ZENODO_ACCESS_TOKEN = os.environ.get("ZENODO_ACCESS_TOKEN", "")
ZENODO_API_BASE_URL = os.environ.get(
    "ZENODO_API_BASE_URL",
    "https://sandbox.zenodo.org" if ZENODO_USE_SANDBOX else "https://zenodo.org",
).rstrip("/")
ZENODO_DEFAULT_COMMUNITY = os.environ.get("ZENODO_DEFAULT_COMMUNITY", "")

# Canonical absolute base URL used in outbound metadata (e.g. Zenodo
# `related_identifiers`). Falls back to the production hostname so the
# permalink embedded in a deposit is still resolvable when the deposit is
# created from a developer machine.
OSPREY_PUBLIC_BASE_URL = os.environ.get(
    "OSPREY_PUBLIC_BASE_URL", "https://osprey.phalkon.io"
).rstrip("/")

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = _env_bool("DJANGO_SESSION_COOKIE_SECURE", default=not DEBUG)
CSRF_COOKIE_SECURE = _env_bool("DJANGO_CSRF_COOKIE_SECURE", default=not DEBUG)
SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", default=False)
