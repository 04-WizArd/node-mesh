from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from . import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
    path("accounts/", include("accounts.urls")),
    path(settings.ADMIN_URL, admin.site.urls),
    path("", include("files.urls")),
]

handler404 = "config.views.not_found"
handler500 = "config.views.server_error"
