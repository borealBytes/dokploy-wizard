"""Task 1 proof orchestration with durable remote-cleanup finalization recovery."""

from __future__ import annotations

import argparse
import os
import sys
from typing import assert_never

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_finalization import (
    BaselineArtifactInputs,
    build_task1_finalization_plan,
)
from dokploy_wizard.proof.model_sync_host_inputs import HostInputNames
from dokploy_wizard.proof.model_sync_identity import ObservedResource
from dokploy_wizard.proof.model_sync_state import AbortGuardError
from dokploy_wizard.proof.model_sync_task1_baseline_helpers import (
    cleanup_report_bytes as _cleanup_report,
)
from dokploy_wizard.proof.model_sync_task1_baseline_helpers import (
    clear_recovery_sidecars as _clear_recovery_sidecars,
)
from dokploy_wizard.proof.model_sync_task1_baseline_helpers import (
    prepare_proof as _prepare_proof,
)
from dokploy_wizard.proof.model_sync_task1_baseline_resume import (
    Task1ResumeInputs,
    resume_completed_cleanup,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    parse_cleanup_report,
    parse_restoration_report,
)
from dokploy_wizard.proof.model_sync_task1_finalization_recovery import (
    abort_validated_remote_restoration,
    bind_validated_remote_cleanup,
    complete_task1_finalization,
    persist_task1_finalization_plan,
)
from dokploy_wizard.proof.model_sync_task1_flow import Task1ProofPreparation
from dokploy_wizard.proof.model_sync_task1_remote_abort import (
    record_remote_mutation_possible,
    remote_abort_record_exists,
)


