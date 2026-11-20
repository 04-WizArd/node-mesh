"""Cross-process replay protection for signed internal requests.

Each accepted nonce is recorded as an empty file created with ``O_EXCL`` in the
node's data directory. ``O_EXCL`` creation is atomic across processes, so all
gunicorn workers of a node share the same view of seen nonces. Entries older
than the signature freshness window can never be replayed successfully (the
timestamp check rejects them first), so they are pruned periodically.

Limitation: nonce files are not fsynced; after a hard crash a request captured
in the last ``max_skew`` seconds could be replayed once. Signed PUTs and
DELETEs are idempotent, which bounds the impact.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path


class NonceStore:
    PRUNE_EVERY = 512

    def __init__(self, directory: Path, ttl_seconds: int) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self._counter = 0
        self._lock = threading.Lock()

    def claim(self, nonce: str) -> bool:
        """Record ``nonce``; return ``False`` if it was already used."""
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.directory / nonce, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        os.close(fd)
        with self._lock:
            self._counter += 1
            should_prune = self._counter % self.PRUNE_EVERY == 0
        if should_prune:
            self.prune()
        return True

    def prune(self) -> int:
        cutoff = time.time() - self.ttl_seconds
        removed = 0
        try:
            entries = list(os.scandir(self.directory))
        except FileNotFoundError:
            return 0
        for entry in entries:
            try:
                if entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
            except FileNotFoundError:
                continue
        return removed
