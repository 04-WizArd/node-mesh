from __future__ import annotations

from functools import cache

from django.apps import AppConfig
from django.conf import settings


class ObjectsConfig(AppConfig):
    name = "objects"
    verbose_name = "Object storage"


@cache
def get_store():
    """Process-wide BlobStore configured from settings."""
    from .blobstore import BlobStore

    store = BlobStore(
        settings.NODE_DATA_DIR,
        max_object_bytes=settings.NODE_MAX_OBJECT_BYTES,
        min_free_bytes=settings.NODE_MIN_FREE_BYTES,
        chunk_size=settings.NODE_IO_CHUNK_BYTES,
        fsync=settings.NODE_FSYNC,
    )
    store.ensure_layout()
    return store


@cache
def get_nonce_store():
    from .replay import NonceStore

    # Twice the skew: a timestamp can be up to `skew` in the past or future.
    return NonceStore(settings.NODE_DATA_DIR / "nonces", ttl_seconds=2 * settings.NODE_MAX_CLOCK_SKEW_SECONDS)
