"""Distributed behaviour against real storage-node processes.

Every test here talks HTTP to gunicorn-hosted storage nodes with real HMAC
authentication and real on-disk storage. Failures are injected by stopping
processes, by a TCP black hole (hung node), and by editing bytes on disk.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.utils import timezone

from cluster.client import client_for
from cluster.models import StorageNode
from files.models import File, Replica
from files.reconcile import Reconciler
from files.services.upload import upload as upload_service
from files.spool import spool_stream

from .conftest import body, download, upload

pytestmark = [pytest.mark.cluster]

DATA = os.urandom(1024 * 1024 + 123)  # > 1 MiB, odd size, every byte value likely present


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replica_states(file_id) -> dict[str, str]:
    return dict(Replica.objects.filter(file_id=file_id).values_list("node__node_id", "status"))


def node(nid: str) -> StorageNode:
    return StorageNode.objects.get(node_id=nid)


def corrupt(path: Path) -> None:
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF  # same size, one flipped byte
    path.write_bytes(bytes(raw))


def reconcile(times: int = 1) -> dict:
    report: dict = {}
    for _ in range(times):
        report = Reconciler().run()
    return report


# --- happy path ---------------------------------------------------------------------
def test_round_trip_replicates_to_three_nodes_byte_for_byte(api, cluster):
    response = upload(api, DATA, name="données.bin")
    assert response.status_code == 201, response.content
    payload = response.json()["file"]
    assert payload["sha256"] == sha(DATA)
    assert payload["replication"] == {"desired": 3, "stored": 3, "healthy": 3, "under_replicated": False}

    file_id = payload["id"]
    assert replica_states(file_id) == {"node-1": "stored", "node-2": "stored", "node-3": "stored"}
    for nid in ("node-1", "node-2", "node-3"):
        assert cluster[nid].object_path(file_id).read_bytes() == DATA

    got = download(api, file_id)
    assert got.status_code == 200
    assert body(got) == DATA
    assert got["X-Content-SHA256"] == sha(DATA)
    assert got["Content-Type"] == "application/octet-stream"
    assert got["Content-Disposition"].startswith("attachment;")
    assert "filename*=utf-8''donn%C3%A9es.bin" in got["Content-Disposition"]


def test_empty_file_round_trip(api, cluster):
    response = upload(api, b"", name="empty.txt")
    assert response.status_code == 201
    assert body(download(api, response.json()["file"]["id"])) == b""


def test_malicious_filename_never_touches_the_filesystem(api, cluster):
    name = "../../../tmp/<img src=x onerror=alert(1)>.txt"
    response = upload(api, b"payload", name=name)
    assert response.status_code == 201
    stored_name = response.json()["file"]["name"]
    assert stored_name == "<img src=x onerror=alert(1)>.txt"  # display string only, JSON-encoded
    for proc in cluster.values():
        for path in proc.data_dir.rglob("*"):
            assert "img" not in path.name and ".." not in path.name


# --- partial failures and quorum ---------------------------------------------------------
def test_one_node_down_still_meets_quorum_and_reports_under_replication(api, cluster):
    cluster["node-3"].stop()
    response = upload(api, DATA)
    assert response.status_code == 201
    rep = response.json()["file"]["replication"]
    assert rep["stored"] == 2 and rep["under_replicated"] is True
    file_id = response.json()["file"]["id"]
    assert replica_states(file_id)["node-3"] == "failed"
    assert node("node-3").health == "unreachable"
    assert body(download(api, file_id)) == DATA


def test_insufficient_quorum_fails_explicitly_and_is_cleaned_up(api, cluster):
    cluster["node-2"].stop()
    cluster["node-3"].stop()
    response = upload(api, DATA, key="retry-me")
    assert response.status_code == 503
    assert response.json() == {"error": "replication_quorum_not_met", "stored": 1, "required": 2}

    file = File.objects.get()
    assert file.status == File.Status.FAILED
    assert file.idempotency_key is None  # the client may retry with the same key
    assert cluster["node-1"].has(file.id)  # partial copy exists until reconciliation
    assert api.get("/api/v1/files/").json()["files"] == []

    reconcile()
    assert not cluster["node-1"].has(file.id)
    assert not Replica.objects.filter(file=file, node__node_id="node-1").exists()

    cluster["node-2"].start()
    retry = upload(api, DATA, key="retry-me")
    assert retry.status_code == 201


def test_hung_node_is_bounded_by_timeouts(api, cluster, black_hole, settings):
    settings.NODEMESH_NODE_WRITE_ATTEMPTS = 1
    StorageNode.objects.filter(node_id="node-3").update(admin_state="disabled")
    StorageNode.objects.create(
        node_id="slow-node", base_url=black_hole.url, health="healthy", disk_free_bytes=10**12
    )

    started = time.monotonic()
    response = upload(api, b"x" * 1000)
    elapsed = time.monotonic() - started

    assert response.status_code == 201
    assert response.json()["file"]["replication"]["stored"] == 2
    assert elapsed < settings.NODEMESH_NODE_READ_TIMEOUT + 3  # parallel, bounded
    assert replica_states(response.json()["file"]["id"])["slow-node"] == "failed"
    assert node("slow-node").health == "unreachable"


def test_invalid_node_credentials_are_rejected_by_the_node(api, cluster, settings):
    from nodemesh_common.signing import Credential

    creds = dict(settings.NODEMESH_NODE_CREDENTIALS)
    creds["node-1"] = Credential(key_id="test-key", secret=b"\x99" * 32)
    settings.NODEMESH_NODE_CREDENTIALS = creds
    response = upload(api, DATA)
    assert response.status_code == 201
    file_id = response.json()["file"]["id"]
    assert replica_states(file_id)["node-1"] == "failed"
    assert Replica.objects.get(file_id=file_id, node__node_id="node-1").last_error == "bad_signature"
    assert not cluster["node-1"].has(file_id)


# --- reads ------------------------------------------------------------------------------------
def test_read_falls_back_when_a_replica_node_is_down(api, cluster):
    file_id = upload(api, DATA).json()["file"]["id"]
    cluster["node-1"].stop()
    got = download(api, file_id)
    assert got.status_code == 200 and body(got) == DATA
    assert node("node-1").health == "unreachable"


def test_corrupt_replica_is_detected_skipped_and_repaired(api, cluster):
    file_id = upload(api, DATA).json()["file"]["id"]
    corrupt(cluster["node-1"].object_path(file_id))

    got = download(api, file_id)
    assert got.status_code == 200 and body(got) == DATA  # never serves the bad copy
    assert replica_states(file_id)["node-1"] == "corrupt"

    reconcile()  # remove the bad copy, then re-replicate onto node-1
    assert replica_states(file_id)["node-1"] == "stored"
    assert cluster["node-1"].object_path(file_id).read_bytes() == DATA


def test_scrub_finds_silent_corruption_without_any_read(api, cluster):
    file_id = upload(api, DATA).json()["file"]["id"]
    corrupt(cluster["node-2"].object_path(file_id))
    Replica.objects.filter(file_id=file_id).update(last_verified_at=timezone.now() - timedelta(days=2))
    report = reconcile()
    assert report["replicas_corrupt"] == 1
    assert report["replicas_created"] == 1
    assert cluster["node-2"].object_path(file_id).read_bytes() == DATA


def test_all_replicas_unavailable_returns_503(api, cluster):
    file_id = upload(api, b"abc").json()["file"]["id"]
    for nid in ("node-1", "node-2", "node-3"):
        cluster[nid].stop()
    response = download(api, file_id)
    assert response.status_code == 503
    assert response.json() == {"error": "no_replica_available"}


def test_data_survives_node_restart(api, cluster):
    file_id = upload(api, DATA).json()["file"]["id"]
    cluster["node-2"].stop()
    cluster["node-2"].start()  # same data directory
    StorageNode.objects.exclude(node_id="node-2").update(admin_state="disabled")
    got = download(api, file_id)
    assert got.status_code == 200 and body(got) == DATA


# --- idempotency and concurrency ---------------------------------------------------------------
def test_duplicate_request_with_same_key_is_replayed(api, cluster):
    first = upload(api, DATA, key="abc-123")
    second = upload(api, DATA, key="abc-123")
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["replayed"] is True
    assert first.json()["file"]["id"] == second.json()["file"]["id"]
    assert File.objects.count() == 1
    conflict = upload(api, b"different content", key="abc-123")
    assert conflict.status_code == 409


@pytest.mark.django_db(transaction=True)
def test_concurrent_uploads_are_isolated_and_intact(cluster, django_user_model, settings):
    if connection.vendor == "sqlite":
        settings.NODEMESH_MAX_PARALLEL_TRANSFERS = 3
    for nid in ("node-1", "node-2", "node-3"):
        StorageNode.objects.get_or_create(node_id=nid, defaults={"base_url": cluster[nid].url})
    owner = django_user_model.objects.create_user("concurrent", password="x")
    payloads = [os.urandom(50_000 + i) for i in range(6)]
    results: list = [None] * len(payloads)
    errors: list = []

    def worker(index: int, data: bytes, key: str | None) -> None:
        import io

        try:
            spooled = spool_stream(io.BytesIO(data), declared_length=len(data), limit=10**8)
            try:
                results[index] = upload_service(
                    owner=owner,
                    spooled=spooled,
                    original_name=f"f{index}",
                    content_type="x/y",
                    idempotency_key=key,
                )
            finally:
                spooled.discard()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i, d, None)) for i, d in enumerate(payloads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert all(r.available and r.stored == 3 for r in results)
    for result, data in zip(results, payloads, strict=True):
        for nid in ("node-1", "node-2", "node-3"):
            assert cluster[nid].object_path(result.file.id).read_bytes() == data

    # Same idempotency key fired concurrently: exactly one file is created.
    dup = os.urandom(10_000)
    results = [None] * 4
    threads = [threading.Thread(target=worker, args=(i, dup, "same-key")) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert File.objects.filter(owner=owner, sha256=sha(dup)).count() == 1
    assert all(type(e).__name__ == "UploadInProgress" for e in errors)


# --- recovery ------------------------------------------------------------------------------------
def test_failed_node_triggers_re_replication_to_a_spare(api, cluster, settings):
    settings.NODEMESH_SUSPECT_AFTER_FAILURES = 2
    StorageNode.objects.create(node_id="node-4", base_url=cluster["node-4"].url)
    file_id = upload(api, DATA).json()["file"]["id"]
    placed = {n for n, s in replica_states(file_id).items() if s == "stored"}
    assert len(placed) == 3
    victim = sorted(placed)[0]
    spare = ({"node-1", "node-2", "node-3", "node-4"} - placed).pop()

    cluster[victim].stop()
    reconcile()  # 1st failed probe: unreachable, no action yet
    assert node(victim).health == "unreachable"
    assert not cluster[spare].has(file_id)
    reconcile()  # 2nd failed probe: suspect -> its replica stops counting -> repair
    assert node(victim).health == "suspect"
    assert replica_states(file_id)[spare] == "stored"
    assert cluster[spare].object_path(file_id).read_bytes() == DATA

    cluster[victim].start()
    reconcile()
    assert node(victim).health == "healthy"
    detail = api.get(f"/api/v1/files/{file_id}/").json()["file"]["replication"]
    assert detail["healthy"] == 4 and detail["under_replicated"] is False  # over-replication is tolerated


def test_interrupted_upload_is_resolved_from_node_state(user, cluster, settings):
    """Simulates a coordinator crash after the PUTs but before finalisation."""
    old = timezone.now() - timedelta(seconds=settings.NODEMESH_PENDING_TIMEOUT_SECONDS + 5)
    data = b"half-finished upload"
    file = File.objects.create(
        owner=user, original_name="x", size=len(data), sha256=sha(data), desired_replicas=3
    )
    File.objects.filter(pk=file.pk).update(created_at=old)
    spool = Path(settings.NODEMESH_SPOOL_DIR) / "crash.bin"
    spool.write_bytes(data)
    for nid in ("node-1", "node-2", "node-3"):
        Replica.objects.create(file=file, node=node(nid))
    for nid in ("node-1", "node-2"):  # only two PUTs completed before the "crash"
        client_for(node(nid)).put_object(file.id, spool, len(data), sha(data))

    lost = File.objects.create(owner=user, original_name="y", size=1, sha256=sha(b"z"), desired_replicas=3)
    File.objects.filter(pk=lost.pk).update(created_at=old)
    Replica.objects.create(file=lost, node=node("node-1"))

    reconcile()
    file.refresh_from_db()
    lost.refresh_from_db()
    assert file.status == File.Status.AVAILABLE
    assert replica_states(file.id) == {"node-1": "stored", "node-2": "stored", "node-3": "stored"}
    assert lost.status == File.Status.FAILED


def test_orphans_are_deleted_and_lost_acknowledgements_adopted(api, cluster, settings):
    settings.NODEMESH_ORPHAN_GRACE_SECONDS = 0
    # 1) Bytes on a node that no metadata references.
    orphan_id = uuid.uuid4()
    spool = Path(settings.NODEMESH_SPOOL_DIR) / "orphan.bin"
    spool.write_bytes(b"orphan")
    client_for(node("node-2")).put_object(orphan_id, spool, 6, sha(b"orphan"))
    # 2) A write that succeeded on the node but was recorded as failed (lost ack).
    file_id = upload(api, DATA).json()["file"]["id"]
    Replica.objects.filter(file_id=file_id, node__node_id="node-3").update(
        status="failed", last_error="timeout"
    )

    report = reconcile()
    assert not cluster["node-2"].has(orphan_id)
    assert report["orphans_deleted"] >= 1
    assert replica_states(file_id)["node-3"] == "stored"


def test_delete_removes_metadata_and_node_copies(api, cluster):
    file_id = upload(api, DATA).json()["file"]["id"]
    assert api.delete(f"/api/v1/files/{file_id}/").status_code == 204
    assert api.get(f"/api/v1/files/{file_id}/").status_code == 404
    assert download(api, file_id).status_code == 404
    reconcile()
    assert not File.objects.filter(pk=file_id).exists()
    assert not any(cluster[n].has(file_id) for n in ("node-1", "node-2", "node-3"))


def test_reconcile_is_idempotent(api, cluster):
    upload(api, DATA)
    reconcile()
    second = reconcile()
    assert second.get("replicas_created", 0) == 0
    assert second.get("replicas_purged", 0) == 0
    assert second.get("nodes_healthy") == 3


def test_request_query_budget(api, cluster):
    """Guards the batched bookkeeping: database round trips dominate small transfers."""
    from django.test.utils import CaptureQueriesContext

    upload(api, b"warm-up")  # nodes are now healthy and freshly seen
    with CaptureQueriesContext(connection) as up:
        response = upload(api, b"x" * 1000)
    assert response.status_code == 201
    with CaptureQueriesContext(connection) as down:
        assert body(download(api, response.json()["file"]["id"])) == b"x" * 1000
    assert len(up.captured_queries) <= 16, len(up.captured_queries)  # 14 measured; was 37
    assert len(down.captured_queries) <= 6, len(down.captured_queries)  # 5 measured
