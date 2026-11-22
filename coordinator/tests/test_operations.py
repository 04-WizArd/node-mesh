"""Operational commands: reconcile heartbeat, node registration guard, status report."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command


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
