# pyright: reportMissingImports=false

"""CLI scaffold for the Dokploy wizard."""

from __future__ import annotations

import argparse
import inspect
import json
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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
    DokployApiClient,
    DokployApiError,
    DokployBootstrapAuthClient,
    DokployBootstrapAuthError,
    DokployCloudflaredBackend,
    DokployCoderBackend,
    DokployDocuSealBackend,
    DokployHeadscaleBackend,
    DokployMatrixBackend,
    DokployMoodleBackend,
    DokployNextcloudBackend,
    DokployOpenClawBackend,
    DokploySeaweedFsBackend,
    DokploySharedCoreBackend,
)
from dokploy_wizard.host_prereqs import (
    DOCKER_APT_PACKAGES,
    UbuntuAptHostPrerequisiteBackend,
    assess_host_prerequisites,
    remediate_host_prerequisites,
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
from dokploy_wizard.packs.coder import CoderBackend, CoderError, ShellCoderBackend
from dokploy_wizard.packs.docuseal import (
    DocuSealBackend,
    DocuSealError,
    ShellDocuSealBackend,
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
from dokploy_wizard.packs.moodle import MoodleBackend, MoodleError, ShellMoodleBackend
from dokploy_wizard.packs.nextcloud import (
    NextcloudBackend,
    NextcloudError,
    NextcloudOpenClawWorkspaceContract,
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
    sanitize_prompt_response,
)
from dokploy_wizard.packs.resolver import has_explicit_pack_selection
from dokploy_wizard.packs.seaweedfs import (
    SeaweedFsBackend,
    SeaweedFsError,
    ShellSeaweedFsBackend,
)
from dokploy_wizard.preflight import (
    REQUIRED_PORTS,
    SUPPORTED_OS_ID,
    PreflightError,
    _is_supported_ubuntu_version,
    collect_host_facts,
    run_preflight,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    DesiredState,
    LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
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
from dokploy_wizard.state.inspection import build_live_drift_report
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

if TYPE_CHECKING:
    from dokploy_wizard.dokploy import DokployProjectSummary

_LIVE_RUN_MOCK_CONTAMINATION_PREFIXES = (
    "DOKPLOY_BOOTSTRAP_",
    "DOKPLOY_MOCK_",
    "CLOUDFLARE_MOCK_",
    "TAILSCALE_MOCK_",
    "HEADSCALE_MOCK_",
)

_HOST_PREREQ_RECHECK_ATTEMPTS = 10
_HOST_PREREQ_RECHECK_DELAY_SECONDS = 1.0
_DEFAULT_DOKPLOY_ADMIN_PASSWORD = "ChangeMeSoon"
_PERSISTED_RETRY_KEYS = {
    "DOKPLOY_API_URL",
    "DOKPLOY_API_KEY",
    "SEAWEEDFS_ACCESS_KEY",
    "SEAWEEDFS_SECRET_KEY",
}


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
            "Install the wizard-managed stack. Provide --env-file for reusable env-file mode "
            "with a sensitive install.env operator file, or omit it for a guided first-run "
            "install in an interactive terminal."
        ),
    )
    install_parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "path to the sensitive reusable install.env operator file "
            "(optional for guided first-run install)"
        ),
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
    install_parser.add_argument(
        "--allow-memory-shortfall",
        action="store_true",
        help="allow install to continue when memory is the only preflight shortfall",
    )
    install_parser.add_argument(
        "--no-print-secrets",
        action="store_true",
        help="persist generated secrets without printing them to stdout",
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
        env_file, raw_env, resolved_state_dir, generated_secrets = _resolve_install_input(
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
            allow_memory_shortfall=getattr(args, "allow_memory_shortfall", False),
            prompt_for_memory_shortfall=not args.non_interactive and _stdin_is_interactive(),
            enforce_live_run_contamination_check=True,
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
        CoderError,
        DokployBootstrapAuthError,
        LifecycleDriftError,
        MatrixError,
        NextcloudError,
        OpenClawError,
        SeaweedFsError,
    ) as error:
        raise SystemExit(str(error)) from error

    print(json.dumps(summary, indent=2, sort_keys=True))
    if not getattr(args, "no_print_secrets", False):
        _emit_generated_secrets(generated_secrets, env_file)
    return 0


def _resolve_install_input(
    *,
    env_file: Path | None,
    state_dir: Path,
    non_interactive: bool,
    dry_run: bool,
) -> tuple[Path, RawEnvInput, Path, dict[str, str]]:
    if env_file is not None:
        return (
            env_file,
            _load_install_raw_env(
                env_file,
                non_interactive=non_interactive,
                warn_on_broad_permissions=not dry_run,
            ),
            state_dir,
            {},
        )
    if non_interactive:
        raise StateValidationError(
            "--env-file is required when --non-interactive is set for install."
        )
    if not _stdin_is_interactive():
        raise StateValidationError(
            "Interactive install requires a TTY when --env-file is not provided."
        )
    resolved_state_dir = _prompt_for_guided_state_dir(state_dir)
    raw_env, generated_secrets = _prompt_for_initial_install_raw_env(
        require_dokploy_auth=not dry_run
    )
    guided_env_file = _guided_install_env_file(resolved_state_dir)
    if guided_env_file.exists():
        raw_env, generated_secrets = _reuse_existing_guided_secrets(
            guided_env_file=guided_env_file,
            raw_env=raw_env,
            generated_secrets=generated_secrets,
        )
    _write_reusable_env_file(guided_env_file, raw_env)
    return guided_env_file, raw_env, resolved_state_dir, generated_secrets


def _load_install_raw_env(
    env_file: Path, *, non_interactive: bool, warn_on_broad_permissions: bool = False
) -> RawEnvInput:
    raw_env = parse_env_file(env_file)
    if warn_on_broad_permissions:
        _warn_if_broad_env_file_permissions(env_file)
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


def _prompt_for_initial_install_raw_env(
    *, require_dokploy_auth: bool
) -> tuple[RawEnvInput, dict[str, str]]:
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
    if guided_values.cloudflare_zone_id is not None:
        raw_env.values["CLOUDFLARE_ZONE_ID"] = guided_values.cloudflare_zone_id
    selection = prompt_for_pack_selection(include_headscale_prompt=False)
    updated_raw_env = apply_prompt_selection(
        raw_env,
        selection,
    )
    if {"openclaw", "my-farm-advisor"} & set(selection.selected_packs):
        admin_email = updated_raw_env.values.get("DOKPLOY_ADMIN_EMAIL", "").strip().lower()
        if admin_email:
            updated_raw_env.values["CLOUDFLARE_ACCESS_OTP_EMAILS"] = admin_email
    return updated_raw_env, selection.generated_secrets


def _prompt_for_guided_state_dir(state_dir: Path) -> Path:
    response = sanitize_prompt_response(
        input(f"Wizard state directory (install.env + state docs only; default: {state_dir}): ")
    ).strip()
    if response == "":
        return state_dir
    return Path(response).expanduser()


def _guided_install_env_file(state_dir: Path) -> Path:
    return state_dir / "install.env"


def _reuse_existing_guided_secrets(
    *,
    guided_env_file: Path,
    raw_env: RawEnvInput,
    generated_secrets: dict[str, str],
) -> tuple[RawEnvInput, dict[str, str]]:
    try:
        existing_raw_env = parse_env_file(guided_env_file)
    except StateValidationError:
        return raw_env, generated_secrets

    values = dict(raw_env.values)
    reused_generated_secrets = dict(generated_secrets)
    for key in ("SEAWEEDFS_ACCESS_KEY", "SEAWEEDFS_SECRET_KEY", "OPENCLAW_GATEWAY_TOKEN"):
        existing_value = existing_raw_env.values.get(key)
        if existing_value is None or key not in values:
            continue
        values[key] = existing_value
        reused_generated_secrets.pop(key, None)
    return RawEnvInput(
        format_version=raw_env.format_version, values=values
    ), reused_generated_secrets


def _write_reusable_env_file(path: Path, raw_env: RawEnvInput) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={value}" for key, value in sorted(raw_env.values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _warn_if_broad_env_file_permissions(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077 == 0:
        return
    print(
        (
            f"Warning: {path} permissions are broader than owner-only "
            f"({mode:o}); install.env files may contain secrets, so prefer 0600."
        ),
        file=sys.stderr,
    )


def _emit_generated_secrets(generated_secrets: dict[str, str], env_file: Path) -> None:
    if not generated_secrets:
        return
    print("")
    print(f"Generated credentials (saved to {env_file}):")
    for key, value in sorted(generated_secrets.items()):
        print(f"  {key}={value}")


def _handle_modify(args: argparse.Namespace) -> int:
    try:
        raw_env = _load_install_raw_env(
            args.env_file,
            non_interactive=args.non_interactive,
            warn_on_broad_permissions=not args.dry_run,
        )
        summary = run_modify_flow(
            env_file=args.env_file,
            state_dir=args.state_dir,
            dry_run=args.dry_run,
            raw_env=raw_env,
            enforce_live_run_contamination_check=True,
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
        CoderError,
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
        loaded_state = load_state_dir(args.state_dir)
        raw_env = parse_env_file(args.env_file)
        desired_state = resolve_desired_state(raw_env)
        snapshot = desired_state.to_dict()
        snapshot["live_drift"] = build_live_drift_report(
            desired_state=desired_state,
            ownership_ledger=loaded_state.ownership_ledger,
        )
        if not args.dry_run:
            write_inspection_snapshot(args.state_dir, raw_env, snapshot)
    except (OSError, StateValidationError) as error:
        raise SystemExit(str(error)) from error

    print(json.dumps(snapshot, indent=2, sort_keys=True))
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
    coder_backend: CoderBackend | None = None,
    openclaw_backend: OpenClawBackend | None = None,
    allow_memory_shortfall: bool = False,
    prompt_for_memory_shortfall: bool = False,
    enforce_live_run_contamination_check: bool = False,
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
        coder_backend=coder_backend,
        openclaw_backend=openclaw_backend,
        allow_modify=False,
        remediate_install_host_prereqs=True,
        allow_memory_shortfall=allow_memory_shortfall,
        prompt_for_memory_shortfall=prompt_for_memory_shortfall,
        enforce_live_run_contamination_check=enforce_live_run_contamination_check,
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
    coder_backend: CoderBackend | None = None,
    openclaw_backend: OpenClawBackend | None = None,
    enforce_live_run_contamination_check: bool = False,
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
        coder_backend=coder_backend,
        openclaw_backend=openclaw_backend,
        allow_modify=True,
        remediate_install_host_prereqs=False,
        allow_memory_shortfall=False,
        prompt_for_memory_shortfall=False,
        enforce_live_run_contamination_check=enforce_live_run_contamination_check,
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
    coder_backend: CoderBackend | None,
    openclaw_backend: OpenClawBackend | None,
    allow_modify: bool,
    remediate_install_host_prereqs: bool,
    allow_memory_shortfall: bool,
    prompt_for_memory_shortfall: bool,
    enforce_live_run_contamination_check: bool,
) -> dict[str, Any]:
    loaded_state = load_state_dir(state_dir)
    existing_state = validate_existing_state(loaded_state)
    raw_env = raw_env or parse_env_file(env_file)
    desired_state = resolve_desired_state(raw_env)
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
        raw_env = _rehydrate_guided_retry_keys(
            env_file=env_file,
            state_dir=state_dir,
            loaded_state=loaded_state,
            raw_env=raw_env,
            dry_run=dry_run,
        )
        desired_state = resolve_desired_state(raw_env)
        try:
            lifecycle_plan = classify_install_request(
                existing_raw=loaded_state.raw_input,
                existing_desired=loaded_state.desired_state,
                existing_applied=loaded_state.applied_state,
                requested_raw=raw_env,
                requested_desired=desired_state,
            )
        except StateValidationError:
            if _can_restart_incomplete_install(loaded_state):
                lifecycle_plan = _build_restart_install_plan(desired_state)
            else:
                raise
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

    if allow_modify and existing_state and lifecycle_plan.mode != "noop":
        raw_env = _rehydrate_guided_retry_keys(
            env_file=env_file,
            state_dir=state_dir,
            loaded_state=loaded_state,
            raw_env=raw_env,
            dry_run=dry_run,
        )
        desired_state = resolve_desired_state(raw_env)
        ownership_ledger = loaded_state.ownership_ledger or OwnershipLedger(
            format_version=desired_state.format_version,
            resources=(),
        )

    if enforce_live_run_contamination_check:
        _validate_live_run_env_for_mutation(
            raw_env=raw_env,
            lifecycle_plan=lifecycle_plan,
            dry_run=dry_run,
        )
        _validate_live_drift_for_mutation(
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            lifecycle_plan=lifecycle_plan,
            dry_run=dry_run,
        )
    host_facts = collect_host_facts(raw_env)
    host_prerequisite_summary: dict[str, Any] | None = None
    if remediate_install_host_prereqs and _host_supports_prerequisite_remediation(host_facts):
        host_facts, host_prerequisite_summary = _prepare_install_host_prerequisites(
            raw_env=raw_env,
            host_facts=host_facts,
            dry_run=dry_run,
        )
    preflight_report = _run_preflight_report(
        desired_state=desired_state,
        host_facts=host_facts,
        allow_memory_shortfall=not allow_modify,
        lifecycle_plan=lifecycle_plan,
        loaded_state=loaded_state,
        bootstrap_backend=backend,
        existing_state=existing_state,
    )
    if not allow_modify:
        _require_install_memory_shortfall_override(
            preflight_report=preflight_report,
            allow_memory_shortfall=allow_memory_shortfall,
            prompt_for_memory_shortfall=prompt_for_memory_shortfall,
        )
    if not dry_run and not existing_state:
        persist_install_scaffold(state_dir, raw_env, desired_state)
    require_real_dokploy_auth = _dokploy_api_auth_required(
        desired_state=desired_state,
        shared_core_backend=shared_core_backend,
        headscale_backend=headscale_backend,
        matrix_backend=matrix_backend,
        nextcloud_backend=nextcloud_backend,
        seaweedfs_backend=seaweedfs_backend,
        coder_backend=coder_backend,
        openclaw_backend=openclaw_backend,
    )
    if lifecycle_plan.mode != "noop":
        raw_env = _ensure_dokploy_api_auth(
            env_file=env_file,
            raw_env=raw_env,
            desired_state=desired_state,
            bootstrap_backend=backend,
            dry_run=dry_run,
            require_real_dokploy_auth=require_real_dokploy_auth,
        )
        desired_state = resolve_desired_state(raw_env)
        _qualify_dokploy_mutation_auth(
            raw_env=raw_env,
            desired_state=desired_state,
            dry_run=dry_run,
            require_real_dokploy_auth=require_real_dokploy_auth,
        )
    if not dry_run and lifecycle_plan.mode != "noop":
        write_target_state(state_dir, raw_env, desired_state)
    tailscale_phase_backend = tailscale_backend or ShellTailscaleBackend(raw_env)
    cloudflare_backend = networking_backend or CloudflareApiBackend(raw_env)
    dokploy_session_client = _build_dokploy_session_client(
        raw_env=raw_env,
        api_url=desired_state.dokploy_api_url or LOCAL_HEALTH_URL,
    )
    cloudflared_connector_backend = None
    if networking_backend is None:
        cloudflared_connector_backend = _build_cloudflared_connector_backend(
            raw_env=raw_env,
            desired_state=desired_state,
            session_client=dokploy_session_client,
        )
    shared_core_phase_backend = shared_core_backend or _build_shared_core_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    headscale_phase_backend = headscale_backend or _build_headscale_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    matrix_phase_backend = matrix_backend or _build_matrix_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    nextcloud_phase_backend = nextcloud_backend or _build_nextcloud_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    moodle_phase_backend = _build_moodle_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    docuseal_phase_backend = _build_docuseal_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    seaweedfs_phase_backend = seaweedfs_backend or _build_seaweedfs_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    coder_phase_backend = coder_backend or _build_coder_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    openclaw_phase_backend = openclaw_backend or _build_openclaw_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    lifecycle_backends = LifecycleBackends(
        bootstrap=backend,
        tailscale=tailscale_phase_backend,
        networking=cloudflare_backend,
        cloudflared=cloudflared_connector_backend,
        shared_core=shared_core_phase_backend,
        headscale=headscale_phase_backend,
        matrix=matrix_phase_backend,
        nextcloud=nextcloud_phase_backend,
        moodle=moodle_phase_backend,
        docuseal=docuseal_phase_backend,
        seaweedfs=seaweedfs_phase_backend,
        coder=coder_phase_backend,
        openclaw=openclaw_phase_backend,
    )

    try:
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
            moodle_backend=moodle_phase_backend,
            docuseal_backend=docuseal_phase_backend,
            seaweedfs_backend=seaweedfs_phase_backend,
            coder_backend=coder_phase_backend,
            openclaw_backend=openclaw_phase_backend,
        )
    except LifecycleDriftError as error:
        lifecycle_plan = _resume_plan_from_drift(
            lifecycle_plan=lifecycle_plan,
            drift_error=error,
        )

    if not dry_run:
        if existing_state:
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
                        lifecycle_checkpoint_contract_version=(
                            LIFECYCLE_CHECKPOINT_CONTRACT_VERSION
                        ),
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
    if host_prerequisite_summary is not None:
        summary["host_prerequisites"] = host_prerequisite_summary
    _append_operator_links(summary, desired_state)
    summary["state_dir"] = str(state_dir)
    return summary


