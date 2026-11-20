"""Settings for the NodeMesh coordinator.

Everything environment-specific comes from environment variables (see
``.env.example``). For local development a ``.env`` file at the repository root
is loaded automatically; real environment variables always take precedence.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from nodemesh_common.env import (
    env_bool,
    env_float,
    env_int,
    env_list,
    env_str,
    load_env_file,
)
from nodemesh_common.signing import parse_node_credentials

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR.parent

_env_file = os.environ.get("NODEMESH_ENV_FILE", str(REPO_DIR / ".env"))
if _env_file:
    load_env_file(Path(_env_file))

# --- Core ----------------------------------------------------------------------
SECRET_KEY = env_str("DJANGO_SECRET_KEY")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
ADMIN_URL = env_str("DJANGO_ADMIN_URL", "admin/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "cluster",
    "files",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "config.middleware.SecurityHeadersMiddleware",
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

# --- Database (PostgreSQL through psycopg 3) -----------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env_str("POSTGRES_HOST"),
        "PORT": env_int("POSTGRES_PORT", 5432),
        "NAME": env_str("POSTGRES_DB"),
        "USER": env_str("POSTGRES_USER"),
        "PASSWORD": env_str("POSTGRES_PASSWORD"),
        "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60, minimum=0),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": env_int("POSTGRES_CONNECT_TIMEOUT", 5, minimum=1),
            "sslmode": env_str("POSTGRES_SSLMODE", "prefer"),
            "application_name": "nodemesh-coordinator",
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentication --------------------------------------------------------------
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "files:index"
LOGOUT_REDIRECT_URL = "accounts:login"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- HTTP security -----------------------------------------------------------------
HTTPS = env_bool("DJANGO_HTTPS", False)
SESSION_COOKIE_SECURE = HTTPS
CSRF_COOKIE_SECURE = HTTPS
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True  # the token is read from a <meta> tag, not the cookie
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env_int("DJANGO_SESSION_COOKIE_AGE", 60 * 60 * 12, minimum=60)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 0, minimum=0) if HTTPS else 0
SECURE_SSL_REDIRECT = HTTPS and env_bool("DJANGO_SSL_REDIRECT", False)
if env_bool("DJANGO_TRUST_X_FORWARDED_PROTO", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Uploads never go through Django's multipart machinery (see files/spool.py),
# so these only bound ordinary form posts.
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 1
FILE_UPLOAD_HANDLERS = ["files.upload_handlers.RejectFileUploads"]

# --- Internationalisation (UI in French, code and API in English) ---------------
LANGUAGE_CODE = "fr-fr"
TIME_ZONE = env_str("DJANGO_TIME_ZONE", "Europe/Paris")
USE_I18N = True
USE_TZ = True

# --- Static files -----------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if env_bool("NODEMESH_STATIC_MANIFEST", not DEBUG)
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# --- NodeMesh: replication and transfers ---------------------------------------------
NODEMESH_REPLICATION_FACTOR = env_int("NODEMESH_REPLICATION_FACTOR", 3, minimum=1)
NODEMESH_WRITE_QUORUM = env_int("NODEMESH_WRITE_QUORUM", 2, minimum=1)
if NODEMESH_WRITE_QUORUM > NODEMESH_REPLICATION_FACTOR:
    raise ImproperlyConfigured("NODEMESH_WRITE_QUORUM cannot exceed NODEMESH_REPLICATION_FACTOR")

NODEMESH_MAX_UPLOAD_BYTES = env_int("NODEMESH_MAX_UPLOAD_BYTES", 100 * 1024 * 1024, minimum=1)
NODEMESH_SPOOL_DIR = Path(env_str("NODEMESH_SPOOL_DIR", str(Path(tempfile.gettempdir()) / "nodemesh-spool")))
NODEMESH_IO_CHUNK_BYTES = env_int("NODEMESH_IO_CHUNK_BYTES", 256 * 1024, minimum=4096)

NODEMESH_NODE_CREDENTIALS = parse_node_credentials(env_str("NODEMESH_NODE_CREDENTIALS", ""))
NODEMESH_NODE_CA_BUNDLE = env_str("NODEMESH_NODE_CA_BUNDLE", "")
NODEMESH_NODE_CONNECT_TIMEOUT = env_float("NODEMESH_NODE_CONNECT_TIMEOUT", 3.0)
NODEMESH_NODE_READ_TIMEOUT = env_float("NODEMESH_NODE_READ_TIMEOUT", 30.0)
NODEMESH_NODE_WRITE_ATTEMPTS = env_int("NODEMESH_NODE_WRITE_ATTEMPTS", 2, minimum=1)
NODEMESH_MAX_PARALLEL_TRANSFERS = env_int("NODEMESH_MAX_PARALLEL_TRANSFERS", 3, minimum=1)
NODEMESH_TRANSFER_POOL_SIZE = env_int("NODEMESH_TRANSFER_POOL_SIZE", 16, minimum=1)

# --- NodeMesh: failure detection and reconciliation ----------------------------------
NODEMESH_SUSPECT_AFTER_FAILURES = env_int("NODEMESH_SUSPECT_AFTER_FAILURES", 3, minimum=1)
NODEMESH_PENDING_TIMEOUT_SECONDS = env_int("NODEMESH_PENDING_TIMEOUT_SECONDS", 900, minimum=1)
NODEMESH_VERIFY_INTERVAL_SECONDS = env_int("NODEMESH_VERIFY_INTERVAL_SECONDS", 86400, minimum=1)
NODEMESH_ORPHAN_GRACE_SECONDS = env_int("NODEMESH_ORPHAN_GRACE_SECONDS", 3600, minimum=0)
NODEMESH_ORPHAN_GC = env_bool("NODEMESH_ORPHAN_GC", True)
NODEMESH_RECONCILE_BATCH = env_int("NODEMESH_RECONCILE_BATCH", 100, minimum=1)

# --- Logging --------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "nodemesh_common.logging.JsonFormatter", "service": "coordinator"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": env_str("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"level": "ERROR"},
        "urllib3": {"level": "WARNING"},
    },
}
