"""Inline previews: deciding what may be shown, and how, without trusting the client.

A preview reuses the verified download path (ownership check, spool-and-verify
from any healthy replica). What differs is only how the bytes are served:

* Only four formats are ever rendered inline: JPEG, PNG, WebP and PDF, plus
  UTF-8 text shown as ``text/plain``. SVG, HTML and every other format are
  never rendered; HTML source is shown as plain text.
* The client-declared Content-Type and the file extension only select a
  *candidate* (and let the UI show a preview button). The served type is
  decided from the verified bytes themselves (magic numbers / strict UTF-8);
  a mismatch is refused.
* Size limits are checked against the metadata before any node is contacted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO

from django.conf import settings

from files.models import File


class Kind(StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    TEXT = "text"


@dataclass(frozen=True)
class Verdict:
    kind: Kind | None
    reason: str = ""  # stable code when kind is None


IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
TEXT_EXTENSIONS = {"txt", "log", "md", "csv", "tsv", "json", "yaml", "yml", "ini", "conf", "toml"}

# Response CSP for previewed bytes: no scripts, no plugins, no framing.
IMAGE_TEXT_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
# Browsers' built-in PDF viewers are embedded plugins: object-src must allow
# the document itself, and a CSP sandbox makes Chromium refuse to render it.
PDF_CSP = (
    "default-src 'none'; object-src 'self'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def candidate(file: File) -> Verdict:
    """What a preview of ``file`` would be, from metadata only (no node traffic)."""
    declared, ext = file.content_type, _extension(file.original_name)
    if declared in IMAGE_TYPES or ext in IMAGE_EXTENSIONS:
        kind, limit = Kind.IMAGE, settings.NODEMESH_PREVIEW_MAX_BYTES
    elif declared == "application/pdf" or ext == "pdf":
        kind, limit = Kind.PDF, settings.NODEMESH_PREVIEW_MAX_BYTES
    elif declared.startswith("text/") or ext in TEXT_EXTENSIONS:
        kind, limit = Kind.TEXT, settings.NODEMESH_PREVIEW_TEXT_MAX_BYTES
    else:
        return Verdict(None, "preview_unsupported")
    if file.size > limit:
        return Verdict(None, "preview_too_large")
    return Verdict(kind)


def sniff(kind: Kind, handle: BinaryIO) -> str | None:
    """Content-Type to serve for verified bytes, or ``None`` if they do not match ``kind``."""
    handle.seek(0)
    head = handle.read(16)
    handle.seek(0)
    if kind is Kind.IMAGE:
        if head.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
        return None
    if kind is Kind.PDF:
        return "application/pdf" if head.startswith(b"%PDF-") else None
    # Text: bounded by NODEMESH_PREVIEW_TEXT_MAX_BYTES before we get here.
    data = handle.read()
    handle.seek(0)
    if b"\x00" in data:
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "text/plain; charset=utf-8"


def csp_for(content_type: str) -> str:
    return PDF_CSP if content_type == "application/pdf" else IMAGE_TEXT_CSP
