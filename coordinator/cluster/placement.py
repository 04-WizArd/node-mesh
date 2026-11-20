"""Replica placement.

Policy (deliberately simple, deterministic and easy to reason about):

* only ACTIVE nodes that are not SUSPECT receive new replicas;
* nodes that reported less free space than the object size are skipped
  (the node enforces its own reserve and answers 507 if it is still short);
* candidates are ordered by health, then by most free space, then by id.

With full replication (RF = number of nodes) every node stores every object,
so adding nodes beyond RF increases total capacity only because placement
spreads objects; it never increases the capacity available to a single object.
"""

from __future__ import annotations

from collections.abc import Iterable

from .health import HEALTH_RANK
from .models import StorageNode


def _free_bytes(node: StorageNode) -> int:
    return node.disk_free_bytes if node.disk_free_bytes is not None else -1


def rank_nodes(nodes: Iterable[StorageNode]) -> list[StorageNode]:
    return sorted(nodes, key=lambda n: (HEALTH_RANK[n.health], -_free_bytes(n), n.node_id))


def write_candidates(size: int, exclude: Iterable[int] = ()) -> list[StorageNode]:
    """Nodes eligible for a new replica of ``size`` bytes, best first."""
    nodes = (
        StorageNode.objects.filter(admin_state=StorageNode.AdminState.ACTIVE)
        .exclude(health=StorageNode.Health.SUSPECT)
        .exclude(pk__in=list(exclude))
    )
    eligible = [n for n in nodes if n.disk_free_bytes is None or n.disk_free_bytes >= size]
    return rank_nodes(eligible)
