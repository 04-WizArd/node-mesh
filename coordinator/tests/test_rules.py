"""Unit tests: metadata invariants, placement, health transitions, sanitisation."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from cluster import health
from cluster.models import StorageNode
from cluster.placement import write_candidates
from files.models import File, Replica
from files.services import quorum
from files.spool import TooLarge, Truncated, clean_content_type, clean_filename, spool_stream


def make_file(owner, **kwargs) -> File:
    defaults = dict(original_name="a.txt", size=1, sha256="0" * 64, desired_replicas=3)
    defaults.update(kwargs)
    return File.objects.create(owner=owner, **defaults)


@pytest.mark.parametrize(
    ("stored", "quorum_size", "expected"), [(2, 2, True), (3, 2, True), (1, 2, False), (0, 1, False)]
)
def test_quorum_decision(stored, quorum_size, expected):
    assert quorum.quorum_met(stored, quorum_size) is expected


def test_under_replication_rule():
    assert quorum.is_under_replicated(2, 3)
    assert not quorum.is_under_replicated(3, 3)
    assert not quorum.is_under_replicated(4, 3)


@pytest.mark.django_db
def test_idempotency_key_is_unique_per_owner(user, other_user):
    make_file(user, idempotency_key="k1")
    make_file(other_user, idempotency_key="k1")  # other owners may reuse it
    make_file(user, idempotency_key=None)
    make_file(user, idempotency_key=None)  # NULL keys never collide
    with pytest.raises(IntegrityError), transaction.atomic():
        make_file(user, idempotency_key="k1")


@pytest.mark.django_db
def test_status_values_are_constrained(user):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_file(user, status="bogus")


@pytest.mark.django_db
def test_one_replica_row_per_file_and_node(user):
    node = StorageNode.objects.create(node_id="node-1", base_url="http://n1:8000")
    file = make_file(user)
    Replica.objects.create(file=file, node=node)
    with pytest.raises(IntegrityError), transaction.atomic():
        Replica.objects.create(file=file, node=node)


@pytest.mark.django_db
def test_replica_counting_ignores_unreliable_nodes(user):
    ok = StorageNode.objects.create(node_id="ok", base_url="http://a:1", health="healthy")
    suspect = StorageNode.objects.create(node_id="sus", base_url="http://b:1", health="suspect")
    draining = StorageNode.objects.create(node_id="dr", base_url="http://c:1", admin_state="draining")
    file = make_file(user)
    for node in (ok, suspect, draining):
        Replica.objects.create(file=file, node=node, status=Replica.Status.STORED)
    counted = quorum.with_replica_counts(File.objects.filter(pk=file.pk)).get()
    assert counted.stored_replicas == 3
    assert counted.countable_replicas == 1


@pytest.mark.django_db
def test_placement_filters_and_orders_nodes():
    def node(nid, **kw):
        return StorageNode.objects.create(node_id=nid, base_url=f"http://{nid}:1", **kw)

    node("healthy-small", health="healthy", disk_free_bytes=100)
    node("healthy-big", health="healthy", disk_free_bytes=10_000)
    node("unknown", health="unknown")
    node("flaky", health="unreachable", disk_free_bytes=10_000)
    node("suspect", health="suspect", disk_free_bytes=10_000)
    node("draining", admin_state="draining", health="healthy")
    node("disabled", admin_state="disabled", health="healthy")
    node("full", health="healthy", disk_free_bytes=10)

    ids = [n.node_id for n in write_candidates(size=50)]
    assert ids == ["healthy-big", "healthy-small", "unknown", "flaky"]
    assert "healthy-small" not in [n.node_id for n in write_candidates(size=500)]


@pytest.mark.django_db
def test_health_transitions(settings):
    settings.NODEMESH_SUSPECT_AFTER_FAILURES = 3
    node = StorageNode.objects.create(node_id="n", base_url="http://n:1")

    def state():
        node.refresh_from_db()
        return node.health, node.consecutive_failures

    health.record_failure(node, "timeout")
    assert state() == ("unreachable", 1)  # one timeout is not proof of failure
    health.record_failure(node, "timeout")
    assert state() == ("unreachable", 2)
    health.record_failure(node, "timeout")
    assert state() == ("suspect", 3)
    health.record_success(node, {"disk_total_bytes": 100, "disk_free_bytes": 40})
    assert state() == ("healthy", 0)
    assert node.disk_free_bytes == 40 and node.last_seen_at is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("..%2F..%2Fetc%2Fpasswd", "passwd"),
        ("C:%5CWindows%5Cevil.exe", "evil.exe"),
        ("%C3%A9t%C3%A9%202026.pdf", "été 2026.pdf"),
        ("a%00b%0Ac.txt", "abc.txt"),
        ("%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E.txt", "<img src=x onerror=alert(1)>.txt"),
        ("", "fichier"),
        ("..", "fichier"),
        ("%FF%FE", "fichier"),
        ("x" * 400, "x" * 255),
    ],
)
def test_clean_filename(raw, expected):
    assert clean_filename(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("image/png", "image/png"),
        ("text/plain; charset=utf-8", "text/plain"),
        ("text/html\r\nX-Injected: 1", "application/octet-stream"),
        ("", "application/octet-stream"),
        ("nonsense", "application/octet-stream"),
    ],
)
def test_clean_content_type(raw, expected):
    assert clean_content_type(raw) == expected


def test_spool_counts_and_hashes_bytes(tmp_path, settings):
    import hashlib
    import io

    settings.NODEMESH_SPOOL_DIR = tmp_path
    data = bytes(range(256)) * 1000
    spooled = spool_stream(io.BytesIO(data), declared_length=len(data), limit=len(data))
    assert spooled.size == len(data)
    assert spooled.sha256 == hashlib.sha256(data).hexdigest()
    assert spooled.path.read_bytes() == data
    spooled.discard()

    with pytest.raises(TooLarge):
        spool_stream(io.BytesIO(data), declared_length=len(data), limit=10)
    with pytest.raises(Truncated):
        spool_stream(io.BytesIO(b"abc"), declared_length=10, limit=100)
    assert list(tmp_path.iterdir()) == []  # nothing left behind on failure
