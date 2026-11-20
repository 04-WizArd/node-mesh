from __future__ import annotations

import errno
import io
import os
import threading
import time

import pytest

from objects.blobstore import (
    BlobStore,
    DigestMismatch,
    IncompleteBody,
    InsufficientStorage,
    ObjectConflict,
    ObjectNotFound,
    ObjectTooLarge,
    PutOutcome,
)

from .conftest import new_id, sha, stream

BINARY = bytes(range(256)) * 300 + os.urandom(12345)


def read_all(store: BlobStore, object_id) -> bytes:
    _, handle = store.open(object_id)
    with handle:
        return handle.read()


def leftovers(store: BlobStore) -> list[str]:
    return os.listdir(store.tmp_dir)


def test_binary_round_trip_preserves_every_byte(store):
    oid = new_id()
    outcome, info = store.put(oid, stream(BINARY), declared_length=len(BINARY), expected_sha256=sha(BINARY))
    assert outcome is PutOutcome.CREATED
    assert info.size == len(BINARY) and info.sha256 == sha(BINARY)
    assert read_all(store, oid) == BINARY
    assert store.verify(oid).ok
    assert leftovers(store) == []


def test_empty_object_is_supported(store):
    oid = new_id()
    store.put(oid, stream(b""), declared_length=0, expected_sha256=sha(b""))
    assert read_all(store, oid) == b""


def test_digest_mismatch_publishes_nothing(store):
    oid = new_id()
    with pytest.raises(DigestMismatch):
        store.put(oid, stream(b"evil"), declared_length=4, expected_sha256=sha(b"good"))
    assert store.stat(oid) is None
    assert leftovers(store) == []


def test_truncated_body_publishes_nothing(store):
    oid = new_id()
    with pytest.raises(IncompleteBody):
        store.put(oid, stream(b"abc"), declared_length=10, expected_sha256=sha(b"abc"))
    assert store.stat(oid) is None
    assert leftovers(store) == []


def test_extra_bytes_beyond_declared_length_are_never_read(store):
    oid = new_id()
    source = stream(b"abcdef")
    store.put(oid, source, declared_length=3, expected_sha256=sha(b"abc"))
    assert read_all(store, oid) == b"abc"
    assert source.tell() == 3


def test_oversized_object_is_rejected_before_reading(store):
    oid = new_id()
    source = stream(b"x")
    with pytest.raises(ObjectTooLarge):
        store.put(oid, source, declared_length=store.max_object_bytes + 1, expected_sha256="0" * 64)
    assert source.tell() == 0


def test_interrupted_transfer_is_cleaned_up(store):
    class Broken(io.RawIOBase):
        def __init__(self):
            self.calls = 0

        def read(self, size=-1):
            self.calls += 1
            if self.calls > 2:
                raise ConnectionResetError("client went away")
            return b"x" * min(size, 4096)

    oid = new_id()
    with pytest.raises(ConnectionResetError):
        store.put(oid, Broken(), declared_length=100_000, expected_sha256="0" * 64)
    assert store.stat(oid) is None
    assert leftovers(store) == []


def test_disk_full_maps_to_insufficient_storage(store):
    class DiskFull(io.RawIOBase):
        def read(self, size=-1):
            raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(InsufficientStorage):
        store.put(new_id(), DiskFull(), declared_length=10, expected_sha256="0" * 64)
    assert leftovers(store) == []


def test_free_space_floor_is_enforced(tmp_path):
    s = BlobStore(tmp_path, max_object_bytes=1024, min_free_bytes=10**18)
    s.ensure_layout()
    with pytest.raises(InsufficientStorage):
        s.put(new_id(), stream(b"a"), declared_length=1, expected_sha256=sha(b"a"))


def test_put_is_idempotent_and_detects_conflicts(store):
    oid = new_id()
    store.put(oid, stream(b"one"), declared_length=3, expected_sha256=sha(b"one"))
    outcome, _ = store.put(oid, stream(b"one"), declared_length=3, expected_sha256=sha(b"one"))
    assert outcome is PutOutcome.ALREADY_EXISTS
    with pytest.raises(ObjectConflict):
        store.put(oid, stream(b"two"), declared_length=3, expected_sha256=sha(b"two"))
    assert read_all(store, oid) == b"one"


def test_concurrent_writers_of_same_key_produce_one_object(store):
    oid = new_id()
    data = os.urandom(200_000)
    outcomes: list[PutOutcome] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            outcomes.append(
                store.put(oid, stream(data), declared_length=len(data), expected_sha256=sha(data))[0]
            )
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert outcomes.count(PutOutcome.CREATED) == 1
    assert outcomes.count(PutOutcome.ALREADY_EXISTS) == 7
    assert read_all(store, oid) == data
    assert leftovers(store) == []


def test_delete_is_idempotent(store):
    oid = new_id()
    store.put(oid, stream(b"z"), declared_length=1, expected_sha256=sha(b"z"))
    assert store.delete(oid) is True
    assert store.delete(oid) is False
    with pytest.raises(ObjectNotFound):
        store.open(oid)
    assert os.listdir(store.trash_dir) == []


def test_verify_detects_bit_rot(store):
    oid = new_id()
    store.put(oid, stream(b"hello world"), declared_length=11, expected_sha256=sha(b"hello world"))
    (store.object_dir(oid) / "data").write_bytes(b"hello w0rld")
    report = store.verify(oid)
    assert not report.ok
    assert report.actual_sha256 == sha(b"hello w0rld")


def test_cleanup_removes_only_stale_entries(store):
    old = store.tmp_dir / "abandoned"
    old.mkdir()
    past = time.time() - 7200
    os.utime(old, (past, past))
    fresh = store.tmp_dir / "in-progress"
    fresh.mkdir()
    assert store.cleanup_stale(3600) == 1
    assert not old.exists() and fresh.exists()


def test_iter_objects_is_ordered_and_resumable(store):
    ids = [new_id() for _ in range(5)]
    for oid in ids:
        store.put(oid, stream(b"d"), declared_length=1, expected_sha256=sha(b"d"))
    listed = [info.object_id for info in store.iter_objects()]
    assert listed == sorted(ids, key=lambda u: u.hex)
    assert [i.object_id for i in store.iter_objects(after=listed[1])] == listed[2:]


def test_storage_paths_stay_inside_objects_dir(store):
    for _ in range(100):
        path = store.object_dir(new_id()).resolve()
        assert path.is_relative_to(store.objects_dir.resolve())
