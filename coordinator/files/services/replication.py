"""Copying a verified local file to storage nodes with bounded parallelism.

Shared by the upload path and by re-replication. Network calls run in worker
threads; all database writes happen in the calling thread as a few batched
statements, so no transaction is held open across network I/O.

Replica rows exist (as PENDING) before any byte is sent, and outcomes are
recorded once the transfers finish. If the coordinator dies in between, the
rows stay PENDING and the reconciler resolves them by asking the nodes.
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


def prepare_replicas(file: File, nodes: Sequence[StorageNode]) -> dict[int, Replica]:
    """Ensure a PENDING replica row exists for each node (2-3 queries in total)."""
    existing = {r.node_id: r for r in Replica.objects.filter(file=file, node__in=list(nodes))}
    if existing:
        Replica.objects.filter(pk__in=[r.pk for r in existing.values()]).update(
            status=Replica.Status.PENDING, last_error="", updated_at=timezone.now()
        )
    missing = [
        Replica(file=file, node=n, status=Replica.Status.PENDING) for n in nodes if n.pk not in existing
    ]
    created = Replica.objects.bulk_create(missing)
    rows = {**existing, **{r.node_id: r for r in created}}
    if any(r.pk is None for r in rows.values()):  # backends without RETURNING
        rows = {r.node_id: r for r in Replica.objects.filter(file=file, node__in=list(nodes))}
    return rows


def _record_outcomes(stored: list[Replica], failed: list[tuple[Replica, str]]) -> None:
    now = timezone.now()
    if stored:
        # The node verified the digest of the bytes it fsynced.
        Replica.objects.filter(pk__in=[r.pk for r in stored]).update(
            status=Replica.Status.STORED, last_error="", stored_at=now, last_verified_at=now, updated_at=now
        )
    by_error: dict[str, list[int]] = {}
    for replica, error in failed:
        by_error.setdefault(error[:500], []).append(replica.pk)
    for error, pks in by_error.items():
        Replica.objects.filter(pk__in=pks).update(
            status=Replica.Status.FAILED, last_error=error, updated_at=now
        )


def replicate(
    file: File,
    path: Path,
    candidates: Sequence[StorageNode],
    *,
    wanted: int,
    client_factory: ClientFactory = client_for,
    sleep: Callable[[float], None] = time.sleep,
    prepared: dict[int, Replica] | None = None,
) -> ReplicationOutcome:
    """Store ``file`` (bytes at ``path``) on up to ``wanted`` of ``candidates``.

    Starts with the first ``wanted`` candidates in parallel; each failure is
    replaced by the next untried candidate until ``wanted`` copies are stored
    or the candidates are exhausted. Replica rows are created (or reset) before
    any byte is sent, so the coordinator never loses track of a possible copy;
    ``prepared`` passes rows the caller already created for the first targets.
    """
    outcome = ReplicationOutcome()
    queue = list(candidates)
    if wanted <= 0 or not queue:
        return outcome

    max_workers = max(1, min(settings.NODEMESH_MAX_PARALLEL_TRANSFERS, wanted))
    rows = prepared if prepared is not None else prepare_replicas(file, queue[: min(wanted, max_workers)])
    in_flight: dict[Future, tuple[StorageNode, Replica]] = {}
    stored_rows: list[Replica] = []
    failed_rows: list[tuple[Replica, str]] = []
    alive: list[StorageNode] = []

    def launch() -> None:
        while queue and len(in_flight) < max_workers and len(outcome.stored) + len(in_flight) < wanted:
            node = queue.pop(0)
            replica = rows.get(node.pk) or prepare_replicas(file, [node])[node.pk]  # spare target
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
                failed_rows.append((replica, exc.code))
                outcome.failed[node.node_id] = exc.code
                if isinstance(exc, NodeUnavailable):
                    health.record_failure(node, exc.code)
                elif isinstance(exc, NodeRejected):
                    alive.append(node)  # the node answered: it is alive
                elif isinstance(exc, NodeMisconfigured):
                    logger.error("node misconfigured", extra={"node_id": node.node_id, "error": exc.code})
            except Exception as exc:  # never let one node's bug break the upload
                logger.exception("unexpected replication error", extra={"node_id": node.node_id})
                failed_rows.append((replica, "internal_error"))
                outcome.failed[node.node_id] = type(exc).__name__
            else:
                stored_rows.append(replica)
                alive.append(node)
                outcome.stored.append(node)
        launch()

    _record_outcomes(stored_rows, failed_rows)
    health.record_successes(alive)

    logger.info(
        "replication finished",
        extra={
            "file_id": str(file.id),
            "stored": [n.node_id for n in outcome.stored],
            "failed": outcome.failed,
        },
    )
    return outcome
