from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretSpec
from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_contract import (
    CoderWorkspaceClock,
    CoderWorkspaceRunner,
    SystemCoderWorkspaceClock,
    WorkspaceHashError,
    WorkspaceIdentityError,
    WorkspaceRecord,
    WorkspaceVerificationIntent,
    WorkspaceVerificationPolicy,
)
from dokploy_wizard.dokploy.coder_secret_workspace_inventory import (
    environment_name,
    sha256_output,
    verification_template,
    workspace_hash_command,
    workspace_records,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationPhase,
    WorkspaceVerificationPlan,
    WorkspaceVerificationReceipt,
    WorkspaceVerificationReceiptStore,
)
from dokploy_wizard.dokploy.coder_secret_workspace_recovery import (
    WorkspaceFailure,
    block_identity,
    create_or_bind_planned,
    create_planned,
    fail_after_cleanup,
    finish_resumed,
    require_matching_receipt,
)


@dataclass(frozen=True, slots=True)
class CoderWorkspaceValueHashVerifier:
    runner: CoderWorkspaceRunner
    state_dir: Path
    clock: CoderWorkspaceClock = SystemCoderWorkspaceClock()
    policy: WorkspaceVerificationPolicy = WorkspaceVerificationPolicy()
    workspace_name: str | None = None

    def verify(self, spec: CoderSecretSpec, owner_id: str) -> str:
        env_name = environment_name(spec.env_name)
        intent = WorkspaceVerificationIntent(
            owner_id,
            env_name,
            sha256(spec.value.encode()).hexdigest(),
        )
        store = WorkspaceVerificationReceiptStore(self.state_dir)
        receipt = store.load()
        if receipt is None or receipt.phase is WorkspaceVerificationPhase.DELETED:
            try:
                receipt = self._create(store, intent)
            except WorkspaceIdentityError as error:
                block_identity(store, error)
            except CoderSecretClientError as error:
                if _is_retriable_planned(store.load()):
                    raise error
                fail_after_cleanup(
                    self,
                    store,
                    WorkspaceFailure(
                        WorkspaceVerificationPhase.FAILED, "verification_failure", error
                    ),
                )
        else:
            require_matching_receipt(receipt, intent)
            try:
                receipt = self._resume(store, receipt)
            except WorkspaceIdentityError as error:
                block_identity(store, error)
            if receipt.phase in {
                WorkspaceVerificationPhase.HASHED,
                WorkspaceVerificationPhase.DELETING,
            }:
                return finish_resumed(self, store, receipt)
        try:
            ready = store.transition(self._await_ready(receipt), WorkspaceVerificationPhase.READY)
            self._require_exact(ready)
            command = (
                "ssh",
                ready.workspace_id or "",
                "--",
                "sh",
                "-lc",
                workspace_hash_command(env_name),
            )
            observed = sha256_output(self.runner(command))
            if observed != intent.expected_value_sha256:
                blocked = store.transition(
                    ready,
                    WorkspaceVerificationPhase.BLOCKED,
                    observed_value_sha256=observed,
                    failure_reason="hash_mismatch",
                )
                self._cleanup(store, blocked, WorkspaceVerificationPhase.BLOCKED)
                raise WorkspaceHashError("Coder verification workspace hash does not match")
            hashed = store.transition(
                ready,
                WorkspaceVerificationPhase.HASHED,
                observed_value_sha256=observed,
            )
            self._cleanup(store, hashed, WorkspaceVerificationPhase.DELETED)
            return observed
        except WorkspaceIdentityError as error:
            block_identity(store, error)
        except WorkspaceHashError as error:
            receipt = store.load()
            if receipt is not None and receipt.failure_reason == "hash_mismatch":
                raise error
            fail_after_cleanup(
                self,
                store,
                WorkspaceFailure(WorkspaceVerificationPhase.BLOCKED, "hash_invalid", error),
            )
        except CoderSecretClientError as error:
            fail_after_cleanup(
                self,
                store,
                WorkspaceFailure(
                    WorkspaceVerificationPhase.FAILED,
                    "verification_failure",
                    error,
                ),
            )

    def _create(
        self,
        store: WorkspaceVerificationReceiptStore,
        intent: WorkspaceVerificationIntent,
    ) -> WorkspaceVerificationReceipt:
        template = verification_template(self.runner(("templates", "list", "--output", "json")))
        receipt = store.write_planned(
            WorkspaceVerificationPlan(
                owner_id=intent.owner_id,
                workspace_name=self.workspace_name
                or f"wizard-secret-proof-{intent.owner_id[:12]}-{uuid4().hex[:12]}",
                template_id=template.template_id,
                template_name=template.template_name,
                env_name=intent.env_name,
                expected_value_sha256=intent.expected_value_sha256,
            )
        )
        return create_planned(self.runner, store, receipt)

    def _resume(
        self, store: WorkspaceVerificationReceiptStore, receipt: WorkspaceVerificationReceipt
    ) -> WorkspaceVerificationReceipt:
        match receipt.phase:
            case WorkspaceVerificationPhase.PLANNED:
                return create_or_bind_planned(self.runner, store, receipt)
            case WorkspaceVerificationPhase.BLOCKED | WorkspaceVerificationPhase.FAILED:
                if receipt.workspace_id is not None:
                    self._cleanup(store, receipt, receipt.phase)
                raise CoderSecretClientError(
                    "Coder workspace verification receipt is terminal",
                    kind="client_workspace_terminal",
                )
            case _:
                return receipt

    def _await_ready(self, receipt: WorkspaceVerificationReceipt) -> WorkspaceVerificationReceipt:
        deadline = self.clock.monotonic() + self.policy.timeout_seconds
        while True:
            workspace = self._require_exact(receipt)
            if workspace is None:
                raise WorkspaceIdentityError("Coder verification workspace identity drifted")
            if workspace.status == "running":
                return receipt
            if self.clock.monotonic() >= deadline:
                raise CoderSecretClientError("Coder verification workspace readiness timed out")
            self.clock.sleep(self.policy.poll_interval_seconds)

    def _cleanup(
        self,
        store: WorkspaceVerificationReceiptStore,
        receipt: WorkspaceVerificationReceipt,
        terminal_phase: WorkspaceVerificationPhase,
    ) -> None:
        if receipt.workspace_id is None:
            raise CoderSecretClientError("Coder verification workspace identity is unavailable")
        current = store.transition(receipt, WorkspaceVerificationPhase.DELETING)
        if self._require_exact(current) is None:
            store.transition(
                current,
                terminal_phase,
                observed_value_sha256=receipt.observed_value_sha256,
                failure_reason=receipt.failure_reason,
            )
            return
        workspace_id = current.workspace_id
        if workspace_id is None:
            raise CoderSecretClientError("Coder verification workspace identity is unavailable")
        self.runner(("delete", "--yes", workspace_id))
        deadline = self.clock.monotonic() + self.policy.timeout_seconds
        while True:
            if self._require_exact(current) is None:
                store.transition(
                    current,
                    terminal_phase,
                    observed_value_sha256=receipt.observed_value_sha256,
                    failure_reason=receipt.failure_reason,
                )
                return
            if self.clock.monotonic() >= deadline:
                raise CoderSecretClientError("Coder verification workspace deletion timed out")
            self.clock.sleep(self.policy.poll_interval_seconds)

    def _require_exact(self, receipt: WorkspaceVerificationReceipt) -> WorkspaceRecord | None:
        if receipt.workspace_id is None:
            raise WorkspaceIdentityError("Coder verification workspace identity drifted")
        matches = tuple(
            workspace
            for workspace in workspace_records(self.runner(("list", "--output", "json")))
            if workspace.workspace_id == receipt.workspace_id
        )
        if len(matches) > 1:
            raise WorkspaceIdentityError("Coder verification workspace identity drifted")
        if not matches:
            return None
        workspace = matches[0]
        if (
            workspace.workspace_name != receipt.workspace_name
            or workspace.owner_id != receipt.workspace_owner_id
            or workspace.owner_name != receipt.workspace_owner_name
            or workspace.template_id != receipt.template_id
            or workspace.template_name != receipt.template_name
        ):
            raise WorkspaceIdentityError("Coder verification workspace identity drifted")
        return workspace


def _is_retriable_planned(receipt: WorkspaceVerificationReceipt | None) -> bool:
    return receipt is not None and receipt.phase is WorkspaceVerificationPhase.PLANNED
