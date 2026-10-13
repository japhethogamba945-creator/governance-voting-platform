"""Django settings. All deployment-specific values come from the environment."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return default if value is None else int(value)


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set")

ALLOWED_HOSTS = [h for h in (env("DJANGO_ALLOWED_HOSTS", "") or "").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "platform_core",
    "accounts",
    "tenancy",
    "audit",
    "members",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "platform_core.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "platform_core.middleware.RequestTransactionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "accounts.middleware.SessionTimeoutMiddleware",
    "accounts.middleware.RequireVerifiedLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Must be last: adds the organisation to the request's RLS context.
    "tenancy.middleware.TenantContextMiddleware",
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
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


def _database(prefix: str) -> dict:
    """Connection settings for one database alias.

    Production connects each alias with its own least-privilege login role
    (``app_rw``, ``app_platform``...). Development and tests may log in with an
    administrative role and ``SET ROLE`` via ``<PREFIX>_ASSUME_ROLE``.
    """
    options: dict = {}
    assume_role = env(f"{prefix}_ASSUME_ROLE")
    if assume_role:
        options["assume_role"] = assume_role
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env(f"{prefix}_NAME", env("DB_NAME", "gvp")),
        "USER": env(f"{prefix}_USER", env("DB_USER", "app_rw")),
        "PASSWORD": env(f"{prefix}_PASSWORD", env("DB_PASSWORD", "")),
        "HOST": env(f"{prefix}_HOST", env("DB_HOST", "localhost")),
        "PORT": env(f"{prefix}_PORT", env("DB_PORT", "5432")),
        "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
        "OPTIONS": options,
    }


DATABASES = {
    "default": _database("DB"),
    # Platform Owner operations (organisation provisioning). Never used for
    # tenant election data.
    "platform": {**_database("PLATFORM_DB"), "TEST": {"MIRROR": "default"}},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "home"

PASSWORD_HASHERS = ["accounts.hashers.Argon2idHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

# --- sessions & cookies -------------------------------------------------
SECURE_COOKIES = env_bool("SECURE_COOKIES", True)
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "__Host-session" if SECURE_COOKIES else "session"
SESSION_COOKIE_SECURE = SECURE_COOKIES
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 12 * 60 * 60
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_NAME = "__Host-csrftoken" if SECURE_COOKIES else "csrftoken"
CSRF_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

SESSION_IDLE_TIMEOUT = env_int("SESSION_IDLE_TIMEOUT", 30 * 60)
OFFICIAL_IDLE_TIMEOUT = env_int("OFFICIAL_IDLE_TIMEOUT", 15 * 60)
SESSION_ABSOLUTE_TIMEOUT = env_int("SESSION_ABSOLUTE_TIMEOUT", 12 * 60 * 60)
STEP_UP_MAX_AGE = env_int("STEP_UP_MAX_AGE", 5 * 60)
PRE_MFA_MAX_AGE = 5 * 60

# Number of reverse proxies in front of the app that append X-Forwarded-For.
NUM_TRUSTED_PROXIES = env_int("NUM_TRUSTED_PROXIES", 0)

# --- transport security -------------------------------------------------
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not DEBUG)
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 0 if DEBUG else 63072000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
if NUM_TRUSTED_PROXIES:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; object-src 'none'"
)

# --- i18n ----------------------------------------------------------------
LANGUAGE_CODE = "en"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# --- email -----------------------------------------------------------------
# Absolute links in emails are built from SITE_URL, never from the Host header.
SITE_URL = env("SITE_URL", "http://localhost:8000" if DEBUG else "")
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
                    if DEBUG else "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "no-reply@localhost")
INVITATION_TTL_DAYS = env_int("INVITATION_TTL_DAYS", 7)

# Uploads (CSV import) are read in memory and capped well below this.
DATA_UPLOAD_MAX_MEMORY_SIZE = 3 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 3 * 1024 * 1024

OTP_TOTP_ISSUER = env("OTP_TOTP_ISSUER", "Governance Voting")

# Request bodies are never logged (see docs/design/05-security-review.md §7).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "filters": {"redact": {"()": "platform_core.logredact.RedactSecretsFilter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain",
                             "filters": ["redact"]}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        # Replace Django's default handlers (which bypass the redaction filter).
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # django.request logs include the request object; keep it to warnings
        # (status + path only) and never attach bodies.
        "django.request": {"level": "WARNING"},
        "django.db.backends": {"level": "WARNING"},
        # Development server access log: route through the redacting handler.
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
