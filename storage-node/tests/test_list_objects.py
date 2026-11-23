"""The read-only inventory behind `make cluster-storage`."""

from __future__ import annotations

import os
from io import StringIO
from pathlib import Path

from django.core.management import call_command

from objects.blobstore import BlobStore

from .conftest import new_id, sha, stream


def inventory() -> str:
    out = StringIO()
    call_command("list_objects", stdout=out)
    return out.getvalue()


def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    """Every path under ``root`` with its mtime and content (directories: mtime only)."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            content = b"" if path.is_dir() else path.read_bytes()
            result[str(path)] = (path.stat().st_mtime_ns, content)
    return result


def make_store(settings) -> BlobStore:
    store = BlobStore(settings.NODE_DATA_DIR, max_object_bytes=1 << 20)
    store.ensure_layout()
    return store


def test_empty_node_is_reported_without_creating_anything(settings):
    assert not settings.NODE_DATA_DIR.exists()
    text = inventory()
    assert "node node-test" in text and "objects: 0 (0 bytes)" in text
    assert not settings.NODE_DATA_DIR.exists()  # read-only: no directory created


def test_lists_objects_with_their_recorded_sha256_without_modifying_them(settings):
    store = make_store(settings)
    ids = sorted((new_id() for _ in range(3)), key=lambda u: u.hex)
    for i, oid in enumerate(ids):
        data = f"object {i}".encode()
        store.put(oid, stream(data), declared_length=len(data), expected_sha256=sha(data))
    before = snapshot(settings.NODE_DATA_DIR)

    text = inventory()

    assert "objects: 3 (24 bytes)" in text
    rows = text.splitlines()[3:6]  # after: node line, count line, column header
    assert [row.split()[0] for row in rows] == [str(o) for o in ids]  # sorted by id
    for i, row in enumerate(rows):
        assert row.split()[2] == sha(f"object {i}".encode())
    assert snapshot(settings.NODE_DATA_DIR) == before  # nothing created, modified or touched


def test_unreadable_metadata_is_reported_not_hidden(settings):
    store = make_store(settings)
    good, bad = new_id(), new_id()
    for oid in (good, bad):
        store.put(oid, stream(b"x"), declared_length=1, expected_sha256=sha(b"x"))
    (store.object_dir(bad) / "meta.json").write_text("{not json")

    text = inventory()

    assert "objects: 1 (1 bytes)" in text and str(good) in text
    assert "unreadable entries: 1" in text and bad.hex in text
