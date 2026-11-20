from __future__ import annotations

import hashlib
import io
import uuid

import pytest

from nodemesh_common import signing
from objects.apps import get_nonce_store, get_store
from objects.blobstore import BlobStore

TEST_KEY = signing.Credential(key_id="test-key", secret=bytes.fromhex("11" * 32))


@pytest.fixture
def store(tmp_path) -> BlobStore:
    s = BlobStore(tmp_path / "data", max_object_bytes=1024 * 1024, chunk_size=4096)
    s.ensure_layout()
    return s


@pytest.fixture(autouse=True)
def isolated_node(settings, tmp_path):
    """Give every API test its own data directory and fresh caches."""
    settings.NODE_DATA_DIR = tmp_path / "node-data"
    get_store.cache_clear()
    get_nonce_store.cache_clear()
    yield
    get_store.cache_clear()
    get_nonce_store.cache_clear()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stream(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def signed_headers(
    method: str,
    path: str,
    *,
    body: bytes = b"",
    query: str = "",
    digest: str | None = None,
    credential: signing.Credential = TEST_KEY,
    node_id: str = "node-test",
    now: float | None = None,
) -> dict[str, str]:
    return signing.sign_request(
        credential,
        node_id=node_id,
        method=method,
        path=path,
        query=query,
        content_sha256=digest or sha(body),
        content_length=len(body),
        now=now,
    )
