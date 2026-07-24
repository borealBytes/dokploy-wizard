from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.lifecycle.changes import LifecyclePlan
from dokploy_wizard.lifecycle.engine import (
    _task1_cloudflare_journal_for_lifecycle,
    _task1_preinstall_snapshot_capture_required,
)
from tests.unit._model_sync_task1_cloudflare_journal_common import _context


@pytest.mark.parametrize(
    ("dry_run", "mode", "phases_to_run", "expects_capture"),
    [
        (False, "apply", (), False),
        (True, "apply", ("networking",), False),
        (False, "noop", ("networking",), False),
        (False, "apply", ("networking", "cloudflare_access"), True),
    ],
)
def test_preinstall_snapshot_capture_requires_task1_mutation_journal(
    tmp_path: Path,
    dry_run: bool,
    mode: str,
    phases_to_run: tuple[str, ...],
    expects_capture: bool,
) -> None:
    # Given
    lifecycle_plan = LifecyclePlan(
        mode=mode,
        reasons=(),
        applicable_phases=("preflight", "networking", "cloudflare_access"),
        phases_to_run=phases_to_run,
        preserved_phases=(),
        initial_completed_steps=(),
        start_phase=None,
        raw_equivalent=False,
        desired_equivalent=False,
    )
    journal = _task1_cloudflare_journal_for_lifecycle(
        state_dir=tmp_path,
        dry_run=dry_run,
        lifecycle_plan=lifecycle_plan,
        proof_context=_context(tmp_path),
    )
    artifact = tmp_path / "task1-cloudflare-pre-install.snapshot.json"

    # When
    if _task1_preinstall_snapshot_capture_required(journal):
        artifact.write_bytes(b"captured")

    # Then
    assert artifact.exists() is expects_capture
