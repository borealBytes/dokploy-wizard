from __future__ import annotations

import stat
from dataclasses import dataclass, field
from multiprocessing import Event, Process
from multiprocessing.synchronize import Event as EventType
from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_strict_proof import (
    RETAINED_TEMPLATES,
    run_strict_proof,
)
from dokploy_wizard.proof.mutation_registry import (
    MutationBackend,
    MutationKind,
    ProofMutationRecorder,
    StrictLeaseNotHeldError,
    StrictMutationError,
    UnregisteredMutatorError,
)
from dokploy_wizard.proof.strict_lease import strict_proof_lease
from dokploy_wizard.proof.workspace_proof_receipts import (
    WorkspaceProofPhase,
    WorkspaceProofRef,
    WorkspaceProofStore,
)


@dataclass(slots=True)
class _Client:
    name: str
    calls: list[str] = field(default_factory=list)
    exists: bool = False
    workspace: WorkspaceProofRef | None = None
    fail_test_once: bool = False

    def find(self, _name: str) -> tuple[WorkspaceProofRef, ...]:
        return () if self.workspace is None else (self.workspace,)

    def create(self, *, template_name: str, workspace_name: str) -> WorkspaceProofRef:
        self.calls.append("create")
        self.exists = True
        self.workspace = WorkspaceProofRef(workspace_name, workspace_name, template_name)
        return self.workspace

    def test(self, _workspace: WorkspaceProofRef) -> None:
        self.calls.append("test")
        if self.fail_test_once:
            self.fail_test_once = False
            raise RuntimeError("injected workspace test failure")

    def stop(self, _workspace: WorkspaceProofRef) -> None:
        self.calls.append("stop")

    def delete(self, _workspace: WorkspaceProofRef) -> None:
        self.calls.append("delete")
        self.exists = False
        self.workspace = None


def _hold_strict_lock(path: str, acquired: EventType, release: EventType) -> None:
    with strict_proof_lease(Path(path)):
        acquired.set()
        release.wait(5)


def test_proof_workspace_all_templates(tmp_path: Path) -> None:
    clients = {name: _Client(name) for name in RETAINED_TEMPLATES}
    result = run_strict_proof(
        artifact_dir=tmp_path,
        strict_rerun=lambda _recorder: None,
        workspace_client=clients.__getitem__,
    )
    assert result.template_names == RETAINED_TEMPLATES
    assert result.cleanup_complete is True
    assert result.totals.control_plane_mutations == 0
    assert result.totals.synchronizer_durable_writes == 0
    for name, client in clients.items():
        assert client.calls == ["create", "test", "stop", "delete"]
        receipt = tmp_path / "workspace-proofs" / name / "workspace-proof-receipt.json"
        assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "kind",
    [MutationKind.CONTROL_PLANE, MutationKind.SYNCHRONIZER_DURABLE_WRITE],
)
def test_strict_zero_control_and_sync(tmp_path: Path, kind: MutationKind) -> None:
    with pytest.raises(StrictMutationError):
        run_strict_proof(
            artifact_dir=tmp_path,
            strict_rerun=lambda recorder: recorder.execute(
                backend=MutationBackend.DOKPLOY.value,
                kind=kind,
                action=lambda: None,
            ),
            workspace_client=lambda _name: _Client("client"),
        )


def test_unregistered_mutator_blocks_action(tmp_path: Path) -> None:
    action_called = False

    def action() -> None:
        nonlocal action_called
        action_called = True

    def rerun(recorder: ProofMutationRecorder) -> None:
        with pytest.raises(UnregisteredMutatorError):
            recorder.execute(backend="unknown", kind=MutationKind.CONTROL_PLANE, action=action)

    result = run_strict_proof(
        artifact_dir=tmp_path,
        strict_rerun=rerun,
        workspace_client=lambda name: _Client(name),
    )
    assert action_called is False
    assert result.cleanup_complete


