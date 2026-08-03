from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard import remote
from dokploy_wizard.proof.model_sync_task1_context import active_task1_proof_context
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextV1
from dokploy_wizard.release import CommitArchiveEvidence
from dokploy_wizard.release_manifest import ReleaseFile, ReleaseManifest
from dokploy_wizard.remote_transport import ParamikoRemoteTransport
from tests.test_remote_task1_receipt import ZeroStateTransport, _context


def test_remote_proof_resolves_service_urls_with_validated_task1_context_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "upload.env"
    env_file.write_text("ROOT_DOMAIN=example.test\nPACKS=seaweedfs,coder\n", encoding="utf-8")
    env_file.chmod(0o600)
    context_file = tmp_path / "context.json"
    context = _context()
    context_file.write_bytes(context.to_bytes())
    context_file.chmod(0o600)
    transport = ZeroStateTransport()
    observed: list[Task1ProofContextV1 | None] = []

    monkeypatch.setattr(ParamikoRemoteTransport, "connect", lambda **_kwargs: transport)
    monkeypatch.setattr(remote, "_validate_task1_proof_context", lambda _args: context)
    monkeypatch.setattr(
        remote,
        "_upload_remote_bundle",
        lambda **_kwargs: CommitArchiveEvidence(
            commit_sha="1" * 40,
            archive_sha256="2" * 64,
            manifest=ReleaseManifest(
                commit_sha="1" * 40,
                archive_sha256="2" * 64,
                files=(ReleaseFile("bootstrap.py", "3" * 64, 0, 0o755),),
            ),
            bootstrap_bytes=b"",
            bootstrap_sha256="3" * 64,
        ),
    )
    monkeypatch.setattr(remote, "_extract_remote_bundle", lambda **_kwargs: None)

    def resolve_links(_env_file: Path) -> tuple[tuple[str, str], ...]:
        observed.append(active_task1_proof_context())
        return ()

    monkeypatch.setattr(remote, "_resolve_expected_service_url_links", resolve_links)

    exit_code = remote.main(
        [
            "proof",
            "--host",
            "host.example.test",
            "--password",
            "password-sentinel",
            "--env-file",
            str(env_file),
            "--task1-proof-context",
            str(context_file),
            "--quiet-remote-output",
        ]
    )

    assert exit_code == 1
    assert observed == [context]
