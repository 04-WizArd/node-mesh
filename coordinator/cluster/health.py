"""Passive and active node health tracking.

Every interaction with a node feeds into its health: success resets it to
HEALTHY; each failure increments ``consecutive_failures``. One failure only
makes a node UNREACHABLE (possibly transient). It becomes SUSPECT after
``NODEMESH_SUSPECT_AFTER_FAILURES`` consecutive failures, at which point its
replicas stop counting towards durability and re-replication starts.

Updates are single SQL statements, so concurrent requests cannot lose an
increment or race on the transition.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db.models import Case, F, Value, When
from django.utils import timezone

from .models import StorageNode

logger = logging.getLogger(__name__)

# Lower is better: used to order read candidates and placement candidates.
HEALTH_RANK = {
    StorageNode.Health.HEALTHY: 0,
    StorageNode.Health.UNKNOWN: 1,
    StorageNode.Health.UNREACHABLE: 2,
    StorageNode.Health.SUSPECT: 3,
}


# A healthy node that answered this recently is not written again on every
# request: in steady state, passive health tracking then costs no query.
HEALTHY_REFRESH = timedelta(seconds=10)


def _needs_write(node: StorageNode, now) -> bool:
    return not (
        node.health == StorageNode.Health.HEALTHY
        and node.consecutive_failures == 0
        and node.last_seen_at is not None
        and now - node.last_seen_at < HEALTHY_REFRESH
    )


def record_success(node: StorageNode, status: dict[str, Any] | None = None) -> None:
    """Mark ``node`` healthy; ``status`` (from a probe) also refreshes disk figures."""
    now = timezone.now()
    fields: dict[str, Any] = {}
    if status:
        total, free = status.get("disk_total_bytes"), status.get("disk_free_bytes")
        if isinstance(total, int) and isinstance(free, int):
            fields = {"disk_total_bytes": total, "disk_free_bytes": free}
    if not fields and not _needs_write(node, now):
        return
    record_successes([node], now=now, extra=fields)


def record_successes(nodes: list[StorageNode], *, now=None, extra: dict[str, Any] | None = None) -> None:
    """Mark several nodes healthy with a single UPDATE (skipping fresh ones)."""
    now = now or timezone.now()
    stale = [n for n in nodes if extra or _needs_write(n, now)]
    if not stale:
        return
    for node in stale:
        if node.health != StorageNode.Health.HEALTHY:
            message = "node recovered" if node.health != StorageNode.Health.UNKNOWN else "node healthy"
            logger.info(message, extra={"node_id": node.node_id, "previous": node.health})
        node.health, node.consecutive_failures, node.last_seen_at = StorageNode.Health.HEALTHY, 0, now
    StorageNode.objects.filter(pk__in=[n.pk for n in stale]).update(
        health=StorageNode.Health.HEALTHY,
        consecutive_failures=0,
        last_seen_at=now,
        last_error="",
        updated_at=now,
        **(extra or {}),
    )


def record_failure(node: StorageNode, error: str) -> None:
    threshold = settings.NODEMESH_SUSPECT_AFTER_FAILURES
    StorageNode.objects.filter(pk=node.pk).update(
        consecutive_failures=F("consecutive_failures") + 1,
        health=Case(
            When(consecutive_failures__gte=threshold - 1, then=Value(StorageNode.Health.SUSPECT)),
            default=Value(StorageNode.Health.UNREACHABLE),
        ),
        last_failure_at=timezone.now(),
        last_error=error[:500],
        updated_at=timezone.now(),
    )
    logger.warning("node request failed", extra={"node_id": node.node_id, "error": error})
