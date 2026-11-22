"""Operational commands: reconcile heartbeat, node registration guard, status report."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from cluster.models import StorageNode
from files.models import File


@pytest.mark.django_db
def test_reconcile_touches_heartbeat_after_a_successful_cycle(tmp_path):
    heartbeat = tmp_path / "hb" / "reconciler.heartbeat"
    call_command("reconcile", heartbeat_file=heartbeat, stdout=StringIO())
    assert heartbeat.exists()


@pytest.mark.django_db
def test_failed_cycle_does_not_touch_heartbeat(tmp_path, monkeypatch):
    def boom(self):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr("files.reconcile.Reconciler.run", boom)
    heartbeat = tmp_path / "reconciler.heartbeat"
    with pytest.raises(CommandError):
        call_command("reconcile", heartbeat_file=heartbeat, stdout=StringIO())
    assert not heartbeat.exists()


@pytest.mark.django_db
def test_register_node_is_idempotent_and_refuses_silent_url_changes():
    out = StringIO()
    call_command("register_node", "node-1", "http://node-1:8000", stdout=out)
    call_command("register_node", "node-1", "http://node-1:8000/", stdout=out)  # same URL: no-op
    call_command("register_node", "node-1", "http://node-1:8000", state="draining", stdout=out)
    assert StorageNode.objects.get().admin_state == "draining"

    with pytest.raises(CommandError, match="refusing to change the URL"):
        call_command("register_node", "node-1", "http://127.0.0.1:8101", stdout=out)
    assert StorageNode.objects.get().base_url == "http://node-1:8000"

    call_command("register_node", "node-1", "http://10.0.0.5:8000", update_url=True, stdout=out)
    assert StorageNode.objects.get().base_url == "http://10.0.0.5:8000"


@pytest.mark.django_db
def test_register_from_env_is_all_or_nothing(monkeypatch):
    StorageNode.objects.create(node_id="node-2", base_url="http://node-2:8000")
    monkeypatch.setenv("NODEMESH_NODES", "node-1=http://127.0.0.1:8101,node-2=http://127.0.0.1:8102")
    with pytest.raises(CommandError):
        call_command("register_node", from_env=True, stdout=StringIO())
    assert not StorageNode.objects.filter(node_id="node-1").exists()  # nothing partially applied


@pytest.mark.django_db
def test_cluster_status_reports_nodes_and_files(user):
    StorageNode.objects.create(node_id="node-1", base_url="http://node-1:8000", health="healthy")
    File.objects.create(
        owner=user, original_name="a", size=1, sha256="0" * 64, desired_replicas=3, status="available"
    )
    out = StringIO()
    call_command("cluster_status", stdout=out)
    text = out.getvalue()
    assert "node-1" in text and "healthy" in text
    assert "available=1" in text and "under-replicated=1" in text
