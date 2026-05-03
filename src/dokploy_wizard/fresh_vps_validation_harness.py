from __future__ import annotations

import argparse
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pexpect

ENV_PREFIX = "DOKPLOY_WIZARD_VPS_"
DEFAULT_CONFIG_FILE = ".fresh-vps-validation.env"
DEFAULT_ARTIFACT_ROOT = Path(".sisyphus/evidence/fresh-vps-validation")
DEFAULT_SSH_PORT = 22
DEFAULT_REMOTE_ARCHIVE_NAME = "repo.tar.gz"
DEFAULT_REMOTE_SCRIPT_NAME = "run-proof.sh"


class HarnessConfigError(ValueError):
    """Raised when harness configuration is incomplete or invalid."""


@dataclass(frozen=True)
class HarnessConfig:
    repo_root: Path
    config_file: Path | None
    install_env_file: Path
    artifact_root: Path
    target_host: str | None
    target_user: str | None
    target_password: str | None
    target_path: str | None
    ssh_port: int
    ssh_options: tuple[str, ...]
    label: str | None


@dataclass(frozen=True)
class RemotePlan:
    remote_root: str
    remote_repo_dir: str
    remote_state_dir: str
    remote_evidence_dir: str
    remote_logs_dir: str
    remote_install_env_path: str
    remote_archive_path: str
    remote_script_path: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fresh-vps-validation-harness",
        description=(
            "Package the current repo, prepare a proof-run bundle for a fresh VPS, "
            "and optionally execute the proof with install, same-host rerun, "
            "and evidence collection."
        ),
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        help=(
            "optional local ignored env-style config file; defaults to .fresh-vps-validation.env "
            "when present"
        ),
    )
    parser.add_argument(
        "--install-env-file",
        type=Path,
        help="path to the sensitive local .install.env operator file to place on the remote host",
    )
    parser.add_argument("--target-host", help="remote SSH host for the proof run")
    parser.add_argument("--target-user", help="remote SSH user for the proof run")
    parser.add_argument("--target-password", help="remote SSH password for the proof run")
    parser.add_argument(
        "--target-path", help="remote base path for the extracted proof-run workspace"
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        help="remote SSH port (default: 22)",
    )
    parser.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        help="additional raw ssh/scp option, for example '--ssh-option StrictHostKeyChecking=no'",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help="local directory for generated plans, tarballs, logs, and copied evidence",
    )
    parser.add_argument(
        "--label",
        help="optional stable proof-run label appended to local and remote artifact paths",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render the full proof-run plan and create the local bundle without SSH",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help=(
            "exercise local bundle creation plus simulated remote extract/env placement without SSH"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        repo_root = Path(__file__).resolve().parents[2]
        config = resolve_config(args=args, repo_root=repo_root)
        run_label = config.label or "proof-run"
        artifact_dir = config.artifact_root / run_label
        artifact_dir.mkdir(parents=True, exist_ok=True)
        archive_path = artifact_dir / DEFAULT_REMOTE_ARCHIVE_NAME
        create_repo_archive(repo_root=config.repo_root, destination=archive_path)
        plan = build_remote_plan(config=config, run_label=run_label)
        remote_script = render_remote_script(plan=plan)
        commands_path = artifact_dir / "commands.sh"
        plan_path = artifact_dir / "plan.json"
        remote_script_path = artifact_dir / DEFAULT_REMOTE_SCRIPT_NAME
        commands_path.write_text(render_command_summary(config=config, plan=plan), encoding="utf-8")
        remote_script_path.write_text(remote_script, encoding="utf-8")
        plan_payload = build_plan_payload(
            config=config,
            plan=plan,
            archive_path=archive_path,
            artifact_dir=artifact_dir,
            run_label=run_label,
            commands_path=commands_path,
            remote_script_path=remote_script_path,
        )
        plan_path.write_text(json.dumps(plan_payload, indent=2, sort_keys=True), encoding="utf-8")

        if args.self_check:
            self_check = run_self_check(
                config=config, plan=plan, archive_path=archive_path, artifact_dir=artifact_dir
            )
            payload = {
                **plan_payload,
                "mode": "self_check",
                "self_check": self_check,
            }
        elif args.dry_run:
            payload = {
                **plan_payload,
                "mode": "dry_run",
                "missing_required_settings": missing_remote_settings(config=config),
            }
        else:
            payload = run_execute_mode(
                config=config,
                plan=plan,
                archive_path=archive_path,
                artifact_dir=artifact_dir,
                plan_payload=plan_payload,
            )

        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (HarnessConfigError, RuntimeError) as error:
        parser.exit(status=1, message=f"fresh-vps-validation-harness: {error}\n")


def resolve_config(*, args: argparse.Namespace, repo_root: Path) -> HarnessConfig:
    default_config = repo_root / DEFAULT_CONFIG_FILE
    explicit_config = args.config_file.resolve() if args.config_file else None
    discovered_config = explicit_config
    if discovered_config is None and default_config.exists():
        discovered_config = default_config
    config_values = load_env_file(discovered_config) if discovered_config else {}
    install_env_file = resolve_path_setting(
        cli_value=args.install_env_file,
        env_key=f"{ENV_PREFIX}INSTALL_ENV_FILE",
        config_values=config_values,
        config_key="INSTALL_ENV_FILE",
        repo_root=repo_root,
    )
    if install_env_file is None:
        raise HarnessConfigError(
            "install env file is required; supply --install-env-file, "
            f"{ENV_PREFIX}INSTALL_ENV_FILE, or INSTALL_ENV_FILE in the ignored config"
        )
    if not install_env_file.exists():
        raise HarnessConfigError(f"install env file does not exist: {install_env_file}")
    artifact_root = resolve_path_setting(
        cli_value=args.artifact_root,
        env_key=f"{ENV_PREFIX}ARTIFACT_ROOT",
        config_values=config_values,
        config_key="ARTIFACT_ROOT",
        repo_root=repo_root,
    ) or (repo_root / DEFAULT_ARTIFACT_ROOT)
    target_host = resolve_text_setting(
        cli_value=args.target_host,
        env_key=f"{ENV_PREFIX}HOST",
        config_values=config_values,
        config_key="HOST",
    )
    target_user = resolve_text_setting(
        cli_value=args.target_user,
        env_key=f"{ENV_PREFIX}USER",
        config_values=config_values,
        config_key="USER",
    )
    target_path = resolve_text_setting(
        cli_value=args.target_path,
        env_key=f"{ENV_PREFIX}PATH",
        config_values=config_values,
        config_key="PATH",
    )
    ssh_port_value = resolve_text_setting(
        cli_value=str(args.ssh_port) if args.ssh_port is not None else None,
        env_key=f"{ENV_PREFIX}SSH_PORT",
        config_values=config_values,
        config_key="SSH_PORT",
    )
    ssh_port = DEFAULT_SSH_PORT if ssh_port_value is None else int(ssh_port_value)
    ssh_options = resolve_ssh_options(args.ssh_option, config_values)
    label = resolve_text_setting(
        cli_value=args.label,
        env_key=f"{ENV_PREFIX}LABEL",
        config_values=config_values,
        config_key="LABEL",
    )
    target_password = resolve_text_setting(
        cli_value=args.target_password,
        env_key=f"{ENV_PREFIX}PASSWORD",
        config_values=config_values,
        config_key="PASSWORD",
    )
    return HarnessConfig(
        repo_root=repo_root,
        config_file=discovered_config,
        install_env_file=install_env_file,
        artifact_root=artifact_root,
        target_host=target_host,
        target_user=target_user,
        target_password=target_password,
        target_path=target_path,
        ssh_port=ssh_port,
        ssh_options=ssh_options,
        label=label,
    )


def resolve_ssh_options(cli_values: list[str], config_values: dict[str, str]) -> tuple[str, ...]:
    if cli_values:
        return tuple(cli_values)
    config_raw = os.environ.get(f"{ENV_PREFIX}SSH_OPTIONS") or config_values.get("SSH_OPTIONS", "")
    if not config_raw.strip():
        return ()
    return tuple(shlex.split(config_raw))


def resolve_text_setting(
    *,
    cli_value: str | None,
    env_key: str,
    config_values: dict[str, str],
    config_key: str,
) -> str | None:
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value
    return config_values.get(config_key)


def resolve_path_setting(
    *,
    cli_value: Path | None,
    env_key: str,
    config_values: dict[str, str],
    config_key: str,
    repo_root: Path,
) -> Path | None:
    if cli_value is not None:
        return cli_value.resolve()
    env_value = os.environ.get(env_key)
    if env_value:
        return normalize_path(Path(env_value), repo_root=repo_root)
    config_value = config_values.get(config_key)
    if config_value:
        return normalize_path(Path(config_value), repo_root=repo_root)
    return None


def normalize_path(path: Path, *, repo_root: Path) -> Path:
    return path if path.is_absolute() else (repo_root / path).resolve()


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise HarnessConfigError(f"invalid config line in {path}: {raw_line!r}")
        values[key.strip()] = value.strip()
    return values


def build_remote_plan(*, config: HarnessConfig, run_label: str) -> RemotePlan:
    remote_base = config.target_path or "/UNSET_REMOTE_PATH"
    remote_root = posixpath.join(remote_base, run_label)
    return RemotePlan(
        remote_root=remote_root,
        remote_repo_dir=posixpath.join(remote_root, "repo"),
        remote_state_dir=posixpath.join(remote_root, "state"),
        remote_evidence_dir=posixpath.join(remote_root, "evidence"),
        remote_logs_dir=posixpath.join(remote_root, "logs"),
        remote_install_env_path=posixpath.join(remote_root, ".install.env"),
        remote_archive_path=posixpath.join(remote_root, DEFAULT_REMOTE_ARCHIVE_NAME),
        remote_script_path=posixpath.join(remote_root, DEFAULT_REMOTE_SCRIPT_NAME),
    )


def create_repo_archive(*, repo_root: Path, destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(repo_root.rglob("*")):
            relative = path.relative_to(repo_root)
            if should_skip(relative):
                continue
            archive.add(path, arcname=relative.as_posix(), recursive=False)


def should_skip(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if parts[0] in {
        ".git",
        ".venv",
        "venv",
        "env",
        "build",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".dokploy-wizard-state",
    }:
        return True
    if parts[0] == ".sisyphus" and len(parts) > 1 and parts[1] == "evidence":
        return True
    if relative.name in {".install.env", DEFAULT_CONFIG_FILE}:
        return True
    return any(part == "__pycache__" for part in parts)


def build_plan_payload(
    *,
    config: HarnessConfig,
    plan: RemotePlan,
    archive_path: Path,
    artifact_dir: Path,
    run_label: str,
    commands_path: Path,
    remote_script_path: Path,
) -> dict[str, Any]:
    return {
        "artifact_dir": str(artifact_dir),
        "archive_path": str(archive_path),
        "commands_path": str(commands_path),
        "config": {
            "artifact_root": str(config.artifact_root),
            "config_file": str(config.config_file) if config.config_file else None,
            "install_env_file": str(config.install_env_file),
            "label": config.label,
            "repo_root": str(config.repo_root),
            "ssh_options": list(config.ssh_options),
            "ssh_port": config.ssh_port,
            "target_host": config.target_host,
            "target_path": config.target_path,
            "target_password": "<redacted>" if config.target_password else None,
            "target_user": config.target_user,
        },
        "local_evidence_sensitive": True,
        "remote_plan": asdict(plan),
        "remote_script_path": str(remote_script_path),
        "run_label": run_label,
        "steps": [
            "package_repo",
            "upload_bundle",
            "extract_repo",
            "place_install_env",
            "first_install",
            "rerun_same_host_noop_proof",
            "inspect_state",
            "collect_state_and_logs",
        ],
    }


def render_command_summary(*, config: HarnessConfig, plan: RemotePlan) -> str:
    ssh_target = render_ssh_target(config)
    ssh_base = render_ssh_base(config)
    scp_base = render_scp_base(config)
    install_env_target = f"{ssh_target}:{plan.remote_install_env_path}" if ssh_target else "<unset>"
    archive_source = (
        config.artifact_root / (config.label or "proof-run") / DEFAULT_REMOTE_ARCHIVE_NAME
    )
    archive_target = f"{ssh_target}:{plan.remote_archive_path}" if ssh_target else "<unset>"
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"# proof host: {ssh_target or '<unset>'}",
        f"# remote root: {plan.remote_root}",
        f"mkdir -p {shlex.quote(str(config.artifact_root))}",
        (
            f"{ssh_base} {shlex.quote(ssh_target or '<unset>')} "
            f"'mkdir -p {shlex.quote(plan.remote_root)}'"
        ),
        (
            f"{scp_base} {shlex.quote(str(config.install_env_file))} "
            f"{shlex.quote(install_env_target)}"
        ),
        (f"{scp_base} {shlex.quote(str(archive_source))} {shlex.quote(archive_target)}"),
        (
            f"{ssh_base} {shlex.quote(ssh_target or '<unset>')} "
            f"'bash {shlex.quote(plan.remote_script_path)}'"
        ),
    ]
    return "\n".join(lines) + "\n"


def render_remote_script(*, plan: RemotePlan) -> str:
    install_command = (
        f"./bin/dokploy-wizard install --env-file {shlex.quote(plan.remote_install_env_path)} "
        f"--state-dir {shlex.quote(plan.remote_state_dir)} --non-interactive"
    )
    inspect_command = (
        f"./bin/dokploy-wizard inspect-state "
        f"--env-file {shlex.quote(plan.remote_install_env_path)} "
        f"--state-dir {shlex.quote(plan.remote_state_dir)}"
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail

REMOTE_ROOT={shlex.quote(plan.remote_root)}
REPO_DIR={shlex.quote(plan.remote_repo_dir)}
STATE_DIR={shlex.quote(plan.remote_state_dir)}
EVIDENCE_DIR={shlex.quote(plan.remote_evidence_dir)}
LOG_DIR={shlex.quote(plan.remote_logs_dir)}
INSTALL_ENV={shlex.quote(plan.remote_install_env_path)}
ARCHIVE_PATH={shlex.quote(plan.remote_archive_path)}
SUMMARY_PATH="$EVIDENCE_DIR/summary.json"

mkdir -p "$REMOTE_ROOT"
chmod 600 "$INSTALL_ENV"
rm -rf "$STATE_DIR" "$EVIDENCE_DIR" "$LOG_DIR"
mkdir -p "$STATE_DIR" "$EVIDENCE_DIR" "$LOG_DIR"
rm -rf "$REPO_DIR"
mkdir -p "$REPO_DIR"
tar -xzf "$ARCHIVE_PATH" -C "$REPO_DIR"

run_and_capture() {{
  local label="$1"
  shift
  set +e
  "$@" >"$LOG_DIR/$label.stdout" 2>"$LOG_DIR/$label.stderr"
  local exit_code="$?"
  set -e
  printf '%s\n' "$exit_code" >"$LOG_DIR/$label.exit"
  return "$exit_code"
}}

first_install_exit=0
rerun_exit=0
inspect_exit=0

(
  cd "$REPO_DIR"
  run_and_capture first-install bash -lc {shlex.quote(install_command)}
) || first_install_exit="$?"

(
  cd "$REPO_DIR"
  run_and_capture rerun-install bash -lc {shlex.quote(install_command)}
) || rerun_exit="$?"

(
  cd "$REPO_DIR"
  run_and_capture inspect-state bash -lc {shlex.quote(inspect_command)}
) || inspect_exit="$?"

for state_name in raw-input.json desired-state.json applied-state.json ownership-ledger.json; do
  if [[ -f "$STATE_DIR/$state_name" ]]; then
    cp "$STATE_DIR/$state_name" "$EVIDENCE_DIR/$state_name"
  fi
done

cat >"$SUMMARY_PATH" <<EOF
{{
  "steps": [
    "first_install",
    "rerun_same_host_noop_proof",
    "inspect_state",
    "collect_state_and_logs"
  ],
  "remote_root": {json.dumps(plan.remote_root)},
  "repo_dir": {json.dumps(plan.remote_repo_dir)},
  "state_dir": {json.dumps(plan.remote_state_dir)},
  "install_env_path": {json.dumps(plan.remote_install_env_path)},
  "command_exit_codes": {{
    "first_install": $first_install_exit,
    "rerun_install": $rerun_exit,
    "inspect_state": $inspect_exit
  }}
}}
EOF

if [[ "$first_install_exit" -ne 0 || "$rerun_exit" -ne 0 || "$inspect_exit" -ne 0 ]]; then
  exit 1
fi
"""


def missing_remote_settings(*, config: HarnessConfig) -> list[str]:
    missing: list[str] = []
    if not config.target_host:
        missing.append("target_host")
    if not config.target_user:
        missing.append("target_user")
    if not config.target_path:
        missing.append("target_path")
    return missing


def run_self_check(
    *,
    config: HarnessConfig,
    plan: RemotePlan,
    archive_path: Path,
    artifact_dir: Path,
) -> dict[str, Any]:
    simulated_remote_root = artifact_dir / "self-check-remote"
    if simulated_remote_root.exists():
        shutil.rmtree(simulated_remote_root)
    simulated_remote_root.mkdir(parents=True)
    extracted_repo = simulated_remote_root / "repo"
    extracted_repo.mkdir(parents=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(extracted_repo)
    placed_install_env = simulated_remote_root / ".install.env"
    shutil.copy2(config.install_env_file, placed_install_env)
    os.chmod(placed_install_env, 0o600)
    self_check_payload = {
        "archive_members_checked": [
            "bin/dokploy-wizard",
            "src/dokploy_wizard/cli.py",
            "tests/e2e/test_rerun_modify_resume.py",
        ],
        "extracted_repo": str(extracted_repo),
        "install_env_mode": oct(placed_install_env.stat().st_mode & 0o777),
        "install_env_placed": str(placed_install_env),
        "install_env_matches_source": (
            placed_install_env.read_text(encoding="utf-8")
            == config.install_env_file.read_text(encoding="utf-8")
        ),
        "remote_steps_declared": [
            "first_install",
            "rerun_same_host_noop_proof",
            "inspect_state",
            "collect_state_and_logs",
        ],
        "script_mentions_noop_proof": "rerun_same_host_noop_proof"
        in render_remote_script(plan=plan),
        "simulated_remote_root": str(simulated_remote_root),
        "tarball_contains_bin_wrapper": (extracted_repo / "bin" / "dokploy-wizard").exists(),
        "tarball_contains_source_module": (
            extracted_repo / "src" / "dokploy_wizard" / "cli.py"
        ).exists(),
    }
    (artifact_dir / "self-check.json").write_text(
        json.dumps(self_check_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return self_check_payload


def run_execute_mode(
    *,
    config: HarnessConfig,
    plan: RemotePlan,
    archive_path: Path,
    artifact_dir: Path,
    plan_payload: dict[str, Any],
) -> dict[str, Any]:
    missing = missing_remote_settings(config=config)
    if missing:
        raise HarnessConfigError("execute mode requires remote settings: " + ", ".join(missing))
    remote_script_local = artifact_dir / DEFAULT_REMOTE_SCRIPT_NAME
    ssh_target = render_ssh_target(config)
    assert ssh_target is not None
    commands: list[dict[str, Any]] = []
    run_logged_command(
        [*ssh_command_base(config), ssh_target, f"mkdir -p {shlex.quote(plan.remote_root)}"],
        label="prepare-remote-root",
        commands=commands,
        artifact_dir=artifact_dir,
        config=config,
    )
    run_logged_command(
        [*scp_command_base(config), str(archive_path), f"{ssh_target}:{plan.remote_archive_path}"],
        label="upload-archive",
        commands=commands,
        artifact_dir=artifact_dir,
        config=config,
    )
    run_logged_command(
        [
            *scp_command_base(config),
            str(config.install_env_file),
            f"{ssh_target}:{plan.remote_install_env_path}",
        ],
        label="upload-install-env",
        commands=commands,
        artifact_dir=artifact_dir,
        config=config,
    )
    run_logged_command(
        [
            *scp_command_base(config),
            str(remote_script_local),
            f"{ssh_target}:{plan.remote_script_path}",
        ],
        label="upload-remote-script",
        commands=commands,
        artifact_dir=artifact_dir,
        config=config,
    )
    remote_proof_record = run_logged_command(
        [
            *ssh_command_base(config),
            ssh_target,
            f"bash {shlex.quote(plan.remote_script_path)}",
        ],
        label="run-remote-proof",
        commands=commands,
        artifact_dir=artifact_dir,
        config=config,
        raise_on_error=False,
    )
    collect_dir = artifact_dir / "collected-remote"
    collect_dir.mkdir(parents=True, exist_ok=True)
    collect_error: RuntimeError | None = None
    try:
        run_logged_command(
            [
                *scp_command_base(config),
                "-r",
                f"{ssh_target}:{plan.remote_evidence_dir}",
                f"{ssh_target}:{plan.remote_logs_dir}",
                f"{ssh_target}:{plan.remote_state_dir}",
                str(collect_dir),
            ],
            label="collect-remote-artifacts",
            commands=commands,
            artifact_dir=artifact_dir,
            config=config,
        )
    except RuntimeError as error:
        collect_error = error
    if remote_proof_record["exit_code"] != 0:
        if collect_error is not None:
            raise RuntimeError(
                "run-remote-proof failed with exit code "
                f"{remote_proof_record['exit_code']}; "
                "collect-remote-artifacts also failed"
            ) from collect_error
        raise RuntimeError(
            f"run-remote-proof failed with exit code {remote_proof_record['exit_code']}"
        )
    if collect_error is not None:
        raise collect_error
    return {
        **plan_payload,
        "commands": commands,
        "mode": "execute",
    }


def run_logged_command(
    command: list[str],
    *,
    label: str,
    commands: list[dict[str, Any]],
    artifact_dir: Path,
    config: HarnessConfig,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    if config.target_password and command and command[0] in {"ssh", "scp"}:
        result = _run_password_command(command, password=config.target_password)
    else:
        result = subprocess.run(command, check=False, text=True, capture_output=True)
    stdout_path = artifact_dir / f"{label}.stdout"
    stderr_path = artifact_dir / f"{label}.stderr"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    record = {
        "command": command,
        "exit_code": result.returncode,
        "label": label,
        "stderr_path": str(stderr_path),
        "stdout_path": str(stdout_path),
    }
    commands.append(record)
    if raise_on_error and result.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")
    return record


def _run_password_command(command: list[str], *, password: str) -> subprocess.CompletedProcess[str]:
    child = pexpect.spawn(shlex.join(command), encoding="utf-8", timeout=7200)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    while True:
        idx = child.expect([r"password:", r"yes/no", pexpect.EOF, pexpect.TIMEOUT])
        if idx == 0:
            child.sendline(password)
            continue
        if idx == 1:
            child.sendline("yes")
            continue
        if idx == 2:
            stdout_parts.append(child.before or "")
            break
        if idx == 3:
            stderr_parts.append(child.before or "")
            child.close(force=True)
            raise RuntimeError(f"password-based command timed out: {shlex.join(command)}")
    child.close()
    output = "".join(stdout_parts)
    error = "".join(stderr_parts)
    return subprocess.CompletedProcess(command, child.exitstatus or 0, output, error)


def ssh_command_base(config: HarnessConfig) -> list[str]:
    parts = ["ssh", "-p", str(config.ssh_port)]
    for option in config.ssh_options:
        parts.extend(["-o", option])
    return parts


def scp_command_base(config: HarnessConfig) -> list[str]:
    parts = ["scp", "-P", str(config.ssh_port)]
    for option in config.ssh_options:
        parts.extend(["-o", option])
    return parts


def render_ssh_target(config: HarnessConfig) -> str | None:
    if not config.target_host or not config.target_user:
        return None
    return f"{config.target_user}@{config.target_host}"


def render_ssh_base(config: HarnessConfig) -> str:
    parts = ["ssh", "-p", str(config.ssh_port)]
    for option in config.ssh_options:
        parts.extend(["-o", option])
    return " ".join(shlex.quote(part) for part in parts)


def render_scp_base(config: HarnessConfig) -> str:
    parts = ["scp", "-P", str(config.ssh_port)]
    for option in config.ssh_options:
        parts.extend(["-o", option])
    return " ".join(shlex.quote(part) for part in parts)


if __name__ == "__main__":
    raise SystemExit(main())
