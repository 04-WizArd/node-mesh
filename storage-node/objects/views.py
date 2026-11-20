"""Internal storage-node API (v1). Only the coordinator may call these endpoints.

See docs/api.md for the full contract. Every response is JSON except object
downloads. Errors use ``{"error": <stable code>, "detail": <optional text>}``.
"""

from __future__ import annotations

import logging
import uuid
from itertools import islice

from django.conf import settings
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET, require_http_methods

from .apps import get_store
from .auth import error, signed_request
from .blobstore import (
    BlobStoreError,
    CorruptObject,
    DigestMismatch,
    IncompleteBody,
    InsufficientStorage,
    ObjectConflict,
    ObjectNotFound,
    ObjectTooLarge,
    PutOutcome,
)

logger = logging.getLogger(__name__)

HEADER_SHA256 = "X-NodeMesh-Content-SHA256"
HEADER_SIZE = "X-NodeMesh-Object-Size"
MAX_LIST_LIMIT = 1000

_STATUS_BY_ERROR: dict[type[BlobStoreError], int] = {
    ObjectTooLarge: 413,
    InsufficientStorage: 507,
    DigestMismatch: 422,
    IncompleteBody: 400,
    ObjectConflict: 409,
    ObjectNotFound: 404,
    CorruptObject: 500,
}


def _storage_error(exc: BlobStoreError, object_id: uuid.UUID) -> JsonResponse:
    status = _STATUS_BY_ERROR.get(type(exc), 500)
    log = logger.error if status >= 500 and status != 507 else logger.warning
    log("object operation failed", extra={"object_id": str(object_id), "reason": exc.code})
    return error(status, exc.code)


# --- probes (unauthenticated, reveal nothing) --------------------------------
@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def readyz(request: HttpRequest) -> JsonResponse:
    store = get_store()
    ready = store.is_writable() and store.has_room_for(0)
    return JsonResponse({"status": "ready" if ready else "not_ready"}, status=200 if ready else 503)


# --- internal API -------------------------------------------------------------
@signed_request
@require_GET
def node_status(request: HttpRequest) -> JsonResponse:
    usage = get_store().disk_usage()
    return JsonResponse(
        {
            "node_id": settings.NODE_ID,
            "protocol": "v1",
            "disk_total_bytes": usage.total,
            "disk_free_bytes": usage.free,
            "min_free_bytes": settings.NODE_MIN_FREE_BYTES,
            "max_object_bytes": settings.NODE_MAX_OBJECT_BYTES,
        }
    )


@signed_request
@require_GET
def list_objects(request: HttpRequest) -> JsonResponse:
    try:
        after = uuid.UUID(request.GET["after"]) if request.GET.get("after") else None
        limit = min(int(request.GET.get("limit", "500")), MAX_LIST_LIMIT)
    except ValueError:
        return error(400, "bad_query")
    if limit < 1:
        return error(400, "bad_query")
    items = list(islice(get_store().iter_objects(after=after), limit + 1))
    page, has_more = items[:limit], len(items) > limit
    return JsonResponse(
        {
            "objects": [info.as_dict() for info in page],
            "next_after": str(page[-1].object_id) if has_more and page else None,
        }
    )


@signed_request
@require_http_methods(["GET", "HEAD", "PUT", "DELETE"])
def object_detail(request: HttpRequest, object_id: uuid.UUID) -> HttpResponse:
    store = get_store()
    try:
        if request.method == "PUT":
            outcome, info = store.put(
                object_id,
                request,
                declared_length=request.declared_length,  # type: ignore[attr-defined]
                expected_sha256=request.verified.content_sha256,  # type: ignore[attr-defined]
            )
            created = outcome is PutOutcome.CREATED
            logger.info(
                "object stored" if created else "object already present",
                extra={"object_id": str(object_id), "size": info.size},
            )
            return JsonResponse(
                {**info.as_dict(), "outcome": outcome.value, "durable": True},
                status=201 if created else 200,
            )

        if request.method == "DELETE":
            existed = store.delete(object_id)
            logger.info("object deleted", extra={"object_id": str(object_id), "existed": existed})
            return HttpResponse(status=204)

        if request.method == "HEAD":
            info = store.stat(object_id)
            if info is None:
                raise ObjectNotFound(str(object_id))
            response = HttpResponse(content_type="application/octet-stream")
            response["Content-Length"] = str(info.size)
            response[HEADER_SHA256] = info.sha256
            response[HEADER_SIZE] = str(info.size)
            return response

        info, handle = store.open(object_id)
        response = FileResponse(handle, content_type="application/octet-stream")
        response["Content-Length"] = str(info.size)
        response[HEADER_SHA256] = info.sha256
        response[HEADER_SIZE] = str(info.size)
        return response
    except BlobStoreError as exc:
        return _storage_error(exc, object_id)


@signed_request
@require_GET
def object_integrity(request: HttpRequest, object_id: uuid.UUID) -> JsonResponse:
    """Re-read the object from disk and compare it with its recorded digest."""
    try:
        report = get_store().verify(object_id)
    except BlobStoreError as exc:
        return _storage_error(exc, object_id)
    if not report.ok:
        logger.error("object failed integrity check", extra={"object_id": str(object_id)})
    return JsonResponse(
        {
            **report.info.as_dict(),
            "actual_size": report.actual_size,
            "actual_sha256": report.actual_sha256,
            "ok": report.ok,
        }
    )


# --- JSON error handlers ------------------------------------------------------
def not_found(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return error(404, "not_found")


def server_error(request: HttpRequest) -> JsonResponse:
    return error(500, "internal_error")
