"""Production adapters for the Host A upgrade proof workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_identity import RemoteProbe
from dokploy_wizard.proof.model_sync_remote import probe_host
from dokploy_wizard.proof.model_sync_results import ProofTransport
from dokploy_wizard.proof.model_sync_strict_proof import (
    StrictProofResult,
    run_strict_proof,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_coder import CoderUpgradeClient
from dokploy_wizard.proof.model_sync_upgrade_host_a_observations import (
    HostAObservation,
    derive_observed_totals,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_process import (
    ModifyCommandObservation,
    ModifyExecutionObservation,
    parse_modify_execution,
    run_bounded_observed_process,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    ManagedHostSnapshot,
    ModifyAttempt,
    RetiredFixtureEvidence,
    UpgradeHostABinding,
    UpgradeHostAError,
)
from dokploy_wizard.proof.mutation_registry import (
    MutationBackend,
    MutationKind,
    ProofMutationRecorder,
)

_BLOCKED_CODE = "CODER_RETIRED_WORKSPACE_NOT_STOPPED"
_OUTPUT_LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True, repr=False)
class ProductionUpgradeConfig:
    host: str
    password: str
    wrapper: Path
    env_file: Path
    artifact_dir: Path
    binding: UpgradeHostABinding
    namespace: proof.ProofNamespace
    transport: ProofTransport


class ProductionUpgradeHostAOperations:
    """Live operations bound to one host, env file, and explicit deploy commit."""

    def __init__(
        self,
        config: ProductionUpgradeConfig,
        coder: CoderUpgradeClient,
    ) -> None:
        self._config = config
        self._coder = coder
        self._latest_observation: HostAObservation | None = None

    def probe(self) -> RemoteProbe:
        return probe_host(
            host=self._config.host,
            password=self._config.password,
            namespace=self._config.namespace,
            proof_transport=self._config.transport,
            timeout_seconds=120,
        )

    def snapshot(self) -> ManagedHostSnapshot:
        return self._coder.snapshot(self._latest_observation)

    def create_retired_fixtures(self) -> RetiredFixtureEvidence:
        return self._coder.create_retired_fixtures()

    def destructive_state_sha256(self) -> str:
        return self._coder.destructive_state_sha256()

    def modify(self) -> ModifyAttempt:
        execution = self._run_modify()
        before = execution.before
        after = execution.after
        command = execution.command
        self._latest_observation = after
        totals = derive_observed_totals(before, after)
        blocked = command.exit_code == 1 and command.failure_code == _BLOCKED_CODE
        if blocked:
            if (
                after.release.commit_sha != self._config.binding.final_commit
                or (not _managed_observation_matches(before, after))
                or (totals.control_plane_mutations != 0 or totals.synchronizer_durable_writes != 0)
            ):
                raise UpgradeHostAError("blocked Host A observation drifted")
            return ModifyAttempt(
                command.exit_code,
                command.failure_code,
                None,
                totals.control_plane_mutations,
                totals.synchronizer_durable_writes,
            )
        if command.exit_code != 0 or command.failure_code is not None:
            raise UpgradeHostAError("Host A modify command observation is invalid")
        if (
            before.release != after.release
            or after.release.commit_sha != self._config.binding.final_commit
        ):
            raise UpgradeHostAError("Host A active release commit does not match Task 18")
        return ModifyAttempt(
            command.exit_code,
            command.failure_code,
            after.release.commit_sha,
            totals.control_plane_mutations,
            totals.synchronizer_durable_writes,
        )

    def stop_running_fixture(self, workspace_id: str) -> None:
        self._coder.stop_running_fixture(workspace_id)

    def strict_proof(self) -> StrictProofResult:
        observation = self._latest_observation
        if (
            observation is None
            or observation.release.commit_sha != self._config.binding.final_commit
            or not observation.catalog_exact
            or not observation.schedule_exact
            or not observation.source_exact
        ):
            raise UpgradeHostAError("Strict proof requires an exact observed Host A upgrade")
        return run_strict_proof(
            artifact_dir=self._config.artifact_dir,
            strict_rerun=self._strict_rerun,
            workspace_client=lambda _template_name: self._coder,
        )

    def _strict_rerun(self, recorder: ProofMutationRecorder) -> None:
        execution = self._run_modify()
        before = execution.before
        after = execution.after
        command = execution.command
        self._latest_observation = after
        if command != ModifyCommandObservation(0, None, "noop", ()):
            raise UpgradeHostAError("Host A strict rerun was not an exact no-op")
        totals = derive_observed_totals(before, after)
        for _mutation in range(totals.control_plane_mutations):
            recorder.execute(
                backend=MutationBackend.CODER.value,
                kind=MutationKind.CONTROL_PLANE,
                action=lambda: None,
            )
        for _write in range(totals.synchronizer_durable_writes):
            recorder.execute(
                backend=MutationBackend.SCHEDULE_HELPER.value,
                kind=MutationKind.SYNCHRONIZER_DURABLE_WRITE,
                action=lambda: None,
            )
        recorder.assert_strict_zero()
        if before.release != after.release or not _managed_observation_matches(before, after):
            raise UpgradeHostAError("Host A strict observation drifted")

    def _run_modify(self) -> ModifyExecutionObservation:
        process = run_bounded_observed_process(
            [
                str(self._config.wrapper),
                "modify",
                "--host",
                self._config.host,
                "--password-stdin",
                "--env-file",
                str(self._config.env_file),
                "--deploy-commit",
                self._config.binding.final_commit,
                "--verbose",
                "--capture-upgrade-observations",
            ],
            stdin=(self._config.password + "\n").encode(),
            output_limit=_OUTPUT_LIMIT,
            timeout_seconds=3600,
        )
        return parse_modify_execution(
            process,
            remote_root=Path("/root/dokploy-wizard"),
        )


def _managed_observation_matches(before: HostAObservation, after: HostAObservation) -> bool:
    return (
        before.migration == after.migration
        and before.catalog == after.catalog
        and before.schedule == after.schedule
        and before.sync_results == after.sync_results
    )
