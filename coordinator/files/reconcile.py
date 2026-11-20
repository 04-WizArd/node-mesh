"""Reconciliation: converge metadata and node contents after failures.

One ``run()`` executes these idempotent steps in order. Each step does its
network I/O outside database transactions and tolerates unreachable nodes
(work is simply retried on the next run):

1. ``probe_nodes``            active health check of every non-disabled node
2. ``resolve_stale_pending``  uploads interrupted by a coordinator crash
3. ``purge``                  remove copies of FAILED/DELETED files
4. ``scrub``                  re-verify STORED replicas on the node (SHA-256)
5. ``repair_bad_replicas``    drop CORRUPT/MISSING copies so they can be replaced
6. ``re_replicate``           restore the desired replica count
7. ``collect_orphans``        adopt untracked valid copies, delete garbage

Running the reconciler twice, concurrently or after a crash is safe: every
node operation is idempotent (PUT by id + digest, DELETE by id), and a
PostgreSQL advisory lock keeps two reconcilers from overlapping.
"""

from __future__ import annotations

import logging
import tempfile
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.models import F
from django.utils import timezone

from cluster import health
from cluster.client import ClientFactory, NodeError, NodeRejected, client_for
from cluster.models import StorageNode
from cluster.placement import write_candidates
from files.models import File, Replica
from files.services import quorum
from files.services.download import NoReplicaAvailable, fetch_verified
from files.services.replication import replicate
from files.services.upload import finalize
from files.spool import spool_dir

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 0x4E4D_5245  # "NMRE"
TERMINAL = (File.Status.FAILED, File.Status.DELETED)


