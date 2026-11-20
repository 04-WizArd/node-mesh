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


def record_success(node: StorageNode, status: dict[str, Any] | None = None) -> None:
    fields: dict[str, Any] = {
        "health": StorageNode.Health.HEALTHY,
        "consecutive_failures": 0,
        "last_seen_at": timezone.now(),
        "last_error": "",
        "updated_at": timezone.now(),
    }
    if status:
        total, free = status.get("disk_total_bytes"), status.get("disk_free_bytes")
        if isinstance(total, int) and isinstance(free, int):
            fields["disk_total_bytes"] = total
            fields["disk_free_bytes"] = free
    if node.health != StorageNode.Health.HEALTHY:
        logger.info("node recovered", extra={"node_id": node.node_id, "previous": node.health})
    StorageNode.objects.filter(pk=node.pk).update(**fields)


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