def _append_operator_links(summary: dict[str, Any], desired_state: DesiredState) -> None:
    if desired_state.openclaw_gateway_token is None or desired_state.cloudflare_access_otp_emails:
        return
    openclaw_hostname = desired_state.hostnames.get("openclaw")
    openclaw_summary = summary.get("openclaw")
    if not isinstance(openclaw_hostname, str) or openclaw_hostname == "":
        return
    if not isinstance(openclaw_summary, dict):
        return
    openclaw_summary["authorized_dashboard_url"] = (
        f"https://{openclaw_hostname}/#token={desired_state.openclaw_gateway_token}"
    )


def _resume_plan_from_drift(
    *, lifecycle_plan: LifecyclePlan, drift_error: LifecycleDriftError
) -> LifecyclePlan:
    drifted_entry = next(
        (entry for entry in drift_error.report.entries if entry.status == "drift"),
        None,
    )
    if drifted_entry is None:
        raise drift_error
    phase = drifted_entry.phase
    if phase not in lifecycle_plan.applicable_phases:
        raise drift_error
    first_index = lifecycle_plan.applicable_phases.index(phase)
    if first_index == 0:
        raise drift_error
    phases_to_run = lifecycle_plan.applicable_phases[first_index:]
    preserved_phases = lifecycle_plan.applicable_phases[:first_index]
    initial_completed_steps = lifecycle_plan.applicable_phases[:first_index]
    reasons = tuple(lifecycle_plan.reasons) + (
        f"Preserved phase drift detected at '{phase}'; "
        "resuming from the first unhealthy preserved phase.",
    )
    return LifecyclePlan(
        mode="resume",
        reasons=reasons,
        applicable_phases=lifecycle_plan.applicable_phases,
        phases_to_run=phases_to_run,
        preserved_phases=preserved_phases,
        initial_completed_steps=initial_completed_steps,
        start_phase=phase,
        raw_equivalent=lifecycle_plan.raw_equivalent,
        desired_equivalent=lifecycle_plan.desired_equivalent,
    )


