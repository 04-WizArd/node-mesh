from django.contrib import admin

from .models import File, Replica


class ReplicaInline(admin.TabularInline):
    model = Replica
    extra = 0
    can_delete = False
    readonly_fields = ("node", "status", "stored_at", "last_verified_at", "last_error")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    """Read-only view: metadata must only change through the services."""

    list_display = ("original_name", "owner", "size", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("original_name", "id", "sha256")
    inlines = [ReplicaInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
