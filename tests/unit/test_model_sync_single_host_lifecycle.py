from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from dokploy_wizard.proof import model_sync_artifacts
from dokploy_wizard.proof.model_sync_lifecycle import HostLifecycleEpoch
from dokploy_wizard.proof.model_sync_remote import RemoteProbe


def _probe(*, boot: str, clean: bool) -> RemoteProbe:
    planes = ("cloudflare", "coder", "docker", "dokploy", "tailscale")
    return RemoteProbe(
        machine_sha256="a" * 64,
        ssh_sha256="b" * 64,
        boot_sha256=boot,
        architecture="amd64",
        namespace_clean=clean,
        inventory={name: () for name in planes},
        plane_states={name: "absent" for name in planes},
    )


def _epoch(identifier: str, previous: str | None) -> HostLifecycleEpoch:
    return HostLifecycleEpoch(
        epoch_id=identifier,
        expected_previous_sha256=previous,
    )


def test_single_host_lifecycle_reaches_fresh_final_epoch() -> None:
    from dokploy_wizard.proof.model_sync_lifecycle import (
        FinalEpochEvidence,
        build_baseline_epoch,
        parse_lifecycle_receipt,
        receipt_sha256,
        record_final_epoch,
        record_host_a_epoch,
        record_teardown_epoch,
    )

    # Given
    baseline = build_baseline_epoch(
        _probe(boot="1" * 64, clean=True),
        _epoch("1" * 64, None),
    )
    host_a = record_host_a_epoch(
        baseline,
        _probe(boot="1" * 64, clean=False),
        _epoch("2" * 64, receipt_sha256(baseline)),
    )
    teardown = record_teardown_epoch(
        host_a,
        _probe(boot="1" * 64, clean=True),
        _epoch("3" * 64, receipt_sha256(host_a)),
    )
    final_evidence = FinalEpochEvidence(
        clean_probe=_probe(boot="2" * 64, clean=True),
        managed_probe=_probe(boot="2" * 64, clean=False),
        epoch=_epoch("4" * 64, receipt_sha256(teardown)),
    )

    # When
    final = record_final_epoch(teardown, final_evidence)
    parsed = parse_lifecycle_receipt(json.loads(final.to_bytes()))

    # Then
    assert parsed == final
    assert final.phase == "final_epoch"
    assert final.host_identity_mode == "single_sequential"
    assert final.namespace_resource_absence_verified is True
    assert final.fresh_install_epoch_verified is True
    assert final.temporal_clean_epoch_evidence is True


def test_single_host_lifecycle_rejects_stale_previous_epoch() -> None:
    from dokploy_wizard.proof.model_sync_lifecycle import (
        build_baseline_epoch,
        record_host_a_epoch,
    )

    # Given
    baseline = build_baseline_epoch(
        _probe(boot="1" * 64, clean=True),
        _epoch("1" * 64, None),
    )

    # When / Then
    with pytest.raises(ValueError, match="previous lifecycle receipt"):
        record_host_a_epoch(
            baseline,
            _probe(boot="1" * 64, clean=False),
            _epoch("2" * 64, "f" * 64),
        )


def test_single_host_lifecycle_rejects_dirty_teardown() -> None:
    from dokploy_wizard.proof.model_sync_lifecycle import (
        build_baseline_epoch,
        receipt_sha256,
        record_host_a_epoch,
        record_teardown_epoch,
    )

    # Given
    baseline = build_baseline_epoch(
        _probe(boot="1" * 64, clean=True),
        _epoch("1" * 64, None),
    )
    host_a = record_host_a_epoch(
        baseline,
        _probe(boot="1" * 64, clean=False),
        _epoch("2" * 64, receipt_sha256(baseline)),
    )

    # When / Then
    with pytest.raises(ValueError, match="clean namespace"):
        record_teardown_epoch(
            host_a,
            _probe(boot="1" * 64, clean=False),
            _epoch("3" * 64, receipt_sha256(host_a)),
        )


