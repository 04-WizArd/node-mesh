"""Bounded spooling of request/response bodies to temporary files.

Uploads are streamed from the client into a temporary file while the size is
counted and the SHA-256 is computed, so memory use is constant regardless of
the file size, and the limit applies to bytes actually received.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote

from django.conf import settings

_CONTENT_TYPE_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]{0,126}/[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]{0,126}$"
)
DEFAULT_CONTENT_TYPE = "application/octet-stream"
DEFAULT_FILENAME = "fichier"
MAX_FILENAME_LENGTH = 255


class SpoolError(Exception):
    code = "spool_error"


class TooLarge(SpoolError):
    code = "file_too_large"


class Truncated(SpoolError):
    code = "incomplete_upload"


@dataclass
class SpooledFile:
    path: Path
    size: int
    sha256: str

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


def spool_dir() -> Path:
    directory = Path(settings.NODEMESH_SPOOL_DIR)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def spool_stream(stream: BinaryIO, *, declared_length: int, limit: int) -> SpooledFile:
    """Copy exactly ``declared_length`` bytes from ``stream`` into a temp file."""
    if declared_length > limit:
        raise TooLarge(f"declared size exceeds {limit} bytes")
    fd, name = tempfile.mkstemp(prefix="upload-", dir=spool_dir())
    path = Path(name)
    hasher = hashlib.sha256()
    received = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while received < declared_length:
                chunk = stream.read(min(settings.NODEMESH_IO_CHUNK_BYTES, declared_length - received))
                if not chunk:
                    raise Truncated(f"received {received} of {declared_length} bytes")
                received += len(chunk)
                if received > limit:
                    raise TooLarge("upload exceeds the configured limit")
                hasher.update(chunk)
                out.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return SpooledFile(path=path, size=received, sha256=hasher.hexdigest())


def spool_chunks(chunks: Iterable[bytes], out: BinaryIO, *, max_bytes: int) -> tuple[int, str]:
    """Write ``chunks`` to ``out`` while hashing; stop as soon as ``max_bytes`` is exceeded."""
    hasher = hashlib.sha256()
    size = 0
    for chunk in chunks:
        size += len(chunk)
        if size > max_bytes:
            raise TooLarge("body longer than expected")
        hasher.update(chunk)
        out.write(chunk)
    return size, hasher.hexdigest()


def clean_filename(raw: str) -> str:
    """Decode and sanitise a client-supplied filename for *display only*.

    The result is never used as a filesystem path. Path components, control
    characters and surrounding whitespace are removed and the length bounded.
    """
    try:
        name = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        name = ""
    name = unicodedata.normalize("NFC", name)
    name = re.split(r"[\\/]", name)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip()
    if name in ("", ".", ".."):
        return DEFAULT_FILENAME
    return name[:MAX_FILENAME_LENGTH]


def clean_content_type(raw: str) -> str:
    value = raw.split(";", 1)[0].strip().lower()
    return value if _CONTENT_TYPE_RE.match(value) else DEFAULT_CONTENT_TYPE
