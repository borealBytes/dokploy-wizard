from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Final

import pytest

from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretSpec
from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace import (
    CoderWorkspaceValueHashVerifier,
)
from dokploy_wizard.dokploy.coder_secret_workspace_contract import (
    WorkspaceVerificationPolicy,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationPhase,
    WorkspaceVerificationPlan,
    WorkspaceVerificationReceiptStore,
)

_WORKSPACE_ID: Final = "00000000-0000-4000-8000-000000000001"
_OWNER_ID: Final = "00000000-0000-4000-8000-000000000002"
_TEMPLATE_ID: Final = "00000000-0000-4000-8000-000000000003"
_OWNER: Final = "a" * 64
_VALUE: Final = "SECRET-CODER-HERMES"


class AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class ScriptedRunner:
    def __init__(self, results: list[str | CoderSecretClientError]) -> None:
        self.commands: list[tuple[str, ...]] = []
        self._results = results

    def __call__(self, command: tuple[str, ...]) -> str:
        self.commands.append(command)
        result = self._results.pop(0)
        if isinstance(result, CoderSecretClientError):
            raise result
        return result


def _spec() -> CoderSecretSpec:
    return CoderSecretSpec(
        name="hermes-openai-api-key",
        env_name="OPENAI_API_KEY",
        value=_VALUE,
        description="Hermes LiteLLM key",
    )


def _templates() -> str:
    return json.dumps([{"id": _TEMPLATE_ID, "name": "ubuntu-vscode-opencode-pi"}])


def _workspaces(*, status: str = "running", name: str = "proof-workspace") -> str:
    return json.dumps(
        [
            {
                "id": _WORKSPACE_ID,
                "name": name,
                "owner_id": _OWNER_ID,
                "owner_name": "admin",
                "template_id": _TEMPLATE_ID,
                "template_name": "ubuntu-vscode-opencode-pi",
                "latest_build": {"status": status},
            }
        ]
    )


def _verifier(
    tmp_path: Path, runner: ScriptedRunner, clock: AdvancingClock
) -> CoderWorkspaceValueHashVerifier:
    return CoderWorkspaceValueHashVerifier(
        runner=runner,
        state_dir=tmp_path,
        clock=clock,
        policy=WorkspaceVerificationPolicy(timeout_seconds=1.0, poll_interval_seconds=1.0),
        workspace_name="proof-workspace",
    )


def test_value_hash_cleans_exact_workspace_after_agent_observation(tmp_path: Path) -> None:
    expected_hash = sha256(_VALUE.encode()).hexdigest()
    runner = ScriptedRunner(
        [
            _templates(),
            "",
            _workspaces(),
            _workspaces(),
            _workspaces(),
            f"{expected_hash}\n",
            _workspaces(),
            "",
            "[]",
        ]
    )
    verifier = _verifier(tmp_path, runner, AdvancingClock())

    observed_hash = verifier.verify(_spec(), _OWNER)

    receipt = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert observed_hash == expected_hash
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.DELETED
    assert receipt.workspace_id == _WORKSPACE_ID
    assert receipt.template_id == _TEMPLATE_ID
    assert receipt.owner_id == _OWNER
    assert runner.commands[-2] == ("delete", "--yes", _WORKSPACE_ID)
    assert all(_VALUE not in argument for command in runner.commands for argument in command)
    assert _VALUE not in (tmp_path / "coder-workspace-verification-receipt.json").read_text()


def test_value_hash_marks_receipt_failed_after_readiness_timeout(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [
            _templates(),
            "",
            _workspaces(status="starting"),
            _workspaces(status="starting"),
            _workspaces(status="starting"),
            _workspaces(),
            "",
            "[]",
        ]
    )
    verifier = _verifier(tmp_path, runner, AdvancingClock())

    with pytest.raises(CoderSecretClientError, match="timed out"):
        verifier.verify(_spec(), _OWNER)

    receipt = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.FAILED
    assert runner.commands[-2] == ("delete", "--yes", _WORKSPACE_ID)