def _prepare_install_host_prerequisites(
    *,
    raw_env: RawEnvInput,
    host_facts: Any,
    dry_run: bool,
) -> tuple[Any, dict[str, Any]]:
    backend = UbuntuAptHostPrerequisiteBackend(raw_env)
    assessment = assess_host_prerequisites(host_facts=host_facts, backend=backend)
    summary: dict[str, Any] = {
        "assessment": assessment.to_dict(),
        "remediation_actions": [],
        "remediation_attempted": False,
    }
    if dry_run:
        return host_facts, summary
    if assessment.outcome != "missing_prerequisites" or not assessment.remediation_eligible:
        return host_facts, summary

    remediation_actions: list[dict[str, Any]] = []
    if assessment.missing_packages:
        remediation_actions.append(
            {
                "action": "apt_install",
                "packages": list(assessment.missing_packages),
            }
        )
    if getattr(assessment, "docker_bootstrap_required", False):
        remediation_actions.append(
            {
                "action": "bootstrap_docker_engine",
                "packages": list(DOCKER_APT_PACKAGES),
                "repository": "official_docker_apt_repository",
            }
        )
    if any(check.name == "docker_daemon" and check.status == "fail" for check in assessment.checks):
        remediation_actions.append({"action": "ensure_docker_daemon"})

    remediate_host_prerequisites(assessment=assessment, backend=backend)
    updated_host_facts = _collect_post_remediation_host_facts(
        raw_env=raw_env,
        assessment=assessment,
    )
    summary["post_remediation_host_facts"] = updated_host_facts.to_dict()
    summary["remediation_actions"] = remediation_actions
    summary["remediation_attempted"] = True
    return updated_host_facts, summary


def _can_treat_required_ports_as_expected(
    loaded_state: Any,
    lifecycle_plan: LifecyclePlan,
    *,
    required_ports: tuple[int, ...],
) -> bool:
    if lifecycle_plan.mode not in {"resume", "noop", "modify"}:
        return False
    applied_state = getattr(loaded_state, "applied_state", None)
    if applied_state is None:
        return False
    if (
        "dokploy_bootstrap" not in applied_state.completed_steps
        and lifecycle_plan.start_phase != "dokploy_bootstrap"
    ):
        return False
    return True


def _rehydrate_guided_retry_keys(
    *,
    env_file: Path,
    state_dir: Path,
    loaded_state: Any,
    raw_env: RawEnvInput,
    dry_run: bool,
) -> RawEnvInput:
    if env_file != _guided_install_env_file(state_dir):
        return raw_env
    persisted_raw = getattr(loaded_state, "raw_input", None)
    if persisted_raw is None:
        return raw_env

    values = dict(raw_env.values)
    changed = False
    for key, persisted_value in persisted_raw.values.items():
        if key not in values:
            values[key] = persisted_value
            changed = True
            continue
        if key not in _PERSISTED_RETRY_KEYS:
            continue
        if values.get(key) == persisted_value:
            continue
        values[key] = persisted_value
        changed = True
    if not changed:
        return raw_env

    updated = RawEnvInput(format_version=raw_env.format_version, values=values)
    if not dry_run:
        _write_reusable_env_file(env_file, updated)
    return updated


def _can_restart_incomplete_install(loaded_state: Any) -> bool:
    applied_state = getattr(loaded_state, "applied_state", None)
    ownership_ledger = getattr(loaded_state, "ownership_ledger", None)
    if applied_state is None or ownership_ledger is None:
        return False
    return bool(applied_state.completed_steps == () and ownership_ledger.resources == ())


def _build_restart_install_plan(desired_state: DesiredState) -> LifecyclePlan:
    applicable_phases = applicable_phases_for(desired_state)
    return LifecyclePlan(
        mode="install",
        reasons=(
            "Existing state dir only contains an incomplete scaffold with no owned resources; "
            "restarting install from the requested env file.",
        ),
        applicable_phases=applicable_phases,
        phases_to_run=applicable_phases[1:],
        preserved_phases=(),
        initial_completed_steps=(),
        start_phase="dokploy_bootstrap",
        raw_equivalent=False,
        desired_equivalent=False,
    )