def run_baseline_host_a(args: argparse.Namespace) -> None:
    """Execute a baseline proof or resume only its locally durable finalization work."""
    from dokploy_wizard.proof import model_sync_cli as cli

    mode: proof.HostIdentityMode = (
        "single_sequential" if args.single_host_sequential else "distinct"
    )
    host_a, password_a, host_b, password_b = cli.resolve_host_inputs(
        HostInputNames(args.host_env, args.password_env, args.host_b_env, args.host_b_password_env),
        mode,
    )
    paths = proof.ProofRecoveryPaths(
        args.env_file,
        args.external_backup,
        args.abort_guard,
        args.artifact_dir,
        args.output,
        args.active_root,
    )
    repository_root = cli._require_active_workspace_root(args.wrapper, paths)
    task1_context_enabled = bool(getattr(args, "task1_proof_context", False))
    if task1_context_enabled and resume_completed_cleanup(
        Task1ResumeInputs(args, paths, host_a, password_a)
    ):
        return
    recovery = cli.begin_proof_recovery(
        paths=paths, pid=os.getpid(), start_time_ticks=cli._self_start_time_ticks()
    )
    if recovery.terminal:
        return
    signal_state = {"critical": False, "completed": False, "pending": 0}
    previous_handlers = cli._install_recovery_handlers(recovery, signal_state)
    completed = False
    remote_cleanup_pending = False
    cleanup_recovered = False
    safely_aborted = False
    finalization_bundle_published = False
    remote_cleanup_error: Exception | None = None
    preparation: Task1ProofPreparation | None = None
    try:
        if recovery.resumable:
            signal_state["critical"] = True
            cli.complete_resumable_finalization(recovery)
            completed = True
            signal_state["completed"] = True
            if task1_context_enabled:
                _clear_recovery_sidecars(args.abort_guard, host_a)
            cli.signal_recovery.replay_pending_signal(
                recovery, signal_state, cli.recover_interrupted_proof
            )
            return
        if recovery.claim is None:
            raise AbortGuardError("nonterminal proof recovery has no claim")
        prepared, namespace, remote_env_file, remote_context, preparation = _prepare_proof(
            args, recovery.claim.token, task1_context_enabled
        )
        transport = cli.resolve_proof_transport(remote_env_file)
        host_a_probe, host_b_probe = cli.probe_baseline_hosts(
            host_a=host_a,
            password_a=password_a,
            host_b=host_b,
            password_b=password_b,
            mode=mode,
            namespace=namespace,
            transport=transport,
        )
        if task1_context_enabled:
            signal_state["critical"] = True
            record_remote_mutation_possible(args.abort_guard, host_a)
            remote_cleanup_pending = True
        cli._run_wrapper(
            args.wrapper,
            host_a,
            password_a,
            remote_env_file,
            remote_context,
            args.proof_commit if task1_context_enabled else None,
        )
        post_install_probe = cli.probe_host(
            host=host_a,
            password=password_a,
            namespace=namespace,
            proof_transport=transport,
        )
        post_install_cloudflare = cli.classify_post_install_resources(
            host_a_probe.inventory["cloudflare"],
            post_install_probe.inventory["cloudflare"],
            namespace,
            ObservedResource,
        )
        baseline = cli.parse_captured_baseline(
            cli.capture_host_a_snapshot(host=host_a, password=password_a),
            stack_name=namespace.stack_name,
        )
        inputs = BaselineArtifactInputs(
            repository_root=repository_root,
            artifact_dir=args.artifact_dir,
            output=args.output,
            source_base_commit=args.source_base_commit,
            proof_commit=args.proof_commit,
            prepared=prepared,
            guard_path=args.abort_guard,
            claim=recovery.claim,
            host_identity_mode=mode,
            host_a=host_a_probe,
            host_b=host_b_probe,
            baseline=baseline,
            post_install_cloudflare=post_install_cloudflare,
            task1_proof=None if preparation is None else preparation.external,
        )
        if task1_context_enabled:
            persist_task1_finalization_plan(
                args.abort_guard, host_a, build_task1_finalization_plan(inputs)
            )
            finalization_bundle_published = True
            try:
                cleanup_bytes = _cleanup_report(args, host_a, password_a, preparation)
            except (OSError, RuntimeError, ValueError) as error:
                remote_cleanup_error = error
            else:
                remote_cleanup_pending = False
                match parse_restoration_report(cleanup_bytes).status:
                    case "cleaned":
                        bind_validated_remote_cleanup(
                            args.abort_guard, host_a, parse_cleanup_report(cleanup_bytes)
                        )
                        complete_task1_finalization(recovery)
                        completed = True
                    case "restored":
                        abort_validated_remote_restoration(
                            recovery, host_a, clear_bundle=finalization_bundle_published
                        )
                        safely_aborted = True
                    case unexpected:
                        assert_never(unexpected)
        else:
            signal_state["critical"] = True
            cli.finalize_baseline_artifacts(inputs)
            completed = True
        if completed:
            signal_state["completed"] = True
            if task1_context_enabled:
                _clear_recovery_sidecars(args.abort_guard, host_a)
            signal_state["critical"] = False
            cli.signal_recovery.replay_pending_signal(
                recovery, signal_state, cli.recover_interrupted_proof
            )
    finally:
        primary_error = sys.exception()
        if remote_cleanup_pending and preparation is not None:
            try:
                cleanup_bytes = _cleanup_report(args, host_a, password_a, preparation)
            except (OSError, RuntimeError, ValueError) as error:
                remote_cleanup_error = error
            else:
                remote_cleanup_pending = False
                match parse_restoration_report(cleanup_bytes).status:
                    case "cleaned":
                        if finalization_bundle_published:
                            bind_validated_remote_cleanup(
                                args.abort_guard, host_a, parse_cleanup_report(cleanup_bytes)
                            )
                            cleanup_recovered = True
                            remote_cleanup_error = None
                        else:
                            abort_validated_remote_restoration(recovery, host_a, clear_bundle=False)
                            safely_aborted = True
                    case "restored":
                        abort_validated_remote_restoration(
                            recovery, host_a, clear_bundle=finalization_bundle_published
                        )
                        safely_aborted = True
                    case unexpected:
                        assert_never(unexpected)
        if cleanup_recovered:
            complete_task1_finalization(recovery)
            completed = True
            signal_state["completed"] = True
            _clear_recovery_sidecars(args.abort_guard, host_a)
        signal_state["critical"] = False
        cli._restore_recovery_handlers(previous_handlers)
        if cleanup_recovered:
            cli.signal_recovery.replay_pending_signal(
                recovery, signal_state, cli.recover_interrupted_proof
            )
        if safely_aborted:
            raise AbortGuardError(
                "Task 1 previous attempt was safely aborted; "
                "recreate or clean the host before retrying"
            )
        if not completed and primary_error is None:
            if remote_cleanup_error is not None:
                raise AbortGuardError(
                    "Task 1 remote cleanup failed; recovery state is preserved"
                ) from remote_cleanup_error
            if task1_context_enabled and remote_abort_record_exists(args.abort_guard):
                raise AbortGuardError("Task 1 finalization recovery is required")
            cli.recover_interrupted_proof(recovery)