def test_single_host_lifecycle_rejects_unchanged_boot_as_final_epoch() -> None:
    from dokploy_wizard.proof.model_sync_lifecycle import (
        FinalEpochEvidence,
        build_baseline_epoch,
        receipt_sha256,
        record_final_epoch,
        record_host_a_epoch,
        record_teardown_epoch,
    )

    # Given
    baseline = build_baseline_epoch(
        _probe(boot="1" * 64, clean=True),
        _epoch("1" * 64, None),
    )
    host_a = record_host_a_epoch(
        baseline,
        _probe(boot="1" * 64, clean=False),
        _epoch("2" * 64, receipt_sha256(baseline)),
    )
    teardown = record_teardown_epoch(
        host_a,
        _probe(boot="1" * 64, clean=True),
        _epoch("3" * 64, receipt_sha256(host_a)),
    )
    evidence = FinalEpochEvidence(
        clean_probe=_probe(boot="1" * 64, clean=True),
        managed_probe=_probe(boot="1" * 64, clean=False),
        epoch=_epoch("4" * 64, receipt_sha256(teardown)),
    )

    # When / Then
    with pytest.raises(ValueError, match="new boot epoch"):
        record_final_epoch(teardown, evidence)


def test_partial_lifecycle_publication_cannot_replace_prior_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dokploy_wizard.proof.model_sync_lifecycle import (
        build_baseline_epoch,
        publish_lifecycle_receipt,
    )

    # Given
    baseline = build_baseline_epoch(
        _probe(boot="1" * 64, clean=True),
        _epoch("1" * 64, None),
    )
    baseline_path = tmp_path / "baseline.json"
    final_path = tmp_path / "final.json"
    publish_lifecycle_receipt(baseline_path, baseline)

    def interrupt(_path: Path, _content: bytes, *, mode: int = 0o600) -> None:
        del mode
        raise SystemExit(137)

    monkeypatch.setattr(model_sync_artifacts, "atomic_write_bytes", interrupt)

    # When / Then
    with pytest.raises(SystemExit):
        publish_lifecycle_receipt(final_path, baseline)
    assert baseline_path.read_bytes() == baseline.to_bytes()
    assert not final_path.exists()


def test_sigkill_cannot_promote_partial_final_lifecycle_epoch(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_lifecycle import (
        FinalEpochEvidence,
        build_baseline_epoch,
        parse_lifecycle_receipt,
        publish_lifecycle_receipt,
        receipt_sha256,
        record_final_epoch,
        record_host_a_epoch,
        record_teardown_epoch,
    )

    # Given
    baseline = build_baseline_epoch(
        _probe(boot="1" * 64, clean=True),
        _epoch("1" * 64, None),
    )
    host_a = record_host_a_epoch(
        baseline,
        _probe(boot="1" * 64, clean=False),
        _epoch("2" * 64, receipt_sha256(baseline)),
    )
    teardown = record_teardown_epoch(
        host_a,
        _probe(boot="1" * 64, clean=True),
        _epoch("3" * 64, receipt_sha256(host_a)),
    )
    final = record_final_epoch(
        teardown,
        FinalEpochEvidence(
            clean_probe=_probe(boot="2" * 64, clean=True),
            managed_probe=_probe(boot="2" * 64, clean=False),
            epoch=_epoch("4" * 64, receipt_sha256(teardown)),
        ),
    )
    baseline_path = tmp_path / "baseline.json"
    final_path = tmp_path / "final.json"
    publish_lifecycle_receipt(baseline_path, baseline)
    child = r"""
import os
import sys
from pathlib import Path
from dokploy_wizard.proof import model_sync_artifacts
from dokploy_wizard.proof.model_sync_lifecycle import (
    parse_lifecycle_receipt,
    publish_lifecycle_receipt,
)
import json

source, output = map(Path, sys.argv[1:])
receipt = parse_lifecycle_receipt(json.loads(source.read_bytes()))
original_link = model_sync_artifacts.os.link

def pause_before_publish(*args, **kwargs):
    print("READY", flush=True)
    sys.stdin.buffer.read(1)
    return original_link(*args, **kwargs)

model_sync_artifacts.os.link = pause_before_publish
publish_lifecycle_receipt(output, receipt)
"""
    process = subprocess.Popen(
        [
            os.environ.get("PYTHON", "python"),
            "-c",
            child,
            str(baseline_path),
            str(final_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert process.stdout is not None
    assert process.stderr is not None
    assert process.stdout.readline().strip() == "READY", process.stderr.read()

    # When
    process.send_signal(signal.SIGKILL)
    _stdout, stderr = process.communicate(timeout=10)

    # Then
    assert process.returncode == -signal.SIGKILL, stderr
    assert baseline_path.read_bytes() == baseline.to_bytes()
    assert not final_path.exists()
    publish_lifecycle_receipt(final_path, final)
    assert parse_lifecycle_receipt(json.loads(final_path.read_bytes())) == final
