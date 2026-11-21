"""Upload orchestration: idempotency, placement, quorum replication, finalisation.

Success semantics (documented in docs/consistency.md):

* ``AVAILABLE`` is recorded — and 201 returned — only once at least
  ``NODEMESH_WRITE_QUORUM`` nodes acknowledged a durable (fsynced) write whose
  SHA-256 they verified against the digest computed by the coordinator.
* Fewer than ``NODEMESH_REPLICATION_FACTOR`` copies with quorum met is still a
  success, reported as ``under_replicated``; the reconciler adds the missing
  copies later.
* Below quorum the file is ``FAILED`` (503). Partial copies are deleted by the
  reconciler. The idempotency key is released so the client can retry.

PostgreSQL cannot commit node filesystem writes atomically. Instead, the
``PENDING`` row and its replica rows are committed *before* any byte is sent,
and the outcome is committed after; a coordinator crash in between leaves a
``PENDING`` file that the reconciler resolves by asking the nodes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.db import IntegrityError, transaction

from cluster.client import ClientFactory, client_for
from cluster.placement import write_candidates
from files.models import File
from files.services import quorum
from files.services.replication import prepare_replicas, replicate
from files.spool import SpooledFile

logger = logging.getLogger(__name__)


class UploadError(Exception):
    code = "upload_error"


class IdempotencyConflict(UploadError):
    code = "idempotency_key_reused"


class UploadInProgress(UploadError):
    code = "upload_in_progress"


@dataclass
class UploadResult:
    file: File
    stored: int
    replayed: bool = False

    @property
    def available(self) -> bool:
        return self.file.status == File.Status.AVAILABLE


def _with_counts(file_id) -> File:
    """Reload a file annotated with ``stored_replicas`` and ``countable_replicas``."""
    return quorum.with_replica_counts(File.objects.filter(pk=file_id)).get()


def _replay(existing: File, spooled: SpooledFile) -> UploadResult:
    if existing.sha256 != spooled.sha256 or existing.size != spooled.size:
        raise IdempotencyConflict("the idempotency key was used for different content")
    if existing.status == File.Status.PENDING:
        raise UploadInProgress("an upload with this idempotency key is still in progress")
    file = _with_counts(existing.pk)
    return UploadResult(file=file, stored=file.stored_replicas, replayed=True)


def finalize(file: File, *, write_quorum: int) -> File:
    """Decide AVAILABLE/FAILED from the replica rows. Safe to call repeatedly.

    Returns the file annotated with ``stored_replicas``/``countable_replicas``.
    """
    with transaction.atomic():
        locked = File.objects.select_for_update().get(pk=file.pk)
        counted = _with_counts(file.pk)
        if locked.status == File.Status.PENDING:
            if quorum.quorum_met(counted.stored_replicas, write_quorum):
                locked.status = File.Status.AVAILABLE
            else:
                locked.status = File.Status.FAILED
                locked.idempotency_key = None  # allow a retry with the same key
            locked.save(update_fields=["status", "idempotency_key", "updated_at"])
    locked.stored_replicas = counted.stored_replicas
    locked.countable_replicas = counted.countable_replicas
    return locked


def upload(
    *,
    owner: AbstractBaseUser,
    spooled: SpooledFile,
    original_name: str,
    content_type: str,
    idempotency_key: str | None = None,
    client_factory: ClientFactory = client_for,
) -> UploadResult:
    replication_factor = settings.NODEMESH_REPLICATION_FACTOR
    write_quorum = settings.NODEMESH_WRITE_QUORUM

    if idempotency_key:
        existing = File.objects.filter(owner=owner, idempotency_key=idempotency_key).first()
        if existing is not None:
            return _replay(existing, spooled)

    candidates = write_candidates(spooled.size)
    first_targets = candidates[:replication_factor] if len(candidates) >= write_quorum else []
    try:
        with transaction.atomic():  # the file and its PENDING replica rows appear together
            file = File.objects.create(
                owner=owner,
                original_name=original_name,
                content_type=content_type,
                size=spooled.size,
                sha256=spooled.sha256,
                desired_replicas=replication_factor,
                idempotency_key=idempotency_key or None,
            )
            prepared = prepare_replicas(file, first_targets) if first_targets else {}
    except IntegrityError:
        # A concurrent request with the same idempotency key won the race.
        existing = File.objects.filter(owner=owner, idempotency_key=idempotency_key).first()
        if existing is None:
            raise
        return _replay(existing, spooled)

    log_extra = {"file_id": str(file.id), "size": file.size, "candidates": len(candidates)}
    if len(candidates) < write_quorum:
        logger.error("not enough eligible nodes for write quorum", extra=log_extra)
    else:
        logger.info("upload accepted, replicating", extra=log_extra)
        replicate(
            file,
            spooled.path,
            candidates,
            wanted=replication_factor,
            client_factory=client_factory,
            prepared=prepared,
        )

    file = finalize(file, write_quorum=write_quorum)
    logger.info(
        "upload finished",
        extra={"file_id": str(file.id), "status": file.status, "stored": file.stored_replicas},
    )
    return UploadResult(file=file, stored=file.stored_replicas)