def _collect_post_remediation_host_facts(*, raw_env: RawEnvInput, assessment: Any) -> Any:
    updated_host_facts = collect_host_facts(raw_env)
    if not _requires_post_remediation_docker_wait(assessment):
        return updated_host_facts

    attempts_remaining = _HOST_PREREQ_RECHECK_ATTEMPTS - 1
    while attempts_remaining > 0 and (
        not getattr(updated_host_facts, "docker_installed", False)
        or not getattr(updated_host_facts, "docker_daemon_reachable", False)
    ):
        time.sleep(_HOST_PREREQ_RECHECK_DELAY_SECONDS)
        updated_host_facts = collect_host_facts(raw_env)
        attempts_remaining -= 1
    return updated_host_facts


def _requires_post_remediation_docker_wait(assessment: Any) -> bool:
    if getattr(assessment, "docker_bootstrap_required", False):
        return True
    return any(
        getattr(check, "name", None) == "docker_daemon" and getattr(check, "status", None) == "fail"
        for check in getattr(assessment, "checks", ())
    )


def _host_supports_prerequisite_remediation(host_facts: Any) -> bool:
    distribution_id = getattr(host_facts, "distribution_id", None)
    version_id = getattr(host_facts, "version_id", None)
    return bool(
        distribution_id == SUPPORTED_OS_ID
        and isinstance(version_id, str)
        and _is_supported_ubuntu_version(version_id)
    )


def _run_preflight_report(
    *,
    desired_state: DesiredState,
    host_facts: Any,
    allow_memory_shortfall: bool,
    lifecycle_plan: LifecyclePlan,
    loaded_state: Any,
    bootstrap_backend: Any,
    existing_state: bool,
) -> Any:
    allowed_ports_in_use = _expected_ports_in_use_for_retry(loaded_state, lifecycle_plan)
    if (
        not allowed_ports_in_use
        and not existing_state
        and lifecycle_plan.mode == "install"
        and getattr(bootstrap_backend, "is_healthy", lambda: False)()
    ):
        allowed_ports_in_use = REQUIRED_PORTS
    parameters = inspect.signature(run_preflight).parameters
    kwargs: dict[str, Any] = {}
    if "allow_memory_shortfall" in parameters:
        kwargs["allow_memory_shortfall"] = allow_memory_shortfall
    if "allowed_ports_in_use" in parameters:
        kwargs["allowed_ports_in_use"] = allowed_ports_in_use
    if kwargs:
        return run_preflight(desired_state, host_facts, **kwargs)
    return run_preflight(desired_state, host_facts)


def _expected_ports_in_use_for_retry(
    loaded_state: Any, lifecycle_plan: LifecyclePlan
) -> tuple[int, ...]:
    if not _can_treat_required_ports_as_expected(
        loaded_state,
        lifecycle_plan,
        required_ports=REQUIRED_PORTS,
    ):
        return ()
    applied_state = getattr(loaded_state, "applied_state", None)
    if applied_state is None:
        return ()
    expected: list[int] = []
    if (
        "dokploy_bootstrap" in applied_state.completed_steps
        or lifecycle_plan.start_phase == "dokploy_bootstrap"
    ):
        expected.extend([80, 443, 3000])
    return tuple(sorted(set(expected)))


def _require_install_memory_shortfall_override(
    *,
    preflight_report: Any,
    allow_memory_shortfall: bool,
    prompt_for_memory_shortfall: bool,
) -> None:
    if not hasattr(preflight_report, "has_only_memory_shortfall_warning"):
        return
    if not preflight_report.has_only_memory_shortfall_warning():
        return
    if allow_memory_shortfall:
        return

    warning_detail = "; ".join(
        check.detail for check in preflight_report.warning_checks() if check.name == "memory"
    )
    if prompt_for_memory_shortfall:
        prompt_message = (
            "Memory shortfall warning: "
            + warning_detail
            + ". This host is below the recommended memory target for the selected scope "
            + "and may be unstable or underprovisioned. Proceed anyway? [y/N] "
        )
        response = sanitize_prompt_response(input(prompt_message)).strip().lower()
        if response in {"y", "yes"}:
            return
        raise PreflightError("Preflight failed: " + warning_detail)

    raise PreflightError(
        "Preflight failed: "
        + warning_detail
        + ". Rerun install with --allow-memory-shortfall to continue non-interactively."
    )


def _validate_live_run_env_for_mutation(
    *, raw_env: RawEnvInput, lifecycle_plan: LifecyclePlan, dry_run: bool
) -> None:
    if dry_run or not lifecycle_plan.phases_to_run:
        return
    offending_keys = sorted(
        key for key in raw_env.values if key.startswith(_LIVE_RUN_MOCK_CONTAMINATION_PREFIXES)
    )
    if not offending_keys:
        return
    raise StateValidationError(
        "Mock/test env contamination is not allowed for mutating live/pre-live runs; "
        "live/pre-live runs require real integrations. "
        f"Offending keys: {offending_keys}."
    )


def _validate_live_drift_for_mutation(
    *,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    lifecycle_plan: LifecyclePlan,
    dry_run: bool,
) -> None:
    if dry_run or not lifecycle_plan.phases_to_run:
        return
    live_drift = build_live_drift_report(
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
    )
    blocking_entries = [
        entry for entry in live_drift["entries"] if _live_drift_entry_blocks_mutation(entry)
    ]
    if not blocking_entries:
        return
    details = " ".join(
        f"[{index}] {_live_drift_entry_message(entry)}"
        for index, entry in enumerate(blocking_entries, start=1)
    )
    raise StateValidationError(
        "Live drift is not allowed for mutating live/pre-live runs; refusing to continue "
        "while live artifacts diverge from wizard-managed ownership. "
        f"Blocking drift: {details}"
    )


def _live_drift_entry_blocks_mutation(entry: dict[str, Any]) -> bool:
    classification = entry.get("classification")
    if classification in {"manual_collision", "host_local_route"}:
        return True
    if classification != "wizard_managed":
        return False
    return entry.get("health") in {"unhealthy", "unknown"}


def _live_drift_entry_message(entry: dict[str, Any]) -> str:
    classification = entry["classification"]
    if classification == "manual_collision":
        pack = entry.get("pack") or "runtime"
        live_kind = entry.get("live_kind") or "resource"
        live_name = entry.get("live_name") or "unknown"
        return (
            f"manual {pack} {live_kind} '{live_name}' collides with wizard-managed state. "
            "Migrate or remove the unowned runtime before install, rerun, or modify can continue."
        )
    if classification == "host_local_route":
        pack = entry.get("pack") or "service"
        path = entry.get("path") or "unknown path"
        return (
            f"host-local {pack} route file '{path}' shadows Dokploy-managed ingress. "
            "Remove the host-local route file so the wizard is the single routing "
            "owner, then rerun."
        )
    pack = entry.get("pack") or "service"
    live_name = entry.get("live_name") or entry.get("expected_service_name") or "unknown"
    health = entry.get("health") or "unhealthy"
    return (
        f"wizard-managed {pack} service '{live_name}' is {health}. "
        "Repair or remove the unhealthy managed runtime until inspect-state "
        "reports clean, then rerun."
    )


def _build_shared_core_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
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
            mail_relay_config={
                key: value
                for key, value in raw_env.values.items()
                if key.startswith("OUTBOUND_SMTP_") and value.strip() != ""
            },
            client=_build_dokploy_api_client(
                raw_env=raw_env,
                api_url=api_url,
                api_key=api_key,
                session_client=session_client,
            ),
        )
    return ShellSharedCoreBackend()


