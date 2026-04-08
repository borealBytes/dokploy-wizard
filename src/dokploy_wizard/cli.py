# pyright: reportMissingImports=false

"""CLI scaffold for the Dokploy wizard."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from dokploy_wizard.bootstrap import (
    LOCAL_HEALTH_URL,
    DokployBootstrapBackend,
    DokployBootstrapError,
    ShellDokployBootstrapBackend,
    reconcile_dokploy,
)
from dokploy_wizard.core import (
    SharedCoreBackend,
    SharedCoreError,
    ShellSharedCoreBackend,
)
from dokploy_wizard.dokploy import (
    DokployBootstrapAuthClient,
    DokployBootstrapAuthError,
    DokployHeadscaleBackend,
    DokployMatrixBackend,
    DokployNextcloudBackend,
    DokploySeaweedFsBackend,
    DokploySharedCoreBackend,
)
from dokploy_wizard.lifecycle import (
    LifecycleBackends,
    LifecycleDriftError,
    LifecyclePlan,
    applicable_phases_for,
    classify_install_request,
    classify_modify_request,
    execute_lifecycle_plan,
    validate_preserved_phases,
)
from dokploy_wizard.networking import (
    CloudflareApiBackend,
    CloudflareError,
)
from dokploy_wizard.packs.headscale import (
    HeadscaleBackend,
    HeadscaleError,
    ShellHeadscaleBackend,
)
from dokploy_wizard.packs.matrix import (
    MatrixBackend,
    MatrixError,
    ShellMatrixBackend,
)
from dokploy_wizard.packs.nextcloud import (
    NextcloudBackend,
    NextcloudError,
    ShellNextcloudBackend,
)
from dokploy_wizard.packs.openclaw import (
    OpenClawBackend,
    OpenClawError,
    ShellOpenClawBackend,
)
from dokploy_wizard.packs.prompts import (
    apply_prompt_selection,
    prompt_for_initial_install_values,
    prompt_for_pack_selection,
)
from dokploy_wizard.packs.resolver import has_explicit_pack_selection
from dokploy_wizard.packs.seaweedfs import (
    SeaweedFsBackend,
    SeaweedFsError,
    ShellSeaweedFsBackend,
)
from dokploy_wizard.preflight import PreflightError, collect_host_facts, run_preflight
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    DesiredState,
    OwnershipLedger,
    RawEnvInput,
    StateValidationError,
    load_state_dir,
    parse_env_file,
    persist_install_scaffold,
    resolve_desired_state,
    validate_existing_state,
    write_applied_checkpoint,
    write_inspection_snapshot,
    write_target_state,
)
from dokploy_wizard.tailscale import ShellTailscaleBackend, TailscaleBackend, TailscaleError
from dokploy_wizard.uninstall import (
    ShellUninstallBackend,
    UninstallBackend,
    UninstallConfirmationError,
    UninstallExecutionError,
    UninstallPlanningError,
    build_pack_disable_plan,
    build_uninstall_plan,
    collect_confirmation_lines,
    execute_uninstall_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dokploy-wizard",
        description="Provision, modify, or remove a Dokploy business stack.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser(
        "install",
        help="install the wizard-managed stack",
        description=(
            "Install the wizard-managed stack. Provide --env-file for reusable env-file mode, "
            "or omit it for a guided first-run install in an interactive terminal."
        ),
    )
    install_parser.add_argument(
        "--env-file",
        type=Path,
        help="path to the reusable env file (optional for guided first-run install)",
    )
    install_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".dokploy-wizard-state"),
        help="directory containing persisted wizard state documents",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the preflight and bootstrap summary without writing state",
    )
    install_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="disable interactive pack-selection prompts",
    )
    install_parser.set_defaults(handler=_handle_install)

    modify_parser = subparsers.add_parser("modify", help="modify supported wizard settings")
    modify_parser.add_argument(
        "--env-file",
        type=Path,
        required=True,
        help="path to the reusable env file",
    )
    modify_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".dokploy-wizard-state"),
        help="directory containing persisted wizard state documents",
    )
    modify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the supported modify plan without writing state",
    )
    modify_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="disable interactive pack-selection prompts",
    )
    modify_parser.set_defaults(handler=_handle_modify)

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="remove wizard-managed resources",
    )
    uninstall_mode = uninstall_parser.add_mutually_exclusive_group()
    uninstall_mode.add_argument(
        "--retain-data",
        action="store_true",
        help="delete retain-safe runtime resources and keep data-bearing owned resources",
    )
    uninstall_mode.add_argument(
        "--destroy-data",
        action="store_true",
        help="delete all wizard-owned resources, including data-bearing ones",
    )
    uninstall_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".dokploy-wizard-state"),
        help="directory containing persisted wizard state documents",
    )
    uninstall_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the uninstall plan without mutating state",
    )
    uninstall_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="disable interactive confirmation prompts",
    )
    uninstall_parser.add_argument(
        "--confirm-file",
        type=Path,
        help="path to a file containing typed uninstall confirmation lines",
    )
    uninstall_parser.set_defaults(handler=_handle_uninstall)

    inspect_state_parser = subparsers.add_parser(
        "inspect-state",
        help="resolve and validate wizard state without running lifecycle actions",
    )
    inspect_state_parser.add_argument(
        "--env-file",
        type=Path,
        required=True,
        help="path to the reusable env file",
    )
    inspect_state_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".dokploy-wizard-state"),
        help="directory containing persisted wizard state documents",
    )
    inspect_state_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved desired state without writing files",
    )
    inspect_state_parser.set_defaults(handler=_handle_inspect_state)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], int], args.handler)
    return handler(args)


def _handle_install(args: argparse.Namespace) -> int:
    try:
        env_file, raw_env, resolved_state_dir = _resolve_install_input(
            env_file=args.env_file,
            state_dir=args.state_dir,
            non_interactive=args.non_interactive,
            dry_run=args.dry_run,
        )
        summary = run_install_flow(
            env_file=env_file,
            state_dir=resolved_state_dir,
            dry_run=args.dry_run,
            raw_env=raw_env,
        )
    except (
        OSError,
        StateValidationError,
        PreflightError,
        DokployBootstrapError,
        CloudflareError,
        SharedCoreError,
        TailscaleError,
        HeadscaleError,
        DokployBootstrapAuthError,
        LifecycleDriftError,
        MatrixError,
        NextcloudError,
        OpenClawError,
        SeaweedFsError,
    ) as error:
        raise SystemExit(str(error)) from error

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _resolve_install_input(
    *,
    env_file: Path | None,
    state_dir: Path,
    non_interactive: bool,
    dry_run: bool,
) -> tuple[Path, RawEnvInput, Path]:
    if env_file is not None:
        return env_file, _load_install_raw_env(env_file, non_interactive=non_interactive), state_dir
    if non_interactive:
        raise StateValidationError(
            "--env-file is required when --non-interactive is set for install."
        )
    if not _stdin_is_interactive():
        raise StateValidationError(
            "Interactive install requires a TTY when --env-file is not provided."
        )
    resolved_state_dir = _prompt_for_guided_state_dir(state_dir)
    raw_env = _prompt_for_initial_install_raw_env(require_dokploy_auth=not dry_run)
    guided_env_file = _guided_install_env_file(resolved_state_dir)
    _write_reusable_env_file(guided_env_file, raw_env)
    return guided_env_file, raw_env, resolved_state_dir


def _load_install_raw_env(env_file: Path, *, non_interactive: bool) -> RawEnvInput:
    raw_env = parse_env_file(env_file)
    if (
        non_interactive
        or has_explicit_pack_selection(raw_env.values)
        or not _stdin_is_interactive()
    ):
        return raw_env
    return apply_prompt_selection(raw_env, prompt_for_pack_selection())


def _stdin_is_interactive() -> bool:
    try:
        return os.isatty(0)
    except OSError:
        return False


def _prompt_for_initial_install_raw_env(*, require_dokploy_auth: bool) -> RawEnvInput:
    guided_values = prompt_for_initial_install_values(require_dokploy_auth=require_dokploy_auth)
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": guided_values.stack_name,
            "ROOT_DOMAIN": guided_values.root_domain,
            "DOKPLOY_SUBDOMAIN": guided_values.dokploy_subdomain,
            "DOKPLOY_ADMIN_EMAIL": guided_values.dokploy_admin_email,
            "ENABLE_HEADSCALE": "true" if guided_values.enable_headscale else "false",
            "CLOUDFLARE_API_TOKEN": guided_values.cloudflare_api_token,
            "CLOUDFLARE_ACCOUNT_ID": guided_values.cloudflare_account_id,
            "CLOUDFLARE_ZONE_ID": guided_values.cloudflare_zone_id,
            "ENABLE_TAILSCALE": "true" if guided_values.enable_tailscale else "false",
        },
    )
    if guided_values.dokploy_admin_password is not None:
        raw_env.values["DOKPLOY_ADMIN_PASSWORD"] = guided_values.dokploy_admin_password
    if guided_values.enable_tailscale:
        assert guided_values.tailscale_auth_key is not None
        assert guided_values.tailscale_hostname is not None
        raw_env.values["TAILSCALE_AUTH_KEY"] = guided_values.tailscale_auth_key
        raw_env.values["TAILSCALE_HOSTNAME"] = guided_values.tailscale_hostname
        raw_env.values["TAILSCALE_ENABLE_SSH"] = (
            "true" if guided_values.tailscale_enable_ssh else "false"
        )
        if guided_values.tailscale_tags:
            raw_env.values["TAILSCALE_TAGS"] = ",".join(guided_values.tailscale_tags)
        if guided_values.tailscale_subnet_routes:
            raw_env.values["TAILSCALE_SUBNET_ROUTES"] = ",".join(
                guided_values.tailscale_subnet_routes
            )
    return apply_prompt_selection(
        raw_env,
        prompt_for_pack_selection(include_headscale_prompt=False),
    )


def _prompt_for_guided_state_dir(state_dir: Path) -> Path:
    response = input(
        f"Wizard state directory (install.env + state docs only; default: {state_dir}): "
    ).strip()
    if response == "":
        return state_dir
    return Path(response).expanduser()


def _guided_install_env_file(state_dir: Path) -> Path:
    return state_dir / "install.env"


def _write_reusable_env_file(path: Path, raw_env: RawEnvInput) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={value}" for key, value in sorted(raw_env.values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _handle_modify(args: argparse.Namespace) -> int:
    try:
        raw_env = _load_install_raw_env(args.env_file, non_interactive=args.non_interactive)
        summary = run_modify_flow(
            env_file=args.env_file,
            state_dir=args.state_dir,
            dry_run=args.dry_run,
            raw_env=raw_env,
        )
    except (
        OSError,
        StateValidationError,
        PreflightError,
        DokployBootstrapError,
        CloudflareError,
        SharedCoreError,
        TailscaleError,
        HeadscaleError,
        LifecycleDriftError,
        MatrixError,
        NextcloudError,
        OpenClawError,
        SeaweedFsError,
    ) as error:
        raise SystemExit(str(error)) from error

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _handle_uninstall(args: argparse.Namespace) -> int:
    try:
        summary = run_uninstall_flow(
            state_dir=args.state_dir,
            destroy_data=args.destroy_data,
            dry_run=args.dry_run,
            non_interactive=args.non_interactive,
            confirm_file=args.confirm_file,
        )
    except (
        OSError,
        StateValidationError,
        UninstallConfirmationError,
        UninstallExecutionError,
        UninstallPlanningError,
    ) as error:
        raise SystemExit(str(error)) from error

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _handle_inspect_state(args: argparse.Namespace) -> int:
    try:
        load_state_dir(args.state_dir)
        raw_env = parse_env_file(args.env_file)
        desired_state = resolve_desired_state(raw_env)
        if not args.dry_run:
            write_inspection_snapshot(args.state_dir, raw_env, desired_state)
    except (OSError, StateValidationError) as error:
        raise SystemExit(str(error)) from error

    print(json.dumps(desired_state.to_dict(), indent=2, sort_keys=True))
    return 0


def run_install_flow(
    *,
    env_file: Path,
    state_dir: Path,
    dry_run: bool,
    raw_env: RawEnvInput | None = None,
    bootstrap_backend: DokployBootstrapBackend | None = None,
    tailscale_backend: TailscaleBackend | None = None,
    networking_backend: Any | None = None,
    shared_core_backend: SharedCoreBackend | None = None,
    headscale_backend: HeadscaleBackend | None = None,
    matrix_backend: MatrixBackend | None = None,
    nextcloud_backend: NextcloudBackend | None = None,
    seaweedfs_backend: SeaweedFsBackend | None = None,
    openclaw_backend: OpenClawBackend | None = None,
) -> dict[str, Any]:
    return _run_lifecycle_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=dry_run,
        raw_env=raw_env,
        bootstrap_backend=bootstrap_backend,
        tailscale_backend=tailscale_backend,
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=headscale_backend,
        matrix_backend=matrix_backend,
        nextcloud_backend=nextcloud_backend,
        seaweedfs_backend=seaweedfs_backend,
        openclaw_backend=openclaw_backend,
        allow_modify=False,
    )


def run_modify_flow(
    *,
    env_file: Path,
    state_dir: Path,
    dry_run: bool,
    raw_env: RawEnvInput | None = None,
    bootstrap_backend: DokployBootstrapBackend | None = None,
    tailscale_backend: TailscaleBackend | None = None,
    networking_backend: Any | None = None,
    shared_core_backend: SharedCoreBackend | None = None,
    headscale_backend: HeadscaleBackend | None = None,
    matrix_backend: MatrixBackend | None = None,
    nextcloud_backend: NextcloudBackend | None = None,
    seaweedfs_backend: SeaweedFsBackend | None = None,
    openclaw_backend: OpenClawBackend | None = None,
) -> dict[str, Any]:
    return _run_lifecycle_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=dry_run,
        raw_env=raw_env,
        bootstrap_backend=bootstrap_backend,
        tailscale_backend=tailscale_backend,
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=headscale_backend,
        matrix_backend=matrix_backend,
        nextcloud_backend=nextcloud_backend,
        seaweedfs_backend=seaweedfs_backend,
        openclaw_backend=openclaw_backend,
        allow_modify=True,
    )


def run_uninstall_flow(
    *,
    state_dir: Path,
    destroy_data: bool,
    dry_run: bool,
    non_interactive: bool,
    confirm_file: Path | None,
    uninstall_backend: UninstallBackend | None = None,
) -> dict[str, Any]:
    loaded_state = load_state_dir(state_dir)
    if not validate_existing_state(loaded_state):
        raise StateValidationError(
            "Cannot uninstall before a successful install has created persisted state."
        )

    assert loaded_state.raw_input is not None
    assert loaded_state.desired_state is not None
    assert loaded_state.applied_state is not None
    assert loaded_state.ownership_ledger is not None
    if (
        loaded_state.applied_state.desired_state_fingerprint
        != loaded_state.desired_state.fingerprint()
    ):
        raise StateValidationError(
            "Persisted applied state fingerprint does not match the persisted desired state."
        )

    plan = build_uninstall_plan(
        raw_input=loaded_state.raw_input,
        desired_state=loaded_state.desired_state,
        ownership_ledger=loaded_state.ownership_ledger,
        destroy_data=destroy_data,
    )
    confirmation_lines: tuple[str, ...] = ()
    if not dry_run:
        confirmation_lines = collect_confirmation_lines(
            non_interactive=non_interactive,
            confirm_file=confirm_file,
            mode=plan.mode,
            environment=loaded_state.desired_state.stack_name,
        )

    execution = execute_uninstall_plan(
        state_dir=state_dir,
        raw_input=loaded_state.raw_input,
        desired_state=loaded_state.desired_state,
        ownership_ledger=loaded_state.ownership_ledger,
        plan=plan,
        backend=uninstall_backend or ShellUninstallBackend(loaded_state.raw_input),
        dry_run=dry_run,
    )
    return {
        "confirmation_lines": list(confirmation_lines),
        "deleted_resources": [item.to_dict() for item in execution.deleted_resources],
        "destroy_data": destroy_data,
        "dry_run": dry_run,
        "environment": loaded_state.desired_state.stack_name,
        "mode": plan.mode,
        "remaining_completed_steps": list(execution.remaining_completed_steps),
        "retained_resources": [resource.to_dict() for resource in plan.retained_resources],
        "state_cleared": execution.state_cleared,
        "state_dir": str(state_dir),
        "warnings": list(plan.warnings),
    }


def _run_lifecycle_flow(
    *,
    env_file: Path,
    state_dir: Path,
    dry_run: bool,
    raw_env: RawEnvInput | None,
    bootstrap_backend: DokployBootstrapBackend | None,
    tailscale_backend: TailscaleBackend | None,
    networking_backend: Any | None,
    shared_core_backend: SharedCoreBackend | None,
    headscale_backend: HeadscaleBackend | None,
    matrix_backend: MatrixBackend | None,
    nextcloud_backend: NextcloudBackend | None,
    seaweedfs_backend: SeaweedFsBackend | None,
    openclaw_backend: OpenClawBackend | None,
    allow_modify: bool,
) -> dict[str, Any]:
    loaded_state = load_state_dir(state_dir)
    existing_state = validate_existing_state(loaded_state)
    raw_env = raw_env or parse_env_file(env_file)
    desired_state = resolve_desired_state(raw_env)
    host_facts = collect_host_facts(raw_env)
    preflight_report = run_preflight(desired_state, host_facts)
    backend = bootstrap_backend or ShellDokployBootstrapBackend(raw_env)
    ownership_ledger = loaded_state.ownership_ledger or OwnershipLedger(
        format_version=desired_state.format_version,
        resources=(),
    )

    disable_plan = None
    disable_execution: dict[str, Any] | None = None
    if allow_modify:
        if not existing_state:
            raise StateValidationError(
                "Cannot modify before a successful install has created state."
            )
        assert loaded_state.raw_input is not None
        assert loaded_state.desired_state is not None
        assert loaded_state.applied_state is not None
        assert loaded_state.ownership_ledger is not None
        lifecycle_plan = classify_modify_request(
            existing_raw=loaded_state.raw_input,
            existing_desired=loaded_state.desired_state,
            existing_applied=loaded_state.applied_state,
            existing_ledger=loaded_state.ownership_ledger,
            requested_raw=raw_env,
            requested_desired=desired_state,
        )
        disable_plan = build_pack_disable_plan(
            existing_desired=loaded_state.desired_state,
            requested_desired=desired_state,
            ownership_ledger=loaded_state.ownership_ledger,
        )
    elif existing_state:
        assert loaded_state.raw_input is not None
        assert loaded_state.desired_state is not None
        assert loaded_state.applied_state is not None
        lifecycle_plan = classify_install_request(
            existing_raw=loaded_state.raw_input,
            existing_desired=loaded_state.desired_state,
            existing_applied=loaded_state.applied_state,
            requested_raw=raw_env,
            requested_desired=desired_state,
        )
    else:
        lifecycle_plan = LifecyclePlan(
            mode="install",
            reasons=("Fresh install requested against an empty state directory.",),
            applicable_phases=applicable_phases_for(desired_state),
            phases_to_run=applicable_phases_for(desired_state)[1:],
            preserved_phases=(),
            initial_completed_steps=(),
            start_phase="dokploy_bootstrap",
            raw_equivalent=False,
            desired_equivalent=False,
        )

    raw_env = _ensure_dokploy_api_auth(
        env_file=env_file,
        raw_env=raw_env,
        desired_state=desired_state,
        bootstrap_backend=backend,
        dry_run=dry_run,
        require_real_dokploy_auth=_dokploy_api_auth_required(
            desired_state=desired_state,
            shared_core_backend=shared_core_backend,
            headscale_backend=headscale_backend,
            matrix_backend=matrix_backend,
            nextcloud_backend=nextcloud_backend,
            seaweedfs_backend=seaweedfs_backend,
        ),
    )
    desired_state = resolve_desired_state(raw_env)
    tailscale_phase_backend = tailscale_backend or ShellTailscaleBackend(raw_env)
    cloudflare_backend = networking_backend or CloudflareApiBackend(raw_env)
    shared_core_phase_backend = shared_core_backend or _build_shared_core_backend(
        raw_env=raw_env,
        desired_state=desired_state,
    )
    headscale_phase_backend = headscale_backend or _build_headscale_backend(
        raw_env=raw_env,
        desired_state=desired_state,
    )
    matrix_phase_backend = matrix_backend or _build_matrix_backend(
        raw_env=raw_env,
        desired_state=desired_state,
    )
    nextcloud_phase_backend = nextcloud_backend or _build_nextcloud_backend(
        raw_env=raw_env,
        desired_state=desired_state,
    )
    seaweedfs_phase_backend = seaweedfs_backend or _build_seaweedfs_backend(
        raw_env=raw_env,
        desired_state=desired_state,
    )
    openclaw_phase_backend = openclaw_backend or ShellOpenClawBackend(raw_env)
    lifecycle_backends = LifecycleBackends(
        bootstrap=backend,
        tailscale=tailscale_phase_backend,
        networking=cloudflare_backend,
        shared_core=shared_core_phase_backend,
        headscale=headscale_phase_backend,
        matrix=matrix_phase_backend,
        nextcloud=nextcloud_phase_backend,
        seaweedfs=seaweedfs_phase_backend,
        openclaw=openclaw_phase_backend,
    )

    validate_preserved_phases(
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
        preserved_phases=lifecycle_plan.preserved_phases,
        bootstrap_backend=backend,
        tailscale_backend=tailscale_phase_backend,
        networking_backend=cloudflare_backend,
        shared_core_backend=shared_core_phase_backend,
        headscale_backend=headscale_phase_backend,
        matrix_backend=matrix_phase_backend,
        nextcloud_backend=nextcloud_phase_backend,
        seaweedfs_backend=seaweedfs_phase_backend,
        openclaw_backend=openclaw_phase_backend,
    )

    if not dry_run:
        if not existing_state:
            persist_install_scaffold(state_dir, raw_env, desired_state)
        else:
            write_target_state(state_dir, raw_env, desired_state)
            if loaded_state.applied_state is None or (
                loaded_state.applied_state.completed_steps != lifecycle_plan.initial_completed_steps
                or loaded_state.applied_state.desired_state_fingerprint
                != desired_state.fingerprint()
            ):
                write_applied_checkpoint(
                    state_dir,
                    AppliedStateCheckpoint(
                        format_version=desired_state.format_version,
                        desired_state_fingerprint=desired_state.fingerprint(),
                        completed_steps=lifecycle_plan.initial_completed_steps,
                    ),
                )

        if allow_modify and disable_plan is not None and disable_plan.deletions:
            execution = execute_uninstall_plan(
                state_dir=state_dir,
                raw_input=raw_env,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                plan=disable_plan,
                backend=ShellUninstallBackend(raw_env),
                dry_run=False,
            )
            ownership_ledger = load_state_dir(state_dir).ownership_ledger or OwnershipLedger(
                format_version=desired_state.format_version,
                resources=(),
            )
            disable_execution = {
                "deleted_resources": [item.to_dict() for item in execution.deleted_resources],
                "remaining_completed_steps": list(execution.remaining_completed_steps),
                "retained_resources": [
                    resource.to_dict() for resource in disable_plan.retained_resources
                ],
                "warnings": list(disable_plan.warnings),
            }

    summary = execute_lifecycle_plan(
        state_dir=state_dir,
        dry_run=dry_run,
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
        preflight_report=preflight_report,
        lifecycle_plan=lifecycle_plan,
        backends=lifecycle_backends,
    )
    if allow_modify and disable_plan is not None:
        summary["disable_teardown"] = {
            "planned_deletions": [item.to_dict() for item in disable_plan.deletions],
            "retained_resources": [
                resource.to_dict() for resource in disable_plan.retained_resources
            ],
            "warnings": list(disable_plan.warnings),
        }
        if disable_execution is not None:
            summary["disable_teardown"]["executed"] = disable_execution
    summary["state_dir"] = str(state_dir)
    return summary


def _build_shared_core_backend(
    *, raw_env: RawEnvInput, desired_state: DesiredState
) -> SharedCoreBackend:
    if raw_env.values.get("DOKPLOY_MOCK_API_MODE") == "true":
        return ShellSharedCoreBackend()
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if api_url and api_key:
        return DokploySharedCoreBackend(
            api_url=api_url,
            api_key=api_key,
            stack_name=desired_state.stack_name,
            plan=desired_state.shared_core,
        )
    return ShellSharedCoreBackend()


def _build_headscale_backend(
    *, raw_env: RawEnvInput, desired_state: DesiredState
) -> HeadscaleBackend:
    if raw_env.values.get("DOKPLOY_MOCK_API_MODE") == "true":
        return ShellHeadscaleBackend(raw_env)
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    hostname = desired_state.hostnames.get("headscale")
    if api_url and api_key and hostname is not None:
        return DokployHeadscaleBackend(
            api_url=api_url,
            api_key=api_key,
            stack_name=desired_state.stack_name,
            hostname=hostname,
        )
    return ShellHeadscaleBackend(raw_env)


def _build_nextcloud_backend(
    *, raw_env: RawEnvInput, desired_state: DesiredState
) -> NextcloudBackend:
    if raw_env.values.get("DOKPLOY_MOCK_API_MODE") == "true":
        return ShellNextcloudBackend(raw_env)
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if not api_url or not api_key or "nextcloud" not in desired_state.enabled_packs:
        return ShellNextcloudBackend(raw_env)
    nextcloud_hostname = desired_state.hostnames.get("nextcloud")
    onlyoffice_hostname = desired_state.hostnames.get("onlyoffice")
    allocation = next(
        (item for item in desired_state.shared_core.allocations if item.pack_name == "nextcloud"),
        None,
    )
    if nextcloud_hostname is None or onlyoffice_hostname is None or allocation is None:
        return ShellNextcloudBackend(raw_env)
    if (
        allocation.postgres is None
        or allocation.redis is None
        or desired_state.shared_core.postgres is None
        or desired_state.shared_core.redis is None
    ):
        return ShellNextcloudBackend(raw_env)
    return DokployNextcloudBackend(
        api_url=api_url,
        api_key=api_key,
        stack_name=desired_state.stack_name,
        nextcloud_hostname=nextcloud_hostname,
        onlyoffice_hostname=onlyoffice_hostname,
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=allocation.postgres,
        redis=allocation.redis,
        integration_secret_ref=f"{desired_state.stack_name}-nextcloud-onlyoffice-jwt-secret",
    )


def _build_matrix_backend(*, raw_env: RawEnvInput, desired_state: DesiredState) -> MatrixBackend:
    if raw_env.values.get("DOKPLOY_MOCK_API_MODE") == "true":
        return ShellMatrixBackend(raw_env)
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if not api_url or not api_key or "matrix" not in desired_state.enabled_packs:
        return ShellMatrixBackend(raw_env)
    hostname = desired_state.hostnames.get("matrix")
    allocation = next(
        (item for item in desired_state.shared_core.allocations if item.pack_name == "matrix"),
        None,
    )
    if (
        hostname is None
        or allocation is None
        or desired_state.shared_core.postgres is None
        or desired_state.shared_core.redis is None
    ):
        return ShellMatrixBackend(raw_env)
    return DokployMatrixBackend(
        api_url=api_url,
        api_key=api_key,
        stack_name=desired_state.stack_name,
        hostname=hostname,
        shared_allocation=allocation,
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        secret_refs=(
            f"{desired_state.stack_name}-matrix-registration-shared-secret",
            f"{desired_state.stack_name}-matrix-macaroon-secret-key",
        ),
    )


def _build_seaweedfs_backend(
    *, raw_env: RawEnvInput, desired_state: DesiredState
) -> SeaweedFsBackend:
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if not api_url or not api_key or "seaweedfs" not in desired_state.enabled_packs:
        return ShellSeaweedFsBackend(raw_env)
    hostname = desired_state.hostnames.get("s3")
    access_key = desired_state.seaweedfs_access_key
    secret_key = desired_state.seaweedfs_secret_key
    if hostname is None or access_key is None or secret_key is None:
        return ShellSeaweedFsBackend(raw_env)
    return DokploySeaweedFsBackend(
        api_url=api_url,
        api_key=api_key,
        stack_name=desired_state.stack_name,
        hostname=hostname,
        access_key=access_key,
        secret_key=secret_key,
    )


def _dokploy_api_auth_required(
    *,
    desired_state: DesiredState,
    shared_core_backend: SharedCoreBackend | None,
    headscale_backend: HeadscaleBackend | None,
    matrix_backend: MatrixBackend | None,
    nextcloud_backend: NextcloudBackend | None,
    seaweedfs_backend: SeaweedFsBackend | None,
) -> bool:
    if shared_core_backend is None and desired_state.shared_core.requires_reconciliation():
        return True
    if headscale_backend is None and "headscale" in desired_state.enabled_packs:
        return True
    if matrix_backend is None and "matrix" in desired_state.enabled_packs:
        return True
    if nextcloud_backend is None and "nextcloud" in desired_state.enabled_packs:
        return True
    if seaweedfs_backend is None and "seaweedfs" in desired_state.enabled_packs:
        return True
    return False


def _ensure_dokploy_api_auth(
    *,
    env_file: Path,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    bootstrap_backend: DokployBootstrapBackend,
    dry_run: bool,
    require_real_dokploy_auth: bool,
) -> RawEnvInput:
    values = dict(raw_env.values)
    if values.get("DOKPLOY_BOOTSTRAP_MOCK_API_KEY") and not dry_run:
        values["DOKPLOY_API_URL"] = desired_state.dokploy_url
        values["DOKPLOY_API_KEY"] = values["DOKPLOY_BOOTSTRAP_MOCK_API_KEY"]
        values["DOKPLOY_MOCK_API_MODE"] = "true"
        updated = RawEnvInput(format_version=raw_env.format_version, values=values)
        _write_reusable_env_file(env_file, updated)
        return updated
    if values.get("DOKPLOY_API_KEY"):
        if "DOKPLOY_API_URL" not in values:
            values["DOKPLOY_API_URL"] = desired_state.dokploy_url
        return RawEnvInput(format_version=raw_env.format_version, values=values)
    if dry_run or not require_real_dokploy_auth:
        return raw_env
    admin_email = values.get("DOKPLOY_ADMIN_EMAIL")
    admin_password = values.get("DOKPLOY_ADMIN_PASSWORD")
    if admin_email is None or admin_password is None:
        raise StateValidationError(
            "Dokploy admin email/password are required to bootstrap local "
            "Dokploy API auth for real installs."
        )

    reconcile_dokploy(dry_run=False, backend=bootstrap_backend)
    result = DokployBootstrapAuthClient(base_url=LOCAL_HEALTH_URL).ensure_api_key(
        admin_email=admin_email,
        admin_password=admin_password,
    )
    values["DOKPLOY_API_URL"] = desired_state.dokploy_url
    values["DOKPLOY_API_KEY"] = result.api_key
    updated = RawEnvInput(format_version=raw_env.format_version, values=values)
    _write_reusable_env_file(env_file, updated)
    return updated
