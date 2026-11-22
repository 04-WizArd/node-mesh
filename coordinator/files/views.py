from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET


@require_GET
@login_required
@ensure_csrf_cookie
def index(request: HttpRequest) -> HttpResponse:
    context = {
        "max_upload_bytes": settings.NODEMESH_MAX_UPLOAD_BYTES,
        "replication_factor": settings.NODEMESH_REPLICATION_FACTOR,
    }
    return render(request, "files/index.html", context)
