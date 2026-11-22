"""Force re-verification of every copy on one node (after a volume loss or restore)."""

from django.core.management.base import BaseCommand, CommandError

from cluster.models import StorageNode
from files.models import Replica


class Command(BaseCommand):
    help = (
        "Mark every stored replica on NODE_ID as unverified. The reconciler's scrub then "
        "re-checks them (in batches of NODEMESH_RECONCILE_BATCH per cycle); missing or corrupt "
        "copies are recorded and re-replicated. Use after replacing or restoring a node volume."
    )

    def add_arguments(self, parser):
        parser.add_argument("node_id")

    def handle(self, *args, node_id: str, **options):
        if not StorageNode.objects.filter(node_id=node_id).exists():
            raise CommandError(f"unknown node {node_id!r}")
        count = Replica.objects.filter(node__node_id=node_id, status=Replica.Status.STORED).update(
            last_verified_at=None
        )
        self.stdout.write(f"{count} replica(s) on {node_id} queued for verification by the reconciler")
