"""Preview policy and endpoint behaviour that needs no storage node."""

from __future__ import annotations

import io
import uuid

import pytest

from files.models import File
from files.services.preview import Kind, candidate, sniff

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"


def make(owner, name="a.bin", content_type="application/octet-stream", size=10, status="available"):
    return File.objects.create(
        owner=owner,
        original_name=name,
        content_type=content_type,
        size=size,
        sha256="0" * 64,
        desired_replicas=3,
        status=status,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "content_type", "expected"),
    [
        ("photo.jpg", "image/jpeg", Kind.IMAGE),
        ("photo.PNG", "application/octet-stream", Kind.IMAGE),  # extension is enough as a candidate
        ("pic.webp", "image/webp", Kind.IMAGE),
        ("report.pdf", "application/pdf", Kind.PDF),
        ("notes.txt", "text/plain", Kind.TEXT),
        ("data.csv", "application/octet-stream", Kind.TEXT),
        ("page.html", "text/html", Kind.TEXT),  # shown as source text, never rendered
        ("logo.svg", "image/svg+xml", None),  # scriptable: never previewed
        ("archive.zip", "application/zip", None),
        ("noext", "application/octet-stream", None),
    ],
)
def test_candidate_from_metadata(user, name, content_type, expected):
    assert candidate(make(user, name, content_type)).kind == expected


@pytest.mark.django_db
def test_candidate_enforces_size_limits(user, settings):
    settings.NODEMESH_PREVIEW_MAX_BYTES = 100
    settings.NODEMESH_PREVIEW_TEXT_MAX_BYTES = 10
    assert candidate(make(user, "a.png", "image/png", size=101)).reason == "preview_too_large"
    assert candidate(make(user, "a.png", "image/png", size=100)).kind is Kind.IMAGE
    assert candidate(make(user, "a.txt", "text/plain", size=11)).reason == "preview_too_large"


@pytest.mark.parametrize(
    ("kind", "data", "expected"),
    [
        (Kind.IMAGE, PNG, "image/png"),
        (Kind.IMAGE, JPEG, "image/jpeg"),
        (Kind.IMAGE, WEBP, "image/webp"),
        (Kind.IMAGE, b"<svg onload=alert(1)>", None),
        (Kind.IMAGE, b"<html><script>alert(1)</script>", None),
        (Kind.PDF, PDF, "application/pdf"),
        (Kind.PDF, PNG, None),
        (Kind.TEXT, "Bonjour, été ✓\n".encode(), "text/plain; charset=utf-8"),
        (Kind.TEXT, b"<script>alert(1)</script>", "text/plain; charset=utf-8"),
        (Kind.TEXT, b"binary\x00data", None),
        (Kind.TEXT, b"\xff\xfe\xfa invalid utf-8", None),
    ],
)
def test_sniff_decides_from_bytes(kind, data, expected):
    handle = io.BytesIO(data)
    assert sniff(kind, handle) == expected
    assert handle.tell() == 0  # left at the start for streaming


@pytest.mark.django_db
def test_preview_requires_authentication(client):
    response = client.get(f"/api/v1/files/{uuid.uuid4()}/preview")
    assert response.status_code == 401


def test_preview_of_another_users_file_is_not_found(api, other_user):
    foreign = make(other_user, "secret.png", "image/png")
    assert api.get(f"/api/v1/files/{foreign.id}/preview").status_code == 404


def test_preview_of_unavailable_file_is_not_found(api, user):
    pending = make(user, "a.png", "image/png", status="pending")
    assert api.get(f"/api/v1/files/{pending.id}/preview").status_code == 404


def test_unsupported_and_oversized_previews_never_reach_nodes(api, user, settings):
    # No node is registered: had the coordinator tried to fetch, it would answer 503.
    settings.NODEMESH_PREVIEW_MAX_BYTES = 100
    zip_file = make(user, "a.zip", "application/zip")
    big = make(user, "big.png", "image/png", size=101)
    unsupported = api.get(f"/api/v1/files/{zip_file.id}/preview")
    assert unsupported.status_code == 415 and unsupported.json() == {"error": "preview_unsupported"}
    too_large = api.get(f"/api/v1/files/{big.id}/preview")
    assert too_large.status_code == 413 and too_large.json() == {"error": "preview_too_large"}


def test_file_listing_announces_preview_capability(api, user):
    make(user, "photo.png", "image/png")
    make(user, "archive.zip", "application/zip")
    files = {f["name"]: f for f in api.get("/api/v1/files/").json()["files"]}
    assert files["photo.png"]["preview"]["kind"] == "image"
    assert files["photo.png"]["preview"]["url"].endswith("/preview")
    assert files["archive.zip"]["preview"] == {
        "kind": None,
        "unavailable_reason": "preview_unsupported",
        "url": None,
    }
