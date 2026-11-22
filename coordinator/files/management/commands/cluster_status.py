"""Read-only overview of node health and replication (used by `make cluster-status`)."""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count, F
from django.utils import timezone

from cluster.models import StorageNode
from files.models import File, Replica
from files.services import quorum


def _gib(value: int | None) -> str:
    return "-" if value is None else f"{value / 1024**3:.1f}"


def _ago(moment) -> str:
    if moment is None:
        return "never"
    seconds = int((timezone.now() - moment).total_seconds())
    return f"{seconds}s ago" if seconds < 120 else f"{seconds // 60}m ago"


class Command(BaseCommand):
    help = "Show storage-node health and the replication state of stored files (read-only)."

    def handle(self, *args, **options):
        self.stdout.write(
            f"Replication factor {settings.NODEMESH_REPLICATION_FACTOR}, "
            f"write quorum {settings.NODEMESH_WRITE_QUORUM}\n"
        )
        stored = dict(
            Replica.objects.filter(status=Replica.Status.STORED, file__status=File.Status.AVAILABLE)
            .values_list("node__node_id")
            .annotate(n=Count("id"))
        )
        header = (
            f"{'node':<10} {'admin':<9} {'health':<12} {'fails':>5} "
            f"{'last seen':>10} {'free GiB':>9} {'copies':>7}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for node in StorageNode.objects.order_by("node_id"):
            self.stdout.write(
                f"{node.node_id:<10} {node.admin_state:<9} {node.health:<12} "
                f"{node.consecutive_failures:>5} {_ago(node.last_seen_at):>10} "
                f"{_gib(node.disk_free_bytes):>9} {stored.get(node.node_id, 0):>7}"
            )
        if not StorageNode.objects.exists():
            self.stdout.write("(no node registered)")

        by_status = dict(File.objects.values_list("status").annotate(n=Count("id")))
        under = (
            quorum.with_replica_counts(File.objects.filter(status=File.Status.AVAILABLE))
            .filter(countable_replicas__lt=F("desired_replicas"))
            .count()
        )
        problem = dict(
            Replica.objects.filter(status__in=[Replica.Status.CORRUPT, Replica.Status.MISSING])
            .values_list("status")
            .annotate(n=Count("id"))
        )
        self.stdout.write("")
        self.stdout.write(
            "Files: "
            + ", ".join(f"{status}={by_status.get(status, 0)}" for status in File.Status.values)
            + f"; under-replicated={under}"
        )
        corrupt, missing = problem.get("corrupt", 0), problem.get("missing", 0)
        self.stdout.write(f"Replicas awaiting repair: corrupt={corrupt}, missing={missing}")
