from django.urls import path

from . import api, views

app_name = "files"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/v1/files/", api.file_collection, name="api-files"),
    path("api/v1/files/<uuid:file_id>/", api.file_detail, name="api-file"),
    path("api/v1/files/<uuid:file_id>/content", api.file_content, name="api-file-content"),
    path("api/v1/files/<uuid:file_id>/preview", api.file_preview, name="api-file-preview"),
    path("api/v1/cluster/nodes/", api.node_list, name="api-nodes"),
]
