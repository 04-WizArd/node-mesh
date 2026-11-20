from django.urls import path

from . import views

urlpatterns = [
    path("status", views.node_status, name="status"),
    path("objects", views.list_objects, name="list-objects"),
    path("objects/<uuid:object_id>", views.object_detail, name="object"),
    path("objects/<uuid:object_id>/integrity", views.object_integrity, name="object-integrity"),
]
