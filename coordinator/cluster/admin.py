from django.contrib import admin

from .models import StorageNode


@admin.register(StorageNode)
class StorageNodeAdmin(admin.ModelAdmin):
    list_display = (
        "node_id",
        "base_url",
        "admin_state",
        "health",
        "consecutive_failures",
        "last_seen_at",
        "disk_free_bytes",
    )
    list_filter = ("admin_state", "health")
    search_fields = ("node_id",)
    readonly_fields = (
        "health",
        "consecutive_failures",
        "last_seen_at",
        "last_failure_at",
        "last_error",
        "disk_total_bytes",
        "disk_free_bytes",
        "created_at",
        "updated_at",
    )
