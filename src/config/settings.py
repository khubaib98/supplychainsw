"""
Settings for the China–Pakistan consignment portal.

Everything that differs between a laptop demo and production is read from
environment variables (see deploy/.env.example). Defaults are safe for local
development only. The desktop edition's launcher sets these variables from
its settings.ini (see core/desktop.py).
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

from config import VERSION

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [part.strip() for part in os.environ.get(name, default).split(",") if part.strip()]


TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
DEBUG = env_bool("DJANGO_DEBUG", False)
# Set by the desktop launcher (launcher/portal_launcher.py): the data folder on this computer.
DESKTOP_HOME = env("PORTAL_DESKTOP_HOME", "")

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "accounts",
    "logistics",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.UserPreferencesMiddleware",
    "accounts.middleware.AccountGateMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.portal",
            ],
        },
    },
]


# SQLite (desktop edition, development): wait for a busy database instead of failing,
# let people read while someone writes (WAL), take the write lock when a transaction
# starts (avoids "database is locked" between writers) and sync every commit to disk,
# because office PCs lose power.
SQLITE_OPTIONS = {
    "timeout": 20,
    "transaction_mode": "IMMEDIATE",
    "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL",
}


def database_from_url(url):
    """Minimal DATABASE_URL parser: postgres://user:pass@host:port/name or sqlite:///path."""
    parsed = urlparse(url)
    if parsed.scheme in {"postgres", "postgresql"}:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or ""),
            "CONN_MAX_AGE": 60,
        }
    if parsed.scheme == "sqlite":
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": unquote(parsed.path) or ":memory:",
            "OPTIONS": SQLITE_OPTIONS,
        }
    raise ImproperlyConfigured(f"Unsupported DATABASE_URL scheme: {parsed.scheme}")


DATA_DIR = Path(env("PORTAL_DATA_DIR", str(BASE_DIR / "data")))
DATABASE_URL = env("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {"default": database_from_url(DATABASE_URL)}
else:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": DATA_DIR / "db.sqlite3", "OPTIONS": SQLITE_OPTIONS}
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

if TESTING:
    # Fast hashing keeps the test-suite quick; never used outside tests.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "logistics:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# --- Languages -------------------------------------------------------------
# Names are shown in their own script so people can always find their language.
LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("zh-hans", "简体中文"),
    ("ur", "اردو"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
# Store everything in UTC; each user sees their own time zone (Asia/Shanghai, Asia/Karachi…).
TIME_ZONE = "UTC"
USE_TZ = True

# --- Static & uploaded files ----------------------------------------------
STATIC_URL = "/static/"
# The desktop edition has no collectstatic step: files are served from the source folders.
STATIC_ROOT = None if DESKTOP_HOME else BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
WHITENOISE_USE_FINDERS = DEBUG or bool(DESKTOP_HOME)
# Uploaded documents are never served directly: they go through an
# authenticated, audited download view (logistics.views.documents).
MEDIA_ROOT = Path(env("PORTAL_MEDIA_ROOT", str(DATA_DIR / "media")))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if env_bool("DJANGO_STATIC_MANIFEST", False)
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 30 * 1024 * 1024

# --- Security ----------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = int(env("DJANGO_SESSION_SECONDS", str(12 * 60 * 60)))
# Cookies ignore the port, so a second portal on the same computer (the desktop demo)
# needs its own cookie names or the two would sign each other out.
COOKIE_PREFIX = env("DJANGO_COOKIE_PREFIX", "")
SESSION_COOKIE_NAME = f"{COOKIE_PREFIX}sessionid"
CSRF_COOKIE_NAME = f"{COOKIE_PREFIX}csrftoken"
LANGUAGE_COOKIE_NAME = f"{COOKIE_PREFIX}django_language"
SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- Email -------------------------------------------------------------------
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = env("SMTP_HOST", "localhost")
EMAIL_PORT = int(env("SMTP_PORT", "587"))
EMAIL_HOST_USER = env("SMTP_USER", "")
EMAIL_HOST_PASSWORD = env("SMTP_PASSWORD", "")
EMAIL_USE_TLS = env_bool("SMTP_TLS", True)
EMAIL_TIMEOUT = 15
EMAIL_FILE_PATH = env("DJANGO_EMAIL_FILE_PATH", str(DATA_DIR / "outbox"))
DEFAULT_FROM_EMAIL = env("PORTAL_FROM_EMAIL", "portal@localhost")

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
}

# --- Portal behaviour -------------------------------------------------------
PORTAL = {
    "NAME": env("PORTAL_NAME", "CN–PK Consignment Portal"),
    "BASE_URL": env("PORTAL_BASE_URL", "http://localhost:8000"),
    # Consignment numbers look like CSG-2026-0042.
    "CONSIGNMENT_PREFIX": env("PORTAL_CONSIGNMENT_PREFIX", "CSG"),
    # Reminder ladder for missing documents, in days after the due date:
    # level 1 = responsible party, level 2 = second reminder, level 3 = escalate to Oversight.
    "REMINDER_DAYS": [int(d) for d in env_list("PORTAL_REMINDER_DAYS", "0,2,5")],
    "REQUIRE_2FA": env_bool("PORTAL_REQUIRE_2FA", False),
    "UPLOAD_MAX_MB": int(env("PORTAL_UPLOAD_MAX_MB", "25")),
    # Documents automatically required when an item type flagged "controlled" is packed.
    "CONTROLLED_ITEM_DOCS": env_list("PORTAL_CONTROLLED_ITEM_DOCS", "EUC,EXL"),
    "LOGIN_MAX_FAILURES": 5,
    "LOGIN_LOCK_MINUTES": 15,
    # Optional ClamAV daemon (host:port) used to scan uploads before they are accepted.
    "CLAMAV": env("PORTAL_CLAMAV", ""),
    "VERSION": VERSION,
    # Desktop edition only (set by the launcher):
    "DESKTOP": bool(DESKTOP_HOME),
    # one-time token for the first-run setup page (/setup/), printed in the launcher window
    "SETUP_TOKEN": env("PORTAL_SETUP_TOKEN", ""),
    # how people reach this computer: [{"kind": "local" | "network" | "public", "url": ...}]
    "ACCESS_URLS": json.loads(env("PORTAL_ACCESS_URLS", "") or "[]"),
    "INSTANCE": env("PORTAL_INSTANCE", ""),
}

# Machine translation of free text (comments/remarks). The user interface
# itself is translated with gettext catalogues and never needs this.
TRANSLATION = {
    # none | libretranslate | anthropic | dummy (tests)
    "PROVIDER": env("MT_PROVIDER", "none"),
    "LIBRETRANSLATE_URL": env("LIBRETRANSLATE_URL", "http://libretranslate:5000"),
    "LIBRETRANSLATE_API_KEY": env("LIBRETRANSLATE_API_KEY", ""),
    "ANTHROPIC_MODEL": env("MT_ANTHROPIC_MODEL", "claude-opus-5-5"),
    "ANTHROPIC_EFFORT": env("MT_ANTHROPIC_EFFORT", "low"),
    "TIMEOUT": float(env("MT_TIMEOUT", "20")),
}