def _build_cloudflared_connector_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
) -> Any | None:
    if raw_env.values.get("DOKPLOY_MOCK_API_MODE") == "true":
        return None
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if not api_url or not api_key:
        return None
    return DokployCloudflaredBackend(
        api_url=api_url,
        api_key=api_key,
        stack_name=desired_state.stack_name,
        public_url=desired_state.dokploy_url,
        client=_build_dokploy_api_client(
            raw_env=raw_env,
            api_url=api_url,
            api_key=api_key,
            session_client=session_client,
        ),
    )


def _build_headscale_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
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
            client=_build_dokploy_api_client(
                raw_env=raw_env,
                api_url=api_url,
                api_key=api_key,
                session_client=session_client,
            ),
        )
    return ShellHeadscaleBackend(raw_env)


def _build_nextcloud_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
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
    admin_user = raw_env.values.get("DOKPLOY_ADMIN_EMAIL", "admin")
    admin_password = raw_env.values.get("DOKPLOY_ADMIN_PASSWORD", "ChangeMeSoon")
    nexa_enabled = _has_openclaw_nexa_env(raw_env)
    nexa_agent_password = None
    if nexa_enabled:
        nexa_agent_password = raw_env.values.get("OPENCLAW_NEXA_AGENT_PASSWORD") or raw_env.values.get(
            "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD"
        )
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
        admin_user=admin_user,
        admin_password=admin_password,
        openclaw_volume_name=(
            f"{desired_state.stack_name}-openclaw-data"
            if "openclaw" in desired_state.enabled_packs
            else None
        ),
        openclaw_workspace_contract=(
            _build_nextcloud_openclaw_workspace_contract(desired_state)
            if nexa_enabled and "openclaw" in desired_state.enabled_packs
            else None
        ),
        nexa_agent_user_id=(raw_env.values.get("OPENCLAW_NEXA_AGENT_USER_ID") if nexa_enabled else None),
        nexa_agent_display_name=(
            raw_env.values.get("OPENCLAW_NEXA_AGENT_DISPLAY_NAME") if nexa_enabled else None
        ),
        nexa_agent_password=nexa_agent_password,
        nexa_agent_email=(raw_env.values.get("OPENCLAW_NEXA_AGENT_EMAIL") if nexa_enabled else None),
        openclaw_rescan_cron=raw_env.values.get("NEXTCLOUD_OPENCLAW_RESCAN_CRON", "*/15 * * * *"),
        openclaw_rescan_timezone=raw_env.values.get("NEXTCLOUD_OPENCLAW_RESCAN_TIMEZONE", "UTC"),
        client=_build_dokploy_api_client(
            raw_env=raw_env,
            api_url=api_url,
            api_key=api_key,
            session_client=session_client,
        ),
    )


def _build_matrix_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
) -> MatrixBackend:
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
        client=_build_dokploy_api_client(
            raw_env=raw_env,
            api_url=api_url,
            api_key=api_key,
            session_client=session_client,
        ),
    )


def _build_moodle_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
) -> MoodleBackend:
    if raw_env.values.get("DOKPLOY_MOCK_API_MODE") == "true":
        return ShellMoodleBackend()
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if not api_url or not api_key or "moodle" not in desired_state.enabled_packs:
        return ShellMoodleBackend()
    hostname = desired_state.hostnames.get("moodle")
    allocation = next(
        (item for item in desired_state.shared_core.allocations if item.pack_name == "moodle"),
        None,
    )
    if hostname is None or allocation is None or desired_state.shared_core.postgres is None:
        return ShellMoodleBackend()
    if allocation.postgres is None:
        return ShellMoodleBackend()
    mail_relay = desired_state.shared_core.mail_relay
    return DokployMoodleBackend(
        api_url=api_url,
        api_key=api_key,
        stack_name=desired_state.stack_name,
        hostname=hostname,
        admin_email=raw_env.values.get("DOKPLOY_ADMIN_EMAIL", "admin@example.com"),
        admin_password=raw_env.values.get("DOKPLOY_ADMIN_PASSWORD", "ChangeMeSoon"),
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        postgres=allocation.postgres,
        smtp_host=None if mail_relay is None else mail_relay.service_name,
        smtp_port=None if mail_relay is None else mail_relay.smtp_port,
        smtp_from_address=None if mail_relay is None else mail_relay.from_address,
        client=_build_dokploy_api_client(
            raw_env=raw_env,
            api_url=api_url,
            api_key=api_key,
            session_client=session_client,
        ),
    )


def _build_docuseal_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
) -> DocuSealBackend:
    if raw_env.values.get("DOKPLOY_MOCK_API_MODE") == "true":
        return ShellDocuSealBackend()
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if not api_url or not api_key or "docuseal" not in desired_state.enabled_packs:
        return ShellDocuSealBackend()
    hostname = desired_state.hostnames.get("docuseal")
    allocation = next(
        (item for item in desired_state.shared_core.allocations if item.pack_name == "docuseal"),
        None,
    )
    if hostname is None or allocation is None or desired_state.shared_core.postgres is None:
        return ShellDocuSealBackend()
    if allocation.postgres is None:
        return ShellDocuSealBackend()
    mail_relay = desired_state.shared_core.mail_relay
    return DokployDocuSealBackend(
        api_url=api_url,
        api_key=api_key,
        stack_name=desired_state.stack_name,
        hostname=hostname,
        admin_email=raw_env.values.get("DOKPLOY_ADMIN_EMAIL", "admin@example.com"),
        admin_password=raw_env.values.get("DOKPLOY_ADMIN_PASSWORD", "ChangeMeSoon"),
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        postgres=allocation.postgres,
        smtp_host=None if mail_relay is None else mail_relay.service_name,
        smtp_port=None if mail_relay is None else mail_relay.smtp_port,
        smtp_domain=desired_state.root_domain,
        smtp_from_address=None if mail_relay is None else mail_relay.from_address,
        client=_build_dokploy_api_client(
            raw_env=raw_env,
            api_url=api_url,
            api_key=api_key,
            session_client=session_client,
        ),
    )


def _build_seaweedfs_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
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
        client=_build_dokploy_api_client(
            raw_env=raw_env,
            api_url=api_url,
            api_key=api_key,
            session_client=session_client,
        ),
    )


def _build_coder_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
) -> CoderBackend:
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if not api_url or not api_key or "coder" not in desired_state.enabled_packs:
        return ShellCoderBackend()
    hostname = desired_state.hostnames.get("coder")
    wildcard_hostname = desired_state.hostnames.get("coder-wildcard")
    allocation = next(
        (item for item in desired_state.shared_core.allocations if item.pack_name == "coder"),
        None,
    )
    if hostname is None or wildcard_hostname is None or allocation is None:
        return ShellCoderBackend()
    if allocation.postgres is None or desired_state.shared_core.postgres is None:
        return ShellCoderBackend()
    return DokployCoderBackend(
        api_url=api_url,
        api_key=api_key,
        stack_name=desired_state.stack_name,
        hostname=hostname,
        wildcard_hostname=wildcard_hostname,
        admin_email=raw_env.values.get("DOKPLOY_ADMIN_EMAIL", "admin@example.com"),
        admin_password=raw_env.values.get("DOKPLOY_ADMIN_PASSWORD", "ChangeMeSoon"),
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        postgres=allocation.postgres,
        hermes_inference_provider=raw_env.values.get(
            "HERMES_INFERENCE_PROVIDER", "opencode-go"
        ),
        hermes_model=raw_env.values.get("HERMES_MODEL", "deepseek-v4-flash"),
        opencode_go_base_url=raw_env.values.get(
            "OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1"
        ),
        opencode_go_api_key=raw_env.values.get("OPENCODE_GO_API_KEY"),
        client=_build_dokploy_api_client(
            raw_env=raw_env,
            api_url=api_url,
            api_key=api_key,
            session_client=session_client,
        ),
    )


