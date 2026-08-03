from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_secret_destroy import CoderSecretDestroyer
from dokploy_wizard.dokploy.coder_service_teardown import (
    CoderServiceTeardown,
    CoderServiceTeardownBinding,
    CoderServiceTeardownError,
)
from dokploy_wizard.dokploy.coder_service_teardown_receipts import (
    CoderServiceTeardownPhase,
    CoderServiceTeardownReceipt,
    CoderServiceTeardownReceiptStore,
)
from dokploy_wizard.packs.coder import CODER_SERVICE_RESOURCE_TYPE
from dokploy_wizard.state import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput
from dokploy_wizard.state.env import resolve_desired_state
from dokploy_wizard.uninstall import executor as executor_module
from dokploy_wizard.uninstall.executor import execute_uninstall_plan
from dokploy_wizard.uninstall.planner import (
    PlannedDeletion,
    UninstallPlan,
    build_uninstall_plan,
)

from .coder_secret_destroy_support import (
    OWNER_ID,
    SECRET_IDENTITIES,
    SecretDestroyClient,
    metadata,
    record_uninstall_deletion,
    seed_uninstall_authority,
    write_completed_secret_receipt,
    write_terminal_workspace_receipt,
)

_BINDING = CoderServiceTeardownBinding(
    stack_name="stack",
    resource_type=CODER_SERVICE_RESOURCE_TYPE,
    resource_id="coder-service",
    resource_scope="stack:stack:coder",
    owner_id=OWNER_ID,
)


class SimulatedCrash(RuntimeError):
    """Represents process loss at a durable transaction boundary."""


class ExecutorBackend:
    def __init__(self, state_dir: Path, client: SecretDestroyClient) -> None:
        self.state_dir = state_dir
        self.client = client
        self.events: list[str] = []
        seed_uninstall_authority(
            state_dir,
            (
                OwnedResource(
                    _BINDING.resource_type,
                    _BINDING.resource_id,
                    _BINDING.resource_scope,
                ),
            ),
        )

    def destroy_coder_secrets(self, *, desired_state: DesiredState) -> None:
        del desired_state
        self.events.append("secret-destroy")
        CoderSecretDestroyer(self.state_dir, self.client, OWNER_ID).destroy()

    def delete(self, deletion: PlannedDeletion) -> None:
        self.events.append(f"delete:{deletion.resource.resource_type}")
        record_uninstall_deletion(self.state_dir, deletion.resource)


def _prepared_transaction(state_dir: Path) -> CoderServiceTeardown:
    write_completed_secret_receipt(state_dir)
    write_terminal_workspace_receipt(state_dir)
    client = SecretDestroyClient([metadata(identity) for identity in SECRET_IDENTITIES])
    CoderSecretDestroyer(state_dir, client, OWNER_ID).destroy()
    return CoderServiceTeardown(state_dir, _BINDING)


def _destroy_inputs() -> tuple[RawEnvInput, DesiredState, OwnershipLedger, UninstallPlan]:
    raw = RawEnvInput(
        format_version=1,
        values={"STACK_NAME": "stack", "ROOT_DOMAIN": "example.test", "ENABLE_CODER": "true"},
    )
    desired = resolve_desired_state(raw)
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                CODER_SERVICE_RESOURCE_TYPE,
                "coder-service",
                "stack:stack:coder",
            ),
        ),
    )
    plan = build_uninstall_plan(
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=True,
    )
    return raw, desired, ledger, plan


