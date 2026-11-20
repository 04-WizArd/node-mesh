from __future__ import annotations

from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

node_id_validator = RegexValidator(
    r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
    "Use a lowercase slug such as 'node-1'.",
)


class StorageNode(models.Model):
    """A storage node known to the coordinator.

    ``admin_state`` is the operator's intent; ``health`` is what the
    coordinator has observed. They are deliberately independent: a draining
    node can be perfectly healthy, and a healthy node can be disabled.
    """

    class AdminState(models.TextChoices):
        ACTIVE = "active", "Active"  # receives new replicas and serves reads
        DRAINING = "draining", "Draining"  # serves reads, receives no new replicas
        DISABLED = "disabled", "Disabled"  # never contacted

    class Health(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"  # never contacted yet
        HEALTHY = "healthy", "Healthy"  # last contact succeeded
        UNREACHABLE = "unreachable", "Unreachable"  # recent failure(s), may be transient
        SUSPECT = "suspect", "Suspect"  # repeated failures: presumed failed

    node_id = models.SlugField(max_length=64, unique=True, validators=[node_id_validator])
    base_url = models.URLField(max_length=255, help_text="Internal endpoint, e.g. http://node-1:8000")
    admin_state = models.CharField(max_length=16, choices=AdminState.choices, default=AdminState.ACTIVE)
    health = models.CharField(max_length=16, choices=Health.choices, default=Health.UNKNOWN)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    disk_total_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    disk_free_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["node_id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(admin_state__in=["active", "draining", "disabled"]),
                name="storagenode_admin_state_valid",
            ),
            models.CheckConstraint(
                condition=Q(health__in=["unknown", "healthy", "unreachable", "suspect"]),
                name="storagenode_health_valid",
            ),
        ]

    def __str__(self) -> str:
        return self.node_id