def test_value_hash_marks_receipt_blocked_for_malformed_agent_hash(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [
            _templates(),
            "",
            _workspaces(),
            _workspaces(),
            _workspaces(),
            "not-a-sha256\nextra\n",
            _workspaces(),
            "",
            "[]",
        ]
    )
    verifier = _verifier(tmp_path, runner, AdvancingClock())

    with pytest.raises(CoderSecretClientError, match="hash is invalid"):
        verifier.verify(_spec(), _OWNER)

    receipt = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.BLOCKED
    assert runner.commands[-2] == ("delete", "--yes", _WORKSPACE_ID)


def test_value_hash_marks_receipt_blocked_for_agent_hash_mismatch(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [
            _templates(),
            "",
            _workspaces(),
            _workspaces(),
            _workspaces(),
            f"{'b' * 64}\n",
            _workspaces(),
            "",
            "[]",
        ]
    )
    verifier = _verifier(tmp_path, runner, AdvancingClock())

    with pytest.raises(CoderSecretClientError, match="does not match"):
        verifier.verify(_spec(), _OWNER)

    receipt = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.BLOCKED
    assert receipt.observed_value_sha256 == "b" * 64


def test_value_hash_rejects_non_uppercase_environment_before_workspace_creation(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner([])
    verifier = _verifier(tmp_path, runner, AdvancingClock())
    invalid = CoderSecretSpec(
        name="hermes-openai-api-key",
        env_name="openai_api_key",
        value=_VALUE,
        description="Hermes LiteLLM key",
    )

    with pytest.raises(CoderSecretClientError, match="environment name is invalid"):
        verifier.verify(invalid, _OWNER)

    assert runner.commands == []


def test_value_hash_leaves_planned_receipt_after_create_timeout(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [
            _templates(),
            CoderSecretClientError(
                "Coder secret command timed out", kind="client_command_timeout"
            ),
        ]
    )
    verifier = _verifier(tmp_path, runner, AdvancingClock())

    with pytest.raises(CoderSecretClientError, match="command timed out"):
        verifier.verify(_spec(), _OWNER)

    receipt = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.PLANNED
    assert receipt.create_attempts == 1


def test_value_hash_retries_planned_create_once_when_timeout_left_no_workspace(
    tmp_path: Path,
) -> None:
    expected_hash = sha256(_VALUE.encode()).hexdigest()
    first = _verifier(
        tmp_path,
        ScriptedRunner(
            [
                _templates(),
                CoderSecretClientError(
                    "Coder secret command timed out", kind="client_command_timeout"
                ),
            ]
        ),
        AdvancingClock(),
    )

    with pytest.raises(CoderSecretClientError, match="command timed out"):
        first.verify(_spec(), _OWNER)

    planned = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert planned is not None
    assert planned.phase is WorkspaceVerificationPhase.PLANNED
    second_runner = ScriptedRunner(
        [
            "[]",
            "",
            _workspaces(),
            _workspaces(),
            _workspaces(),
            f"{expected_hash}\n",
            _workspaces(),
            "",
            "[]",
        ]
    )

    observed_hash = _verifier(tmp_path, second_runner, AdvancingClock()).verify(_spec(), _OWNER)

    assert observed_hash == expected_hash
    assert second_runner.commands[1][0] == "create"


def test_value_hash_rejects_unattempted_planned_workspace(tmp_path: Path) -> None:
    expected_hash = sha256(_VALUE.encode()).hexdigest()
    store = WorkspaceVerificationReceiptStore(tmp_path)
    store.write_planned(
        WorkspaceVerificationPlan(
            owner_id=_OWNER,
            workspace_name="proof-workspace",
            template_id=_TEMPLATE_ID,
            template_name="ubuntu-vscode-opencode-pi",
            env_name="OPENAI_API_KEY",
            expected_value_sha256=expected_hash,
        )
    )
    runner = ScriptedRunner(
        [_workspaces(), _workspaces(), _workspaces(), f"{expected_hash}\n", _workspaces(), "", "[]"]
    )

    with pytest.raises(CoderSecretClientError, match="identity drifted"):
        _verifier(tmp_path, runner, AdvancingClock()).verify(_spec(), _OWNER)

    assert not any(command[0] == "create" for command in runner.commands)
    assert not any(command[0] == "delete" for command in runner.commands)


def test_value_hash_exhausts_planned_create_retries_without_unowned_delete(tmp_path: Path) -> None:
    first = _verifier(
        tmp_path,
        ScriptedRunner(
            [
                _templates(),
                CoderSecretClientError(
                    "Coder secret command timed out", kind="client_command_timeout"
                ),
            ]
        ),
        AdvancingClock(),
    )
    with pytest.raises(CoderSecretClientError, match="command timed out"):
        first.verify(_spec(), _OWNER)
    second = _verifier(
        tmp_path,
        ScriptedRunner(
            [
                "[]",
                CoderSecretClientError(
                    "Coder secret command timed out", kind="client_command_timeout"
                ),
            ]
        ),
        AdvancingClock(),
    )
    with pytest.raises(CoderSecretClientError, match="command timed out"):
        second.verify(_spec(), _OWNER)
    exhausted_runner = ScriptedRunner(["[]"])

    with pytest.raises(CoderSecretClientError, match="retries exhausted"):
        _verifier(tmp_path, exhausted_runner, AdvancingClock()).verify(_spec(), _OWNER)

    receipt = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.FAILED
    assert receipt.failure_reason == "create_retry_exhausted"
    assert not any(command[0] in {"create", "delete"} for command in exhausted_runner.commands)


@pytest.mark.parametrize(
    "workspace_payload",
    [
        json.dumps(json.loads(_workspaces()) * 2),
        json.dumps([{**json.loads(_workspaces())[0], "template_name": "other-template"}]),
    ],
)
def test_value_hash_blocks_ambiguous_or_mismatched_planned_workspace(
    tmp_path: Path, workspace_payload: str
) -> None:
    store = WorkspaceVerificationReceiptStore(tmp_path)
    store.write_planned(
        WorkspaceVerificationPlan(
            owner_id=_OWNER,
            workspace_name="proof-workspace",
            template_id=_TEMPLATE_ID,
            template_name="ubuntu-vscode-opencode-pi",
            env_name="OPENAI_API_KEY",
            expected_value_sha256=sha256(_VALUE.encode()).hexdigest(),
        )
    )
    runner = ScriptedRunner([workspace_payload])

    with pytest.raises(CoderSecretClientError, match="identity drifted"):
        _verifier(tmp_path, runner, AdvancingClock()).verify(_spec(), _OWNER)

    receipt = store.load()
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.BLOCKED
    assert not any(command[0] == "delete" for command in runner.commands)


def test_value_hash_blocks_identity_drift_before_ssh_or_delete(tmp_path: Path) -> None:
    runner = ScriptedRunner([_templates(), "", _workspaces(), _workspaces(name="other-workspace")])
    verifier = _verifier(tmp_path, runner, AdvancingClock())

    with pytest.raises(CoderSecretClientError, match="identity drifted"):
        verifier.verify(_spec(), _OWNER)

    receipt = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.BLOCKED
    assert not any(command[0] in {"ssh", "delete"} for command in runner.commands)


def test_value_hash_retains_failed_receipt_when_exact_delete_fails(tmp_path: Path) -> None:
    expected_hash = sha256(_VALUE.encode()).hexdigest()
    runner = ScriptedRunner(
        [
            _templates(),
            "",
            _workspaces(),
            _workspaces(),
            _workspaces(),
            f"{expected_hash}\n",
            _workspaces(),
            CoderSecretClientError(
                "Coder secret command failed", kind="client_command_failed"
            ),
        ]
    )
    verifier = _verifier(tmp_path, runner, AdvancingClock())

    with pytest.raises(CoderSecretClientError, match="command failed"):
        verifier.verify(_spec(), _OWNER)

    receipt = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.FAILED
    assert runner.commands[-1] == ("delete", "--yes", _WORKSPACE_ID)


def test_value_hash_resumes_bound_workspace_after_interrupted_verification(tmp_path: Path) -> None:
    expected_hash = sha256(_VALUE.encode()).hexdigest()
    store = WorkspaceVerificationReceiptStore(tmp_path)
    store.write_planned(
        WorkspaceVerificationPlan(
            owner_id=_OWNER,
            workspace_name="proof-workspace",
            template_id=_TEMPLATE_ID,
            template_name="ubuntu-vscode-opencode-pi",
            env_name="OPENAI_API_KEY",
            expected_value_sha256=expected_hash,
        )
    )
    store.begin_create(store.load() or pytest.fail("planned receipt missing"))
    store.bind_workspace(_WORKSPACE_ID, _OWNER_ID, "admin")
    runner = ScriptedRunner(
        [_workspaces(), _workspaces(), f"{expected_hash}\n", _workspaces(), "", "[]"]
    )
    verifier = _verifier(tmp_path, runner, AdvancingClock())

    observed_hash = verifier.verify(_spec(), _OWNER)

    receipt = store.load()
    assert observed_hash == expected_hash
    assert receipt is not None
    assert receipt.phase is WorkspaceVerificationPhase.DELETED
    assert not any(command[0] == "create" for command in runner.commands)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"unexpected": "field"}, "invalid"),
        ({"phase": "created"}, "invalid"),
        ({"workspace_id": _WORKSPACE_ID}, "invalid"),
        ({"workspace_owner_id": _OWNER_ID}, "invalid"),
        ({"observed_value_sha256": "b" * 64}, "invalid"),
        ({"expected_value_sha256": "not-a-hash"}, "invalid"),
        ({"failure_reason": "forged"}, "invalid"),
        ({"created_at": "not-a-timestamp"}, "invalid"),
        ({"updated_at": "2000-01-01T00:00:00Z"}, "invalid"),
    ],
)
def test_workspace_receipt_rejects_tampered_lifecycle_fields(
    tmp_path: Path, changes: dict[str, str], error: str
) -> None:
    store = WorkspaceVerificationReceiptStore(tmp_path)
    store.write_planned(
        WorkspaceVerificationPlan(
            owner_id=_OWNER,
            workspace_name="proof-workspace",
            template_id=_TEMPLATE_ID,
            template_name="ubuntu-vscode-opencode-pi",
            env_name="OPENAI_API_KEY",
            expected_value_sha256=sha256(_VALUE.encode()).hexdigest(),
        )
    )
    path = tmp_path / "coder-workspace-verification-receipt.json"
    payload = json.loads(path.read_text())
    payload.update(changes)
    path.write_text(json.dumps(payload))
    path.chmod(0o600)

    with pytest.raises(CoderSecretClientError, match=error):
        store.load()


