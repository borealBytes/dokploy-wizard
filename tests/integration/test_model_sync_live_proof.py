from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_cli import main
from dokploy_wizard.proof.model_sync_host_b import (
    HostIdentity,
    assert_followup_proof_contract,
    assert_namespace_identity,
)


def test_namespace_identity_rejects_same_machine_and_mismatched_architecture() -> None:
    host_a = HostIdentity(machine_sha256="a" * 64, ssh_sha256="b" * 64, architecture="amd64")
    same_host = HostIdentity(machine_sha256="a" * 64, ssh_sha256="c" * 64, architecture="amd64")
    wrong_architecture = HostIdentity(
        machine_sha256="d" * 64,
        ssh_sha256="e" * 64,
        architecture="arm64",
    )

    with pytest.raises(ValueError):
        assert_namespace_identity(host_a=host_a, host_b=same_host)
    with pytest.raises(ValueError):
        assert_namespace_identity(host_a=host_a, host_b=wrong_architecture)


@pytest.mark.parametrize(
    "contract_name",
    ["upgrade_host_a_contract", "final_proof_contract", "reseed_pair_contract"],
)
def test_followup_proof_contract_rejects_missing_required_receipt(contract_name: str) -> None:
    with pytest.raises(ValueError):
        assert_followup_proof_contract(contract_name=contract_name, receipts=())


def test_baseline_host_a_requires_all_named_environment_inputs_without_artifact(
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.json"

    exit_code = main(
        [
            "baseline-host-a",
            "--wrapper",
            "/workspaces/model-sync/bin/dokploy-wizard-remote",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--external-backup",
            str(tmp_path / "backup"),
            "--abort-guard",
            str(tmp_path / "guard.json"),
            "--host-env",
            "MISSING_HOST",
            "--password-env",
            "MISSING_PASSWORD",
            "--host-b-env",
            "MISSING_HOST_B",
            "--host-b-password-env",
            "MISSING_PASSWORD_B",
            "--source-base-commit",
            "a" * 40,
            "--proof-commit",
            "b" * 40,
            "--artifact-dir",
            str(tmp_path),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert not output.exists()
