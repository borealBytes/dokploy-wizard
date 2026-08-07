from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from dokploy_wizard import remote
from dokploy_wizard.proof.model_sync_cli_wrapper import (
    ProofWrapperInvocation,
    run_proof_wrapper,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextV1
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema import (
    append_stage,
    complete_receipt,
    new_receipt,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    TASK1_REMOTE_PROOF_STAGES,
    Task1RemoteProofBinding,
    Task1RemoteProofExpectation,
)
from dokploy_wizard.remote_transport import (
    ParamikoRemoteTransport,
    RemoteCapturedCommandFailure,
    RemoteCommandFailure,
    RemoteCommandOutput,
    RemoteTransportSession,
)


class BoundedRemoteFailure(RuntimeError):
    """Raised by the fake transport at a selected remote phase."""


class ZeroStateTransport:
    def ensure_dir(self, _remote_path: str) -> None:
        return None

    def upload(self, _local_path: Path, _remote_path: str) -> None:
        return None

    def download(self, _remote_path: str, _local_path: Path) -> None:
        return None

    def remove(self, _remote_path: str) -> None:
        return None

    def chmod(self, _remote_path: str, _mode: int) -> None:
        return None

    def run(self, _subcommand: str, _command: str) -> None:
        return None

    def capture(self, subcommand: str, _command: str, _limits: Any) -> Any:
        raise RemoteCapturedCommandFailure(
            subcommand=subcommand,
            reason="nonzero status",
            stderr=b"receipt absent",
            exit_status=1,
        )

    def close(self) -> None:
        return None


class ReceiptTransport(ZeroStateTransport):
    def __init__(self, receipt: bytes, *, fail_subcommand: str | None = None) -> None:
        self.receipt = receipt
        self.fail_subcommand = fail_subcommand
        self.commands: list[tuple[str, str]] = []
        self.capture_calls: list[tuple[str, str]] = []

    def run(self, subcommand: str, command: str) -> None:
        self.commands.append((subcommand, command))
        if subcommand == self.fail_subcommand:
            raise BoundedRemoteFailure("bounded remote failure")

    def capture(self, subcommand: str, command: str, _limits: Any) -> RemoteCommandOutput:
        self.capture_calls.append((subcommand, command))
        return RemoteCommandOutput(stdout=self.receipt, stderr=b"")


def _context() -> Task1ProofContextV1:
    digest = "a" * 64
    return Task1ProofContextV1(
        context_id="b" * 32,
        source_env_sha256=digest,
        normalized_env_sha256="c" * 64,
        overlay_env_sha256="d" * 64,
        uploaded_env_sha256="e" * 64,
        namespace_sha256="f" * 64,
        expected_restored_source_sha256=digest,
        source_env_mode=0o600,
        root_domain="example.test",
        stack_name="proof-stack",
        tunnel_name="proof-tunnel",
        dokploy_subdomain="dokploy-proof",
        coder_subdomain="coder-proof",
        seaweedfs_subdomain="s3-proof",
        litellm_admin_subdomain="litellm-proof",
    )


def _binding() -> Task1RemoteProofBinding:
    return Task1RemoteProofBinding(
        proof_commit="1" * 40,
        context_sha256="2" * 64,
        uploaded_env_sha256="3" * 64,
        archive_sha256="4" * 64,
    )


def _terminal_receipt_bytes() -> bytes:
    receipt = new_receipt(_binding(), ())
    for index, stage in enumerate(TASK1_REMOTE_PROOF_STAGES, start=5):
        receipt = append_stage(receipt, stage, f"{index:x}" * 64)
    return complete_receipt(receipt).to_bytes()


def test_context_proof_rejects_exit_zero_without_remote_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "upload.env"
    env_file.write_text(
        "ROOT_DOMAIN=example.test\nPACKS=seaweedfs,coder\n",
        encoding="utf-8",
    )
    context_file = tmp_path / "context.json"
    context_file.write_bytes(_context().to_bytes())
    transport = ZeroStateTransport()
    monkeypatch.setattr(ParamikoRemoteTransport, "connect", lambda **_kwargs: transport)
    monkeypatch.setattr(remote, "_validate_task1_proof_context", lambda _args: _context())
    monkeypatch.setattr(
        remote,
        "_upload_remote_bundle",
        lambda **_kwargs: SimpleNamespace(commit_sha="1" * 40, archive_sha256="2" * 64),
    )
    monkeypatch.setattr(remote, "_extract_remote_bundle", lambda **_kwargs: None)

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


def test_bounded_wrapper_rejects_exit_zero_without_result_bytes(tmp_path: Path) -> None:
    invocation = ProofWrapperInvocation(
        wrapper=Path("wrapper"),
        host="host-a",
        password="password-sentinel",
        env_file=tmp_path / "upload.env",
        task1_proof_context=tmp_path / "context.json",
        task1_expectation=Task1RemoteProofExpectation(
            proof_commit="1" * 40,
            context_sha256="2" * 64,
            uploaded_env_sha256="3" * 64,
        ),
    )

    def empty_result(
        command: list[str],
        *,
        stdin: bytes,
        output_limit: int,
        timeout_seconds: int,
        label: str,
        nonzero_error_factory: Callable[[bytes], RuntimeError] | None = None,
    ) -> bytes:
        return b""

    with pytest.raises(RuntimeError, match="receipt"):
        run_proof_wrapper(invocation, empty_result)


def test_transport_emits_ordered_stage_commands_and_collects_terminal_receipt() -> None:
    transport = ReceiptTransport(_terminal_receipt_bytes())
    session = RemoteTransportSession(
        transport,
        "/root/dokploy-wizard",
        task1_proof_context=Path("context.json"),
    )

    session.initialize_task1_receipt(_binding())
    result = session.run_proof(task1_binding=_binding())

    assert result == _terminal_receipt_bytes()
    assert [subcommand for subcommand, _command in transport.commands] == [
        "initialize-task1-receipt",
        "mutate-install",
        "verify-services",
        "inspect-state",
    ]
    assert "--stage install" in transport.commands[1][1]
    assert "--stage verify" in transport.commands[2][1]
    assert "--stage inspect" in transport.commands[3][1]
    assert len(transport.capture_calls) == 1
    assert transport.capture_calls[0][0] == "collect-task1-receipt"


def test_transport_propagates_nonzero_before_terminal_collection() -> None:
    transport = ReceiptTransport(
        _terminal_receipt_bytes(),
        fail_subcommand="mutate-install",
    )
    session = RemoteTransportSession(
        transport,
        "/root/dokploy-wizard",
        task1_proof_context=Path("context.json"),
    )

    with pytest.raises(RemoteCommandFailure, match="mutate-install"):
        session.run_proof(task1_binding=_binding())

    assert transport.capture_calls == []


def test_bounded_wrapper_classifies_known_phase_without_retaining_stderr(tmp_path: Path) -> None:
    expectation = Task1RemoteProofExpectation("1" * 40, "2" * 64, "3" * 64)
    invocation = ProofWrapperInvocation(
        Path("wrapper"),
        "host-a",
        "password-sentinel",
        tmp_path / "upload.env",
        tmp_path / "context.json",
        expectation,
    )
    secret = b"secret-stderr-sentinel"
    marker = b"Task 1 remote proof state after install is absent or unsafe"

    def nonzero(
        command: list[str],
        *,
        stdin: bytes,
        output_limit: int,
        timeout_seconds: int,
        label: str,
        nonzero_error_factory: Callable[[bytes], RuntimeError] | None = None,
    ) -> bytes:
        assert nonzero_error_factory is not None
        raise nonzero_error_factory(marker + b"\n" + secret)

    with pytest.raises(RuntimeError, match="state after install is absent or unsafe") as error:
        run_proof_wrapper(invocation, nonzero)

    assert secret.decode() not in str(error.value)
