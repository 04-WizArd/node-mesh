"""Verified reads with replica fallback (spool-and-verify).

The coordinator downloads a replica into an anonymous temporary file while
counting bytes and hashing them, and hands the file to the client only after
size and SHA-256 match the metadata. A replica that is unreachable, missing or
corrupt is skipped and recorded; the next one is tried.

Costs (documented in docs/consistency.md): the client receives the first byte
only after the whole object has been fetched and verified, and the coordinator
needs temporary disk space equal to the object size per concurrent download.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from cluster import health
from cluster.client import ClientFactory, NodeError, NodeRejected, NodeUnavailable, client_for
from cluster.health import HEALTH_RANK
from cluster.models import StorageNode
from files.models import File, Replica
from files.spool import TooLarge, spool_chunks, spool_dir

logger = logging.getLogger(__name__)


class NoReplicaAvailable(Exception):
    code = "no_replica_available"


@dataclass
class VerifiedContent:
    handle: BinaryIO  # positioned at 0; deleted from disk when closed
    replica: Replica


def readable_replicas(file: File) -> list[Replica]:
    """STORED replicas on nodes that may be contacted, best node first."""
    rank = Case(
        *(When(node__health=state, then=Value(order)) for state, order in HEALTH_RANK.items()),
        output_field=IntegerField(),
    )
    return list(
        Replica.objects.filter(file=file, status=Replica.Status.STORED)
        .exclude(node__admin_state=StorageNode.AdminState.DISABLED)
        .select_related("node")
        .annotate(rank=rank)
        .order_by("rank", "node__node_id")
    )


def _set_replica(replica: Replica, status: str, error: str) -> None:
    Replica.objects.filter(pk=replica.pk).update(
        status=status, last_error=error[:500], updated_at=timezone.now()
    )
    logger.error(
        "replica marked %s", status, extra={"file_id": str(replica.file_id), "node_id": replica.node.node_id}
    )


def anonymous_spool() -> BinaryIO:
    """Unnamed temp file: removed by the OS as soon as it is closed, even on crash."""
    return tempfile.TemporaryFile(dir=spool_dir())


def fetch_from_replica(
    file: File,
    replica: Replica,
    client_factory: ClientFactory,
    make_handle: Callable[[], BinaryIO] = anonymous_spool,
) -> BinaryIO | None:
    """Return a verified temp file from one replica, or ``None`` (state recorded).

    The handle is closed on every failure path; callers that pass a factory
    creating *named* files remain responsible for unlinking them.
    """
    node = replica.node
    handle = make_handle()
    try:
        with client_factory(node).get_object(file.id) as chunks:
            size, digest = spool_chunks(chunks, handle, max_bytes=file.size)
    except NodeRejected as exc:
        handle.close()
        health.record_success(node)
        if exc.status == 404:
            _set_replica(replica, Replica.Status.MISSING, "object not found on node")
        else:
            logger.warning("replica read rejected", extra={"node_id": node.node_id, "error": exc.code})
        return None
    except NodeError as exc:
        handle.close()
        if isinstance(exc, NodeUnavailable):
            health.record_failure(node, exc.code)
        return None
    except TooLarge:
        handle.close()
        health.record_success(node)
        _set_replica(replica, Replica.Status.CORRUPT, "replica larger than recorded size")
        return None
    except BaseException:
        handle.close()
        raise

    health.record_success(node)
    if size != file.size or digest != file.sha256:
        handle.close()
        _set_replica(replica, Replica.Status.CORRUPT, "size or SHA-256 mismatch on read")
        return None

    Replica.objects.filter(pk=replica.pk).update(last_verified_at=timezone.now())
    handle.seek(0)
    return handle


def fetch_verified(
    file: File,
    *,
    client_factory: ClientFactory = client_for,
    make_handle: Callable[[], BinaryIO] = anonymous_spool,
) -> VerifiedContent:
    for replica in readable_replicas(file):
        handle = fetch_from_replica(file, replica, client_factory, make_handle)
        if handle is not None:
            return VerifiedContent(handle=handle, replica=replica)
    logger.error("no readable replica", extra={"file_id": str(file.id)})
    raise NoReplicaAvailable(str(file.id))
