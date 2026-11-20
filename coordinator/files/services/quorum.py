"""Replica counting rules shared by uploads, reads, the API and the reconciler."""

from __future__ import annotations

from django.db.models import Count, Q, QuerySet

from cluster.models import StorageNode
from files.models import File, Replica

# A replica counts towards durability only if it is STORED on a node that is
# expected to keep it: ACTIVE (not draining/disabled) and not presumed failed.
COUNTABLE_REPLICA = Q(
    replicas__status=Replica.Status.STORED,
    replicas__node__admin_state=StorageNode.AdminState.ACTIVE,
) & ~Q(replicas__node__health=StorageNode.Health.SUSPECT)


def with_replica_counts(queryset: QuerySet[File]) -> QuerySet[File]:
    return queryset.annotate(
        stored_replicas=Count("replicas", filter=Q(replicas__status=Replica.Status.STORED), distinct=True),
        countable_replicas=Count("replicas", filter=COUNTABLE_REPLICA, distinct=True),
    )


def quorum_met(stored: int, write_quorum: int) -> bool:
    return stored >= write_quorum


def is_under_replicated(countable: int, desired: int) -> bool:
    return countable < desired