def _build_openclaw_backend(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    session_client: DokployBootstrapAuthClient | None = None,
) -> OpenClawBackend:
    if raw_env.values.get("DOKPLOY_MOCK_API_MODE") == "true":
        return ShellOpenClawBackend(raw_env)
    api_url = desired_state.dokploy_api_url
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if not api_url or not api_key:
        return ShellOpenClawBackend(raw_env)
    if not ({"openclaw", "my-farm-advisor"} & set(desired_state.enabled_packs)):
        return ShellOpenClawBackend(raw_env)
    openclaw_primary_model, openclaw_fallback_models = _advisor_model_selection(
        raw_env, env_prefix="OPENCLAW"
    )
    my_farm_primary_model, my_farm_fallback_models = _advisor_model_selection(
        raw_env, env_prefix="MY_FARM_ADVISOR"
    )
    return DokployOpenClawBackend(
        api_url=api_url,
        api_key=api_key,
        stack_name=desired_state.stack_name,
        gateway_token=desired_state.openclaw_gateway_token,
        openclaw_internal_hostname=_openclaw_internal_hostname(raw_env, desired_state),
        openclaw_gateway_password=_advisor_env_optional(raw_env, "OPENCLAW_GATEWAY_PASSWORD")
        or _advisor_env_optional(raw_env, "ADVISOR_GATEWAY_PASSWORD")
        or raw_env.values.get("DOKPLOY_ADMIN_PASSWORD", "ChangeMeSoon"),
        my_farm_gateway_password=_advisor_env_optional(raw_env, "MY_FARM_ADVISOR_GATEWAY_PASSWORD")
        or _advisor_env_optional(raw_env, "ADVISOR_GATEWAY_PASSWORD")
        or raw_env.values.get("DOKPLOY_ADMIN_PASSWORD", "ChangeMeSoon"),
        trusted_proxy_emails=desired_state.cloudflare_access_otp_emails,
        openclaw_primary_model=openclaw_primary_model,
        openclaw_fallback_models=openclaw_fallback_models,
        openclaw_openrouter_api_key=_advisor_env_optional(raw_env, "OPENCLAW_OPENROUTER_API_KEY"),
        openclaw_nvidia_api_key=_advisor_env_optional(raw_env, "OPENCLAW_NVIDIA_API_KEY"),
        openclaw_telegram_bot_token=_advisor_env_optional(raw_env, "OPENCLAW_TELEGRAM_BOT_TOKEN"),
        openclaw_telegram_owner_user_id=_advisor_env_optional(
            raw_env, "OPENCLAW_TELEGRAM_OWNER_USER_ID"
        ),
        openclaw_nexa_env=_openclaw_nexa_env(raw_env),
        my_farm_primary_model=my_farm_primary_model,
        my_farm_fallback_models=my_farm_fallback_models,
        my_farm_openrouter_api_key=_advisor_env_optional(
            raw_env, "MY_FARM_ADVISOR_OPENROUTER_API_KEY"
        ),
        my_farm_nvidia_api_key=_advisor_env_optional(raw_env, "MY_FARM_ADVISOR_NVIDIA_API_KEY"),
        my_farm_telegram_bot_token=_advisor_env_optional(
            raw_env, "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN"
        ),
        my_farm_telegram_owner_user_id=_advisor_env_optional(
            raw_env, "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID"
        ),
        model_provider=_advisor_env_value(
            raw_env,
            "ADVISOR_MODEL_PROVIDER",
            default="openai",
        ),
        model_name=_advisor_env_value(
            raw_env,
            "ADVISOR_MODEL_NAME",
            default="gpt-4o-mini",
        ),
        trusted_proxies=_advisor_env_value(
            raw_env,
            "ADVISOR_TRUSTED_PROXIES",
            default="127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
        ),
        nvidia_visible_devices=_advisor_env_value(
            raw_env,
            "ADVISOR_NVIDIA_VISIBLE_DEVICES",
            default="all",
        ),
        client=_build_dokploy_api_client(
            raw_env=raw_env,
            api_url=api_url,
            api_key=api_key,
            session_client=session_client,
        ),
    )


def _openclaw_nexa_env(raw_env: RawEnvInput) -> dict[str, str]:
    return {
        key: value
        for key, value in sorted(raw_env.values.items())
        if key.startswith("OPENCLAW_NEXA_") and value.strip() != ""
    }


def _openclaw_internal_hostname(raw_env: RawEnvInput, desired_state: DesiredState) -> str | None:
    raw_value = raw_env.values.get("OPENCLAW_INTERNAL_SUBDOMAIN")
    if raw_value is None:
        return None
    normalized = raw_value.strip().lower()
    if normalized in {"", "disabled", "off", "false", "none"}:
        return None
    return desired_state.hostnames.get("openclaw-internal")


def _has_openclaw_nexa_env(raw_env: RawEnvInput) -> bool:
    return bool(_openclaw_nexa_env(raw_env))


def _build_nextcloud_openclaw_workspace_contract(
    desired_state: DesiredState,
) -> NextcloudOpenClawWorkspaceContract:
    stack_name = desired_state.stack_name
    return NextcloudOpenClawWorkspaceContract(
        enabled=True,
        external_mount_name="/OpenClaw",
        external_mount_path="/mnt/openclaw",
        visible_root="/mnt/openclaw/workspace/nexa",
        contract_path="/mnt/openclaw/workspace/nexa/contract.json",
        runtime_state_source="server-owned env + durable state JSON",
        notes=(
            "Nextcloud exposes the Nexa workspace as an operator/user surface only.",
            "Credential values and identity-bearing fields remain server-owned and must not be overridden from workspace files.",
        ),
    )