def test_relative_artifact_path_rejects_before_callbacks(tmp_path: Path) -> None:
    called = False

    def rerun(_recorder: ProofMutationRecorder) -> None:
        nonlocal called
        called = True

    with pytest.raises(ValueError):
        run_strict_proof(
            artifact_dir=Path("relative"),
            strict_rerun=rerun,
            workspace_client=lambda name: _Client(name),
        )
    assert called is False


def test_strict_lock_not_held_blocks_callbacks_and_clients(tmp_path: Path) -> None:
    acquired = Event()
    release = Event()
    lock_path = tmp_path / ".strict-proof.lock"
    child = Process(target=_hold_strict_lock, args=(str(lock_path), acquired, release))
    child.start()
    assert acquired.wait(5)
    calls: list[str] = []

    def blocked_rerun(_recorder: ProofMutationRecorder) -> None:
        calls.append("rerun")

    def blocked_client(name: str) -> _Client:
        calls.append(name)
        return _Client(name)

    try:
        with pytest.raises(StrictLeaseNotHeldError):
            run_strict_proof(
                artifact_dir=tmp_path,
                strict_rerun=blocked_rerun,
                workspace_client=blocked_client,
            )
        assert calls == []
    finally:
        release.set()
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join(5)
    assert child.exitcode == 0


def test_proof_workspace_crash_recovery(tmp_path: Path) -> None:
    clients = {name: _Client(name) for name in RETAINED_TEMPLATES}
    first = clients[RETAINED_TEMPLATES[0]]
    first.fail_test_once = True
    with pytest.raises(RuntimeError, match="injected workspace test failure"):
        run_strict_proof(
            artifact_dir=tmp_path,
            strict_rerun=lambda _recorder: None,
            workspace_client=clients.__getitem__,
        )
    first_receipt = WorkspaceProofStore(
        tmp_path / "workspace-proofs" / RETAINED_TEMPLATES[0]
    ).load()
    assert first_receipt is not None
    assert first_receipt.phase is WorkspaceProofPhase.CREATED

    result = run_strict_proof(
        artifact_dir=tmp_path,
        strict_rerun=lambda _recorder: None,
        workspace_client=clients.__getitem__,
    )
    assert first.calls.count("create") == 1
    assert result.cleanup_complete
    assert result.totals.control_plane_mutations == 0
    assert result.totals.synchronizer_durable_writes == 0
    for template_name, client in clients.items():
        receipt = WorkspaceProofStore(tmp_path / "workspace-proofs" / template_name).load()
        assert receipt is not None
        assert receipt.phase is WorkspaceProofPhase.DELETED
        assert client.workspace is None


def test_strict_rerun_precedes_workspace_clients(tmp_path: Path) -> None:
    events: list[str] = []

    def rerun(_recorder: ProofMutationRecorder) -> None:
        events.append("strict")

    def client(template_name: str) -> _Client:
        events.append(template_name)
        return _Client(template_name)

    result = run_strict_proof(
        artifact_dir=tmp_path,
        strict_rerun=rerun,
        workspace_client=client,
    )
    assert events == ["strict", *RETAINED_TEMPLATES]
    assert "sentinel-host" not in repr(result)


def test_client_failure_propagates_without_cleanup_success(tmp_path: Path) -> None:
    clients = {name: _Client(name) for name in RETAINED_TEMPLATES}
    first = clients[RETAINED_TEMPLATES[0]]
    first.fail_test_once = True
    with pytest.raises(RuntimeError, match="injected workspace test failure"):
        run_strict_proof(
            artifact_dir=tmp_path,
            strict_rerun=lambda _recorder: None,
            workspace_client=clients.__getitem__,
        )
    receipt = WorkspaceProofStore(tmp_path / "workspace-proofs" / RETAINED_TEMPLATES[0]).load()
    assert receipt is not None
    assert receipt.phase is WorkspaceProofPhase.CREATED
