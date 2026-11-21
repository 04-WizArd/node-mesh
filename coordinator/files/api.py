"""Client-facing JSON API (v1). Session authentication + CSRF; see docs/api.md."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from cluster.models import StorageNode
from files.models import File
from files.services import quorum
from files.services.download import NoReplicaAvailable, fetch_verified
from files.services.upload import IdempotencyConflict, UploadInProgress, upload
from files.spool import SpoolError, TooLarge, clean_content_type, clean_filename, spool_stream

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
FORM_CONTENT_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")
MAX_PAGE_SIZE = 200


def api_error(status: int, code: str, **extra) -> JsonResponse:
    return JsonResponse({"error": code, **extra}, status=status)


def api_login_required(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if not request.user.is_authenticated:
            return api_error(401, "authentication_required")
        return view(request, *args, **kwargs)

    return wrapper


def serialize_file(file: File) -> dict:
    stored = getattr(file, "stored_replicas", None)
    countable = getattr(file, "countable_replicas", None)
    return {
        "id": str(file.id),
        "name": file.original_name,
        "content_type": file.content_type,
        "size": file.size,
        "sha256": file.sha256,
        "status": file.status,
        "created_at": file.created_at.isoformat(),
        "replication": {
            "desired": file.desired_replicas,
            "stored": stored,
            "healthy": countable,
            "under_replicated": None
            if countable is None
            else quorum.is_under_replicated(countable, file.desired_replicas),
        },
        "download_url": reverse("files:api-file-content", args=[file.id]),
    }


def _owned_file(request: HttpRequest, file_id: uuid.UUID) -> File | None:
    """Return the caller's AVAILABLE file, or None (404 for absent *and* foreign files)."""
    queryset = quorum.with_replica_counts(
        File.objects.filter(pk=file_id, owner=request.user, status=File.Status.AVAILABLE)
    )
    return queryset.first()


# --- /api/v1/files/ -----------------------------------------------------------------
@api_login_required
@require_http_methods(["GET", "POST"])
def file_collection(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        return _upload(request)
    try:
        limit = min(int(request.GET.get("limit", "50")), MAX_PAGE_SIZE)
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        return api_error(400, "bad_query")
    if limit < 1 or offset < 0:
        return api_error(400, "bad_query")
    queryset = File.objects.filter(owner=request.user, status=File.Status.AVAILABLE)
    total = queryset.count()
    page = quorum.with_replica_counts(queryset.order_by("-created_at"))[offset : offset + limit]
    return JsonResponse(
        {
            "files": [serialize_file(f) for f in page],
            "count": total,
            "limit": limit,
            "offset": offset,
        }
    )


def _upload(request: HttpRequest) -> HttpResponse:
    content_type = (request.content_type or "").lower()
    if content_type in FORM_CONTENT_TYPES:
        return api_error(415, "raw_body_required")
    if "chunked" in request.META.get("HTTP_TRANSFER_ENCODING", "").lower():
        return api_error(411, "length_required")
    # RFC 9112 §6.3: a request without Content-Length or Transfer-Encoding has an empty body.
    raw_length = request.META.get("CONTENT_LENGTH") or "0"
    try:
        declared = int(raw_length)
    except ValueError:
        return api_error(400, "bad_length")
    limit = settings.NODEMESH_MAX_UPLOAD_BYTES
    if declared < 0:
        return api_error(400, "bad_length")
    if declared > limit:
        return api_error(413, "file_too_large", max_bytes=limit)

    key = request.headers.get("Idempotency-Key") or None
    if key is not None and not IDEMPOTENCY_KEY_RE.match(key):
        return api_error(400, "bad_idempotency_key")

    try:
        spooled = spool_stream(request, declared_length=declared, limit=limit)
    except TooLarge:
        return api_error(413, "file_too_large", max_bytes=limit)
    except SpoolError as exc:
        return api_error(400, exc.code)

    try:
        result = upload(
            owner=request.user,
            spooled=spooled,
            original_name=clean_filename(request.headers.get("X-Filename", "")),
            content_type=clean_content_type(request.content_type or ""),
            idempotency_key=key,
        )
    except IdempotencyConflict as exc:
        return api_error(409, exc.code)
    except UploadInProgress as exc:
        response = api_error(409, exc.code)
        response["Retry-After"] = "5"
        return response
    finally:
        spooled.discard()

    if not result.available:
        return api_error(
            503,
            "replication_quorum_not_met",
            stored=result.stored,
            required=settings.NODEMESH_WRITE_QUORUM,
        )
    return JsonResponse(
        {"file": serialize_file(result.file), "replayed": result.replayed},
        status=200 if result.replayed else 201,
    )


# --- /api/v1/files/<id>/ ---------------------------------------------------------------
@api_login_required
@require_http_methods(["GET", "DELETE"])
def file_detail(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    if request.method == "DELETE":
        with transaction.atomic():
            file = (
                File.objects.select_for_update()
                .filter(pk=file_id, owner=request.user, status=File.Status.AVAILABLE)
                .first()
            )
            if file is None:
                return api_error(404, "not_found")
            file.status = File.Status.DELETED
            file.idempotency_key = None
            file.save(update_fields=["status", "idempotency_key", "updated_at"])
        logger.info("file deleted by owner", extra={"file_id": str(file_id)})
        return HttpResponse(status=204)

    file = _owned_file(request, file_id)
    if file is None:
        return api_error(404, "not_found")
    return JsonResponse({"file": serialize_file(file)})


@api_login_required
@require_GET
def file_content(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    file = _owned_file(request, file_id)
    if file is None:
        return api_error(404, "not_found")
    try:
        content = fetch_verified(file)
    except NoReplicaAvailable as exc:
        return api_error(503, exc.code)
    response = FileResponse(
        content.handle,
        as_attachment=True,
        filename=file.original_name,
        content_type="application/octet-stream",
    )
    response["Content-Length"] = str(file.size)
    response["X-Content-SHA256"] = file.sha256
    response["Cache-Control"] = "private, no-store"
    return response


# --- /api/v1/cluster/nodes/ (staff only) -----------------------------------------------
@api_login_required
@require_GET
def node_list(request: HttpRequest) -> HttpResponse:
    if not request.user.is_staff:
        return api_error(403, "forbidden")
    nodes = [
        {
            "node_id": n.node_id,
            "admin_state": n.admin_state,
            "health": n.health,
            "consecutive_failures": n.consecutive_failures,
            "last_seen_at": n.last_seen_at.isoformat() if n.last_seen_at else None,
            "disk_total_bytes": n.disk_total_bytes,
            "disk_free_bytes": n.disk_free_bytes,
        }
        for n in StorageNode.objects.all()
    ]
    return JsonResponse({"nodes": nodes})
