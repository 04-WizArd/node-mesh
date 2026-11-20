"""Copying a verified local file to storage nodes with bounded parallelism.

Shared by the upload path and by re-replication. Network calls run in worker
threads; all database writes happen in the calling thread, one short
statement at a time, so no transaction is held open across network I/O.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from cluster import health
from cluster.client import (
    ClientFactory,
    NodeError,
    NodeMisconfigured,
    NodeRejected,
    NodeUnavailable,
    client_for,
)
from cluster.models import StorageNode
from files.models import File, Replica

logger = logging.getLogger(__name__)

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def transfer_pool() -> ThreadPoolExecutor:
    """Process-wide pool for node transfers.

    Long-lived threads keep their (thread-local) HTTP sessions, so keep-alive
    connections to nodes are reused across uploads; the pool size also caps the
    total number of concurrent node transfers per coordinator process.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=settings.NODEMESH_TRANSFER_POOL_SIZE, thread_name_prefix="transfer"
            )
        return _pool


@dataclass
class ReplicationOutcome:
    stored: list[StorageNode] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # node_id -> error code


def _put_with_retries(
    factory: ClientFactory, node: StorageNode, file: File, path: Path, sleep: Callable[[float], None]
) -> None:
    attempts = settings.NODEMESH_NODE_WRITE_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            factory(node).put_object(file.id, path, file.size, file.sha256)
            return
        except NodeUnavailable:
            # PUT is idempotent (same id + same digest => 200), so retrying is safe
            # even when the previous attempt may have succeeded.
            if attempt == attempts:
                raise
            sleep(0.2 * attempt)


def _mark(replica: Replica, status: str, error: str = "") -> None:
    now = timezone.now()
    fields: dict[str, object] = {"status": status, "last_error": error[:500], "updated_at": now}
    if status == Replica.Status.STORED:
        # The node verified the digest of the bytes it fsynced.
        fields.update(stored_at=now, last_verified_at=now)
    Replica.objects.filter(pk=replica.pk).update(**fields)


def replicate(
    file: File,
    path: Path,
    candidates: Sequence[StorageNode],
    *,
    wanted: int,
    client_factory: ClientFactory = client_for,
    sleep: Callable[[float], None] = time.sleep,
) -> ReplicationOutcome:
    """Store ``file`` (bytes at ``path``) on up to ``wanted`` of ``candidates``.

    Starts with the first ``wanted`` candidates in parallel; each failure is
    replaced by the next untried candidate until ``wanted`` copies are stored
    or the candidates are exhausted. Replica rows are created (or reset) before
    any byte is sent, so the coordinator never loses track of a possible copy.
    """
    outcome = ReplicationOutcome()
    queue = list(candidates)
    if wanted <= 0 or not queue:
        return outcome

    max_workers = max(1, min(settings.NODEMESH_MAX_PARALLEL_TRANSFERS, wanted))
    in_flight: dict[Future, tuple[StorageNode, Replica]] = {}

    def launch() -> None:
        while queue and len(in_flight) < max_workers and len(outcome.stored) + len(in_flight) < wanted:
            node = queue.pop(0)
            replica, _ = Replica.objects.update_or_create(
                file=file,
                node=node,
                defaults={"status": Replica.Status.PENDING, "last_error": ""},
            )
            future = transfer_pool().submit(_put_with_retries, client_factory, node, file, path, sleep)
            in_flight[future] = (node, replica)

    launch()
    while in_flight:
        done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
        for future in done:
            node, replica = in_flight.pop(future)
            try:
                future.result()
            except NodeError as exc:
                _mark(replica, Replica.Status.FAILED, exc.code)
                outcome.failed[node.node_id] = exc.code
                if isinstance(exc, NodeUnavailable):
                    health.record_failure(node, exc.code)
                elif isinstance(exc, NodeRejected):
                    health.record_success(node)  # the node answered: it is alive
                elif isinstance(exc, NodeMisconfigured):
                    logger.error("node misconfigured", extra={"node_id": node.node_id, "error": exc.code})
            except Exception as exc:  # never let one node's bug break the upload
                logger.exception("unexpected replication error", extra={"node_id": node.node_id})
                _mark(replica, Replica.Status.FAILED, "internal_error")
                outcome.failed[node.node_id] = type(exc).__name__
            else:
                _mark(replica, Replica.Status.STORED)
                health.record_success(node)
                outcome.stored.append(node)
        launch()

    logger.info(
        "replication finished",
        extra={
            "file_id": str(file.id),
            "stored": [n.node_id for n in outcome.stored],
            "failed": outcome.failed,
        },
    )
    return outcome