@contextmanager
def reconcile_lock() -> Iterator[bool]:
    """Cluster-wide mutual exclusion on PostgreSQL; always granted elsewhere (tests)."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_ID])
        acquired = bool(cursor.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_ID])


def _set_replica(replica_pk: int, status: str, error: str = "", **extra) -> None:
    Replica.objects.filter(pk=replica_pk).update(
        status=status, last_error=error[:500], updated_at=timezone.now(), **extra
    )


class Reconciler:
    def __init__(self, client_factory: ClientFactory = client_for) -> None:
        self.client_factory = client_factory
        self.report: Counter[str] = Counter()
        self.batch = settings.NODEMESH_RECONCILE_BATCH
        # Nodes that failed this cycle's probe: later steps do not contact them,
        # which avoids piling up timeouts and failure counts on a node that is
        # merely restarting.
        self.down: set[int] = set()

    # ------------------------------------------------------------------------
    def run(self) -> dict[str, int]:
        self.report = Counter()
        self.down = set()
        with reconcile_lock() as acquired:
            if not acquired:
                logger.info("another reconciler holds the lock; skipping this cycle")
                return {"skipped": 1}
            steps = [
                self.probe_nodes,
                self.resolve_stale_pending,
                self.purge,
                self.scrub,
                self.repair_bad_replicas,
                self.re_replicate,
            ]
            if settings.NODEMESH_ORPHAN_GC:
                steps.append(self.collect_orphans)
            for step in steps:
                try:
                    step()
                except Exception:  # one faulty step must not block the others
                    logger.exception("reconcile step failed", extra={"step": step.__name__})
                    self.report[f"{step.__name__}_errors"] += 1
        result = dict(self.report)
        logger.info("reconcile cycle finished", extra={"report": result})
        return result

    def _client(self, node: StorageNode):
        return self.client_factory(node)

    def _node_failed(self, node: StorageNode, exc: NodeError) -> None:
        if isinstance(exc, NodeRejected):
            health.record_success(node)
        else:
            health.record_failure(node, exc.code)

    # -- 1 -----------------------------------------------------------------------
    def probe_nodes(self) -> None:
        nodes = list(StorageNode.objects.exclude(admin_state=StorageNode.AdminState.DISABLED))

        def probe(node: StorageNode):
            try:
                return node, self._client(node).status(), None
            except NodeError as exc:
                return node, None, exc

        workers = max(1, min(8, len(nodes)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            results = list(pool.map(probe, nodes))
        for node, status, exc in results:  # database writes stay in this thread
            if exc is None and status is not None and status.get("node_id") == node.node_id:
                health.record_success(node, status)
                self.report["nodes_healthy"] += 1
            else:
                health.record_failure(node, exc.code if exc else "node_id_mismatch")
                self.down.add(node.pk)
                self.report["nodes_failing"] += 1

    # -- 2 -----------------------------------------------------------------------
    def resolve_stale_pending(self) -> None:
        cutoff = timezone.now() - timedelta(seconds=settings.NODEMESH_PENDING_TIMEOUT_SECONDS)
        stale = File.objects.filter(status=File.Status.PENDING, created_at__lt=cutoff)[: self.batch]
        for file in stale:
            replicas = (
                Replica.objects.filter(file=file)
                .exclude(status=Replica.Status.STORED)
                .exclude(node__admin_state=StorageNode.AdminState.DISABLED)
                .exclude(node_id__in=self.down)
                .select_related("node")
            )
            for replica in replicas:
                try:
                    found = self._client(replica.node).head_object(file.id)
                except NodeError as exc:
                    self._node_failed(replica.node, exc)
                    continue
                if found and found.sha256 == file.sha256 and found.size == file.size:
                    now = timezone.now()
                    _set_replica(replica.pk, Replica.Status.STORED, stored_at=now, last_verified_at=None)
                else:
                    _set_replica(replica.pk, Replica.Status.FAILED, "absent after interrupted upload")
            final = finalize(file, write_quorum=settings.NODEMESH_WRITE_QUORUM)
            self.report[f"pending_resolved_{final.status}"] += 1

    # -- 3 -----------------------------------------------------------------------
    def purge(self) -> None:
        replicas = (
            Replica.objects.filter(file__status__in=TERMINAL)
            .exclude(node__admin_state=StorageNode.AdminState.DISABLED)
            .exclude(node_id__in=self.down)
            .select_related("node")[: self.batch]
        )
        for replica in replicas:
            try:
                self._client(replica.node).delete_object(replica.file_id)
            except NodeError as exc:
                self._node_failed(replica.node, exc)
                continue
            replica.delete()
            self.report["replicas_purged"] += 1
        deleted, _ = File.objects.filter(status=File.Status.DELETED, replicas__isnull=True).delete()
        self.report["files_removed"] += deleted

    # -- 4 -----------------------------------------------------------------------
    def scrub(self) -> None:
        cutoff = timezone.now() - timedelta(seconds=settings.NODEMESH_VERIFY_INTERVAL_SECONDS)
        replicas = (
            Replica.objects.filter(status=Replica.Status.STORED, file__status=File.Status.AVAILABLE)
            .exclude(node__admin_state=StorageNode.AdminState.DISABLED)
            .exclude(node__health=StorageNode.Health.SUSPECT)
            .exclude(node_id__in=self.down)
            .filter(last_verified_at__isnull=True)
            .select_related("node", "file")
        )
        stale = (
            Replica.objects.filter(
                status=Replica.Status.STORED,
                file__status=File.Status.AVAILABLE,
                last_verified_at__lt=cutoff,
            )
            .exclude(node__admin_state=StorageNode.AdminState.DISABLED)
            .exclude(node__health=StorageNode.Health.SUSPECT)
            .exclude(node_id__in=self.down)
            .select_related("node", "file")
            .order_by("last_verified_at")
        )
        for replica in [*replicas[: self.batch], *stale[: self.batch]][: self.batch]:
            file = replica.file
            try:
                result = self._client(replica.node).check_integrity(file.id)
            except NodeRejected as exc:
                health.record_success(replica.node)
                if exc.status == 404:
                    _set_replica(replica.pk, Replica.Status.MISSING, "absent during scrub")
                    self.report["replicas_missing"] += 1
                continue
            except NodeError as exc:
                self._node_failed(replica.node, exc)
                continue
            health.record_success(replica.node)
            if result.get("actual_sha256") == file.sha256 and result.get("actual_size") == file.size:
                Replica.objects.filter(pk=replica.pk).update(last_verified_at=timezone.now())
                self.report["replicas_verified"] += 1
            else:
                _set_replica(replica.pk, Replica.Status.CORRUPT, "SHA-256 mismatch during scrub")
                self.report["replicas_corrupt"] += 1
                logger.error(
                    "corrupt replica detected",
                    extra={"file_id": str(file.id), "node_id": replica.node.node_id},
                )

    # -- 5 -----------------------------------------------------------------------
    def repair_bad_replicas(self) -> None:
        for replica in Replica.objects.filter(status=Replica.Status.MISSING)[: self.batch]:
            _set_replica(replica.pk, Replica.Status.FAILED, "missing copy forgotten")
            self.report["missing_cleared"] += 1
        corrupt = (
            Replica.objects.filter(status=Replica.Status.CORRUPT)
            .exclude(node__admin_state=StorageNode.AdminState.DISABLED)
            .exclude(node_id__in=self.down)
            .select_related("node")[: self.batch]
        )
        for replica in corrupt:
            try:
                self._client(replica.node).delete_object(replica.file_id)
            except NodeError as exc:
                self._node_failed(replica.node, exc)
                continue
            _set_replica(replica.pk, Replica.Status.FAILED, "corrupt copy removed")
            self.report["corrupt_removed"] += 1

    # -- 6 -----------------------------------------------------------------------
    def re_replicate(self) -> None:
        under = (
            quorum.with_replica_counts(File.objects.filter(status=File.Status.AVAILABLE))
            .filter(countable_replicas__lt=F("desired_replicas"))
            .order_by("created_at")[: self.batch]
        )
        for file in under:
            holders = Replica.objects.filter(file=file, status=Replica.Status.STORED).values_list(
                "node_id", flat=True
            )
            need = file.desired_replicas - file.countable_replicas
            targets = write_candidates(file.size, exclude=list(holders))
            if not targets:
                self.report["re_replication_no_target"] += 1
                continue

            spooled: list[Path] = []

            def named_spool(created: list[Path] = spooled):
                handle = tempfile.NamedTemporaryFile(dir=spool_dir(), prefix="repair-", delete=False)  # noqa: SIM115
                created.append(Path(handle.name))
                return handle

            try:
                try:
                    content = fetch_verified(
                        file, client_factory=self.client_factory, make_handle=named_spool
                    )
                except NoReplicaAvailable:
                    self.report["re_replication_no_source"] += 1
                    continue
                source_path = Path(content.handle.name)
                content.handle.close()
                outcome = replicate(
                    file, source_path, targets, wanted=need, client_factory=self.client_factory
                )
                self.report["replicas_created"] += len(outcome.stored)
            finally:
                for path in spooled:
                    path.unlink(missing_ok=True)

    # -- 7 -----------------------------------------------------------------------
    def collect_orphans(self) -> None:
        grace = timedelta(seconds=settings.NODEMESH_ORPHAN_GRACE_SECONDS)
        nodes = (
            StorageNode.objects.filter(health=StorageNode.Health.HEALTHY)
            .exclude(admin_state=StorageNode.AdminState.DISABLED)
            .exclude(pk__in=self.down)
        )
        for node in nodes:
            client = self._client(node)
            after = None
            while True:
                try:
                    objects, after = client.list_objects(after=after, limit=500)
                except NodeError as exc:
                    self._node_failed(node, exc)
                    break
                self._reconcile_listing(node, client, objects, grace)
                if not after:
                    break

    def _reconcile_listing(self, node, client, objects: list[dict], grace: timedelta) -> None:
        ids = [o["object_id"] for o in objects]
        files = {str(f.id): f for f in File.objects.filter(id__in=ids)}
        replicas = {str(r.file_id): r for r in Replica.objects.filter(node=node, file_id__in=ids)}
        now = timezone.now()
        for obj in objects:
            oid = obj["object_id"]
            file, replica = files.get(oid), replicas.get(oid)
            matches = file is not None and obj["sha256"] == file.sha256 and obj["size"] == file.size

            if file is not None and file.status == File.Status.AVAILABLE:
                if replica is None and matches:
                    Replica.objects.create(file=file, node=node, status=Replica.Status.STORED, stored_at=now)
                    self.report["replicas_adopted"] += 1
                    continue
                if replica is not None and replica.status == Replica.Status.FAILED:
                    if matches:  # e.g. a write that timed out but actually succeeded
                        _set_replica(replica.pk, Replica.Status.STORED, stored_at=now, last_verified_at=None)
                        self.report["replicas_adopted"] += 1
                    else:
                        _set_replica(replica.pk, Replica.Status.CORRUPT, "unexpected content on node")
                    continue
                if replica is not None or matches:
                    continue  # tracked; other steps own it
            elif file is not None and replica is not None:
                continue  # PENDING upload in flight, or purge() owns terminal files

            # Untracked bytes: no file, terminal file without replica row, or
            # wrong content for an AVAILABLE file without replica row.
            if file is None or file.status == File.Status.AVAILABLE:
                stored_at = _parse_time(obj.get("stored_at"))
                if stored_at is None or now - stored_at < grace:
                    continue
            if file is not None and file.status == File.Status.PENDING:
                continue
            try:
                client.delete_object(oid)
            except NodeError as exc:
                self._node_failed(node, exc)
                return
            self.report["orphans_deleted"] += 1
            logger.warning("orphan object deleted", extra={"node_id": node.node_id, "object_id": oid})


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
