from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from cluster.models import StorageNode


class File(models.Model):
    """Metadata for one immutable stored object.

    The UUID is also the storage key on every node. Content never changes
    after upload: replacing a file means uploading a new object.

    Lifecycle::

        PENDING ──quorum met──▶ AVAILABLE ──user deletes──▶ DELETED ──purged──▶ (row removed)
           │
           └──quorum missed / abandoned──▶ FAILED (partial replicas garbage-collected)
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        AVAILABLE = "available", "Available"
        FAILED = "failed", "Failed"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="files")
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, default="application/octet-stream")
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    desired_replicas = models.PositiveSmallIntegerField()
    # NULL (not "") means "no key": NULLs never collide in the unique constraint below.
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)  # noqa: DJ001
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="file_unique_idempotency_key_per_owner",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["pending", "available", "failed", "deleted"]),
                name="file_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(desired_replicas__gte=1), name="file_desired_replicas_positive"
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "status", "-created_at"], name="file_owner_status_created"),
            models.Index(fields=["status", "created_at"], name="file_status_created"),
        ]

    def __str__(self) -> str:
        return f"{self.original_name} ({self.id})"


class Replica(models.Model):
    """The state of one copy of a file on one node, as known by the coordinator.

    ``FAILED`` after a timeout is ambiguous (the node may have stored the
    object); the reconciler resolves it by asking the node.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # write in progress
        STORED = "stored", "Stored"  # node acknowledged a durable, digest-verified write
        FAILED = "failed", "Failed"  # write failed or replica removed
        CORRUPT = "corrupt", "Corrupt"  # bytes on the node do not match the digest
        MISSING = "missing", "Missing"  # node reported the object as absent

    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="replicas")
    node = models.ForeignKey(StorageNode, on_delete=models.PROTECT, related_name="replicas")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    stored_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["file", "node"], name="replica_unique_file_node"),
            models.CheckConstraint(
                condition=Q(status__in=["pending", "stored", "failed", "corrupt", "missing"]),
                name="replica_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["node", "status"], name="replica_node_status"),
            models.Index(fields=["status", "last_verified_at"], name="replica_status_verified"),
        ]

    def __str__(self) -> str:
        return f"{self.file_id}@{self.node_id}: {self.status}"
