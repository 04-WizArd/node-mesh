"""Authentication of coordinator requests (see ``nodemesh_common.signing``)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse

from nodemesh_common.signing import EMPTY_SHA256, SignatureError, verify_request

from .apps import get_nonce_store

logger = logging.getLogger(__name__)

View = Callable[..., HttpResponse]


def error(status: int, code: str, detail: str = "") -> JsonResponse:
    body = {"error": code}
    if detail:
        body["detail"] = detail
    return JsonResponse(body, status=status)


def _declared_length(request: HttpRequest) -> int | None:
    raw = request.META.get("CONTENT_LENGTH") or ""
    if raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return -1
    return value


def signed_request(view: View) -> View:
    """Reject any request that is not signed by the coordinator.

    Sets ``request.verified`` (a ``VerifiedRequest``) and
    ``request.declared_length`` for the wrapped view.
    """

    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if "chunked" in request.META.get("HTTP_TRANSFER_ENCODING", "").lower():
            return error(411, "length_required", "chunked bodies are not accepted")
        length = _declared_length(request)
        if length is not None and length < 0:
            return error(400, "bad_length")
        if request.method == "PUT" and length is None:
            return error(411, "length_required")
        length = length or 0

        try:
            verified = verify_request(
                keys=settings.NODE_HMAC_KEYS,
                node_id=settings.NODE_ID,
                method=request.method or "",
                path=request.path,
                query=request.META.get("QUERY_STRING", ""),
                headers=request.headers,
                content_length=length,
                max_skew_seconds=settings.NODE_MAX_CLOCK_SKEW_SECONDS,
            )
        except SignatureError as exc:
            logger.warning(
                "rejected internal request",
                extra={"reason": exc.code, "method": request.method, "path": request.path},
            )
            return error(401, "unauthorized", exc.code)

        if request.method != "PUT" and (length != 0 or verified.content_sha256 != EMPTY_SHA256):
            return error(400, "unexpected_body")

        if not get_nonce_store().claim(verified.nonce):
            logger.warning(
                "replayed internal request rejected",
                extra={"method": request.method, "path": request.path},
            )
            return error(401, "unauthorized", "replayed_request")

        request.verified = verified  # type: ignore[attr-defined]
        request.declared_length = length  # type: ignore[attr-defined]
        return view(request, *args, **kwargs)

    return wrapper