def test_workspace_receipt_rejects_symlink_and_insecure_file_or_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceVerificationReceiptStore(tmp_path)
    receipt = store.write_planned(
        WorkspaceVerificationPlan(
            owner_id=_OWNER,
            workspace_name="proof-workspace",
            template_id=_TEMPLATE_ID,
            template_name="ubuntu-vscode-opencode-pi",
            env_name="OPENAI_API_KEY",
            expected_value_sha256=sha256(_VALUE.encode()).hexdigest(),
        )
    )
    path = tmp_path / "coder-workspace-verification-receipt.json"
    target = tmp_path / "forged.json"
    target.write_text(path.read_text())
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(CoderSecretClientError, match="cannot be read"):
        store.load()

    path.unlink()
    store.write(receipt)
    path.chmod(0o640)
    with pytest.raises(CoderSecretClientError, match="cannot be read"):
        store.load()
    path.chmod(0o600)
    os.chmod(tmp_path, 0o755)
    with pytest.raises(CoderSecretClientError, match="cannot be read"):
        store.load()
    os.chmod(tmp_path, 0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(CoderSecretClientError, match="cannot be read"):
        store.load()


def test_workspace_receipt_rejects_oversized_data(tmp_path: Path) -> None:
    store = WorkspaceVerificationReceiptStore(tmp_path)
    store.write_planned(
        WorkspaceVerificationPlan(
            owner_id=_OWNER,
            workspace_name="proof-workspace",
            template_id=_TEMPLATE_ID,
            template_name="ubuntu-vscode-opencode-pi",
            env_name="OPENAI_API_KEY",
            expected_value_sha256=sha256(_VALUE.encode()).hexdigest(),
        )
    )
    path = tmp_path / "coder-workspace-verification-receipt.json"
    path.write_bytes(b"x" * (64 * 1024 + 1))
    path.chmod(0o600)

    with pytest.raises(CoderSecretClientError, match="cannot be read"):
        store.load()
