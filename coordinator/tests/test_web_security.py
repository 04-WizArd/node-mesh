"""Authentication, authorisation, CSRF, input validation and XSS regressions."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from files.models import File

from .conftest import UPLOAD_URL, upload

STATIC = Path(__file__).resolve().parents[1] / "static"
TEMPLATES = Path(__file__).resolve().parents[1]


@pytest.fixture
def foreign_file(other_user):
    return File.objects.create(
        owner=other_user,
        original_name="secret.txt",
        size=3,
        sha256="0" * 64,
        desired_replicas=3,
        status=File.Status.AVAILABLE,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", UPLOAD_URL),
        ("POST", UPLOAD_URL),
        ("GET", f"{UPLOAD_URL}{uuid.uuid4()}/"),
        ("DELETE", f"{UPLOAD_URL}{uuid.uuid4()}/"),
        ("GET", f"{UPLOAD_URL}{uuid.uuid4()}/content"),
        ("GET", "/api/v1/cluster/nodes/"),
    ],
)
def test_api_requires_authentication(client, method, path):
    response = client.generic(method, path)
    assert response.status_code == 401
    assert response.json() == {"error": "authentication_required"}


@pytest.mark.django_db
def test_index_redirects_anonymous_users_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/login/")


def test_other_users_files_are_invisible(api, foreign_file):
    base = f"{UPLOAD_URL}{foreign_file.id}/"
    assert api.get(base).status_code == 404
    assert api.get(base + "content").status_code == 404
    assert api.delete(base).status_code == 404
    assert api.get(UPLOAD_URL).json()["files"] == []
    foreign_file.refresh_from_db()
    assert foreign_file.status == File.Status.AVAILABLE


def test_mutations_require_csrf_token(user, foreign_file):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert upload(client, b"data").status_code == 403
    assert client.delete(f"{UPLOAD_URL}{uuid.uuid4()}/").status_code == 403
    assert client.post("/accounts/logout/").status_code == 403


def test_upload_rejects_oversized_declared_length(api, settings):
    settings.NODEMESH_MAX_UPLOAD_BYTES = 10
    response = upload(api, b"x" * 11)
    assert response.status_code == 413
    assert response.json()["error"] == "file_too_large"
    assert File.objects.count() == 0


def test_upload_rejects_chunked_and_form_bodies(api):
    assert upload(api, b"x", **{"Transfer-Encoding": "chunked"}).status_code == 411
    response = api.post(UPLOAD_URL, {"file": "x"})  # multipart form
    assert response.status_code == 415


def test_upload_validates_idempotency_key(api):
    response = upload(api, b"x", key="bad key with spaces")
    assert response.status_code == 400


def test_nodes_endpoint_is_staff_only(api, user):
    assert api.get("/api/v1/cluster/nodes/").status_code == 403
    user.is_staff = True
    user.save()
    assert api.get("/api/v1/cluster/nodes/").status_code == 200


@pytest.mark.django_db
def test_login_redirects_instead_of_returning_tokens(client):
    get_user_model().objects.create_user("bob", password="correct-horse-battery")
    response = client.post("/accounts/login/", {"username": "bob", "password": "correct-horse-battery"})
    assert response.status_code == 302 and response["Location"] == "/"
    assert "application/json" not in response.get("Content-Type", "")


@pytest.mark.django_db
def test_logout_requires_post(client, user):
    client.force_login(user)
    assert client.get("/accounts/logout/").status_code == 405
    assert client.post("/accounts/logout/").status_code == 302
    assert client.get(UPLOAD_URL).status_code == 401


@pytest.mark.django_db
def test_registration_creates_and_logs_in_user(client):
    response = client.post(
        "/accounts/register/",
        {"username": "carol", "password1": "Unusual-pass-9271", "password2": "Unusual-pass-9271"},
    )
    assert response.status_code == 302
    assert client.get(UPLOAD_URL).status_code == 200


@pytest.mark.django_db
def test_registration_enforces_password_policy(client):
    response = client.post(
        "/accounts/register/", {"username": "dave", "password1": "123", "password2": "123"}
    )
    assert response.status_code == 200
    assert not get_user_model().objects.filter(username="dave").exists()


def test_security_headers_on_pages(api):
    response = api.get("/")
    assert response.status_code == 200
    csp = response["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Referrer-Policy"] == "same-origin"


def test_no_html_sinks_in_frontend_code():
    """Stored-XSS regression guard: user data must never reach an HTML sink."""
    source = (STATIC / "js" / "files.js").read_text()
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "onclick="):
        assert sink not in code, sink


def test_no_inline_scripts_in_templates():
    for template in TEMPLATES.rglob("templates/**/*.html"):
        html = template.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), template
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", html, re.IGNORECASE), template


@pytest.mark.django_db
def test_api_unknown_routes_return_json(client):
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


@pytest.mark.django_db
def test_probes(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
