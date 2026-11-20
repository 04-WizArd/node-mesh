from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness: the process answers HTTP."""
    return JsonResponse({"status": "ok"})


@require_GET
def readyz(request: HttpRequest) -> JsonResponse:
    """Readiness: the metadata database is reachable."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "not_ready"}, status=503)
    return JsonResponse({"status": "ready"})


def not_found(request: HttpRequest, exception: Exception | None = None):
    if request.path.startswith("/api/"):
        return JsonResponse({"error": "not_found"}, status=404)
    from django.views.defaults import page_not_found

    return page_not_found(request, exception)


def server_error(request: HttpRequest):
    if request.path.startswith("/api/"):
        return JsonResponse({"error": "internal_error"}, status=500)
    from django.views.defaults import server_error as default

    return default(request)