def test_teardown_intent_does_not_authorize_local_finalization(tmp_path: Path) -> None:
    transaction = _prepared_transaction(tmp_path)

    receipt = transaction.begin()

    assert receipt.phase == "intent"
    with pytest.raises(CoderServiceTeardownError, match="service deletion"):
        transaction.finalize()
    assert (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert (tmp_path / "coder-secret-destroy-receipts-v1.json").exists()
    assert (tmp_path / "coder-workspace-verification-receipt.json").exists()


def test_malformed_service_teardown_receipt_blocks_without_unlink(tmp_path: Path) -> None:
    transaction = _prepared_transaction(tmp_path)
    path = tmp_path / "coder-service-teardown-receipt-v1.json"
    path.write_text('{"schema_version":1}', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(CoderServiceTeardownError, match="receipt"):
        transaction.begin()

    assert (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert (tmp_path / "coder-secret-destroy-receipts-v1.json").exists()
    assert (tmp_path / "coder-workspace-verification-receipt.json").exists()


def test_crash_after_service_delete_before_completion_retries_without_coder_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw, desired, ledger, plan = _destroy_inputs()
    write_completed_secret_receipt(tmp_path)
    write_terminal_workspace_receipt(tmp_path)
    client = SecretDestroyClient([metadata(identity) for identity in SECRET_IDENTITIES])
    backend = ExecutorBackend(tmp_path, client)
    original_complete = CoderServiceTeardown.complete
    crashed = False

    def crash_once(transaction: CoderServiceTeardown) -> CoderServiceTeardownReceipt:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise SimulatedCrash("crash before service completion receipt")
        return original_complete(transaction)

    monkeypatch.setattr(CoderServiceTeardown, "complete", crash_once)

    with pytest.raises(SimulatedCrash, match="before service completion"):
        execute_uninstall_plan(
            state_dir=tmp_path,
            raw_input=raw,
            desired_state=desired,
            ownership_ledger=ledger,
            plan=plan,
            backend=backend,
            dry_run=False,
        )

    receipt = CoderServiceTeardownReceiptStore(tmp_path).load()
    assert receipt is not None and receipt.phase == "intent"
    api_events = tuple(client.events)
    client.api_available = False

    execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        plan=plan,
        backend=backend,
        dry_run=False,
    )

    assert tuple(client.events) == api_events
    assert backend.events.count(f"delete:{CODER_SERVICE_RESOURCE_TYPE}") == 2
    assert not (tmp_path / "coder-service-teardown-receipt-v1.json").exists()


def test_finalized_teardown_receipt_is_removed_after_ledger_boundary_without_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw, desired, ledger, plan = _destroy_inputs()
    write_completed_secret_receipt(tmp_path)
    write_terminal_workspace_receipt(tmp_path)
    client = SecretDestroyClient([metadata(identity) for identity in SECRET_IDENTITIES])
    backend = ExecutorBackend(tmp_path, client)
    original_finish = executor_module.finish_coder_service

    def crash_before_finish(transaction: CoderServiceTeardown) -> None:
        del transaction
        raise SimulatedCrash("crash before teardown receipt unlink")

    monkeypatch.setattr(executor_module, "finish_coder_service", crash_before_finish)

    with pytest.raises(SimulatedCrash, match="before teardown receipt unlink"):
        execute_uninstall_plan(
            state_dir=tmp_path,
            raw_input=raw,
            desired_state=desired,
            ownership_ledger=ledger,
            plan=plan,
            backend=backend,
            dry_run=False,
        )

    receipt = CoderServiceTeardownReceiptStore(tmp_path).load()
    assert receipt is not None and receipt.phase == "finalized"
    client.api_available = False
    monkeypatch.setattr(executor_module, "finish_coder_service", original_finish)
    empty_plan = UninstallPlan(
        mode="destroy",
        environment="stack",
        deletions=(),
        retained_resources=(),
        warnings=(),
    )

    execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        plan=empty_plan,
        backend=backend,
        dry_run=False,
    )

    assert not (tmp_path / "coder-service-teardown-receipt-v1.json").exists()


@pytest.mark.parametrize(
    ("crash_phase", "removed_filename"),
    (
        ("source_removed", "coder-secret-receipts-v1.json"),
        ("destroy_removed", "coder-secret-destroy-receipts-v1.json"),
        ("workspace_removed", "coder-workspace-verification-receipt.json"),
    ),
)
def test_finalization_resumes_after_each_receipt_unlink_without_coder_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: CoderServiceTeardownPhase,
    removed_filename: str,
) -> None:
    transaction = _prepared_transaction(tmp_path)
    transaction.begin()
    transaction.complete()
    unrelated = tmp_path / "unrelated-state.json"
    unrelated.write_text("preserve", encoding="utf-8")
    original_write = CoderServiceTeardownReceiptStore.write
    crashed = False

    def crash_once(
        store: CoderServiceTeardownReceiptStore,
        receipt: CoderServiceTeardownReceipt,
    ) -> None:
        nonlocal crashed
        if receipt.phase == crash_phase and not crashed:
            crashed = True
            raise SimulatedCrash("crash after receipt unlink")
        original_write(store, receipt)

    monkeypatch.setattr(CoderServiceTeardownReceiptStore, "write", crash_once)

    with pytest.raises(SimulatedCrash, match="after receipt unlink"):
        transaction.finalize()

    assert not (tmp_path / removed_filename).exists()
    assert (tmp_path / "coder-service-teardown-receipt-v1.json").exists()

    transaction.finalize()
    transaction.finish()

    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert not (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert not (tmp_path / "coder-secret-destroy-receipts-v1.json").exists()
    assert not (tmp_path / "coder-workspace-verification-receipt.json").exists()
    assert not (tmp_path / "coder-service-teardown-receipt-v1.json").exists()
