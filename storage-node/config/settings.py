"""Settings for a NodeMesh storage node.

One codebase serves every node; an instance is defined entirely by its
environment. Storage nodes have no database: object bytes and their small JSON
metadata live on the node's own volume, and the coordinator's PostgreSQL is the
source of truth for everything else.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from nodemesh_common.env import env_bool, env_int, env_list, env_str, load_env_file
from nodemesh_common.signing import is_valid_node_id, parse_key_list

BASE_DIR = Path(__file__).resolve().parent.parent

# Optional per-instance env file (e.g. for running several nodes on one laptop).
_env_file = env_str("NODE_ENV_FILE", "")
if _env_file:
    load_env_file(Path(_env_file))

# --- Node identity and storage ---------------------------------------------
NODE_ID = env_str("NODE_ID")
if not is_valid_node_id(NODE_ID):
    raise ValueError("NODE_ID must be a lowercase slug such as 'node-1'")
NODE_DATA_DIR = Path(env_str("NODE_DATA_DIR")).resolve()
NODE_MAX_OBJECT_BYTES = env_int("NODE_MAX_OBJECT_BYTES", 1024 * 1024 * 1024, minimum=1)
NODE_MIN_FREE_BYTES = env_int("NODE_MIN_FREE_BYTES", 256 * 1024 * 1024, minimum=0)
NODE_IO_CHUNK_BYTES = env_int("NODE_IO_CHUNK_BYTES", 1024 * 1024, minimum=4096)
NODE_FSYNC = env_bool("NODE_FSYNC", True)

# --- Internal authentication ------------------------------------------------
# "key-id:hexsecret[,key-id2:hexsecret2]" — list several keys during rotation.
NODE_HMAC_KEYS = parse_key_list(env_str("NODE_HMAC_KEYS"))
if not NODE_HMAC_KEYS:
    raise ValueError("NODE_HMAC_KEYS must contain at least one key")
NODE_MAX_CLOCK_SKEW_SECONDS = env_int("NODE_MAX_CLOCK_SKEW_SECONDS", 60, minimum=1)

# --- Django -----------------------------------------------------------------
# The node uses no sessions, cookies or signed tokens, so the Django secret key
# protects nothing; a random per-process value is acceptable when unset.
SECRET_KEY = env_str("DJANGO_SECRET_KEY", "") or secrets.token_urlsafe(50)
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = ["objects"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware"]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DATABASES: dict = {}
APPEND_SLASH = False
USE_TZ = True
TIME_ZONE = "UTC"

# Request bodies are streamed by the object view; nothing should ever buffer a
# large body in memory through request.body / request.POST.
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024
FILE_UPLOAD_HANDLERS: list[str] = []

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Deliberately silenced deploy checks: this is a machine-to-machine JSON API.
#  W002/W003 - no browser, no cookies, no forms: clickjacking/CSRF do not apply;
#              every request is authenticated by HMAC instead (objects/auth.py).
#  W004/W008 - TLS for node traffic is provided by the transport (private
#              network, WireGuard/SSH tunnel or a TLS proxy), see docs/security.md.
SILENCED_SYSTEM_CHECKS = ["security.W002", "security.W003", "security.W004", "security.W008"]

LOG_LEVEL = env_str("LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "nodemesh_common.logging.JsonFormatter", "service": f"storage-node:{NODE_ID}"}
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {"django.request": {"level": "ERROR"}},
}
