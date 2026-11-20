"""Settings for the storage-node test suite (never used in deployments)."""

import os
import tempfile

os.environ["NODE_ID"] = "node-test"
os.environ["NODE_DATA_DIR"] = tempfile.mkdtemp(prefix="nodemesh-node-test-")
os.environ["NODE_HMAC_KEYS"] = "test-key:" + "11" * 32
os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
os.environ["NODE_MIN_FREE_BYTES"] = "0"
os.environ.pop("NODE_ENV_FILE", None)

from .settings import *  # noqa: F403
