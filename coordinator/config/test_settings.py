"""Settings for the coordinator test suite.

Tests NEVER use the development database: this module ignores POSTGRES_* and
the repository .env file. It uses TEST_POSTGRES_* when set (CI, or an isolated
throwaway PostgreSQL), and otherwise a temporary SQLite file.
"""

import os
import tempfile

_TEST_NODE_IDS = ["node-1", "node-2", "node-3", "node-4", "slow-node"]
TEST_NODE_SECRETS = {node: f"{i + 1:02x}" * 32 for i, node in enumerate(_TEST_NODE_IDS)}

os.environ["NODEMESH_ENV_FILE"] = ""  # do not load the developer's .env
os.environ.update(
    {
        "DJANGO_SECRET_KEY": "test-only-secret-key-not-used-anywhere-else-0123456789",
        "DJANGO_ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
        "POSTGRES_HOST": "unused",
        "POSTGRES_DB": "unused",
        "POSTGRES_USER": "unused",
        "POSTGRES_PASSWORD": "unused",
        "NODEMESH_NODE_CREDENTIALS": ",".join(
            f"{node}:test-key:{secret}" for node, secret in TEST_NODE_SECRETS.items()
        ),
        "NODEMESH_REPLICATION_FACTOR": "3",
        "NODEMESH_WRITE_QUORUM": "2",
        "NODEMESH_NODE_CONNECT_TIMEOUT": "1",
        "NODEMESH_NODE_READ_TIMEOUT": "2",
        "NODEMESH_STATIC_MANIFEST": "false",
        "NODEMESH_SPOOL_DIR": tempfile.mkdtemp(prefix="nodemesh-spool-test-"),
    }
)

from .settings import *  # noqa: E402,F403

if os.environ.get("TEST_POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": os.environ["TEST_POSTGRES_HOST"],
            "PORT": int(os.environ.get("TEST_POSTGRES_PORT", "5432")),
            "NAME": os.environ.get("TEST_POSTGRES_DB", "nodemesh_test"),
            "USER": os.environ["TEST_POSTGRES_USER"],
            "PASSWORD": os.environ["TEST_POSTGRES_PASSWORD"],
            "OPTIONS": {"connect_timeout": 5},
        }
    }
else:
    _sqlite_dir = tempfile.mkdtemp(prefix="nodemesh-db-test-")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.path.join(_sqlite_dir, "db.sqlite3"),
            "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"},
            "TEST": {"NAME": os.path.join(_sqlite_dir, "test.sqlite3")},
        }
    }

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # speed only
LOGGING["root"]["level"] = "WARNING"  # noqa: F405
