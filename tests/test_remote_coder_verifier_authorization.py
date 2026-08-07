from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path

import pytest

from dokploy_wizard import remote
from dokploy_wizard.proof.coder_workspace_create_preflight_types import (
    CoderCreatePreflightReport,
)
from dokploy_wizard.remote_transport import (
    RemoteCommandCaptureLimits,
    RemoteCommandOutput,
    RemoteTransportSession,
)


class PrivateArtifactTransport:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.commands: list[str] = []
        self.removed: list[str] = []

    def download(self, _remote_path: str, local_path: Path) -> None:
        local_path.write_bytes(self.content)

    def ensure_dir(self, _remote_path: str) -> None:
        return

    def upload(self, _local_path: Path, _remote_path: str) -> None:
        return

    def remove(self, _remote_path: str) -> None:
        self.removed.append(_remote_path)

    def chmod(self, _remote_path: str, _mode: int) -> None:
        return

    def run(self, _subcommand: str, _command: str) -> None:
        self.commands.append(_command)

    def capture(
        self,
        _subcommand: str,
        _command: str,
        _limits: RemoteCommandCaptureLimits,
    ) -> RemoteCommandOutput:
        return RemoteCommandOutput(b"", b"")


def test_private_download_is_atomic_and_mode_0600(tmp_path: Path) -> None:
    output = tmp_path / "authorization.json"
    session = RemoteTransportSession(PrivateArtifactTransport(b"private"), "/remote")

    remote._download_private_file(session, "/remote/private.json", output)

    assert output.read_bytes() == b"private"
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_private_download_refuses_replacement(tmp_path: Path) -> None:
    output = tmp_path / "authorization.json"
    output.write_bytes(b"existing")
    session = RemoteTransportSession(PrivateArtifactTransport(b"replacement"), "/remote")

    with pytest.raises(RuntimeError, match="already exists"):
        remote._download_private_file(session, "/remote/private.json", output)

    assert output.read_bytes() == b"existing"


def test_modify_receives_authorization_only_after_private_upload() -> None:
    session = RemoteTransportSession(PrivateArtifactTransport(b"private"), "/remote")

    without_authorization = remote._build_modify_command(
        session,
        capture_upgrade_observations=False,
    )
    session.remote_coder_verifier_authorization_path = "/remote/authorization.json"
    with_authorization = remote._build_modify_command(
        session,
        capture_upgrade_observations=False,
    )

    assert "SUPERSESSION_AUTHORIZATION" not in without_authorization
    assert (
        "DOKPLOY_WIZARD_CODER_VERIFIER_SUPERSESSION_AUTHORIZATION="
        in with_authorization
    )


def test_capture_executes_module_from_source_layout(tmp_path: Path) -> None:
    transport = PrivateArtifactTransport(b"private")
    session = RemoteTransportSession(transport, "/remote", task1_proof_context=Path("context.json"))
    args = Namespace(
        output=tmp_path / "authorization.json",
        machine_sha256="1" * 64,
        ssh_sha256="2" * 64,
        lifecycle_sha256="3" * 64,
        stack_sha256="4" * 64,
        final_commit="5" * 40,
        attempt_context_sha256="6" * 64,
    )

    remote._capture_coder_verifier_authorization(
        args=args, session=session, password="fixture-password"
    )

    assert len(transport.commands) == 1
    assert "env PYTHONPATH=src python3 -m" in transport.commands[0]
    assert len(transport.removed) == 1


def test_workspace_create_preflight_downloads_private_report_and_removes_remote_copy(
    tmp_path: Path,
) -> None:
    # Given
    report = CoderCreatePreflightReport(
        url_status="reachable",
        token_status="issued",
        auth_status="authenticated",
        target_template_count=1,
        organization_count=1,
        target_organization_count=1,
        active_template_version_health="healthy",
        preset_selection="required",
        required_parameter_default_gap_count=0,
        required_external_auth_unsatisfied_count=0,
        external_auth_status="not_required",
        blockers=("preset_selection_required",),
    ).to_bytes()
    transport = PrivateArtifactTransport(report)
    session = RemoteTransportSession(transport, "/remote", task1_proof_context=Path("context.json"))
    output = tmp_path / "preflight.json"

    # When
    remote._capture_coder_workspace_create_preflight(
        session=session,
        password="fixture-password",
        output=output,
    )

    # Then
    assert output.read_bytes() == report
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert transport.removed == ["/remote/state/.coder-workspace-create-preflight.json"]
    assert len(transport.commands) == 1
    assert "dokploy_wizard.proof.coder_workspace_create_preflight" in transport.commands[0]
    assert "/remote/task1-proof-context.json" in transport.commands[0]


def test_workspace_create_preflight_discards_invalid_downloaded_bytes(tmp_path: Path) -> None:
    # Given
    transport = PrivateArtifactTransport(b'{"raw":"must-not-persist"}\n')
    session = RemoteTransportSession(transport, "/remote", task1_proof_context=Path("context.json"))
    output = tmp_path / "preflight.json"

    # When / Then
    with pytest.raises(RuntimeError, match="report is invalid"):
        remote._capture_coder_workspace_create_preflight(
            session=session,
            password="fixture-password",
            output=output,
        )

    assert not output.exists()
    assert transport.removed == ["/remote/state/.coder-workspace-create-preflight.json"]
