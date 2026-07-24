from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dokploy_wizard.state import RawEnvInput, resolve_desired_state


def _source_values() -> dict[str, str]:
    return {
        "ROOT_DOMAIN": "example.test",
        "PACKS": "seaweedfs,coder",
        "AI_DEFAULT_PROVIDER": "openrouter",
        "AI_DEFAULT_MODEL": "example/model",
        "VPS_HOST": "192.0.2.1",
        "VPS_ROOT_PASSWORD": "do-not-upload",
        "LITELLM_NVIDIA_API_KEY": "remove-for-proof",
    }


def _source_bytes(values: dict[str, str]) -> bytes:
    return "".join(f"{key}={value}\n" for key, value in values.items()).encode()


def test_task1_context_derives_external_only_unique_overlay(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_task1_context import (
        TASK1_PROOF_OVERLAY_KEYS,
        derive_task1_proof_context,
    )
    from dokploy_wizard.proof.model_sync_task1_materialization import (
        materialize_task1_external_files,
    )

    source_values = _source_values()
    source_bytes = _source_bytes(source_values)
    source_path = tmp_path / ".install-min.env"
    source_path.write_bytes(source_bytes)
    source_path.chmod(0o600)

    prepared = derive_task1_proof_context(
        source_values=source_values,
        source_bytes=source_bytes,
        source_path=source_path,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(prepared.materialization)

    uploaded = prepared.uploaded_env_file.read_text(encoding="utf-8")
    overlay = prepared.overlay
    labels = [
        overlay["DOKPLOY_SUBDOMAIN"],
        overlay["CODER_SUBDOMAIN"],
        overlay["SEAWEEDFS_SUBDOMAIN"],
        overlay["LITELLM_ADMIN_SUBDOMAIN"],
    ]
    assert source_path.read_bytes() == source_bytes
    assert frozenset(overlay) == TASK1_PROOF_OVERLAY_KEYS
    assert labels == [
        "dp-0123456789abcdef0123456789abcdef",
        "cd-0123456789abcdef0123456789abcdef",
        "s3-0123456789abcdef0123456789abcdef",
        "lm-0123456789abcdef0123456789abcdef",
    ]
    assert all("." not in label for label in labels)
    assert "VPS_ROOT_PASSWORD" not in uploaded
    assert "LITELLM_NVIDIA_API_KEY" not in uploaded
    assert prepared.context.uploaded_env_sha256 == hashlib.sha256(uploaded.encode()).hexdigest()
    assert prepared.uploaded_env_file.stat().st_mode & 0o777 == 0o600
    assert prepared.context_file.stat().st_mode & 0o777 == 0o600
    assert prepared.seed_file.stat().st_mode & 0o777 == 0o600
    assert prepared.receipt_file.stat().st_mode & 0o777 == 0o600
    assert prepared.proof_directory.stat().st_mode & 0o777 == 0o700


def test_task1_context_rejects_unapproved_source_wildcard_and_overlay(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_task1_context import (
        Task1ProofContextError,
        derive_task1_proof_context,
    )

    source_values = _source_values() | {"CODER_WILDCARD_SUBDOMAIN": "*.coder"}

    with pytest.raises(Task1ProofContextError, match="CODER_WILDCARD_SUBDOMAIN"):
        derive_task1_proof_context(
            source_values=source_values,
            source_bytes=_source_bytes(source_values),
            source_path=tmp_path / ".install-min.env",
            proof_directory=tmp_path / "proof",
            attempt_token="0123456789abcdef0123456789abcdef",
        )


def test_task1_context_projects_only_after_activation_and_rejects_env_alone(tmp_path: Path) -> None:
    from dokploy_wizard.proof.model_sync_task1_context import (
        Task1ProofContextError,
        activate_task1_proof_context,
        derive_task1_proof_context,
        project_task1_desired_state,
        require_task1_proof_context,
    )

    source_values = _source_values()
    prepared = derive_task1_proof_context(
        source_values=source_values,
        source_bytes=_source_bytes(source_values),
        source_path=tmp_path / ".install-min.env",
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    raw_env = RawEnvInput(format_version=1, values=prepared.uploaded_values)

    with pytest.raises(Task1ProofContextError, match="--task1-proof-context"):
        require_task1_proof_context(raw_env)
    with pytest.raises(Task1ProofContextError, match="--task1-proof-context"):
        resolve_desired_state(raw_env)

    with activate_task1_proof_context(prepared.context):
        desired = resolve_desired_state(raw_env)
        projected = project_task1_desired_state(desired)

    assert "coder" in projected.hostnames
    assert "coder-wildcard" not in projected.hostnames
    assert projected.hostnames["litellm-admin"] == prepared.context.litellm_admin_hostname


@pytest.mark.parametrize("command", ["install", "modify", "inspect-state"])
def test_local_lifecycle_commands_accept_explicit_task1_context(command: str) -> None:
    from dokploy_wizard.cli import build_parser

    args = build_parser().parse_args(
        [command, "--env-file", "upload.env", "--task1-proof-context", "context.json"]
    )

    assert args.task1_proof_context == Path("context.json")


def test_task1_context_keeps_coder_control_plane_without_wildcard_routing(tmp_path: Path) -> None:
    from dokploy_wizard.packs.coder import ShellCoderBackend, reconcile_coder
    from dokploy_wizard.proof.model_sync_task1_context import (
        activate_task1_proof_context,
        derive_task1_proof_context,
    )
    from dokploy_wizard.state import OwnershipLedger

    source_values = _source_values()
    prepared = derive_task1_proof_context(
        source_values=source_values,
        source_bytes=_source_bytes(source_values),
        source_path=tmp_path / ".install-min.env",
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    raw_env = RawEnvInput(format_version=1, values=prepared.uploaded_values)

    with activate_task1_proof_context(prepared.context):
        desired = resolve_desired_state(raw_env)
        phase = reconcile_coder(
            dry_run=True,
            desired_state=desired,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=ShellCoderBackend(),
        )

    assert phase.result.enabled is True
    assert phase.result.hostname == desired.hostnames["coder"]
    assert phase.result.wildcard_hostname is None


def test_coder_compose_omits_wildcard_env_and_router_when_context_disables_it() -> None:
    from dokploy_wizard.core.planner import build_shared_core_plan
    from dokploy_wizard.dokploy.coder import _render_compose_file

    postgres = build_shared_core_plan("proof-stack", ("coder",)).postgres
    assert postgres is not None
    allocation = next(
        item
        for item in build_shared_core_plan("proof-stack", ("coder",)).allocations
        if item.pack_name == "coder"
    )
    assert allocation.postgres is not None

    rendered = _render_compose_file(
        stack_name="proof-stack",
        hostname="coder.example.test",
        wildcard_hostname=None,
        postgres_service_name=postgres.service_name,
        postgres=allocation.postgres,
    )

    assert "CODER_WILDCARD_ACCESS_URL" not in rendered.compose_file
    assert "-wildcard.rule" not in rendered.compose_file
    assert "Host(`coder.example.test`)" in rendered.compose_file
