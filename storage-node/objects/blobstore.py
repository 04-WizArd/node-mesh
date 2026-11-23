"""Durable, content-verified object storage on a local filesystem.

Layout under the node's data directory::

    objects/ab/cd/<uuid-hex>/data        object bytes
    objects/ab/cd/<uuid-hex>/meta.json   {"object_id", "size", "sha256", "stored_at"}
    tmp/                                 in-progress writes (same filesystem)
    trash/                               deletions in progress

Write protocol (``put``):

1. Stream the body into ``tmp/<hex>.<random>/data`` in bounded chunks while
   counting bytes and computing SHA-256. Never read more than the declared
   length, never accept more than ``max_object_bytes``.
2. Reject the write if the byte count or digest differs from what the caller
   declared (the digest is covered by the request signature).
3. ``fsync`` the data file, write and ``fsync`` ``meta.json``, ``fsync`` the
   temporary directory.
4. ``rename`` the temporary directory to its final location and ``fsync`` the
   parent. A directory rename is atomic and fails if the target already exists,
   so an object is either entirely visible or not visible at all, and two
   concurrent writers of the same key cannot both "win".

Keys are server-generated UUIDs: no client-provided name ever reaches a path.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Protocol

DATA_FILE = "data"
META_FILE = "meta.json"
META_FORMAT = 1


class Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class BlobStoreError(Exception):
    code = "storage_error"


class ObjectTooLarge(BlobStoreError):
    code = "object_too_large"


class InsufficientStorage(BlobStoreError):
    code = "insufficient_storage"


class DigestMismatch(BlobStoreError):
    code = "digest_mismatch"


class IncompleteBody(BlobStoreError):
    code = "incomplete_body"


class ObjectConflict(BlobStoreError):
    code = "object_conflict"


class ObjectNotFound(BlobStoreError):
    code = "not_found"


class CorruptObject(BlobStoreError):
    code = "corrupt_object"


@dataclass(frozen=True)
class ObjectInfo:
    object_id: uuid.UUID
    size: int
    sha256: str
    stored_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "object_id": str(self.object_id),
            "size": self.size,
            "sha256": self.sha256,
            "stored_at": self.stored_at,
        }


@dataclass(frozen=True)
class IntegrityReport:
    info: ObjectInfo
    actual_size: int
    actual_sha256: str

    @property
    def ok(self) -> bool:
        return self.actual_size == self.info.size and self.actual_sha256 == self.info.sha256


@dataclass(frozen=True)
class DiskUsage:
    total: int
    used: int
    free: int


class PutOutcome(Enum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"


class BlobStore:
    def __init__(
        self,
        root: Path,
        *,
        max_object_bytes: int,
        min_free_bytes: int = 0,
        chunk_size: int = 1024 * 1024,
        fsync: bool = True,
    ) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.tmp_dir = self.root / "tmp"
        self.trash_dir = self.root / "trash"
        self.max_object_bytes = max_object_bytes
        self.min_free_bytes = min_free_bytes
        self.chunk_size = chunk_size
        self.fsync_enabled = fsync

    # -- layout ------------------------------------------------------------
    def ensure_layout(self) -> None:
        for directory in (self.objects_dir, self.tmp_dir, self.trash_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def object_dir(self, object_id: uuid.UUID) -> Path:
        key = object_id.hex  # 32 lowercase hex chars: cannot contain separators
        return self.objects_dir / key[:2] / key[2:4] / key

    # -- durability helpers -------------------------------------------------
    def _fsync_file(self, handle: BinaryIO) -> None:
        handle.flush()
        if self.fsync_enabled:
            os.fsync(handle.fileno())

    def _fsync_dir(self, directory: Path) -> None:
        if not self.fsync_enabled:
            return
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _ensure_dir(self, directory: Path) -> None:
        """Create ``directory`` (and parents) making each new entry durable."""
        missing: list[Path] = []
        current = directory
        while not current.exists():
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            try:
                path.mkdir()
            except FileExistsError:
                continue
            self._fsync_dir(path.parent)

    # -- capacity -------------------------------------------------------------
    def disk_usage(self) -> DiskUsage:
        usage = shutil.disk_usage(self.root)
        return DiskUsage(total=usage.total, used=usage.used, free=usage.free)

    def has_room_for(self, size: int) -> bool:
        return self.disk_usage().free - size >= self.min_free_bytes

    # -- reads -----------------------------------------------------------------
    def stat(self, object_id: uuid.UUID) -> ObjectInfo | None:
        directory = self.object_dir(object_id)
        meta_path = directory / META_FILE
        try:
            raw = meta_path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            meta = json.loads(raw)
            info = ObjectInfo(
                object_id=uuid.UUID(meta["object_id"]),
                size=int(meta["size"]),
                sha256=str(meta["sha256"]),
                stored_at=str(meta["stored_at"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise CorruptObject("unreadable object metadata") from exc
        if info.object_id != object_id or not (directory / DATA_FILE).is_file():
            raise CorruptObject("object metadata does not match its location")
        return info

    def open(self, object_id: uuid.UUID) -> tuple[ObjectInfo, BinaryIO]:
        info = self.stat(object_id)
        if info is None:
            raise ObjectNotFound(str(object_id))
        try:
            handle = open(self.object_dir(object_id) / DATA_FILE, "rb")  # noqa: SIM115
        except FileNotFoundError as exc:  # deleted between stat and open
            raise ObjectNotFound(str(object_id)) from exc
        return info, handle

    def verify(self, object_id: uuid.UUID) -> IntegrityReport:
        info, handle = self.open(object_id)
        hasher = hashlib.sha256()
        size = 0
        with handle:
            while chunk := handle.read(self.chunk_size):
                hasher.update(chunk)
                size += len(chunk)
        return IntegrityReport(info=info, actual_size=size, actual_sha256=hasher.hexdigest())

    def iter_objects(
        self, after: uuid.UUID | None = None, unreadable: list[str] | None = None
    ) -> Iterator[ObjectInfo]:
        """Yield stored objects in ascending key order, starting after ``after``.

        Entries whose name or metadata cannot be read are skipped; pass a list
        as ``unreadable`` to collect their directory names instead of losing them.
        """
        after_hex = after.hex if after else ""
        if not self.objects_dir.is_dir():
            return
        for level1 in sorted(p.name for p in os.scandir(self.objects_dir) if p.is_dir()):
            if level1 < after_hex[:2]:
                continue
            for level2 in sorted(p.name for p in os.scandir(self.objects_dir / level1) if p.is_dir()):
                if (level1 + level2) < after_hex[:4]:
                    continue
                parent = self.objects_dir / level1 / level2
                for key in sorted(p.name for p in os.scandir(parent) if p.is_dir()):
                    if key <= after_hex:
                        continue
                    try:
                        info = self.stat(uuid.UUID(hex=key))
                    except (ValueError, CorruptObject):
                        if unreadable is not None:
                            unreadable.append(key)
                        continue
                    if info is not None:
                        yield info

    # -- writes ----------------------------------------------------------------
    def put(
        self,
        object_id: uuid.UUID,
        stream: Readable,
        *,
        declared_length: int,
        expected_sha256: str,
    ) -> tuple[PutOutcome, ObjectInfo]:
        if declared_length < 0:
            raise IncompleteBody("negative length")
        if declared_length > self.max_object_bytes:
            raise ObjectTooLarge(f"object exceeds {self.max_object_bytes} bytes")

        existing = self.stat(object_id)
        if existing is not None:
            return self._resolve_existing(existing, declared_length, expected_sha256)

        if not self.has_room_for(declared_length):
            raise InsufficientStorage("not enough free space")

        self._ensure_dir(self.tmp_dir)
        tmp = self.tmp_dir / f"{object_id.hex}.{uuid.uuid4().hex[:12]}"
        tmp.mkdir()
        try:
            info = self._write_temp(tmp, object_id, stream, declared_length, expected_sha256)
            target = self.object_dir(object_id)
            self._ensure_dir(target.parent)
            try:
                os.rename(tmp, target)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                # A concurrent writer published the same key first.
                winner = self.stat(object_id)
                if winner is None:
                    raise
                return self._resolve_existing(winner, declared_length, expected_sha256)
            self._fsync_dir(target.parent)
            return PutOutcome.CREATED, info
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise InsufficientStorage("disk full") from exc
            raise
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    def _resolve_existing(
        self, existing: ObjectInfo, declared_length: int, expected_sha256: str
    ) -> tuple[PutOutcome, ObjectInfo]:
        if existing.sha256 == expected_sha256 and existing.size == declared_length:
            return PutOutcome.ALREADY_EXISTS, existing
        raise ObjectConflict("a different object is already stored under this id")

    def _write_temp(
        self,
        tmp: Path,
        object_id: uuid.UUID,
        stream: Readable,
        declared_length: int,
        expected_sha256: str,
    ) -> ObjectInfo:
        hasher = hashlib.sha256()
        received = 0
        with open(tmp / DATA_FILE, "xb") as out:
            while received < declared_length:
                chunk = stream.read(min(self.chunk_size, declared_length - received))
                if not chunk:
                    raise IncompleteBody(f"body ended after {received} of {declared_length} bytes")
                received += len(chunk)
                if received > self.max_object_bytes:  # defence in depth
                    raise ObjectTooLarge("object exceeds the configured limit")
                hasher.update(chunk)
                out.write(chunk)
            self._fsync_file(out)

        digest = hasher.hexdigest()
        if digest != expected_sha256:
            raise DigestMismatch("received content does not match the declared SHA-256")

        info = ObjectInfo(
            object_id=object_id,
            size=received,
            sha256=digest,
            stored_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        meta = {"format": META_FORMAT, **info.as_dict()}
        with open(tmp / META_FILE, "xb") as out:
            out.write(json.dumps(meta, sort_keys=True).encode("utf-8"))
            self._fsync_file(out)
        self._fsync_dir(tmp)
        return info

    def delete(self, object_id: uuid.UUID) -> bool:
        """Remove an object. Returns ``False`` when it did not exist (idempotent)."""
        target = self.object_dir(object_id)
        self._ensure_dir(self.trash_dir)
        graveyard = self.trash_dir / f"{object_id.hex}.{uuid.uuid4().hex[:12]}"
        try:
            os.rename(target, graveyard)
        except FileNotFoundError:
            return False
        self._fsync_dir(target.parent)
        shutil.rmtree(graveyard, ignore_errors=True)
        return True

    # -- maintenance -------------------------------------------------------------
    def cleanup_stale(self, max_age_seconds: float) -> int:
        """Remove abandoned temporary writes and unfinished deletions."""
        cutoff = time.time() - max_age_seconds
        removed = 0
        for directory in (self.tmp_dir, self.trash_dir):
            if not directory.is_dir():
                continue
            for entry in os.scandir(directory):
                try:
                    if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                        continue
                except FileNotFoundError:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    Path(entry.path).unlink(missing_ok=True)
                removed += 1
        return removed

    def is_writable(self) -> bool:
        try:
            self._ensure_dir(self.tmp_dir)
            probe = self.tmp_dir / f".probe-{uuid.uuid4().hex}"
            probe.write_bytes(b"")
            probe.unlink()
            return True
        except OSError:
            return False
