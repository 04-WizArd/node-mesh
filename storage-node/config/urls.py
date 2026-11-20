from django.urls import include, path

from objects import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
    path("internal/v1/", include("objects.urls")),
]

handler404 = "objects.views.not_found"
handler500 = "objects.views.server_error"