def _dokploy_api_auth_required(
    *,
    desired_state: DesiredState,
    shared_core_backend: SharedCoreBackend | None,
    headscale_backend: HeadscaleBackend | None,
    matrix_backend: MatrixBackend | None,
    nextcloud_backend: NextcloudBackend | None,
    seaweedfs_backend: SeaweedFsBackend | None,
    coder_backend: CoderBackend | None,
    openclaw_backend: OpenClawBackend | None,
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
    if coder_backend is None and "coder" in desired_state.enabled_packs:
        return True
    if openclaw_backend is None and (
        {"openclaw", "my-farm-advisor"} & set(desired_state.enabled_packs)
    ):
        return True
    return False


def _advisor_env_value(raw_env: RawEnvInput, key: str, *, default: str) -> str:
    value = raw_env.values.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _advisor_env_optional(raw_env: RawEnvInput, key: str) -> str | None:
    value = raw_env.values.get(key)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _advisor_model_selection(
    raw_env: RawEnvInput, *, env_prefix: str
) -> tuple[str | None, tuple[str, ...]]:
    """Return repo-facing model envs mapped onto current OpenClaw config semantics.

    `<PREFIX>_PRIMARY_MODEL` and `<PREFIX>_FALLBACK_MODELS` are dokploy-wizard
    conventions, not OpenClaw-native env names. We normalize them here and hand the
    results to the Dokploy OpenClaw backend, which seeds the current OpenClaw config
    shape at `agents.defaults.model.primary` / `fallbacks`.

    Service env remains the source of truth for deployment because Dokploy/container
    env wins over seeded config fallback values at runtime.
    """

    return (
        _advisor_primary_model(raw_env, env_prefix=env_prefix),
        _advisor_model_list(raw_env, env_prefix=env_prefix),
    )


def _advisor_primary_model(raw_env: RawEnvInput, *, env_prefix: str) -> str | None:
    explicit = _advisor_env_optional(raw_env, f"{env_prefix}_PRIMARY_MODEL")
    if explicit is not None:
        return _normalize_advisor_model_ref(explicit)
    provider = _advisor_env_optional(raw_env, "ADVISOR_MODEL_PROVIDER")
    model_name = _advisor_env_optional(raw_env, "ADVISOR_MODEL_NAME")
    if provider is None or model_name is None:
        return None
    return _normalize_advisor_model_ref(
        model_name if "/" in model_name else f"{provider}/{model_name}"
    )


def _advisor_model_list(raw_env: RawEnvInput, *, env_prefix: str) -> tuple[str, ...]:
    raw_value = _advisor_env_optional(raw_env, f"{env_prefix}_FALLBACK_MODELS")
    if raw_value is None:
        return ()
    return tuple(
        _normalize_advisor_model_ref(item.strip()) for item in raw_value.split(",") if item.strip()
    )


def _normalize_advisor_model_ref(model_ref: str) -> str:
    normalized = model_ref.strip()
    legacy_aliases = {
        "nvidia/moonshot/kimi-k2.5": "nvidia/moonshotai/kimi-k2.5",
    }
    return legacy_aliases.get(normalized, normalized)


_DOKPLOY_AUTH_PROBE_DESCRIPTION = "Wizard-owned Dokploy auth qualifier"
_DOKPLOY_AUTH_PROBE_COMPOSE = """services:
  auth-probe:
    image: alpine:3.20
    command: [\"sh\", \"-c\", \"sleep 3600\"]
    restart: unless-stopped
"""


def _qualify_dokploy_mutation_auth(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    dry_run: bool,
    require_real_dokploy_auth: bool,
) -> None:
    if (
        dry_run
        or not require_real_dokploy_auth
        or raw_env.values.get("DOKPLOY_MOCK_API_MODE") == "true"
        or raw_env.values.get("DOKPLOY_BOOTSTRAP_MOCK_API_KEY") is not None
    ):
        return
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    api_url = raw_env.values.get("DOKPLOY_API_URL") or LOCAL_HEALTH_URL
    if api_key is None:
        raise StateValidationError(
            "Dokploy mutation auth qualification requires a usable DOKPLOY_API_KEY."
        )

    session_client = _build_dokploy_session_client(raw_env=raw_env, api_url=api_url)
    client = _build_dokploy_api_client(
        raw_env=raw_env,
        api_url=api_url,
        api_key=api_key,
        session_client=session_client,
    )
    probe_project_name = f"{desired_state.stack_name}-dokploy-wizard-auth-probe"
    probe_compose_name = probe_project_name

    projects = client.list_projects()
    for stale_project in tuple(
        project for project in projects if project.name == probe_project_name
    ):
        delete_project = getattr(client, "delete_project", None)
        if callable(delete_project):
            try:
                delete_project(project_id=stale_project.project_id)
            except DokployApiError:
                pass
    projects = client.list_projects()
    project = _find_dokploy_project(projects, probe_project_name)
    probe_project_id = project.project_id if project is not None else None
    environment_id = _default_dokploy_environment_id(project) if project is not None else None

    if project is None:
        try:
            created = client.create_project(
                name=probe_project_name,
                description=_DOKPLOY_AUTH_PROBE_DESCRIPTION,
                env="",
            )
        except DokployApiError as error:
            raise StateValidationError(
                f"Dokploy mutation auth qualification failed during project.create: {error}"
            ) from error
        environment_id = created.environment_id
        probe_project_id = created.project_id

    assert environment_id is not None

    compose_id = _find_dokploy_compose_id(project, probe_compose_name)
    if compose_id is None:
        try:
            compose = client.create_compose(
                name=probe_compose_name,
                environment_id=environment_id,
                compose_file=_DOKPLOY_AUTH_PROBE_COMPOSE,
                app_name=probe_compose_name,
            )
        except DokployApiError as error:
            projects = client.list_projects()
            project = _find_dokploy_project(projects, probe_project_name)
            compose_id = _find_dokploy_compose_id(project, probe_compose_name)
            if compose_id is None or not _looks_like_duplicate_dokploy_error(error):
                raise StateValidationError(
                    f"Dokploy mutation auth qualification failed during compose.create: {error}"
                ) from error
        else:
            compose_id = compose.compose_id

    assert compose_id is not None

    try:
        client.update_compose(
            compose_id=compose_id,
            compose_file=_DOKPLOY_AUTH_PROBE_COMPOSE,
        )
    except DokployApiError as error:
        raise StateValidationError(
            f"Dokploy mutation auth qualification failed during compose.update: {error}"
        ) from error

    try:
        try:
            deploy = client.deploy_compose(
                compose_id=compose_id,
                title="dokploy-wizard auth qualifier",
                description="Verifies compose mutation auth before lifecycle execution.",
            )
        except DokployApiError as error:
            raise StateValidationError(
                f"Dokploy mutation auth qualification failed during compose.deploy: {error}"
            ) from error
        if not deploy.success:
            raise StateValidationError(
                "Dokploy mutation auth qualification failed during compose.deploy: "
                f"{deploy.message or 'deploy returned unsuccessful result.'}"
            )
    finally:
        if probe_project_id is not None:
            try:
                delete_project = getattr(client, "delete_project", None)
                if callable(delete_project):
                    delete_project(project_id=probe_project_id)
            except DokployApiError:
                pass


def _optional_stripped_env_value(values: dict[str, str], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_dokploy_admin_auth(values: dict[str, str]) -> tuple[str | None, str | None]:
    admin_email = _optional_stripped_env_value(values, "DOKPLOY_ADMIN_EMAIL")
    admin_password = _optional_stripped_env_value(values, "DOKPLOY_ADMIN_PASSWORD")
    if admin_email is not None and admin_password is None:
        admin_password = _DEFAULT_DOKPLOY_ADMIN_PASSWORD
    return admin_email, admin_password


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
    admin_email, admin_password = _resolve_dokploy_admin_auth(values)
    if admin_email is not None and admin_password is not None:
        values["DOKPLOY_ADMIN_PASSWORD"] = admin_password
    if values.get("DOKPLOY_BOOTSTRAP_MOCK_API_KEY") and not dry_run:
        values["DOKPLOY_API_URL"] = LOCAL_HEALTH_URL
        values["DOKPLOY_API_KEY"] = values["DOKPLOY_BOOTSTRAP_MOCK_API_KEY"]
        values["DOKPLOY_MOCK_API_MODE"] = "true"
        updated = RawEnvInput(format_version=raw_env.format_version, values=values)
        _write_reusable_env_file(env_file, updated)
        return updated
    if values.get("DOKPLOY_API_KEY"):
        values["DOKPLOY_API_URL"] = LOCAL_HEALTH_URL
        updated = RawEnvInput(format_version=raw_env.format_version, values=values)
        if _can_reuse_existing_dokploy_api_key(
            raw_env=updated,
            dry_run=dry_run,
            require_real_dokploy_auth=require_real_dokploy_auth,
        ):
            if not dry_run:
                _write_reusable_env_file(env_file, updated)
            return updated
    if dry_run or not require_real_dokploy_auth:
        return raw_env
    if admin_email is None or admin_password is None:
        raise StateValidationError(
            "Dokploy admin email/password are required to bootstrap local "
            "Dokploy API auth for real installs."
        )

    reconcile_dokploy(dry_run=False, backend=bootstrap_backend)
    result = _refresh_local_dokploy_api_key(
        admin_email=admin_email,
        admin_password=admin_password,
    )
    values["DOKPLOY_API_URL"] = LOCAL_HEALTH_URL
    values["DOKPLOY_API_KEY"] = result.api_key
    updated = RawEnvInput(format_version=raw_env.format_version, values=values)
    _write_reusable_env_file(env_file, updated)
    return updated


def _can_reuse_existing_dokploy_api_key(
    *,
    raw_env: RawEnvInput,
    dry_run: bool,
    require_real_dokploy_auth: bool,
) -> bool:
    if dry_run or not require_real_dokploy_auth:
        return True
    api_key = raw_env.values.get("DOKPLOY_API_KEY")
    if api_key is None:
        return False
    try:
        DokployApiClient(api_url=LOCAL_HEALTH_URL, api_key=api_key).list_projects()
    except DokployApiError:
        return False
    return True


def _build_dokploy_api_client(
    *,
    raw_env: RawEnvInput,
    api_url: str,
    api_key: str,
    session_client: DokployBootstrapAuthClient | None = None,
) -> DokployApiClient:
    session_client = session_client or _build_dokploy_session_client(
        raw_env=raw_env,
        api_url=api_url,
    )
    if session_client is None:
        return DokployApiClient(api_url=api_url, api_key=api_key)
    return DokployApiClient(
        api_url=api_url,
        api_key=api_key,
        list_projects_session_fallback=_build_dokploy_session_list_projects_fallback(
            raw_env=raw_env,
            session_client=session_client,
        ),
        project_create_session_fallback=_build_dokploy_session_project_create_fallback(
            raw_env=raw_env,
            session_client=session_client,
        ),
        compose_create_session_fallback=_build_dokploy_session_compose_create_fallback(
            raw_env=raw_env,
            session_client=session_client,
        ),
        compose_update_session_fallback=_build_dokploy_session_compose_update_fallback(
            raw_env=raw_env,
            session_client=session_client,
        ),
        deploy_session_fallback=_build_dokploy_session_deploy_fallback(
            raw_env=raw_env,
            session_client=session_client,
        ),
        list_compose_schedules_session_fallback=_build_dokploy_session_schedule_list_fallback(
            raw_env=raw_env,
            session_client=session_client,
        ),
        create_schedule_session_fallback=cast(
            Any,
            _build_dokploy_session_schedule_create_fallback(
                raw_env=raw_env,
                session_client=session_client,
            ),
        ),
        update_schedule_session_fallback=cast(
            Any,
            _build_dokploy_session_schedule_update_fallback(
                raw_env=raw_env,
                session_client=session_client,
            ),
        ),
        delete_schedule_session_fallback=_build_dokploy_session_schedule_delete_fallback(
            raw_env=raw_env,
            session_client=session_client,
        ),
    )


def _build_dokploy_session_client(
    *, raw_env: RawEnvInput, api_url: str
) -> DokployBootstrapAuthClient | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password:
        return None
    return DokployBootstrapAuthClient(base_url=api_url)


def _build_dokploy_session_deploy_fallback(
    *, raw_env: RawEnvInput, session_client: DokployBootstrapAuthClient | None
) -> Callable[[str, str | None, str | None], Any] | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password or session_client is None:
        return None

    def _fallback(compose_id: str, title: str | None, description: str | None) -> Any:
        return session_client.deploy_compose(
            admin_email=admin_email,
            admin_password=admin_password,
            compose_id=compose_id,
            title=title,
            description=description,
        )

    return _fallback


def _build_dokploy_session_list_projects_fallback(
    *, raw_env: RawEnvInput, session_client: DokployBootstrapAuthClient | None
) -> Callable[[], Any] | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password or session_client is None:
        return None

    def _fallback() -> Any:
        return session_client.list_projects(
            admin_email=admin_email,
            admin_password=admin_password,
        )

    return _fallback


def _build_dokploy_session_project_create_fallback(
    *, raw_env: RawEnvInput, session_client: DokployBootstrapAuthClient | None
) -> Callable[[str, str | None, str | None], Any] | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password or session_client is None:
        return None

    def _fallback(name: str, description: str | None, env: str | None) -> Any:
        return session_client.create_project(
            admin_email=admin_email,
            admin_password=admin_password,
            name=name,
            description=description,
            env=env,
        )

    return _fallback


def _build_dokploy_session_compose_create_fallback(
    *, raw_env: RawEnvInput, session_client: DokployBootstrapAuthClient | None
) -> Callable[[str, str, str, str], Any] | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password or session_client is None:
        return None

    def _fallback(name: str, environment_id: str, compose_file: str, app_name: str) -> Any:
        return session_client.create_compose(
            admin_email=admin_email,
            admin_password=admin_password,
            name=name,
            environment_id=environment_id,
            compose_file=compose_file,
            app_name=app_name,
        )

    return _fallback


def _build_dokploy_session_compose_update_fallback(
    *, raw_env: RawEnvInput, session_client: DokployBootstrapAuthClient | None
) -> Callable[[str, str], Any] | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password or session_client is None:
        return None

    def _fallback(compose_id: str, compose_file: str) -> Any:
        return session_client.update_compose(
            admin_email=admin_email,
            admin_password=admin_password,
            compose_id=compose_id,
            compose_file=compose_file,
        )

    return _fallback


def _build_dokploy_session_schedule_list_fallback(
    *, raw_env: RawEnvInput, session_client: DokployBootstrapAuthClient | None
) -> Callable[[str], Any] | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password or session_client is None:
        return None

    def _fallback(compose_id: str) -> Any:
        return session_client.list_compose_schedules(
            admin_email=admin_email,
            admin_password=admin_password,
            compose_id=compose_id,
        )

    return _fallback


def _build_dokploy_session_schedule_create_fallback(
    *, raw_env: RawEnvInput, session_client: DokployBootstrapAuthClient | None
) -> Callable[[str, str, str, str, str, str, str, bool], Any] | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password or session_client is None:
        return None

    def _fallback(
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> Any:
        return session_client.create_schedule(
            admin_email=admin_email,
            admin_password=admin_password,
            name=name,
            compose_id=compose_id,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )

    return _fallback


def _build_dokploy_session_schedule_update_fallback(
    *, raw_env: RawEnvInput, session_client: DokployBootstrapAuthClient | None
) -> Callable[[str, str, str, str, str, str, str, str, bool], Any] | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password or session_client is None:
        return None

    def _fallback(
        schedule_id: str,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> Any:
        return session_client.update_schedule(
            admin_email=admin_email,
            admin_password=admin_password,
            schedule_id=schedule_id,
            name=name,
            compose_id=compose_id,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )

    return _fallback


def _build_dokploy_session_schedule_delete_fallback(
    *, raw_env: RawEnvInput, session_client: DokployBootstrapAuthClient | None
) -> Callable[[str], Any] | None:
    admin_email, admin_password = _resolve_dokploy_admin_auth(raw_env.values)
    if not admin_email or not admin_password or session_client is None:
        return None

    def _fallback(schedule_id: str) -> Any:
        return session_client.delete_schedule(
            admin_email=admin_email,
            admin_password=admin_password,
            schedule_id=schedule_id,
        )

    return _fallback


def _find_dokploy_project(
    projects: tuple[DokployProjectSummary, ...], name: str
) -> DokployProjectSummary | None:
    return next((project for project in projects if project.name == name), None)


def _default_dokploy_environment_id(project: DokployProjectSummary) -> str:
    default = next(
        (environment for environment in project.environments if environment.is_default), None
    )
    if default is not None:
        return default.environment_id
    if project.environments:
        return project.environments[0].environment_id
    raise StateValidationError(
        f"Dokploy mutation auth qualification project '{project.name}' has no environments."
    )


def _find_dokploy_compose_id(
    project: DokployProjectSummary | None, compose_name: str
) -> str | None:
    if project is None:
        return None
    for environment in project.environments:
        for compose in environment.composes:
            if compose.name == compose_name:
                return compose.compose_id
    return None


def _looks_like_duplicate_dokploy_error(error: DokployApiError) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in ("already exists", "duplicate", "unique"))


def _refresh_local_dokploy_api_key(
    *, admin_email: str, admin_password: str, attempts: int = 3
) -> Any:
    last_error: Exception | None = None
    for _ in range(attempts):
        result = DokployBootstrapAuthClient(base_url=LOCAL_HEALTH_URL).ensure_api_key(
            admin_email=admin_email,
            admin_password=admin_password,
            key_name=f"dokploy-wizard-{uuid.uuid4().hex[:12]}",
        )
        try:
            DokployApiClient(api_url=LOCAL_HEALTH_URL, api_key=result.api_key).list_projects()
            return result
        except DokployApiError as error:
            last_error = error
    raise DokployBootstrapAuthError(
        "Dokploy local API key refresh succeeded but the returned key remained unauthorized."
    ) from last_error
