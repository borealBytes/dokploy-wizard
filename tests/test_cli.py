# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from dokploy_wizard import cli
from dokploy_wizard.dokploy import (
    DokployApiError,
    DokployBootstrapAuthError,
    DokployBootstrapAuthResult,
    DokployComposeRecord,
    DokployComposeSummary,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.compose_noop import persist_compose_artifact_hash
from dokploy_wizard.lifecycle import (
    LifecyclePlan,
    applicable_phases_for,
    classify_install_request,
    classify_modify_request,
)
from dokploy_wizard.lifecycle import changes as lifecycle_changes
from dokploy_wizard.lifecycle import engine as lifecycle_engine
from dokploy_wizard.lifecycle.drift import DriftEntry, DriftReport, LifecycleDriftError
from dokploy_wizard.networking import planner as networking_planner
from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessPolicy,
)
from dokploy_wizard.packs import prompts as prompt_module
from dokploy_wizard.packs.prompts import (
    GuidedInstallValues,
    PromptSelection,
    prompt_for_initial_install_values,
)
from dokploy_wizard.preflight import (
    HostFacts,
    PreflightCheck,
    PreflightError,
    PreflightReport,
    derive_required_profile,
)
from dokploy_wizard.remote_transport import RemoteTransportSession
from dokploy_wizard.state import (
    SEAWEEDFS_GENERATED_SECRETS_FILE,
    AppliedStateCheckpoint,
    ComposeArtifactHashState,
    LiteLLMGeneratedKeys,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    SeaweedFsGeneratedSecrets,
    StateValidationError,
    SurfSenseGeneratedSecrets,
    ensure_litellm_generated_keys,
    load_state_dir,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_litellm_generated_keys,
    write_ownership_ledger,
    write_seaweedfs_generated_secrets,
    write_surfsense_generated_secrets,
    write_target_state,
)
from dokploy_wizard.state import inspection as inspection_module

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "dokploy-wizard"
FIXTURES_DIR = REPO_ROOT / "fixtures"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def _raw_input(values: dict[str, str]) -> RawEnvInput:
    return RawEnvInput(format_version=1, values=values)


def _farm_modify_values(**overrides: str) -> dict[str, str]:
    values = {
        "STACK_NAME": "wizard-stack",
        "ROOT_DOMAIN": "example.com",
        "ENABLE_NEXTCLOUD": "true",
        "ENABLE_MY_FARM_ADVISOR": "true",
        "AI_DEFAULT_API_KEY": "shared-key",
        "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
        "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
    }
    values.update(overrides)
    return values


def _classify_modify_plan(
    *, existing_values: dict[str, str], requested_values: dict[str, str]
) -> LifecyclePlan:
    existing_raw = _raw_input(existing_values)
    requested_raw = _raw_input(requested_values)
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)
    return classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )


class _ProofTransport:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.ensured_dirs: list[str] = []
        self.uploads: list[tuple[Path, str]] = []
        self.chmods: list[tuple[str, int]] = []

    def ensure_dir(self, remote_path: str) -> None:
        self.ensured_dirs.append(remote_path)

    def upload(self, local_path: Path, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))

    def chmod(self, remote_path: str, mode: int) -> None:
        self.chmods.append((remote_path, mode))

    def run(self, subcommand: str, command: str) -> None:
        self.commands.append((subcommand, command))


def test_compose_hash_state_round_trips_through_applied_checkpoint() -> None:
    compose_hash = ComposeArtifactHashState.from_rendered_compose(
        service_id="svc-openclaw",
        rendered_compose=(
            "services:\r\n"
            "  app:  \r\n"
            "    image: ghcr.io/example/openclaw:latest\r\n"
            "    environment:\r\n"
            "      SECRET_TOKEN: should-not-be-persisted\r\n"
        ),
    )
    checkpoint = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint="abc123",
        completed_steps=("preflight", "openclaw"),
        compose_artifact_hashes={"openclaw": compose_hash},
    )

    payload = checkpoint.to_dict()
    round_trip = AppliedStateCheckpoint.from_dict(json.loads(json.dumps(payload)))

    assert payload["compose_artifact_hashes"] == {
        "openclaw": {
            "service_id": "svc-openclaw",
            "rendered_compose_sha256": compose_hash.rendered_compose_sha256,
        }
    }
    assert "SECRET_TOKEN" not in json.dumps(payload)
    assert "should-not-be-persisted" not in json.dumps(payload)
    assert round_trip == checkpoint


def test_compose_hash_state_loads_missing_hash_metadata_for_backward_compatibility(
    tmp_path: Path,
) -> None:
    applied_state_path = tmp_path / "applied-state.json"
    applied_state_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "desired_state_fingerprint": "abc123",
                "completed_steps": ["preflight", "openclaw"],
            }
        ),
        encoding="utf-8",
    )

    loaded_state = load_state_dir(tmp_path)

    assert loaded_state.applied_state is not None
    assert loaded_state.applied_state.compose_artifact_hashes == {}


def test_compose_hash_state_changes_when_rendered_compose_changes() -> None:
    original = ComposeArtifactHashState.from_rendered_compose(
        service_id="svc-farm",
        rendered_compose=(
            "services:\n"
            "  farm:\n"
            "    image: ghcr.io/borealbytes/my-farm-advisor:latest\n"
            "    environment:\n"
            "      MODEL=anthropic/claude-sonnet-4\n"
        ),
    )
    changed = ComposeArtifactHashState.from_rendered_compose(
        service_id="svc-farm",
        rendered_compose=(
            "services:\n"
            "  farm:\n"
            "    image: ghcr.io/borealbytes/my-farm-advisor:latest\n"
            "    environment:\n"
            "      MODEL=openrouter/openrouter/hunter-alpha\n"
        ),
    )

    checkpoint = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint="abc123",
        completed_steps=("preflight", "my-farm-advisor"),
        compose_artifact_hashes={"my-farm-advisor": changed},
    )

    assert changed.rendered_compose_sha256 != original.rendered_compose_sha256
    assert checkpoint.to_dict()["compose_artifact_hashes"]["my-farm-advisor"]["service_id"] == (
        "svc-farm"
    )


def test_remote_proof_default_flow_is_verification_first_after_install() -> None:
    transport = _ProofTransport()
    session = RemoteTransportSession(transport=transport, remote_root="/root/dokploy-wizard")

    session.run_proof()

    assert transport.commands == [
        (
            "mutate-install",
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard install --env-file "
            "/root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state --non-interactive",
        ),
        (
            "verify-services",
            "PYTHONUNBUFFERED=1 PYTHONPATH=./src${PYTHONPATH:+:$PYTHONPATH} python3 -m "
            "dokploy_wizard.service_verification_runner --env-file "
            "/root/dokploy-wizard/.install.env --state-dir /root/dokploy-wizard/state",
        ),
        (
            "inspect-state",
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard inspect-state --env-file "
            "/root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state",
        ),
    ]


def test_remote_proof_strict_mode_keeps_explicit_idempotency_install() -> None:
    transport = _ProofTransport()
    session = RemoteTransportSession(transport=transport, remote_root="/root/dokploy-wizard")

    session.run_proof(strict_idempotency=True)

    assert transport.commands == [
        (
            "mutate-install",
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard install --env-file "
            "/root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state --non-interactive",
        ),
        (
            "verify-services",
            "PYTHONUNBUFFERED=1 PYTHONPATH=./src${PYTHONPATH:+:$PYTHONPATH} python3 -m "
            "dokploy_wizard.service_verification_runner --env-file "
            "/root/dokploy-wizard/.install.env --state-dir /root/dokploy-wizard/state",
        ),
        (
            "assert-strict-idempotency",
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard install --env-file "
            "/root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state --non-interactive",
        ),
        (
            "inspect-state",
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard inspect-state --env-file "
            "/root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state",
        ),
    ]


def test_remote_upload_accepts_install_min_env_for_proof_without_remote_name_drift(
    tmp_path: Path,
) -> None:
    transport = _ProofTransport()
    session = RemoteTransportSession(transport=transport, remote_root="/root/dokploy-wizard")
    repo_archive = tmp_path / "repo.tar.gz"
    install_min_env = tmp_path / ".install-min.env"
    repo_archive.write_bytes(b"placeholder archive")
    install_min_env.write_text("STACK_NAME=wizard-stack\nROOT_DOMAIN=example.com\n", encoding="utf-8")

    session.upload_bundle(repo_archive, install_min_env)

    assert transport.ensured_dirs == ["/root/dokploy-wizard"]
    assert transport.uploads == [
        (repo_archive, "/root/dokploy-wizard/repo.tar.gz"),
        (install_min_env, "/root/dokploy-wizard/.install.env"),
    ]
    assert transport.chmods == [("/root/dokploy-wizard/.install.env", 0o600)]


def test_help_lists_expected_subcommands() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert "inspect-state" in result.stdout
    assert "install" in result.stdout
    assert "modify" in result.stdout
    assert "uninstall" in result.stdout
    assert result.stderr == ""


def test_inspect_state_help_lists_task_two_flags() -> None:
    result = run_cli("inspect-state", "--help")

    assert result.returncode == 0
    assert "--env-file" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--dry-run" in result.stdout
    assert result.stderr == ""


def test_handle_inspect_state_includes_live_drift_and_persists_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "inspect.env"
    env_file.write_text(
        (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    expected_live_drift = {
        "detected": True,
        "entries": [
            {
                "classification": "manual_collision",
                "detail": "collision",
                "expected_service_name": "wizard-stack-my-farm-advisor",
                "live_kind": "container",
                "live_name": "my-farm-advisor-manual",
                "managed": False,
                "pack": "my-farm-advisor",
                "scope": "stack:wizard-stack:my-farm-advisor",
                "status": "Up 2 minutes",
            }
        ],
        "inspection": {
            "docker": {"available": True, "detail": "docker inspected"},
            "host_routes": {"available": True, "detail": "routes inspected"},
        },
        "status": "drift_detected",
        "summary": {
            "wizard_managed": 0,
            "manual_collision": 1,
            "host_local_route": 0,
            "unknown_unmanaged": 0,
        },
    }
    monkeypatch.setattr(
        cli,
        "build_live_drift_report",
        lambda **_: expected_live_drift,
    )

    assert (
        cli._handle_inspect_state(
            argparse.Namespace(
                env_file=env_file,
                state_dir=state_dir,
                dry_run=False,
            )
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["live_drift"] == expected_live_drift
    assert payload["advisor_status"]["my_farm_advisor"]["display_name"] == "Nexa Farm"
    assert json.loads((state_dir / "desired-state.json").read_text(encoding="utf-8")) == payload
    assert (state_dir / "raw-input.json").exists()
    assert not (state_dir / "applied-state.json").exists()
    assert not (state_dir / "ownership-ledger.json").exists()


def test_inspect_state_reports_farm_status_with_redacted_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "inspect-farm.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_SUBDOMAIN=dokploy",
                "DOKPLOY_ADMIN_EMAIL=operator@example.com",
                "DOKPLOY_ADMIN_PASSWORD=super-secret-password",
                "CLOUDFLARE_API_TOKEN=cf-secret-token",
                "CLOUDFLARE_ACCOUNT_ID=account-123",
                "PACKS=my-farm-advisor",
                "AI_DEFAULT_API_KEY=shared-secret-key",
                "AI_DEFAULT_BASE_URL=https://models.example.com/v1",
                "MY_FARM_ADVISOR_PRIMARY_MODEL=anthropic/claude-sonnet-4",
                "MY_FARM_ADVISOR_GATEWAY_PASSWORD=farm-secret-password",
                "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN=SECRET_TEST_FARM_BOT_TOKEN_VALUE",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "build_live_drift_report", lambda **_: {"status": "clean"})

    assert (
        cli._handle_inspect_state(
            argparse.Namespace(env_file=env_file, state_dir=tmp_path / "state", dry_run=False)
        )
        == 0
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["advisor_status"]["my_farm_advisor"] == {
        "display_name": "Nexa Farm",
        "enabled": True,
        "hostname": "farm.example.com",
        "channels": ["telegram"],
        "workspace_mount_names": ["/Nexa Farm", "/Nexa Farm Data Pipeline"],
    }
    raw_snapshot = json.loads((tmp_path / "state" / "raw-input.json").read_text(encoding="utf-8"))
    assert raw_snapshot["values"]["CLOUDFLARE_API_TOKEN"] == "<redacted>"
    assert raw_snapshot["values"]["DOKPLOY_ADMIN_PASSWORD"] == "<redacted>"
    assert raw_snapshot["values"]["AI_DEFAULT_API_KEY"] == "<redacted>"
    assert raw_snapshot["values"]["MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN"] == "<redacted>"
    env_specs = {entry["name"]: entry for entry in payload["dokploy_env_specs"]}
    bot_token_spec = env_specs["MY_FARM_ADVISOR_MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN"]
    assert bot_token_spec["owner"] == "my-farm-advisor"
    assert bot_token_spec["sensitive"] is True
    assert bot_token_spec["redacted_fingerprint"].startswith("sha256:")
    assert "cf-secret-token" not in json.dumps(payload)
    assert "shared-secret-key" not in json.dumps(payload)
    assert "SECRET_TEST_FARM_BOT_TOKEN_VALUE" not in output


def test_inspect_state_reports_disabled_farm_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "inspect-openclaw.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_SUBDOMAIN=dokploy",
                "DOKPLOY_ADMIN_EMAIL=operator@example.com",
                "CLOUDFLARE_API_TOKEN=cf-token",
                "CLOUDFLARE_ACCOUNT_ID=account-123",
                "PACKS=openclaw",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "build_live_drift_report", lambda **_: {"status": "clean"})

    assert (
        cli._handle_inspect_state(
            argparse.Namespace(env_file=env_file, state_dir=tmp_path / "state", dry_run=True)
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["advisor_status"]["my_farm_advisor"] == {
        "display_name": "Nexa Farm",
        "enabled": False,
        "hostname": None,
        "channels": [],
        "workspace_mount_names": [],
    }


def test_inspect_state_redacts_litellm_generated_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "inspect-litellm.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_SUBDOMAIN=dokploy",
                "DOKPLOY_ADMIN_EMAIL=operator@example.com",
                "DOKPLOY_ADMIN_PASSWORD=super-secret-password",
                "ENABLE_MY_FARM_ADVISOR=true",
                "LITELLM_LOCAL_BASE_URL=http://local-model.internal:61434/v1",
                "LITELLM_LOCAL_MODEL=unsloth-active",
                "LITELLM_LOCAL_API_KEY=sk-no-key-required",
                "LITELLM_MASTER_KEY=litellm-master-secret",
                "LITELLM_SALT_KEY=litellm-salt-secret",
                "LITELLM_VIRTUAL_KEY_OPENCLAW=litellm-virtual-secret",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "build_live_drift_report", lambda **_: {"status": "clean"})

    assert (
        cli._handle_inspect_state(
            argparse.Namespace(env_file=env_file, state_dir=tmp_path / "state", dry_run=False)
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    raw_snapshot = json.loads((tmp_path / "state" / "raw-input.json").read_text(encoding="utf-8"))

    assert raw_snapshot["values"]["LITELLM_MASTER_KEY"] == "<redacted>"
    assert raw_snapshot["values"]["LITELLM_SALT_KEY"] == "<redacted>"
    assert raw_snapshot["values"]["LITELLM_VIRTUAL_KEY_OPENCLAW"] == "<redacted>"
    assert "litellm-master-secret" not in json.dumps(payload)
    assert "litellm-salt-secret" not in json.dumps(payload)
    assert "litellm-virtual-secret" not in json.dumps(payload)


def test_inspect_state_redacts_litellm_provider_api_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "inspect-litellm-provider-keys.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_SUBDOMAIN=dokploy",
                "DOKPLOY_ADMIN_EMAIL=operator@example.com",
                "ENABLE_MY_FARM_ADVISOR=true",
                "AI_DEFAULT_PROVIDER=openrouter",
                "AI_DEFAULT_MODEL=anthropic/claude-sonnet-4",
                "LITELLM_LOCAL_BASE_URL=http://local-model.internal:61434/v1",
                "LITELLM_LOCAL_MODEL=unsloth-active",
                "LITELLM_LOCAL_API_KEY=sk-no-key-required",
                "LITELLM_OPENROUTER_API_KEY=sk-test-openrouter-secret-12345678",
                "LITELLM_OPENROUTER_MODELS=anthropic/claude-sonnet-4",
                "LITELLM_OPENCODE_GO_API_KEY=sk-test-opencode-secret-12345678",
                "LITELLM_OPENCODE_GO_BASE_URL=https://opencode-go.example.com/v1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "build_live_drift_report", lambda **_: {"status": "clean"})

    assert (
        cli._handle_inspect_state(
            argparse.Namespace(env_file=env_file, state_dir=tmp_path / "state", dry_run=False)
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    raw_snapshot = json.loads((tmp_path / "state" / "raw-input.json").read_text(encoding="utf-8"))

    assert raw_snapshot["values"]["LITELLM_OPENROUTER_API_KEY"] == "<redacted>"
    assert raw_snapshot["values"]["LITELLM_OPENCODE_GO_API_KEY"] == "<redacted>"
    assert "sk-test-openrouter-secret-12345678" not in json.dumps(payload)
    assert "sk-test-opencode-secret-12345678" not in json.dumps(payload)


def test_inspect_state_reports_surfsense_redacted_resource_and_secret_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "inspect-surfsense.env"
    state_dir = tmp_path / "state"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_SUBDOMAIN=dokploy",
                "DOKPLOY_ADMIN_EMAIL=operator@example.com",
                "DOKPLOY_ADMIN_PASSWORD=super-secret-password",
                "DOKPLOY_API_URL=https://dokploy.example.com/api",
                "DOKPLOY_API_KEY=dokploy-secret-key",
                "PACKS=surfsense",
                "LITELLM_OPENROUTER_API_KEY=SECRET_TEST_OPENROUTER_PROVIDER_KEY",
                "SURFSENSE_VERSION=0.0.25",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="surfsense_service",
                    resource_id="dokploy-compose:svc-1:surfsense-service",
                    scope="stack:wizard-stack:surfsense:service",
                ),
                OwnedResource(
                    resource_type="surfsense_data",
                    resource_id="dokploy-compose:svc-1:surfsense-data",
                    scope="stack:wizard-stack:surfsense:data",
                ),
            ),
        ),
    )
    write_surfsense_generated_secrets(
        state_dir,
        SurfSenseGeneratedSecrets(
            format_version=1,
            secrets={
                "secret_key": "SECRET_TEST_SURFSENSE_SECRET_KEY",
                "jwt_secret": "SECRET_TEST_SURFSENSE_JWT_SECRET",
                "db_password": "SECRET_TEST_SURFSENSE_DB_PASSWORD",
                "zero_admin_password": "SECRET_TEST_SURFSENSE_ZERO_PASSWORD",
                "searxng_secret": "SECRET_TEST_SURFSENSE_SEARXNG_SECRET",
            },
        ),
    )
    write_litellm_generated_keys(
        state_dir,
        LiteLLMGeneratedKeys(
            format_version=1,
            master_key="sk-litellm-master-secret",
            salt_key="litellm-salt-secret",
            virtual_keys={"surfsense": "sk-litellm-surfsense-secret"},
        ),
    )
    monkeypatch.setattr(cli, "build_live_drift_report", lambda **_: {"status": "clean"})

    assert (
        cli._handle_inspect_state(
            argparse.Namespace(env_file=env_file, state_dir=state_dir, dry_run=False)
        )
        == 0
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    surfsense_status = payload["surfsense_status"]
    public_surfaces = json.dumps(surfsense_status["public_surfaces"]).lower()

    assert surfsense_status["enabled"] is True
    assert surfsense_status["hostnames"] == {
        "frontend": "surfsense.example.com",
        "backend": "surfsense-api.example.com",
        "zero_cache": "surfsense-zero.example.com",
    }
    assert surfsense_status["public_surfaces"] == [
        {"name": "frontend", "url": "https://surfsense.example.com/"},
        {"name": "backend_ready", "url": "https://surfsense-api.example.com/ready"},
        {"name": "zero_keepalive", "url": "https://surfsense-zero.example.com/keepalive"},
    ]
    assert surfsense_status["internal_health_checks"] == [
        {"name": "searxng_healthz", "url": "http://searxng:8080/healthz", "public": False}
    ]
    assert surfsense_status["owned_resources"]["service"]["present"] is True
    assert surfsense_status["owned_resources"]["data"]["present"] is True
    assert surfsense_status["generated_runtime_values"] == [
        {"name": "secret_key", "present": True, "source": "surfsense-generated-secrets.json"},
        {"name": "jwt_secret", "present": True, "source": "surfsense-generated-secrets.json"},
        {"name": "db_password", "present": True, "source": "surfsense-generated-secrets.json"},
        {
            "name": "zero_admin_password",
            "present": True,
            "source": "surfsense-generated-secrets.json",
        },
        {"name": "searxng_secret", "present": True, "source": "surfsense-generated-secrets.json"},
    ]
    assert surfsense_status["litellm_consumer"] == {
        "name": "surfsense",
        "credential_kind": "virtual_key",
        "present": True,
        "source": "litellm-generated-keys.json:surfsense",
    }
    for forbidden in ("postgres", "redis", "searxng", "celery", "migrations"):
        assert forbidden not in public_surfaces
    for secret in (
        "super-secret-password",
        "dokploy-secret-key",
        "SECRET_TEST_OPENROUTER_PROVIDER_KEY",
        "SECRET_TEST_SURFSENSE_SECRET_KEY",
        "SECRET_TEST_SURFSENSE_JWT_SECRET",
        "SECRET_TEST_SURFSENSE_DB_PASSWORD",
        "SECRET_TEST_SURFSENSE_ZERO_PASSWORD",
        "SECRET_TEST_SURFSENSE_SEARXNG_SECRET",
        "sk-litellm-surfsense-secret",
    ):
        assert secret not in output


def test_seaweedfs_env_file_mode_generates_state_backed_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    install_env = tmp_path / "install.env"
    install_env.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_API_URL=https://dokploy.example.com/api",
                "DOKPLOY_API_KEY=dokploy-api-key",
                "PACKS=seaweedfs",
                "",
            ]
        ),
        encoding="utf-8",
    )
    original_install_env = install_env.read_text(encoding="utf-8")
    raw_env = parse_env_file(install_env)
    desired_state = resolve_desired_state(raw_env)

    monkeypatch.setattr(cli, "_build_dokploy_api_client", lambda **_: cast(Any, object()))

    first_backend = cli._build_seaweedfs_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
    )
    second_backend = cli._build_seaweedfs_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
    )

    first_credentials = first_backend.get_credentials()
    second_credentials = second_backend.get_credentials()

    assert desired_state.seaweedfs_access_key is None
    assert desired_state.seaweedfs_secret_key is None
    assert first_credentials is not None
    assert first_credentials == second_credentials
    assert first_credentials[0].startswith("seaweedfs-access-key-")
    assert first_credentials[1].startswith("seaweedfs-secret-key-")
    assert install_env.read_text(encoding="utf-8") == original_install_env
    assert (state_dir / SEAWEEDFS_GENERATED_SECRETS_FILE).stat().st_mode & 0o777 == 0o600


def test_inspect_state_reports_seaweedfs_generated_credentials_without_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "inspect-seaweedfs.env"
    state_dir = tmp_path / "state"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "PACKS=seaweedfs",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_seaweedfs_generated_secrets(
        state_dir,
        SeaweedFsGeneratedSecrets(
            format_version=1,
            access_key="SECRET_TEST_SEAWEEDFS_GENERATED_ACCESS",
            secret_key="SECRET_TEST_SEAWEEDFS_GENERATED_SECRET",
        ),
    )
    monkeypatch.setattr(cli, "build_live_drift_report", lambda **_: {"status": "clean"})

    assert (
        cli._handle_inspect_state(
            argparse.Namespace(env_file=env_file, state_dir=state_dir, dry_run=False)
        )
        == 0
    )

    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["seaweedfs_access_key"] == "<REDACTED>"
    assert payload["seaweedfs_secret_key"] == "<REDACTED>"
    assert payload["seaweedfs_status"] == {
        "display_name": "SeaweedFS",
        "enabled": True,
        "hostname": "s3.example.com",
        "credential_source": "generated-state",
        "generated_runtime_values": [
            {
                "name": "access_key",
                "present": True,
                "source": "seaweedfs-generated-secrets.json",
            },
            {
                "name": "secret_key",
                "present": True,
                "source": "seaweedfs-generated-secrets.json",
            },
        ],
    }
    assert "SECRET_TEST_SEAWEEDFS_GENERATED_ACCESS" not in output
    assert "SECRET_TEST_SEAWEEDFS_GENERATED_SECRET" not in output


def test_resolve_desired_state_accepts_litellm_routing_env_without_legacy_advisor_model_keys() -> None:
    raw_env = _raw_input(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_MY_FARM_ADVISOR": "true",
            "AI_DEFAULT_PROVIDER": "openrouter",
            "AI_DEFAULT_MODEL": "anthropic/claude-sonnet-4",
            "LITELLM_OPENROUTER_API_KEY": "<REDACTED>",
            "LITELLM_OPENROUTER_MODELS": "anthropic/claude-sonnet-4",
            "LITELLM_OPENCODE_GO_API_KEY": "<REDACTED>",
        }
    )

    desired_state = resolve_desired_state(raw_env)

    assert "my-farm-advisor" in desired_state.enabled_packs


def test_surfsense_litellm_model_uses_shared_consumer_allowlist_default() -> None:
    raw_env = _raw_input(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "surfsense",
            "AI_DEFAULT_PROVIDER": "local-model.internal",
            "AI_DEFAULT_MODEL": "unsloth-active",
            "LITELLM_LOCAL_BASE_URL": "http://local-model.internal:61434/v1",
            "LITELLM_LOCAL_MODEL": "unsloth-active",
            "LITELLM_LOCAL_API_KEY": "SECRET_TEST_LOCAL_PROVIDER_KEY",
            "LITELLM_OPENROUTER_API_KEY": "SECRET_TEST_OPENROUTER_PROVIDER_KEY",
            "LITELLM_OPENROUTER_MODELS": "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini",
        }
    )
    desired_state = resolve_desired_state(raw_env)

    model = cli._surfsense_litellm_model(
        raw_env=raw_env,
        shared_core_plan=desired_state.shared_core,
    )
    models = cli._surfsense_litellm_models(
        raw_env=raw_env,
        shared_core_plan=desired_state.shared_core,
    )

    assert model == "local-model.internal/unsloth-active"
    assert models == (
        "local-model.internal/unsloth-active",
        "openrouter/hunter-alpha",
    )


def test_install_env_examples_are_placeholder_only_and_use_openrouter_defaults() -> None:
    full_example_path = REPO_ROOT / ".install.env.example"
    farm_example_path = REPO_ROOT / ".install-my-farm-advisor.env.example"
    examples = {
        full_example_path: parse_env_file(full_example_path),
        farm_example_path: parse_env_file(farm_example_path),
    }

    assert examples[full_example_path].values["PACKS"] == (
        "nextcloud,openclaw,my-farm-advisor,seaweedfs,coder,docuseal,surfsense"
    )
    assert examples[farm_example_path].values["PACKS"] == "my-farm-advisor,nextcloud,seaweedfs"
    assert examples[full_example_path].values["SURFSENSE_SUBDOMAIN"] == "surfsense"
    assert examples[full_example_path].values["SURFSENSE_API_SUBDOMAIN"] == "surfsense-api"
    assert examples[full_example_path].values["SURFSENSE_ZERO_SUBDOMAIN"] == "surfsense-zero"
    assert examples[full_example_path].values["SURFSENSE_VERSION"] == "0.0.25"
    assert examples[full_example_path].values["LITELLM_OPENROUTER_MODELS"] == examples[
        farm_example_path
    ].values["LITELLM_OPENROUTER_MODELS"]
    install_min_path = REPO_ROOT / ".install-min.env"
    if install_min_path.exists():
        assert examples[full_example_path].values["LITELLM_OPENROUTER_MODELS"] == parse_env_file(
            install_min_path
        ).values["LITELLM_OPENROUTER_MODELS"]

    for parsed in examples.values():
        assert parsed.values["AI_DEFAULT_PROVIDER"] == "openrouter"
        assert parsed.values["AI_DEFAULT_MODEL"] == "deepseek/deepseek-v4-flash:free"
        assert "ENABLE_MY_FARM_ADVISOR" not in parsed.values
        resolve_desired_state(parsed)

    shared_optional_commented_keys = (
        "DOCKER_USERNAME",
        "DOCKER_PAT",
        "ENABLE_TAILSCALE",
        "TAILSCALE_AUTH_KEY",
        "TAILSCALE_HOSTNAME",
        "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN",
        "TELEGRAM_DATA_PIPELINE_BOT_TOKEN",
        "R2_BUCKET_NAME",
        "R2_ENDPOINT",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "CF_ACCOUNT_ID",
        "LITELLM_LOCAL_BASE_URL",
        "LITELLM_LOCAL_MODEL",
        "LITELLM_LOCAL_API_KEY",
        "LITELLM_OPENCODE_GO_API_KEY",
        "LITELLM_OPENCODE_GO_WILDCARD",
        "LITELLM_NVIDIA_API_KEY",
        "NVIDIA_BASE_URL",
    )
    optional_commented_keys_by_example = {
        full_example_path: ("OPENCLAW_TELEGRAM_BOT_TOKEN", *shared_optional_commented_keys),
        farm_example_path: shared_optional_commented_keys,
    }
    for example_env_path, optional_commented_keys in optional_commented_keys_by_example.items():
        parsed = examples[example_env_path]
        example_env = example_env_path.read_text(encoding="utf-8")
        for key in optional_commented_keys:
            assert f"# {key}=" in example_env
            assert key not in parsed.values

    generated_secret_keys = (
        "DOKPLOY_API_KEY",
        "DOKPLOY_PASSWORD",
        "SEAWEEDFS_ACCESS_KEY",
        "SEAWEEDFS_SECRET_KEY",
        "OPENCLAW_GATEWAY_PASSWORD",
        "OPENCLAW_GATEWAY_TOKEN",
        "MY_FARM_ADVISOR_GATEWAY_PASSWORD",
        "SURFSENSE_SECRET_KEY",
        "SURFSENSE_JWT_SECRET",
        "SURFSENSE_DB_PASSWORD",
        "SURFSENSE_ZERO_ADMIN_PASSWORD",
        "SURFSENSE_SEARXNG_SECRET",
        "SURFSENSE_LITELLM_VIRTUAL_KEY",
    )
    for key in generated_secret_keys:
        for example_env_path, parsed in examples.items():
            example_env = example_env_path.read_text(encoding="utf-8")
            assert key in example_env
            assert key not in parsed.values

    live_secret_patterns = (
        r"sk-[A-Za-z0-9_-]{8,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----",
    )
    forbidden_literals = [
        "Global API Key=<",
        "".join(chr(code) for code in (116, 117, 120, 100, 101, 115, 107, 116, 111, 112, 46, 116, 97, 105, 108, 98, 49, 50, 97, 97, 53, 46, 116, 115, 46, 110, 101, 116)),
        "local-model.internal",
    ]
    install_min_values = parse_env_file(REPO_ROOT / ".install-min.env").values
    for key in ("ROOT_DOMAIN", "TAILSCALE_HOSTNAME"):
        private_value = install_min_values.get(key, "")
        if private_value and private_value != "example.com":
            forbidden_literals.append(private_value)
    private_local_base_url = install_min_values.get("LITELLM_LOCAL_BASE_URL", "")
    if private_local_base_url:
        forbidden_literals.append(private_local_base_url)
    for example_env_path in examples:
        example_env = example_env_path.read_text(encoding="utf-8")
        for pattern in live_secret_patterns:
            assert re.search(pattern, example_env) is None
        for literal in forbidden_literals:
            assert literal not in example_env

    placeholder_keys = (
        "CLOUDFLARE_API_TOKEN",
        "DOKPLOY_ADMIN_PASSWORD",
        "LITELLM_OPENROUTER_API_KEY",
    )
    for key in placeholder_keys:
        for parsed in examples.values():
            value = parsed.values[key]
            assert value.startswith("<") and value.endswith(">")


def test_inspect_state_reports_both_advisors_with_user_visible_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "inspect-both.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_SUBDOMAIN=dokploy",
                "DOKPLOY_ADMIN_EMAIL=operator@example.com",
                "CLOUDFLARE_API_TOKEN=cf-token",
                "CLOUDFLARE_ACCOUNT_ID=account-123",
                "PACKS=openclaw,my-farm-advisor",
                "AI_DEFAULT_API_KEY=shared-key",
                "AI_DEFAULT_BASE_URL=https://models.example.com/v1",
                "MY_FARM_ADVISOR_PRIMARY_MODEL=anthropic/claude-sonnet-4",
                "OPENCLAW_NEXA_AGENT_PASSWORD=nexa-password",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "build_live_drift_report", lambda **_: {"status": "clean"})

    assert (
        cli._handle_inspect_state(
            argparse.Namespace(env_file=env_file, state_dir=tmp_path / "state", dry_run=True)
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["advisor_status"]["openclaw"]["display_name"] == "Nexa Claw"
    assert payload["advisor_status"]["openclaw"]["workspace_mount_name"] == "/OpenClaw"
    assert payload["advisor_status"]["my_farm_advisor"]["display_name"] == "Nexa Farm"
    assert payload["advisor_status"]["my_farm_advisor"]["enabled"] is True


def test_build_live_drift_report_classifies_required_collision_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            **parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env").values,
            "STACK_NAME": "openmerge",
            "ROOT_DOMAIN": "openmerge.me",
            "PACKS": "my-farm-advisor,nextcloud,openclaw,coder,seaweedfs",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
            "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    ownership_ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                "openclaw_service",
                "svc-openclaw",
                f"stack:{desired_state.stack_name}:openclaw",
            ),
        ),
    )
    route_file = tmp_path / "my-farm-advisor.yaml"
    route_file.write_text(
        "http:\n"
        "  routers:\n"
        "    farm:\n"
        f"      rule: Host(`{desired_state.hostnames['my-farm-advisor']}`)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(inspection_module, "_docker_cli_available", lambda: True)
    monkeypatch.setattr(
        inspection_module,
        "_list_docker_services",
        lambda: (f"{desired_state.stack_name}-openclaw",),
    )
    monkeypatch.setattr(
        inspection_module,
        "_list_docker_containers",
        lambda: (
            {"name": "openclaw-manual", "status": "Up 5 minutes"},
            {
                "name": f"{desired_state.stack_name}-my-farm-advisor",
                "status": "Exited (1) 10 seconds ago",
            },
            {"name": f"{desired_state.stack_name}-helper", "status": "Up 1 minute"},
        ),
    )
    monkeypatch.setattr(
        inspection_module,
        "_list_service_task_statuses",
        lambda service_name: ("Exited (1) 5 seconds ago",)
        if service_name == f"{desired_state.stack_name}-openclaw"
        else (),
    )
    monkeypatch.setattr(inspection_module, "_ROUTE_SEARCH_DIRS", (tmp_path,))

    report = inspection_module.build_live_drift_report(
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
    )

    classifications = {entry["classification"] for entry in report["entries"]}
    assert report["summary"]["wizard_managed"] == 1
    assert report["summary"]["manual_collision"] >= 2
    assert report["summary"]["host_local_route"] == 1
    assert report["summary"]["unknown_unmanaged"] == 1
    assert classifications == {
        "wizard_managed",
        "manual_collision",
        "host_local_route",
        "unknown_unmanaged",
    }
    wizard_entry = next(
        entry for entry in report["entries"] if entry["classification"] == "wizard_managed"
    )
    assert wizard_entry["pack"] == "openclaw"
    assert wizard_entry["health"] == "unhealthy"
    manual_entries = [
        entry for entry in report["entries"] if entry["classification"] == "manual_collision"
    ]
    assert any(entry["live_name"] == "openclaw-manual" for entry in manual_entries)
    assert any(
        entry["live_name"] == f"{desired_state.stack_name}-my-farm-advisor"
        for entry in manual_entries
    )
    route_entry = next(
        entry for entry in report["entries"] if entry["classification"] == "host_local_route"
    )
    assert route_entry["pack"] == "my-farm-advisor"
    unknown_entry = next(
        entry for entry in report["entries"] if entry["classification"] == "unknown_unmanaged"
    )
    assert unknown_entry["live_name"] == f"{desired_state.stack_name}-helper"


def test_build_live_drift_report_recognizes_label_backed_managed_compose_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            **parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env").values,
            "STACK_NAME": "openmerge",
            "ROOT_DOMAIN": "openmerge.me",
            "PACKS": "my-farm-advisor,nextcloud,openclaw,coder,seaweedfs",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
            "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    ownership_ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                "openclaw_service",
                "svc-openclaw",
                f"stack:{desired_state.stack_name}:openclaw",
            ),
            OwnedResource(
                "openclaw_service",
                "svc-my-farm",
                f"stack:{desired_state.stack_name}:my-farm-advisor",
            ),
            OwnedResource(
                "onlyoffice_service",
                "svc-onlyoffice",
                f"stack:{desired_state.stack_name}:onlyoffice-service",
            ),
            OwnedResource(
                "nextcloud_service",
                "svc-nextcloud",
                f"stack:{desired_state.stack_name}:nextcloud-service",
            ),
            OwnedResource(
                "coder_service",
                "svc-coder",
                f"stack:{desired_state.stack_name}:coder:service",
            ),
            OwnedResource(
                "seaweedfs_service",
                "svc-seaweedfs",
                f"stack:{desired_state.stack_name}:seaweedfs-service",
            ),
            OwnedResource(
                "shared_core_litellm",
                "svc-shared-litellm",
                f"stack:{desired_state.stack_name}:shared-litellm",
            ),
            OwnedResource(
                "shared_core_postgres",
                "svc-shared-postgres",
                f"stack:{desired_state.stack_name}:shared-postgres",
            ),
            OwnedResource(
                "shared_core_redis",
                "svc-shared-redis",
                f"stack:{desired_state.stack_name}:shared-redis",
            ),
            OwnedResource(
                "openclaw_mem0_service",
                "svc-mem0",
                f"stack:{desired_state.stack_name}:openclaw-sidecar:mem0",
            ),
            OwnedResource(
                "openclaw_qdrant_service",
                "svc-qdrant",
                f"stack:{desired_state.stack_name}:openclaw-sidecar:qdrant",
            ),
            OwnedResource(
                "openclaw_runtime_service",
                "svc-runtime",
                f"stack:{desired_state.stack_name}:openclaw-sidecar:nexa-runtime",
            ),
        ),
    )
    my_farm_container = "openmerge-my-farm-advisor-jy6axb-openmerge-my-farm-advisor-1"
    onlyoffice_container = "openmerge-nextcloud-a5izk5-openmerge-onlyoffice-1"
    nextcloud_container = "openmerge-nextcloud-a5izk5-openmerge-nextcloud-1"
    openclaw_internal_container = "openmerge-openclaw-3f2eds-openmerge-openclaw-1"
    openclaw_public_container = "openmerge-openclaw-3f2eds-openmerge-openclaw-public-1"
    mem0_container = "openmerge-openclaw-3f2eds-mem0-1"
    qdrant_container = "openmerge-openclaw-3f2eds-qdrant-1"
    runtime_container = "openmerge-openclaw-3f2eds-nexa-runtime-1"
    coder_container = "openmerge-coder-8do2ol-openmerge-coder-1"
    seaweedfs_container = "openmerge-seaweedfs-cr6zrs-openmerge-seaweedfs-1"
    shared_postgres_container = "openmerge-shared-nojgtz-openmerge-shared-postgres-1"
    shared_redis_container = "openmerge-shared-nojgtz-openmerge-shared-redis-1"
    cloudflared_container = "openmerge-cloudflared-ghphch-openmerge-cloudflared-1"
    auth_probe_container = "openmerge-dokploy-wizard-auth-probe-ocm4ux-auth-probe-1"
    coder_workspace_container = "coder-clayton-openmergeme-workspace-2026-04-21"
    coder_named_template_container = "coder-clayton-openwork"
    my_farm_hostname = desired_state.hostnames["my-farm-advisor"]
    onlyoffice_hostname = desired_state.hostnames["onlyoffice"]

    monkeypatch.setattr(inspection_module, "_docker_cli_available", lambda: True)
    monkeypatch.setattr(inspection_module, "_list_docker_services", lambda: ())
    monkeypatch.setattr(
        inspection_module,
        "_list_docker_containers",
        lambda: (
            {
                "name": my_farm_container,
                "status": "Up 8 seconds (health: starting)",
                "labels": {
                    "dokploy-wizard.slot": "my-farm-advisor_suite",
                    "dokploy-wizard.variant": "my-farm-advisor",
                    "traefik.http.routers.openmerge-my-farm-advisor.rule": (
                        f"Host(`{my_farm_hostname}`)"
                    ),
                    "traefik.http.services.openmerge-my-farm-advisor.loadbalancer.server.port": (
                        "18789"
                    ),
                },
            },
            {
                "name": nextcloud_container,
                "status": "Up 21 hours (healthy)",
                "labels": {
                    "com.docker.compose.service": "openmerge-nextcloud",
                    "com.docker.compose.project": "openmerge-nextcloud-a5izk5",
                },
            },
            {
                "name": onlyoffice_container,
                "status": "Up 21 hours (healthy)",
                "labels": {
                    "com.docker.compose.service": "openmerge-onlyoffice",
                    "com.docker.compose.project": "openmerge-nextcloud-a5izk5",
                    "traefik.http.routers.openmerge-onlyoffice.rule": (
                        f"Host(`{onlyoffice_hostname}`)"
                    ),
                    "traefik.http.services.openmerge-onlyoffice.loadbalancer.server.port": "80",
                },
            },
            {
                "name": openclaw_internal_container,
                "status": "Up 2 minutes (healthy)",
                "labels": {
                    "com.docker.compose.service": "openmerge-openclaw",
                    "com.docker.compose.project": "openmerge-openclaw-3f2eds",
                },
            },
            {
                "name": openclaw_public_container,
                "status": "Up 2 minutes (healthy)",
                "labels": {
                    "com.docker.compose.service": "openmerge-openclaw-public",
                    "com.docker.compose.project": "openmerge-openclaw-3f2eds",
                },
            },
            {
                "name": coder_container,
                "status": "Up 2 minutes (healthy)",
                "labels": {
                    "com.docker.compose.service": "openmerge-coder",
                    "com.docker.compose.project": "openmerge-coder-8do2ol",
                },
            },
            {
                "name": seaweedfs_container,
                "status": "Up 2 minutes (healthy)",
                "labels": {
                    "com.docker.compose.service": "openmerge-seaweedfs",
                    "com.docker.compose.project": "openmerge-seaweedfs-cr6zrs",
                },
            },
            {
                "name": shared_postgres_container,
                "status": "Up 2 minutes",
                "labels": {
                    "com.docker.compose.service": "openmerge-shared-postgres",
                    "com.docker.compose.project": "openmerge-shared-nojgtz",
                },
            },
            {
                "name": shared_redis_container,
                "status": "Up 2 minutes",
                "labels": {
                    "com.docker.compose.service": "openmerge-shared-redis",
                    "com.docker.compose.project": "openmerge-shared-nojgtz",
                },
            },
            {
                "name": mem0_container,
                "status": "Up 2 minutes (healthy)",
                "labels": {
                    "com.docker.compose.service": "mem0",
                    "com.docker.compose.project": "openmerge-openclaw-3f2eds",
                },
            },
            {
                "name": qdrant_container,
                "status": "Up 2 minutes",
                "labels": {
                    "com.docker.compose.service": "qdrant",
                    "com.docker.compose.project": "openmerge-openclaw-3f2eds",
                },
            },
            {
                "name": runtime_container,
                "status": "Up 2 minutes (healthy)",
                "labels": {
                    "com.docker.compose.service": "nexa-runtime",
                    "com.docker.compose.project": "openmerge-openclaw-3f2eds",
                },
            },
            {
                "name": cloudflared_container,
                "status": "Up 2 minutes",
                "labels": {
                    "com.docker.compose.service": "openmerge-cloudflared",
                    "com.docker.compose.project": "openmerge-cloudflared-ghphch",
                },
            },
            {
                "name": auth_probe_container,
                "status": "Up 2 minutes",
                "labels": {},
            },
            {
                "name": coder_workspace_container,
                "status": "Up 2 minutes",
                "labels": {},
            },
            {
                "name": coder_named_template_container,
                "status": "Up 2 minutes",
                "labels": {},
            },
        ),
    )
    monkeypatch.setattr(inspection_module, "_ROUTE_SEARCH_DIRS", ())

    report = inspection_module.build_live_drift_report(
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
    )

    wizard_entries = [
        entry for entry in report["entries"] if entry["classification"] == "wizard_managed"
    ]
    manual_entries = [
        entry for entry in report["entries"] if entry["classification"] == "manual_collision"
    ]
    unknown_entries = [entry for entry in report["entries"] if entry["classification"] == "unknown"]

    assert any(
        entry["pack"] == "my-farm-advisor"
        and entry["live_kind"] == "container"
        and entry["live_name"] == my_farm_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "nextcloud"
        and entry["live_kind"] == "container"
        and entry["live_name"] == nextcloud_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "onlyoffice"
        and entry["live_kind"] == "container"
        and entry["live_name"] == onlyoffice_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "coder"
        and entry["live_kind"] == "container"
        and entry["live_name"] == coder_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "seaweedfs"
        and entry["live_kind"] == "container"
        and entry["live_name"] == seaweedfs_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "shared-core"
        and entry["live_kind"] == "container"
        and entry["live_name"] == shared_postgres_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "shared-core"
        and entry["live_kind"] == "container"
        and entry["live_name"] == shared_redis_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "openclaw"
        and entry["live_kind"] == "container"
        and entry["live_name"] == openclaw_internal_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "openclaw"
        and entry["live_kind"] == "container"
        and entry["live_name"] == mem0_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "openclaw"
        and entry["live_kind"] == "container"
        and entry["live_name"] == qdrant_container
        for entry in wizard_entries
    )
    assert any(
        entry["pack"] == "openclaw"
        and entry["live_kind"] == "container"
        and entry["live_name"] == runtime_container
        for entry in wizard_entries
    )
    assert not any(entry["live_name"] == my_farm_container for entry in manual_entries)
    assert not any(entry["live_name"] == onlyoffice_container for entry in manual_entries)
    assert not any(entry["live_name"] == mem0_container for entry in manual_entries)
    assert not any(entry["live_name"] == qdrant_container for entry in manual_entries)
    assert not any(entry["live_name"] == runtime_container for entry in manual_entries)
    assert not any(entry["live_name"] == openclaw_internal_container for entry in manual_entries)
    assert not any(entry["live_name"] == openclaw_public_container for entry in manual_entries)
    assert not any(entry["live_name"] == cloudflared_container for entry in manual_entries)
    assert not any(entry["live_name"] == auth_probe_container for entry in manual_entries)
    assert not any(entry["live_name"] == coder_workspace_container for entry in manual_entries)
    assert not any(entry["live_name"] == coder_named_template_container for entry in manual_entries)
    assert not any(entry["live_name"] == runtime_container for entry in unknown_entries)
    assert report["summary"]["wizard_managed"] == 12
    assert report["summary"]["manual_collision"] == 0


def test_build_live_drift_report_does_not_match_seaweedfs_alias_by_random_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            **parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env").values,
            "STACK_NAME": "openmerge",
            "ROOT_DOMAIN": "openmerge.me",
            "PACKS": "seaweedfs",
            "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    ownership_ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                "seaweedfs_service",
                "svc-seaweedfs",
                f"stack:{desired_state.stack_name}:seaweedfs-service",
            ),
        ),
    )

    monkeypatch.setattr(inspection_module, "_docker_cli_available", lambda: True)
    monkeypatch.setattr(inspection_module, "_list_docker_services", lambda: ())
    monkeypatch.setattr(
        inspection_module,
        "_list_docker_containers",
        lambda: (
            {
                "name": "dokploy-redis.1.k28bbjjkc7u0s36hc5tq8lhlz",
                "status": "Up 35 minutes",
                "labels": {
                    "com.docker.swarm.service.name": "dokploy-redis",
                },
            },
            {
                "name": "openmerge-seaweedfs-good123-openmerge-seaweedfs-1",
                "status": "Up 5 minutes (healthy)",
                "labels": {
                    "com.docker.compose.service": f"{desired_state.stack_name}-seaweedfs",
                },
            },
        ),
    )
    monkeypatch.setattr(
        inspection_module,
        "_list_service_task_statuses",
        lambda service_name: () if service_name == f"{desired_state.stack_name}-seaweedfs" else (),
    )
    monkeypatch.setattr(inspection_module, "_ROUTE_SEARCH_DIRS", ())

    report = inspection_module.build_live_drift_report(
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
    )

    manual_entries = [
        entry for entry in report["entries"] if entry["classification"] == "manual_collision"
    ]
    assert not any(
        entry["live_name"] == "dokploy-redis.1.k28bbjjkc7u0s36hc5tq8lhlz"
        for entry in manual_entries
    )


def test_build_live_drift_report_recognizes_surfsense_compose_label_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            **parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env").values,
            "STACK_NAME": "openmerge",
            "ROOT_DOMAIN": "openmerge.me",
            "PACKS": "surfsense",
            "LITELLM_OPENROUTER_API_KEY": "shared-key",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    ownership_ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                "surfsense_service",
                "svc-surfsense",
                f"stack:{desired_state.stack_name}:surfsense:service",
            ),
        ),
    )
    surfsense_project = "openmerge-surfsense-mzch4i"
    surfsense_containers = tuple(
        {
            "name": f"{surfsense_project}-{service}-1",
            "status": "Up 2 minutes (healthy)",
            "labels": {
                "com.docker.compose.project": surfsense_project,
                "com.docker.compose.service": service,
            },
        }
        for service in (
            "backend",
            "frontend",
            "zero-cache",
            "searxng",
            "celery_worker",
            "celery_beat",
            "migrations",
        )
    )
    unrelated_stack_container = "openmerge-surfsense-mzch4i-debug-shell-1"

    monkeypatch.setattr(inspection_module, "_docker_cli_available", lambda: True)
    monkeypatch.setattr(inspection_module, "_list_docker_services", lambda: ())
    monkeypatch.setattr(
        inspection_module,
        "_list_docker_containers",
        lambda: (
            *surfsense_containers,
            {
                "name": unrelated_stack_container,
                "status": "Up 2 minutes",
                "labels": {
                    "com.docker.compose.project": surfsense_project,
                    "com.docker.compose.service": "debug-shell",
                },
            },
        ),
    )
    monkeypatch.setattr(inspection_module, "_ROUTE_SEARCH_DIRS", ())

    report = inspection_module.build_live_drift_report(
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
    )

    wizard_entries = [
        entry for entry in report["entries"] if entry["classification"] == "wizard_managed"
    ]
    unknown_entries = [
        entry
        for entry in report["entries"]
        if entry["classification"] == "unknown_unmanaged"
    ]
    manual_entries = [
        entry for entry in report["entries"] if entry["classification"] == "manual_collision"
    ]

    assert any(
        entry["pack"] == "surfsense"
        and entry["scope"] == f"stack:{desired_state.stack_name}:surfsense:service"
        and entry["live_kind"] == "container"
        and entry["live_name"] == f"{surfsense_project}-backend-1"
        for entry in wizard_entries
    )
    for container in surfsense_containers:
        assert not any(entry["live_name"] == container["name"] for entry in unknown_entries)
        assert not any(entry["live_name"] == container["name"] for entry in manual_entries)
    assert [entry["live_name"] for entry in unknown_entries] == [unrelated_stack_container]


def test_install_help_lists_task_three_flags() -> None:
    result = run_cli("install", "--help")

    assert result.returncode == 0
    assert "--env-file" in result.stdout
    assert "guided first-run install" in result.stdout
    assert "sensitive install.env operator file" in result.stdout
    assert "--no-print-secrets" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--non-interactive" in result.stdout
    assert "--allow-memory-shortfall" in result.stdout
    assert result.stderr == ""


def test_guided_install_prompts_include_dokploy_guidance() -> None:
    prompts: list[str] = []
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "secret-123",
            "",
            "n",
            "cf-token",
            "account-123",
            "",
            "",
        ]
    )

    def fake_prompt(message: str) -> str:
        prompts.append(message)
        return next(responses)

    values = prompt_for_initial_install_values(fake_prompt)

    assert values.stack_name == "example-com"
    assert values.dokploy_subdomain == "dokploy"
    assert values.dokploy_admin_email == "clayton@superiorbyteworks.com"
    assert values.dokploy_admin_password == "secret-123"
    assert values.enable_headscale is True
    assert values.enable_tailscale is False
    combined = "\n".join(prompts)
    assert "Dokploy subdomain" in combined
    assert "create the first admin and mint an API key" in combined
    assert "Private network mode" in combined
    assert "Need help finding your Cloudflare token" in combined
    assert "Cloudflare zone ID (optional; press Enter to look up from example.com)" in combined
    assert "Default AI API key for Hermes, K-Dense BYOK, and advisor backup models" in combined
    assert "Tailscale auth key" not in combined


def test_guided_install_defaults_dokploy_admin_password_to_change_me_soon() -> None:
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "",
            "",
            "n",
            "cf-token",
            "account-123",
            "",
            "",
        ]
    )

    values = prompt_for_initial_install_values(lambda _: next(responses))

    assert values.dokploy_admin_password == "ChangeMeSoon"


def test_install_parser_allows_missing_env_file() -> None:
    parser = cli.build_parser()

    args = parser.parse_args(["install"])

    assert args.env_file is None


def test_install_without_env_file_fails_cleanly_in_non_interactive_mode(tmp_path: Path) -> None:
    args = argparse.Namespace(
        env_file=None,
        state_dir=tmp_path / "state",
        dry_run=False,
        non_interactive=True,
        no_print_secrets=False,
    )

    with pytest.raises(SystemExit, match="--env-file is required when --non-interactive"):
        cli._handle_install(args)


def test_guided_install_writes_env_file_and_runs_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    custom_state_dir = tmp_path / "custom-state"

    def fake_run_install_flow(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt_for_guided_state_dir", lambda _: custom_state_dir)
    monkeypatch.setattr(
        cli,
        "prompt_for_initial_install_values",
        lambda **kwargs: GuidedInstallValues(
            stack_name="guided-stack",
            root_domain="example.com",
            dokploy_subdomain="dokploy",
            dokploy_admin_email="clayton@superiorbyteworks.com",
            dokploy_admin_password="secret-123",
            ai_default_api_key="ai-key-123",
            ai_default_base_url="https://opencode.ai/zen/go/v1",
            enable_headscale=True,
            cloudflare_api_token="token-123",
            cloudflare_account_id="account-123",
            cloudflare_zone_id=None,
            enable_tailscale=False,
            tailscale_auth_key=None,
            tailscale_hostname=None,
            tailscale_enable_ssh=False,
            tailscale_tags=(),
            tailscale_subnet_routes=(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "prompt_for_pack_selection",
        lambda **kwargs: PromptSelection(
            selected_packs=("openclaw",),
            disabled_packs=(),
            seaweedfs_access_key=None,
            seaweedfs_secret_key=None,
            generated_secrets={},
            advisor_env={},
            openclaw_channels=("telegram",),
            my_farm_advisor_channels=(),
        ),
    )
    monkeypatch.setattr(cli, "run_install_flow", fake_run_install_flow)

    args = argparse.Namespace(
        env_file=None,
        state_dir=tmp_path / "state",
        dry_run=False,
        non_interactive=False,
        no_print_secrets=False,
    )

    assert cli._handle_install(args) == 0
    env_file = custom_state_dir / "install.env"
    assert env_file.exists()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    env_contents = env_file.read_text(encoding="utf-8")
    assert "STACK_NAME=guided-stack" in env_contents
    assert "AI_DEFAULT_API_KEY=ai-key-123" in env_contents
    assert "AI_DEFAULT_BASE_URL=https://opencode.ai/zen/go/v1" in env_contents
    assert "ROOT_DOMAIN=example.com" in env_contents
    assert "DOKPLOY_SUBDOMAIN=dokploy" in env_contents
    assert "DOKPLOY_ADMIN_EMAIL=clayton@superiorbyteworks.com" in env_contents
    assert "DOKPLOY_ADMIN_PASSWORD=secret-123" in env_contents
    assert "ENABLE_HEADSCALE=true" in env_contents
    assert "CLOUDFLARE_API_TOKEN=token-123" in env_contents
    assert "CLOUDFLARE_ZONE_ID" not in env_contents
    assert "PACKS=openclaw" in env_contents
    assert "OPENCLAW_CHANNELS=telegram" in env_contents
    assert "CLOUDFLARE_ACCESS_OTP_EMAILS=clayton@superiorbyteworks.com" in env_contents
    assert "OPENCLAW_GATEWAY_TOKEN=" not in env_contents
    assert captured["env_file"] == env_file
    assert captured["state_dir"] == custom_state_dir
    assert captured["dry_run"] is False
    raw_env = captured["raw_env"]
    assert isinstance(raw_env, RawEnvInput)
    assert raw_env.values["STACK_NAME"] == "guided-stack"
    assert raw_env.values["DOKPLOY_SUBDOMAIN"] == "dokploy"


def test_guided_install_reuses_existing_seaweedfs_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    custom_state_dir = tmp_path / "custom-state"
    custom_state_dir.mkdir(parents=True)
    existing_env_file = custom_state_dir / "install.env"
    existing_env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=guided-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_SUBDOMAIN=dokploy",
                "DOKPLOY_ADMIN_EMAIL=admin@example.com",
                "DOKPLOY_ADMIN_PASSWORD=secret-123",
                "ENABLE_HEADSCALE=true",
                "CLOUDFLARE_API_TOKEN=token-123",
                "CLOUDFLARE_ACCOUNT_ID=account-123",
                "PACKS=seaweedfs",
                "SEAWEEDFS_ACCESS_KEY=seaweed-existing",
                "SEAWEEDFS_SECRET_KEY=seaweed-secret-existing",
                "",
            ]
        ),
        encoding="utf-8",
    )

    def fake_run_install_flow(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt_for_guided_state_dir", lambda _: custom_state_dir)
    monkeypatch.setattr(
        cli,
        "prompt_for_initial_install_values",
        lambda **kwargs: GuidedInstallValues(
            stack_name="guided-stack",
            root_domain="example.com",
            dokploy_subdomain="dokploy",
            dokploy_admin_email="clayton@superiorbyteworks.com",
            dokploy_admin_password="secret-123",
            ai_default_api_key=None,
            ai_default_base_url=None,
            enable_headscale=True,
            cloudflare_api_token="token-123",
            cloudflare_account_id="account-123",
            cloudflare_zone_id=None,
            enable_tailscale=False,
            tailscale_auth_key=None,
            tailscale_hostname=None,
            tailscale_enable_ssh=False,
            tailscale_tags=(),
            tailscale_subnet_routes=(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "prompt_for_pack_selection",
        lambda **kwargs: PromptSelection(
            selected_packs=("seaweedfs",),
            disabled_packs=(),
            seaweedfs_access_key="seaweed-new",
            seaweedfs_secret_key="seaweed-secret-new",
            generated_secrets={
                "SEAWEEDFS_ACCESS_KEY": "seaweed-new",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret-new",
            },
            advisor_env={},
            openclaw_channels=(),
            my_farm_advisor_channels=(),
        ),
    )
    monkeypatch.setattr(cli, "run_install_flow", fake_run_install_flow)

    args = argparse.Namespace(
        env_file=None,
        state_dir=tmp_path / "state",
        dry_run=False,
        non_interactive=False,
        no_print_secrets=False,
        allow_memory_shortfall=False,
    )

    assert cli._handle_install(args) == 0
    env_contents = existing_env_file.read_text(encoding="utf-8")
    assert "SEAWEEDFS_ACCESS_KEY=seaweed-existing" in env_contents
    assert "SEAWEEDFS_SECRET_KEY=seaweed-secret-existing" in env_contents
    raw_env = captured["raw_env"]
    assert isinstance(raw_env, RawEnvInput)
    assert raw_env.values["SEAWEEDFS_ACCESS_KEY"] == "seaweed-existing"
    assert raw_env.values["SEAWEEDFS_SECRET_KEY"] == "seaweed-secret-existing"


def test_build_coder_backend_uses_litellm_virtual_key_for_hermes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "coder",
            "DOKPLOY_API_URL": "https://dokploy.example.com/api",
            "DOKPLOY_API_KEY": "dokploy-api-key",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "ChangeMeSoon",
            "AI_DEFAULT_API_KEY": "upstream-shared-key",
            "AI_DEFAULT_BASE_URL": "https://upstream.example.invalid/v1",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    state_dir = tmp_path / "state"
    generated_keys = ensure_litellm_generated_keys(state_dir)
    captured: dict[str, object] = {}
    sentinel_client = object()

    monkeypatch.setattr(cli, "_build_dokploy_api_client", lambda **kwargs: sentinel_client)
    monkeypatch.setattr(
        cli,
        "DokployCoderBackend",
        lambda **kwargs: captured.update(kwargs) or cast(Any, object()),
    )

    backend = cli._build_coder_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
    )

    assert backend is not None
    assert captured["ai_default_api_key"] == generated_keys.virtual_keys["coder-hermes"]
    assert captured["ai_default_api_key"] != raw_env.values["AI_DEFAULT_API_KEY"]
    assert captured["client"] is sentinel_client


def test_build_openclaw_backend_uses_generated_litellm_virtual_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "openclaw,my-farm-advisor",
            "DOKPLOY_API_URL": "https://dokploy.example.com/api",
            "DOKPLOY_API_KEY": "dokploy-api-key",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "ChangeMeSoon",
            "OPENCLAW_CHANNELS": "telegram",
            "MY_FARM_ADVISOR_CHANNELS": "telegram",
            "AI_DEFAULT_API_KEY": "upstream-shared-key",
            "AI_DEFAULT_BASE_URL": "https://upstream.example.invalid/v1",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    state_dir = tmp_path / "state"
    generated_keys = ensure_litellm_generated_keys(state_dir)
    captured: dict[str, object] = {}
    sentinel_client = object()

    monkeypatch.setattr(cli, "_build_dokploy_api_client", lambda **kwargs: sentinel_client)
    monkeypatch.setattr(
        cli,
        "DokployOpenClawBackend",
        lambda **kwargs: captured.update(kwargs) or cast(Any, object()),
    )

    backend = cli._build_openclaw_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
        litellm_generated_keys=generated_keys,
    )
    captured_keys = cast(Any, captured["litellm_generated_keys"])

    assert backend is not None
    assert captured["litellm_generated_keys"] is generated_keys
    assert captured_keys.virtual_keys["openclaw"] == generated_keys.virtual_keys["openclaw"]
    assert captured_keys.virtual_keys["my-farm-advisor"] == generated_keys.virtual_keys["my-farm-advisor"]
    assert captured["client"] is sentinel_client
    assert captured["state_dir"] == state_dir


def test_install_persists_post_auth_target_before_later_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
            "ENABLE_HEADSCALE": "true",
            "CLOUDFLARE_API_TOKEN": "token-123",
            "CLOUDFLARE_ACCOUNT_ID": "account-123",
        },
    )
    state_dir = tmp_path / "state"

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3000"

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            return DokployBootstrapAuthResult(
                api_key="dokp-key-123",
                api_url="http://127.0.0.1:3000",
                admin_email=admin_email,
                organization_id="org-1",
                used_sign_up=False,
                auth_path="/api/auth/sign-in/email",
                session_path="/api/user.session",
            )

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            del package_name
            return True

        def docker_daemon_reachable(self) -> bool:
            return True

    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)
    monkeypatch.setattr(
        cli,
        "DokployApiClient",
        lambda *, api_url, api_key, **kwargs: type(
            "_ValidDokployClient",
            (),
            {
                "__init__": lambda self: None,
                "list_projects": lambda self: (),
                "ai_providers_all": lambda self: (),
            },
        )(),
    )
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts())
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda desired_state, host_facts, *, allow_memory_shortfall=False: PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
    )
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_qualify_dokploy_mutation_auth", lambda **_: None)
    monkeypatch.setattr(
        cli,
        "validate_preserved_phases",
        lambda **_: (_ for _ in ()).throw(StateValidationError("post-auth failure")),
    )

    with pytest.raises(StateValidationError, match="post-auth failure"):
        cli.run_install_flow(
            env_file=env_file,
            state_dir=state_dir,
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
        )

    loaded_state = load_state_dir(state_dir)
    assert loaded_state.raw_input is not None
    assert loaded_state.desired_state is not None
    assert loaded_state.applied_state is not None
    assert "DOKPLOY_API_KEY" not in loaded_state.raw_input.values
    assert loaded_state.desired_state.dokploy_api_url == "http://127.0.0.1:3000"

    requested_raw = parse_env_file(env_file)
    requested_desired = resolve_desired_state(requested_raw)
    classify_install_request(
        existing_raw=loaded_state.raw_input,
        existing_desired=loaded_state.desired_state,
        existing_applied=loaded_state.applied_state,
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )


def test_install_retry_accepts_stale_state_when_only_dokploy_api_url_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    existing_raw = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )
    requested_raw = RawEnvInput(
        format_version=existing_raw.format_version,
        values={
            **existing_raw.values,
            "DOKPLOY_API_URL": "https://dokploy.example.com",
            "DOKPLOY_API_KEY": "dokp-key-123",
        },
    )
    monkeypatch.setattr(
        cli,
        "_ensure_dokploy_api_auth",
        lambda **kwargs: kwargs["raw_env"],
    )
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(
        cli,
        "execute_lifecycle_plan",
        lambda **kwargs: {"lifecycle": {"mode": "noop"}, "state_status": "existing", "ok": True},
    )

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=requested_raw,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["lifecycle"]["mode"] == "noop"
    assert summary["state_status"] == "existing"


def test_install_retry_sanitizes_legacy_disabled_openclaw_token_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    existing_raw = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "my-farm-advisor",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
        },
    )
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    legacy_desired_payload = existing_desired.to_dict()
    legacy_desired_payload["openclaw_gateway_token"] = "legacy-openclaw-token"
    (state_dir / "desired-state.json").write_text(
        json.dumps(legacy_desired_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts())
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(
        cli,
        "execute_lifecycle_plan",
        lambda **kwargs: {
            "lifecycle": {"mode": kwargs["lifecycle_plan"].mode},
            "state_status": "existing",
            "ok": True,
        },
    )

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=existing_raw,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["lifecycle"]["mode"] == "noop"
    assert summary["state_status"] == "existing"
    assert load_state_dir(state_dir).desired_state is not None
    assert load_state_dir(state_dir).desired_state.openclaw_gateway_token is None


def test_install_retry_accepts_cloudflare_token_rotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    existing_raw = parse_env_file(FIXTURES_DIR / "cloudflare-valid.env")
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )
    requested_raw = RawEnvInput(
        format_version=existing_raw.format_version,
        values={
            **existing_raw.values,
            "CLOUDFLARE_API_TOKEN": "new-token-456",
        },
    )
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(
        cli,
        "execute_lifecycle_plan",
        lambda **kwargs: {"lifecycle": {"mode": "noop"}, "state_status": "existing", "ok": True},
    )

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=requested_raw,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["lifecycle"]["mode"] == "noop"
    assert summary["state_status"] == "existing"


def test_install_restarts_from_empty_scaffold_when_saved_env_drifted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    existing_raw = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )
    requested_raw = RawEnvInput(
        format_version=existing_raw.format_version,
        values={
            **existing_raw.values,
            "SEAWEEDFS_ACCESS_KEY": "new-access-key",
            "SEAWEEDFS_SECRET_KEY": "new-secret-key",
            "ENABLE_SEAWEEDFS": "true",
        },
    )
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts())
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda desired_state, host_facts, *, allow_memory_shortfall=False: PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(requested_raw)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "execute_lifecycle_plan",
        lambda **kwargs: {
            "lifecycle": {"mode": kwargs["lifecycle_plan"].mode},
            "state_status": "fresh",
            "ok": True,
        },
    )

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=requested_raw,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["lifecycle"]["mode"] == "install"
    assert summary["state_status"] == "fresh"


def test_install_resume_tolerates_required_ports_used_by_existing_dokploy_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / ".dokploy-wizard-state"
    existing_raw = RawEnvInput(
        format_version=1,
        values={
            **parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env").values,
            "DOKPLOY_API_URL": "https://dokploy.example.com",
            "DOKPLOY_API_KEY": "dokp-key-123",
            "SEAWEEDFS_ACCESS_KEY": "seaweed-existing",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret-existing",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
            "MY_FARM_ADVISOR_CHANNELS": "matrix",
            "PACKS": "matrix,my-farm-advisor,nextcloud,openclaw,seaweedfs",
        },
    )
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=("preflight", "dokploy_bootstrap", "networking", "shared_core"),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(
            format_version=existing_desired.format_version,
            resources=(OwnedResource("cloudflare_tunnel", "tunnel-1", "account:account-123"),),
        ),
    )
    env_file = state_dir / "install.env"
    env_file.write_text(
        "\n".join(
            [
                *(
                    f"{key}={value}"
                    for key, value in existing_raw.values.items()
                    if key
                    not in {
                        "DOKPLOY_API_URL",
                        "DOKPLOY_API_KEY",
                        "SEAWEEDFS_ACCESS_KEY",
                        "SEAWEEDFS_SECRET_KEY",
                        "MY_FARM_ADVISOR_CHANNELS",
                    }
                ),
                "SEAWEEDFS_ACCESS_KEY=seaweed-new",
                "SEAWEEDFS_SECRET_KEY=seaweed-secret-new",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cli,
        "collect_host_facts",
        lambda _: _host_facts(docker_installed=True, docker_daemon_reachable=True),
    )
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(
        cli,
        "_run_preflight_report",
        lambda **kwargs: PreflightReport(
            host_facts=HostFacts(
                distribution_id="ubuntu",
                version_id="24.04",
                cpu_count=8,
                memory_gb=16,
                disk_gb=200,
                disk_path="/var/lib/docker",
                docker_installed=True,
                docker_daemon_reachable=True,
                ports_in_use=(80, 443, 3000),
                environment_classification="vps",
                hostname="test-host",
            ),
            required_profile=derive_required_profile(existing_desired),
            checks=(
                PreflightCheck(
                    name="required_ports",
                    status="fail",
                    detail="required ports already in use: [80, 443, 3000]",
                ),
            ),
            advisories=(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "execute_lifecycle_plan",
        lambda **kwargs: {
            "lifecycle": {"mode": kwargs["lifecycle_plan"].mode},
            "state_status": "existing",
            "ok": True,
        },
    )

    summary = cli.run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["lifecycle"]["mode"] == "resume"
    assert summary["state_status"] == "existing"


def test_install_fresh_state_tolerates_required_ports_when_dokploy_is_already_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text(
        (FIXTURES_DIR / "full.env").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _stub_install_flow_after_preflight(monkeypatch)
    monkeypatch.setattr(
        cli,
        "collect_host_facts",
        lambda _: HostFacts(
            distribution_id="ubuntu",
            version_id="24.04",
            cpu_count=8,
            memory_gb=16,
            disk_gb=200,
            disk_path="/var/lib/docker",
            docker_installed=True,
            docker_daemon_reachable=True,
            ports_in_use=(80, 443, 3000),
            environment_classification="vps",
            hostname="test-host",
        ),
    )

    summary = cli.run_install_flow(
        env_file=env_file,
        state_dir=tmp_path / ".dokploy-wizard-state",
        dry_run=False,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["ok"] is True


def test_install_rehydrates_guided_retry_keys_from_persisted_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / ".dokploy-wizard-state"
    state_dir.mkdir(parents=True)
    env_file = state_dir / "install.env"
    existing_raw = RawEnvInput(
        format_version=1,
        values={
            **parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env").values,
            "DOKPLOY_API_URL": "https://dokploy.example.com",
            "DOKPLOY_API_KEY": "dokp-key-123",
            "SEAWEEDFS_ACCESS_KEY": "seaweed-existing",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret-existing",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
            "MY_FARM_ADVISOR_CHANNELS": "matrix",
            "PACKS": "matrix,my-farm-advisor,nextcloud,openclaw,seaweedfs",
        },
    )
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=("preflight", "dokploy_bootstrap", "networking", "shared_core"),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(
            format_version=existing_desired.format_version,
            resources=(OwnedResource("cloudflare_tunnel", "tunnel-1", "account:account-123"),),
        ),
    )
    env_file.write_text(
        "\n".join(
            [
                *(
                    f"{key}={value}"
                    for key, value in existing_raw.values.items()
                    if key
                    not in {
                        "DOKPLOY_API_URL",
                        "DOKPLOY_API_KEY",
                        "SEAWEEDFS_ACCESS_KEY",
                        "SEAWEEDFS_SECRET_KEY",
                    }
                ),
                "SEAWEEDFS_ACCESS_KEY=seaweed-new",
                "SEAWEEDFS_SECRET_KEY=seaweed-secret-new",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts())
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda desired_state, host_facts, *, allow_memory_shortfall=False: PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(existing_desired),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "execute_lifecycle_plan",
        lambda **kwargs: {
            "lifecycle": {"mode": kwargs["lifecycle_plan"].mode},
            "state_status": "existing",
            "ok": True,
        },
    )

    summary = cli.run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    healed_env = parse_env_file(env_file)
    assert healed_env.values["DOKPLOY_API_URL"] == "https://dokploy.example.com"
    assert healed_env.values["DOKPLOY_API_KEY"] == "dokp-key-123"
    assert healed_env.values["SEAWEEDFS_ACCESS_KEY"] == "seaweed-existing"
    assert healed_env.values["SEAWEEDFS_SECRET_KEY"] == "seaweed-secret-existing"
    assert (
        healed_env.values["MY_FARM_ADVISOR_CHANNELS"]
        == existing_raw.values["MY_FARM_ADVISOR_CHANNELS"]
    )
    assert summary["lifecycle"]["mode"] == "resume"
    assert summary["state_status"] == "existing"


def test_expected_ports_in_use_for_retry_includes_80_and_443_after_bootstrap() -> None:
    loaded_state = type(
        "LoadedState",
        (),
        {
            "applied_state": AppliedStateCheckpoint(
                format_version=1,
                desired_state_fingerprint="fp-1",
                completed_steps=("preflight", "dokploy_bootstrap"),
            ),
            "ownership_ledger": OwnershipLedger(
                format_version=1,
                resources=(
                    OwnedResource(
                        resource_type="cloudflare_tunnel",
                        resource_id="tunnel-1",
                        scope="account:account-1",
                    ),
                ),
            ),
        },
    )()
    lifecycle_plan = LifecyclePlan(
        mode="resume",
        reasons=("retry",),
        applicable_phases=("preflight", "dokploy_bootstrap", "networking"),
        phases_to_run=("networking",),
        preserved_phases=("dokploy_bootstrap",),
        initial_completed_steps=("preflight", "dokploy_bootstrap"),
        start_phase="networking",
        raw_equivalent=True,
        desired_equivalent=True,
    )

    assert cli._expected_ports_in_use_for_retry(loaded_state, lifecycle_plan) == (
        80,
        443,
        3000,
    )


def test_expected_ports_in_use_for_retry_allows_bootstrap_ports_when_resuming_at_bootstrap() -> (
    None
):
    loaded_state = type(
        "LoadedState",
        (),
        {
            "applied_state": AppliedStateCheckpoint(
                format_version=1,
                desired_state_fingerprint="fp-1",
                completed_steps=("preflight",),
            ),
            "ownership_ledger": OwnershipLedger(format_version=1, resources=()),
        },
    )()
    lifecycle_plan = LifecyclePlan(
        mode="resume",
        reasons=("incomplete",),
        applicable_phases=("preflight", "dokploy_bootstrap", "networking"),
        phases_to_run=("dokploy_bootstrap", "networking"),
        preserved_phases=("preflight",),
        initial_completed_steps=("preflight",),
        start_phase="dokploy_bootstrap",
        raw_equivalent=True,
        desired_equivalent=True,
    )

    assert cli._expected_ports_in_use_for_retry(loaded_state, lifecycle_plan) == (
        80,
        443,
        3000,
    )


def test_install_resumes_from_first_preserved_phase_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle_plan = LifecyclePlan(
        mode="noop",
        reasons=("state matches",),
        applicable_phases=(
            "preflight",
            "dokploy_bootstrap",
            "networking",
            "cloudflare_access",
            "shared_core",
            "headscale",
            "nextcloud",
            "seaweedfs",
            "openclaw",
        ),
        phases_to_run=(),
        preserved_phases=(
            "preflight",
            "dokploy_bootstrap",
            "networking",
            "cloudflare_access",
            "shared_core",
            "headscale",
            "nextcloud",
            "seaweedfs",
            "openclaw",
        ),
        initial_completed_steps=(
            "preflight",
            "dokploy_bootstrap",
            "networking",
            "cloudflare_access",
            "shared_core",
            "headscale",
            "nextcloud",
            "seaweedfs",
            "openclaw",
        ),
        start_phase=None,
        raw_equivalent=True,
        desired_equivalent=True,
    )

    resumed = cli._resume_plan_from_drift(
        lifecycle_plan=lifecycle_plan,
        drift_error=LifecycleDriftError(
            "Lifecycle drift detected before mutation: nextcloud: unhealthy",
            report=DriftReport(
                entries=(
                    DriftEntry(phase="preflight", status="ok", detail="ok"),
                    DriftEntry(phase="nextcloud", status="drift", detail="unhealthy"),
                )
            ),
        ),
    )

    assert resumed.mode == "resume"
    assert resumed.start_phase == "nextcloud"
    assert resumed.preserved_phases == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "cloudflare_access",
        "shared_core",
        "headscale",
    )
    assert resumed.phases_to_run == ("nextcloud", "seaweedfs", "openclaw")
    assert resumed.initial_completed_steps == resumed.preserved_phases
    assert "resuming from the first unhealthy preserved phase" in resumed.reasons[-1]


def test_modify_farm_env_change_reruns_only_my_farm_phase() -> None:
    plan = _classify_modify_plan(
        existing_values=_farm_modify_values(ENABLE_OPENCLAW="true"),
        requested_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            MY_FARM_ADVISOR_PRIMARY_MODEL="anthropic/claude-opus-4.1",
        ),
    )

    assert plan.mode == "modify"
    assert "MY_FARM_ADVISOR_PRIMARY_MODEL" in plan.reasons[0]
    assert plan.phases_to_run == ("my-farm-advisor",)


def test_modify_disabled_farm_ignores_empty_pack_only_env_drift() -> None:
    base_values = _farm_modify_values(ENABLE_MY_FARM_ADVISOR="false")
    base_values.pop("MY_FARM_ADVISOR_PRIMARY_MODEL")

    plan = _classify_modify_plan(
        existing_values=base_values,
        requested_values={**base_values, "MY_FARM_ADVISOR_PRIMARY_MODEL": ""},
    )

    assert plan.mode == "noop"
    assert plan.phases_to_run == ()


def test_modify_shared_ai_defaults_rerun_only_selected_advisor_phases() -> None:
    farm_only_plan = _classify_modify_plan(
        existing_values=_farm_modify_values(
            AI_DEFAULT_PROVIDER="openrouter",
            AI_DEFAULT_MODEL="anthropic/claude-sonnet-4",
        ),
        requested_values=_farm_modify_values(
            AI_DEFAULT_PROVIDER="opencode-go",
            AI_DEFAULT_MODEL="deepseek-v4-flash",
        ),
    )
    both_advisors_plan = _classify_modify_plan(
        existing_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            AI_DEFAULT_PROVIDER="openrouter",
            AI_DEFAULT_MODEL="anthropic/claude-sonnet-4",
        ),
        requested_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            AI_DEFAULT_PROVIDER="openrouter",
            AI_DEFAULT_MODEL="anthropic/claude-opus-4.1",
        ),
    )

    assert "AI_DEFAULT_PROVIDER" in farm_only_plan.reasons[0]
    assert "AI_DEFAULT_MODEL" in both_advisors_plan.reasons[0]
    assert farm_only_plan.phases_to_run == ("shared_core", "my-farm-advisor")
    assert both_advisors_plan.phases_to_run == (
        "shared_core",
        "openclaw",
        "my-farm-advisor",
    )


def test_modify_litellm_provider_model_change_schedules_gateway_and_consumers() -> None:
    alias_plan = _classify_modify_plan(
        existing_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            ENABLE_CODER="true",
            ENABLE_SEAWEEDFS="true",
            SEAWEEDFS_ACCESS_KEY="seaweed-access",
            SEAWEEDFS_SECRET_KEY="seaweed-secret",
            OPENCODE_GO_API_KEY="opencode-go-key",
            LITELLM_OPENROUTER_MODELS=("openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini"),
        ),
        requested_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            ENABLE_CODER="true",
            ENABLE_SEAWEEDFS="true",
            SEAWEEDFS_ACCESS_KEY="seaweed-access",
            SEAWEEDFS_SECRET_KEY="seaweed-secret",
            OPENCODE_GO_API_KEY="opencode-go-key",
            LITELLM_OPENROUTER_MODELS=(
                "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini,"
                "openrouter/healer-alpha=openrouter/anthropic/claude-3.7-sonnet"
            ),
        ),
    )

    key_reconcile_plan = _classify_modify_plan(
        existing_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            ENABLE_CODER="true",
            ENABLE_SEAWEEDFS="true",
            SEAWEEDFS_ACCESS_KEY="seaweed-access",
            SEAWEEDFS_SECRET_KEY="seaweed-secret",
            LITELLM_OPENCODE_GO_API_KEY="opencode-go-key-1",
            AI_DEFAULT_PROVIDER="opencode-go",
            AI_DEFAULT_MODEL="deepseek-v4-flash",
        ),
        requested_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            ENABLE_CODER="true",
            ENABLE_SEAWEEDFS="true",
            SEAWEEDFS_ACCESS_KEY="seaweed-access",
            SEAWEEDFS_SECRET_KEY="seaweed-secret",
            LITELLM_OPENCODE_GO_API_KEY="opencode-go-key-2",
            AI_DEFAULT_PROVIDER="opencode-go",
            AI_DEFAULT_MODEL="deepseek-v4-flash",
        ),
    )

    wildcard_plan = _classify_modify_plan(
        existing_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            ENABLE_CODER="true",
            ENABLE_SEAWEEDFS="true",
            SEAWEEDFS_ACCESS_KEY="seaweed-access",
            SEAWEEDFS_SECRET_KEY="seaweed-secret",
            LITELLM_OPENCODE_GO_API_KEY="opencode-go-key-1",
            LITELLM_OPENCODE_GO_WILDCARD="false",
            AI_DEFAULT_PROVIDER="opencode-go",
            AI_DEFAULT_MODEL="deepseek-v4-flash",
        ),
        requested_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            ENABLE_CODER="true",
            ENABLE_SEAWEEDFS="true",
            SEAWEEDFS_ACCESS_KEY="seaweed-access",
            SEAWEEDFS_SECRET_KEY="seaweed-secret",
            LITELLM_OPENCODE_GO_API_KEY="opencode-go-key-1",
            LITELLM_OPENCODE_GO_WILDCARD="true",
            AI_DEFAULT_PROVIDER="opencode-go",
            AI_DEFAULT_MODEL="deepseek-v4-flash",
        ),
    )

    assert alias_plan.mode == "modify"
    assert "LITELLM_OPENROUTER_MODELS" in alias_plan.reasons[0]
    assert alias_plan.start_phase == "shared_core"
    assert alias_plan.phases_to_run == ("shared_core", "coder", "openclaw", "my-farm-advisor")
    assert key_reconcile_plan.mode == "modify"
    assert "LITELLM_OPENCODE_GO_API_KEY" in key_reconcile_plan.reasons[0]
    assert key_reconcile_plan.phases_to_run == (
        "shared_core",
        "coder",
        "openclaw",
        "my-farm-advisor",
    )
    assert wildcard_plan.mode == "modify"
    assert "LITELLM_OPENCODE_GO_WILDCARD" in wildcard_plan.reasons[0]
    assert wildcard_plan.phases_to_run == (
        "shared_core",
        "coder",
        "openclaw",
        "my-farm-advisor",
    )
    assert "nextcloud" not in alias_plan.phases_to_run
    assert "seaweedfs" not in alias_plan.phases_to_run
    assert "nextcloud" not in key_reconcile_plan.phases_to_run
    assert "seaweedfs" not in key_reconcile_plan.phases_to_run
    assert "nextcloud" not in wildcard_plan.phases_to_run
    assert "seaweedfs" not in wildcard_plan.phases_to_run


def test_modify_litellm_consumer_removal_reruns_shared_core() -> None:
    plan = _classify_modify_plan(
        existing_values=_farm_modify_values(ENABLE_OPENCLAW="true"),
        requested_values=_farm_modify_values(ENABLE_OPENCLAW="false"),
    )

    assert plan.mode == "modify"
    assert "ENABLE_OPENCLAW" in plan.reasons[0]
    assert plan.phases_to_run == ("networking", "shared_core", "cloudflare_access")


def test_modify_litellm_coder_removal_reruns_shared_core_without_unrelated_packs() -> None:
    plan = _classify_modify_plan(
        existing_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            ENABLE_CODER="true",
            ENABLE_SEAWEEDFS="true",
            SEAWEEDFS_ACCESS_KEY="seaweed-access",
            SEAWEEDFS_SECRET_KEY="seaweed-secret",
        ),
        requested_values=_farm_modify_values(
            ENABLE_OPENCLAW="true",
            ENABLE_CODER="false",
            ENABLE_SEAWEEDFS="true",
            SEAWEEDFS_ACCESS_KEY="seaweed-access",
            SEAWEEDFS_SECRET_KEY="seaweed-secret",
        ),
    )

    assert plan.mode == "modify"
    assert "ENABLE_CODER" in plan.reasons[0]
    assert plan.phases_to_run == ("networking", "shared_core")
    assert "nextcloud" not in plan.phases_to_run
    assert "seaweedfs" not in plan.phases_to_run


def test_modify_add_my_farm_later_refreshes_nextcloud_without_openclaw() -> None:
    existing_values = _farm_modify_values(ENABLE_MY_FARM_ADVISOR="false")
    existing_values.pop("MY_FARM_ADVISOR_PRIMARY_MODEL")

    plan = _classify_modify_plan(
        existing_values=existing_values,
        requested_values=_farm_modify_values(),
    )

    assert plan.mode == "modify"
    assert "ENABLE_MY_FARM_ADVISOR" in plan.reasons[0]
    assert plan.phases_to_run == (
        "shared_core",
        "nextcloud",
        "my-farm-advisor",
        "cloudflare_access",
    )


def test_lifecycle_install_refreshes_nextcloud_after_my_farm_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    summary, refresh_calls, events = _run_lifecycle_refresh(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        values=_lifecycle_refresh_values(),
        lifecycle_plan=_build_lifecycle_test_plan(
            desired_state=resolve_desired_state(_raw_input(_lifecycle_refresh_values())),
            mode="install",
            phases_to_run=("nextcloud", "my-farm-advisor"),
        ),
    )

    assert summary["nextcloud"]["phase"] == "nextcloud"
    assert summary["my_farm_advisor"]["phase"] == "my-farm-advisor"
    assert refresh_calls == ["operator@example.com"]
    assert events == ["nextcloud", "my-farm-advisor", "refresh"]


def test_lifecycle_install_refreshes_once_after_last_advisor_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    values = _lifecycle_refresh_values(ENABLE_OPENCLAW="true")

    _, refresh_calls, events = _run_lifecycle_refresh(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        values=values,
        lifecycle_plan=_build_lifecycle_test_plan(
            desired_state=resolve_desired_state(_raw_input(values)),
            mode="install",
            phases_to_run=("nextcloud", "openclaw", "my-farm-advisor"),
        ),
    )

    assert refresh_calls == ["operator@example.com"]
    assert events == ["nextcloud", "openclaw", "my-farm-advisor", "refresh"]


def test_lifecycle_install_refreshes_after_openclaw_when_farm_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    values = _lifecycle_refresh_values(
        ENABLE_OPENCLAW="true",
        ENABLE_MY_FARM_ADVISOR="false",
    )
    values.pop("MY_FARM_ADVISOR_PRIMARY_MODEL")

    _, refresh_calls, events = _run_lifecycle_refresh(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        values=values,
        lifecycle_plan=_build_lifecycle_test_plan(
            desired_state=resolve_desired_state(_raw_input(values)),
            mode="install",
            phases_to_run=("nextcloud", "openclaw"),
        ),
    )

    assert refresh_calls == ["operator@example.com"]
    assert events == ["nextcloud", "openclaw", "refresh"]


def test_lifecycle_modify_adding_farm_triggers_single_nextcloud_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing_values = _lifecycle_refresh_values(ENABLE_MY_FARM_ADVISOR="false")
    existing_values.pop("MY_FARM_ADVISOR_PRIMARY_MODEL")
    requested_values = _lifecycle_refresh_values()
    lifecycle_plan = _classify_modify_plan(
        existing_values=existing_values,
        requested_values=requested_values,
    )
    lifecycle_plan = LifecyclePlan(
        mode=lifecycle_plan.mode,
        reasons=lifecycle_plan.reasons,
        applicable_phases=lifecycle_plan.applicable_phases,
        phases_to_run=lifecycle_plan.phases_to_run,
        preserved_phases=(),
        initial_completed_steps=(),
        start_phase=lifecycle_plan.start_phase,
        raw_equivalent=lifecycle_plan.raw_equivalent,
        desired_equivalent=lifecycle_plan.desired_equivalent,
    )

    _, refresh_calls, events = _run_lifecycle_refresh(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        values=requested_values,
        lifecycle_plan=lifecycle_plan,
    )

    assert refresh_calls == ["operator@example.com"]
    assert events == ["shared_core", "nextcloud", "my-farm-advisor", "refresh", "cloudflare_access"]


def test_modify_remove_my_farm_later_runs_only_farm_phase() -> None:
    requested_values = _farm_modify_values(ENABLE_MY_FARM_ADVISOR="false")
    requested_values.pop("MY_FARM_ADVISOR_PRIMARY_MODEL")

    existing_raw = _raw_input(_farm_modify_values())
    requested_raw = _raw_input(requested_values)
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    assert lifecycle_changes._removed_pack_phases(existing_desired, requested_desired) == {
        "my-farm-advisor"
    }

    plan = _classify_modify_plan(
        existing_values=_farm_modify_values(),
        requested_values=requested_values,
    )

    assert plan.mode == "modify"
    assert "ENABLE_MY_FARM_ADVISOR" in plan.reasons[0]
    assert plan.phases_to_run == ("shared_core",)
    assert plan.preserved_phases[-1] == "nextcloud"


def test_modify_same_farm_target_noops_with_farm_phase_preserved() -> None:
    plan = _classify_modify_plan(
        existing_values=_farm_modify_values(ENABLE_OPENCLAW="true"),
        requested_values=_farm_modify_values(ENABLE_OPENCLAW="true"),
    )

    assert plan.mode == "noop"
    assert plan.phases_to_run == ()
    assert "my-farm-advisor" in plan.preserved_phases
    assert "openclaw" in plan.preserved_phases


def test_modify_openclaw_only_to_both_advisors_refreshes_nextcloud_without_rerunning_openclaw() -> (
    None
):
    existing_values = _farm_modify_values(
        ENABLE_OPENCLAW="true",
        ENABLE_MY_FARM_ADVISOR="false",
    )
    existing_values.pop("MY_FARM_ADVISOR_PRIMARY_MODEL")

    plan = _classify_modify_plan(
        existing_values=existing_values,
        requested_values=_farm_modify_values(ENABLE_OPENCLAW="true"),
    )

    assert plan.mode == "modify"
    assert "ENABLE_MY_FARM_ADVISOR" in plan.reasons[0]
    assert plan.phases_to_run == (
        "shared_core",
        "nextcloud",
        "my-farm-advisor",
        "cloudflare_access",
    )
    assert "openclaw" not in plan.phases_to_run


def test_modify_both_advisors_to_openclaw_only_runs_only_farm_teardown_phase() -> None:
    existing_raw = _raw_input(_farm_modify_values(ENABLE_OPENCLAW="true"))
    requested_values = _farm_modify_values(
        ENABLE_OPENCLAW="true",
        ENABLE_MY_FARM_ADVISOR="false",
    )
    requested_values.pop("MY_FARM_ADVISOR_PRIMARY_MODEL")
    requested_raw = _raw_input(requested_values)
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = _classify_modify_plan(
        existing_values=_farm_modify_values(ENABLE_OPENCLAW="true"),
        requested_values=requested_values,
    )

    assert lifecycle_changes._removed_pack_phases(existing_desired, requested_desired) == {
        "my-farm-advisor"
    }
    assert plan.mode == "modify"
    assert "ENABLE_MY_FARM_ADVISOR" in plan.reasons[0]
    assert plan.phases_to_run == ("shared_core",)
    assert "openclaw" not in plan.phases_to_run


def test_modify_farm_only_to_both_advisors_runs_openclaw_without_rerunning_farm() -> None:
    plan = _classify_modify_plan(
        existing_values=_farm_modify_values(),
        requested_values=_farm_modify_values(ENABLE_OPENCLAW="true"),
    )

    assert plan.mode == "modify"
    assert "ENABLE_OPENCLAW" in plan.reasons[0]
    assert plan.phases_to_run == ("networking", "shared_core", "openclaw", "cloudflare_access")
    assert "my-farm-advisor" not in plan.phases_to_run


def test_guided_dry_run_does_not_require_dokploy_admin_password() -> None:
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "",
            "n",
            "cf-token",
            "account-123",
            "",
            "",
        ]
    )

    values = prompt_for_initial_install_values(
        lambda _: next(responses), require_dokploy_auth=False
    )

    assert values.dokploy_admin_password is None
    assert values.enable_headscale is True


def test_guided_install_tailscale_mode_prompts_for_auth_key() -> None:
    prompts: list[str] = []
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "secret-123",
            "tailscale",
            "tskey-123",
            "wizard-host",
            "y",
            "tag:admin",
            "10.254.0.0/24",
            "n",
            "cf-token",
            "account-123",
            "",
            "",
        ]
    )

    def fake_prompt(message: str) -> str:
        prompts.append(message)
        return next(responses)

    values = prompt_for_initial_install_values(fake_prompt)

    assert values.stack_name == "example-com"
    assert values.enable_headscale is False
    assert values.enable_tailscale is True
    assert values.tailscale_auth_key == "tskey-123"
    assert values.tailscale_hostname == "wizard-host"
    assert values.tailscale_enable_ssh is True
    assert values.tailscale_tags == ("tag:admin",)
    assert values.tailscale_subnet_routes == ("10.254.0.0/24",)
    combined = "\n".join(prompts)
    assert "Tailscale auth key" in combined


def test_guided_install_can_emit_cloudflare_help(capsys: pytest.CaptureFixture[str]) -> None:
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "secret-123",
            "",
            "y",
            "cf-token",
            "account-123",
            "",
            "",
        ]
    )

    values = prompt_for_initial_install_values(lambda _: next(responses), output=print)

    assert values.cloudflare_api_token == "cf-token"
    captured = capsys.readouterr()
    assert "https://dash.cloudflare.com/profile/api-tokens" in captured.out
    assert "Zone -> DNS -> Edit" in captured.out
    assert "Zone -> SSL and Certificates -> Edit" in captured.out
    assert "Advanced Certificate Manager must be enabled for the zone" in captured.out


def test_guided_install_sanitizes_bracketed_paste_sequences() -> None:
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "secret-123",
            "",
            "n",
            "\x1b[200~cf-token\x1b[201~",
            "\x1b[200~account-123\x1b[201~",
            "\x1b[200~zone-123\x1b[201~",
            "",
        ]
    )

    values = prompt_for_initial_install_values(lambda _: next(responses))

    assert values.cloudflare_api_token == "cf-token"
    assert values.cloudflare_account_id == "account-123"
    assert values.cloudflare_zone_id == "zone-123"


def test_guided_install_sanitizes_caret_notation_paste_sequences() -> None:
    responses = iter(
        [
            "example.com",
            "",
            "",
            "",
            "secret-123",
            "",
            "n",
            "^[[200~cf-token^[[201~",
            "^[[200~account-123^[[201~",
            "^[[200~zone-123^[[201~",
            "",
        ]
    )

    values = prompt_for_initial_install_values(lambda _: next(responses))

    assert values.cloudflare_api_token == "cf-token"
    assert values.cloudflare_account_id == "account-123"
    assert values.cloudflare_zone_id == "zone-123"


def test_guided_state_dir_sanitizes_bracketed_paste(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pasted = f"\x1b[200~{tmp_path / 'guided-state'}\x1b[201~"
    monkeypatch.setattr("builtins.input", lambda _: pasted)

    assert (
        cli._prompt_for_guided_state_dir(Path(".dokploy-wizard-state")) == tmp_path / "guided-state"
    )


def test_guided_state_dir_sanitizes_caret_notation_arrow_and_paste(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pasted = f"^[[A^[[200~{tmp_path / 'guided-state'}^[[201~"
    monkeypatch.setattr("builtins.input", lambda _: pasted)

    assert (
        cli._prompt_for_guided_state_dir(Path(".dokploy-wizard-state")) == tmp_path / "guided-state"
    )


def test_guided_install_generates_seaweedfs_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(["n", "n", "y", "n", "n"])
    monkeypatch.setattr(
        prompt_module,
        "_generate_credential",
        lambda prefix: f"{prefix}-generated",
    )

    selection = prompt_module.prompt_for_pack_selection(
        lambda _: next(responses),
        include_headscale_prompt=False,
    )

    assert selection.seaweedfs_access_key == "seaweed-generated"
    assert selection.seaweedfs_secret_key == "seaweed-secret-generated"
    assert selection.generated_secrets == {
        "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
        "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
    }


def test_guided_install_defaults_openclaw_to_telegram_when_matrix_disabled() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        prompt_module,
        "_generate_credential",
        lambda prefix: f"{prefix}-generated",
    )
    responses = iter(
        [
            "n",  # matrix default stays no
            "y",  # nextcloud default yes
            "y",  # seaweedfs default yes
            "y",  # openclaw default yes
            "",  # openclaw channel default telegram
            "y",  # nvidia key default yes
            "nv-key",
            "y",  # openrouter key default yes
            "or-key",
            "",  # primary model default nvidia/moonshotai/kimi-k2.5
            "",  # fallback model default opencode-go/deepseek-v4-flash
            "bot-token",
            "123456789",
            "n",  # my farm advisor default no
        ]
    )

    selection = prompt_module.prompt_for_pack_selection(
        lambda _: next(responses),
        include_headscale_prompt=False,
    )
    monkeypatch.undo()

    assert selection.selected_packs == ("nextcloud", "openclaw", "seaweedfs")
    assert selection.openclaw_channels == ("telegram",)
    assert selection.generated_secrets == {
        "OPENCLAW_GATEWAY_PASSWORD": "openclaw-ui-generated",
        "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
        "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
    }
    assert selection.advisor_env == {
        "OPENCLAW_GATEWAY_PASSWORD": "openclaw-ui-generated",
        "OPENCLAW_FALLBACK_MODELS": "opencode-go/deepseek-v4-flash",
        "OPENCLAW_NVIDIA_API_KEY": "nv-key",
        "OPENCLAW_OPENROUTER_API_KEY": "or-key",
        "OPENCLAW_PRIMARY_MODEL": "nvidia/moonshotai/kimi-k2.5",
        "OPENCLAW_TELEGRAM_BOT_TOKEN": "bot-token",
        "OPENCLAW_TELEGRAM_OWNER_USER_ID": "123456789",
    }


def test_guided_install_keeps_matrix_default_for_openclaw_when_matrix_enabled() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        prompt_module,
        "_generate_credential",
        lambda prefix: f"{prefix}-generated",
    )
    responses = iter(
        [
            "y",  # matrix yes
            "y",  # nextcloud default yes
            "y",  # seaweedfs default yes
            "y",  # openclaw default yes
            "",  # default channel should become matrix
            "y",
            "nv-key",
            "y",
            "or-key",
            "",
            "",
            "n",  # no telegram bot prompt because channel matrix only
        ]
    )

    selection = prompt_module.prompt_for_pack_selection(
        lambda _: next(responses),
        include_headscale_prompt=False,
    )
    monkeypatch.undo()

    assert selection.selected_packs == ("matrix", "nextcloud", "openclaw", "seaweedfs")
    assert selection.openclaw_channels == ("matrix",)
    assert selection.generated_secrets == {
        "OPENCLAW_GATEWAY_PASSWORD": "openclaw-ui-generated",
        "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
        "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
    }
    assert selection.advisor_env["OPENCLAW_GATEWAY_PASSWORD"] == "openclaw-ui-generated"
    assert selection.advisor_env["OPENCLAW_PRIMARY_MODEL"] == "nvidia/moonshotai/kimi-k2.5"
    assert selection.advisor_env["OPENCLAW_FALLBACK_MODELS"] == "opencode-go/deepseek-v4-flash"
    assert "OPENCLAW_TELEGRAM_BOT_TOKEN" not in selection.advisor_env


def test_append_operator_links_adds_openclaw_authorized_dashboard_url() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "OPENCLAW_GATEWAY_TOKEN": "token-123",
            },
        )
    )
    summary = {"openclaw": {"outcome": "applied"}}

    cli._append_operator_links(summary, desired_state)

    assert summary["openclaw"]["authorized_dashboard_url"] == (
        "https://openclaw.example.com/#token=token-123"
    )


def test_append_operator_links_skips_when_token_absent() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )
    summary = {"openclaw": {"outcome": "applied"}}

    cli._append_operator_links(summary, desired_state)

    assert "authorized_dashboard_url" not in summary["openclaw"]


def test_append_operator_links_skips_when_access_auth_handles_openclaw() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "OPENCLAW_GATEWAY_TOKEN": "token-123",
            },
        )
    )
    summary = {"openclaw": {"outcome": "applied"}}

    cli._append_operator_links(summary, desired_state)

    assert "authorized_dashboard_url" not in summary["openclaw"]


@pytest.mark.skip(reason="Paused: non-local routes")
def test_advisor_model_normalization_maps_legacy_nvidia_kimi_id() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "OPENCLAW_PRIMARY_MODEL": "nvidia/moonshot/kimi-k2.5",
            "OPENCLAW_FALLBACK_MODELS": (
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free,"
                "nvidia/moonshot/kimi-k2.5"
            ),
        },
    )

    assert cli._advisor_primary_model(raw_env, env_prefix="OPENCLAW") == (
        "nvidia/moonshotai/kimi-k2.5"
    )
    assert cli._advisor_model_list(raw_env, env_prefix="OPENCLAW") == (
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/moonshotai/kimi-k2.5",
    )


def test_guided_install_prints_generated_seaweedfs_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._emit_generated_secrets(
        {
            "SEAWEEDFS_ACCESS_KEY": "seaweed-generated",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret-generated",
        },
        Path("/tmp/install.env"),
    )

    captured = capsys.readouterr()
    assert "Generated credentials" in captured.out
    assert "SEAWEEDFS_ACCESS_KEY=seaweed-generated" in captured.out
    assert "SEAWEEDFS_SECRET_KEY=seaweed-secret-generated" in captured.out


def test_write_reusable_env_file_sets_owner_only_permissions(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    cli._write_reusable_env_file(
        env_file,
        RawEnvInput(format_version=1, values={"STACK_NAME": "example"}),
    )

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_load_install_raw_env_warns_on_broad_permissions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    raw_env = cli._load_install_raw_env(
        env_file,
        non_interactive=True,
        warn_on_broad_permissions=True,
    )

    captured = capsys.readouterr()
    assert raw_env.values["STACK_NAME"] == "example"
    assert "permissions are broader than owner-only" in captured.err
    assert "0600" in captured.err
    assert str(env_file) in captured.err


def test_load_install_raw_env_skips_warning_when_permissions_are_owner_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o600)

    cli._load_install_raw_env(
        env_file,
        non_interactive=True,
        warn_on_broad_permissions=True,
    )

    captured = capsys.readouterr()
    assert captured.err == ""


def test_handle_install_suppresses_generated_secret_output_when_requested(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"

    monkeypatch.setattr(
        cli,
        "_resolve_install_input",
        lambda **_: (
            env_file,
            RawEnvInput(format_version=1, values={"STACK_NAME": "example"}),
            tmp_path / "state",
            {"SEAWEEDFS_SECRET_KEY": "generated-secret"},
        ),
    )
    monkeypatch.setattr(cli, "run_install_flow", lambda **_: {"ok": True})

    result = cli._handle_install(
        argparse.Namespace(
            env_file=None,
            state_dir=tmp_path / "state",
            dry_run=False,
            non_interactive=False,
            no_print_secrets=True,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"ok": true' in captured.out
    assert "Generated credentials" not in captured.out
    assert "generated-secret" not in captured.out


def test_handle_modify_warns_on_broad_env_file_permissions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    monkeypatch.setattr(cli, "run_modify_flow", lambda **_: {"ok": True})

    result = cli._handle_modify(
        argparse.Namespace(
            env_file=env_file,
            state_dir=tmp_path / "state",
            dry_run=False,
            non_interactive=True,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"ok": true' in captured.out
    assert "permissions are broader than owner-only" in captured.err


def test_handle_modify_dry_run_skips_env_file_permission_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    monkeypatch.setattr(cli, "run_modify_flow", lambda **_: {"ok": True})

    result = cli._handle_modify(
        argparse.Namespace(
            env_file=env_file,
            state_dir=tmp_path / "state",
            dry_run=True,
            non_interactive=True,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""


def test_ensure_dokploy_api_auth_rewrites_env_with_generated_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    qualified_endpoints: list[str] = []

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3000"

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            assert admin_email == "admin@example.com"
            assert admin_password == "secret-123"
            return DokployBootstrapAuthResult(
                api_key="dokp-key-123",
                api_url="http://127.0.0.1:3000",
                admin_email=admin_email,
                organization_id="org-1",
                used_sign_up=False,
                auth_path="/api/auth/sign-in/email",
                session_path="/api/user.session",
            )

    class ValidDokployClient:
        def __init__(self, *, api_url: str, api_key: str) -> None:
            assert api_url == "http://127.0.0.1:3000"
            assert api_key == "dokp-key-123"

        def list_projects(self) -> tuple[object, ...]:
            qualified_endpoints.append("project.all")
            return ()

        def ai_providers_all(self) -> tuple[object, ...]:
            qualified_endpoints.append("ai.getAll")
            return ()

    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)
    monkeypatch.setattr(cli, "DokployApiClient", ValidDokployClient)

    updated = cli._ensure_dokploy_api_auth(
        env_file=env_file,
        raw_env=raw_env,
        desired_state=desired_state,
        bootstrap_backend=FakeBootstrapBackend(),
        dry_run=False,
        require_real_dokploy_auth=True,
    )

    assert updated.values["DOKPLOY_API_KEY"] == "dokp-key-123"
    assert updated.values["DOKPLOY_API_URL"] == "http://127.0.0.1:3000"
    assert qualified_endpoints == ["project.all"]
    written = env_file.read_text(encoding="utf-8")
    assert "DOKPLOY_API_KEY=dokp-key-123" in written
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_ensure_dokploy_api_auth_refreshes_invalid_existing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
            "DOKPLOY_API_KEY": "stale-key",
            "DOKPLOY_API_URL": "http://127.0.0.1:3000",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3000"

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            return DokployBootstrapAuthResult(
                api_key="fresh-key-123",
                api_url="http://127.0.0.1:3000",
                admin_email=admin_email,
                organization_id="org-1",
                used_sign_up=False,
                auth_path="/api/auth/sign-in/email",
                session_path="/api/user.session",
            )

    class ValidatingDokployClient:
        def __init__(self, *, api_url: str, api_key: str) -> None:
            assert api_url == "http://127.0.0.1:3000"
            self.api_key = api_key

        def list_projects(self) -> tuple[object, ...]:
            if self.api_key == "stale-key":
                raise cli.DokployApiError("unauthorized")
            assert self.api_key == "fresh-key-123"
            return ()

        def ai_providers_all(self) -> tuple[object, ...]:
            assert self.api_key == "fresh-key-123"
            return ()

    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)
    monkeypatch.setattr(cli, "DokployApiClient", ValidatingDokployClient)

    updated = cli._ensure_dokploy_api_auth(
        env_file=env_file,
        raw_env=raw_env,
        desired_state=desired_state,
        bootstrap_backend=FakeBootstrapBackend(),
        dry_run=False,
        require_real_dokploy_auth=True,
    )

    assert updated.values["DOKPLOY_API_KEY"] == "fresh-key-123"
    assert updated.values["DOKPLOY_API_URL"] == "http://127.0.0.1:3000"


def test_ensure_dokploy_api_auth_reuses_project_key_when_ai_requires_session_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
            "DOKPLOY_API_KEY": "project-only-key",
            "DOKPLOY_API_URL": "http://127.0.0.1:3000",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            raise AssertionError(f"bootstrap auth refresh should not be called for {base_url}")

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            raise AssertionError("bootstrap auth refresh should not be called")

    class ValidatingDokployClient:
        def __init__(self, *, api_url: str, api_key: str) -> None:
            assert api_url == "http://127.0.0.1:3000"
            self.api_key = api_key

        def list_projects(self) -> tuple[object, ...]:
            assert self.api_key == "project-only-key"
            return ()

        def ai_providers_all(self) -> tuple[object, ...]:
            raise AssertionError("ai.getAll is session-backed and not part of API-key qualification")

    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)
    monkeypatch.setattr(cli, "DokployApiClient", ValidatingDokployClient)

    updated = cli._ensure_dokploy_api_auth(
        env_file=env_file,
        raw_env=raw_env,
        desired_state=desired_state,
        bootstrap_backend=FakeBootstrapBackend(),
        dry_run=False,
        require_real_dokploy_auth=True,
    )

    assert updated.values["DOKPLOY_API_KEY"] == "project-only-key"
    assert updated.values["DOKPLOY_API_URL"] == "http://127.0.0.1:3000"
    assert "DOKPLOY_API_KEY=project-only-key" in env_file.read_text(encoding="utf-8")


def test_ensure_dokploy_api_auth_accepts_new_project_key_when_ai_requires_session_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3000"

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            return DokployBootstrapAuthResult(
                api_key="fresh-project-only-key",
                api_url="http://127.0.0.1:3000",
                admin_email=admin_email,
                organization_id="org-1",
                used_sign_up=False,
                auth_path="/api/auth/sign-in/email",
                session_path="/api/user.session",
            )

    class ProjectOnlyDokployClient:
        def __init__(self, *, api_url: str, api_key: str) -> None:
            assert api_url == "http://127.0.0.1:3000"
            assert api_key == "fresh-project-only-key"

        def list_projects(self) -> tuple[object, ...]:
            return ()

        def ai_providers_all(self) -> tuple[object, ...]:
            raise AssertionError("ai.getAll is session-backed and not part of API-key qualification")

    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)
    monkeypatch.setattr(cli, "DokployApiClient", ProjectOnlyDokployClient)

    updated = cli._ensure_dokploy_api_auth(
        env_file=env_file,
        raw_env=raw_env,
        desired_state=desired_state,
        bootstrap_backend=FakeBootstrapBackend(),
        dry_run=False,
        require_real_dokploy_auth=True,
    )

    assert updated.values["DOKPLOY_API_KEY"] == "fresh-project-only-key"
    assert "DOKPLOY_API_KEY=fresh-project-only-key" in env_file.read_text(encoding="utf-8")


def test_ensure_dokploy_api_auth_reuses_valid_existing_key_even_when_password_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "changed-password",
            "DOKPLOY_API_KEY": "valid-key",
            "DOKPLOY_API_URL": "http://127.0.0.1:3000",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    class ValidDokployClient:
        def __init__(self, *, api_url: str, api_key: str) -> None:
            assert api_url == "http://127.0.0.1:3000"
            assert api_key == "valid-key"

        def list_projects(self) -> tuple[object, ...]:
            return ()

        def ai_providers_all(self) -> tuple[object, ...]:
            return ()

    def fail_refresh(**_: object) -> object:
        raise AssertionError("bootstrap auth refresh should not be called")

    monkeypatch.setattr(cli, "DokployApiClient", ValidDokployClient)
    monkeypatch.setattr(cli, "_refresh_local_dokploy_api_key", fail_refresh)

    updated = cli._ensure_dokploy_api_auth(
        env_file=env_file,
        raw_env=raw_env,
        desired_state=desired_state,
        bootstrap_backend=FakeBootstrapBackend(),
        dry_run=False,
        require_real_dokploy_auth=True,
    )

    assert updated.values["DOKPLOY_API_KEY"] == "valid-key"
    assert updated.values["DOKPLOY_API_URL"] == "http://127.0.0.1:3000"


def test_handle_install_warns_on_broad_env_file_permissions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    monkeypatch.setattr(cli, "run_install_flow", lambda **_: {"ok": True})

    result = cli._handle_install(
        argparse.Namespace(
            env_file=env_file,
            state_dir=tmp_path / "state",
            dry_run=False,
            non_interactive=True,
            no_print_secrets=False,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"ok": true' in captured.out
    assert "permissions are broader than owner-only" in captured.err


def test_handle_install_dry_run_skips_env_file_permission_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text("STACK_NAME=example\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    env_file.chmod(0o644)

    monkeypatch.setattr(cli, "run_install_flow", lambda **_: {"ok": True})

    result = cli._handle_install(
        argparse.Namespace(
            env_file=env_file,
            state_dir=tmp_path / "state",
            dry_run=True,
            non_interactive=True,
            no_print_secrets=False,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""


def test_ensure_dokploy_api_auth_fails_when_auth_cannot_be_bootstrapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "install.env"
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            del base_url

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            raise DokployBootstrapAuthError("no working auth endpoint")

    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)

    with pytest.raises(DokployBootstrapAuthError, match="no working auth endpoint"):
        cli._ensure_dokploy_api_auth(
            env_file=env_file,
            raw_env=raw_env,
            desired_state=desired_state,
            bootstrap_backend=FakeBootstrapBackend(),
            dry_run=False,
            require_real_dokploy_auth=True,
        )


def test_run_install_flow_persists_scaffold_before_auth_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
        },
    )
    order: list[str] = []

    class FakeBootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("install should not be called")

    monkeypatch.setattr(
        cli,
        "collect_host_facts",
        lambda _: _host_facts(),
    )
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda *_, **__: PreflightReport(
            host_facts=_host_facts(),
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "_prepare_install_host_prerequisites",
        lambda **kwargs: (kwargs["host_facts"], {}),
    )

    def fake_persist_install_scaffold(
        state_dir: Path, scaffold_raw_env: RawEnvInput, scaffold_desired_state: object
    ) -> None:
        del state_dir, scaffold_desired_state
        order.append("persist")
        assert "DOKPLOY_API_KEY" not in scaffold_raw_env.values

    def fake_ensure_dokploy_api_auth(**kwargs: object) -> RawEnvInput:
        del kwargs
        order.append("ensure")
        raise RuntimeError("stop after auth ordering check")

    monkeypatch.setattr(cli, "persist_install_scaffold", fake_persist_install_scaffold)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", fake_ensure_dokploy_api_auth)

    with pytest.raises(RuntimeError, match="stop after auth ordering check"):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=FakeBootstrapBackend(),
        )

    assert order == ["persist", "ensure"]


def test_install_rejects_mock_contamination_before_auth_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "install.env"
    state_dir = tmp_path / "state"
    env_path.write_text(
        (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    def fail_if_scaffold_called(*_: object, **__: object) -> None:
        raise AssertionError("persist_install_scaffold should not be reached")

    def fail_if_called(**_: object) -> RawEnvInput:
        raise AssertionError("_ensure_dokploy_api_auth should not be reached")

    monkeypatch.setattr(cli, "persist_install_scaffold", fail_if_scaffold_called)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", fail_if_called)

    with pytest.raises(SystemExit, match="live/pre-live runs require real integrations") as error:
        cli._handle_install(
            argparse.Namespace(
                env_file=env_path,
                state_dir=state_dir,
                dry_run=False,
                non_interactive=True,
            )
        )

    message = str(error.value)
    assert not state_dir.exists()
    assert "CLOUDFLARE_MOCK_ACCOUNT_OK" in message
    assert "CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID" in message
    assert "DOKPLOY_BOOTSTRAP_HEALTHY" in message
    assert "DOKPLOY_BOOTSTRAP_MOCK_API_KEY" in message
    assert "DOKPLOY_MOCK_API_MODE" in message
    assert "HEADSCALE_MOCK_HEALTHY" in message


def test_install_rejects_blocking_live_drift_before_auth_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "install.env"
    state_dir = tmp_path / "state"
    clean_env = "\n".join(
        line
        for line in (FIXTURES_DIR / "lifecycle-headscale.env")
        .read_text(encoding="utf-8")
        .splitlines()
        if not line.startswith(
            (
                "CLOUDFLARE_MOCK_",
                "DOKPLOY_BOOTSTRAP_",
                "DOKPLOY_MOCK_",
                "HEADSCALE_MOCK_",
            )
        )
    )
    env_path.write_text(
        clean_env + "\n",
        encoding="utf-8",
    )

    def fail_if_scaffold_called(*_: object, **__: object) -> None:
        raise AssertionError("persist_install_scaffold should not be reached")

    def fail_if_called(**_: object) -> RawEnvInput:
        raise AssertionError("_ensure_dokploy_api_auth should not be reached")

    monkeypatch.setattr(cli, "persist_install_scaffold", fail_if_scaffold_called)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", fail_if_called)
    monkeypatch.setattr(
        cli,
        "build_live_drift_report",
        lambda **_: {
            "detected": True,
            "entries": [
                {
                    "classification": "manual_collision",
                    "detail": "manual openclaw collision",
                    "live_kind": "container",
                    "live_name": "openclaw-manual",
                    "pack": "openclaw",
                },
                {
                    "classification": "manual_collision",
                    "detail": "manual my-farm collision",
                    "live_kind": "container",
                    "live_name": "wizard-stack-my-farm-advisor",
                    "pack": "my-farm-advisor",
                },
                {
                    "classification": "wizard_managed",
                    "detail": "managed my-farm unhealthy",
                    "expected_service_name": "wizard-stack-my-farm-advisor",
                    "health": "unhealthy",
                    "live_name": "wizard-stack-my-farm-advisor",
                    "managed": True,
                    "pack": "my-farm-advisor",
                    "scope": "stack:wizard-stack:my-farm-advisor",
                },
            ],
            "inspection": {
                "docker": {"available": True, "detail": "docker inspected"},
                "host_routes": {"available": True, "detail": "routes inspected"},
            },
            "status": "drift_detected",
            "summary": {
                "wizard_managed": 1,
                "manual_collision": 2,
                "host_local_route": 0,
                "unknown_unmanaged": 0,
            },
        },
    )

    with pytest.raises(SystemExit, match="Live drift is not allowed") as error:
        cli._handle_install(
            argparse.Namespace(
                env_file=env_path,
                state_dir=state_dir,
                dry_run=False,
                non_interactive=True,
                no_print_secrets=False,
                allow_memory_shortfall=False,
            )
        )

    message = str(error.value)
    assert not state_dir.exists()
    assert "openclaw-manual" in message
    assert "wizard-stack-my-farm-advisor" in message
    assert "Migrate or remove the unowned runtime" in message
    assert "inspect-state reports clean" in message


def test_install_allows_clean_live_drift_report_to_proceed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts())
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr(
        cli,
        "build_live_drift_report",
        lambda **_: {
            "detected": False,
            "entries": [],
            "inspection": {
                "docker": {"available": True, "detail": "docker inspected"},
                "host_routes": {"available": True, "detail": "routes inspected"},
            },
            "status": "clean",
            "summary": {
                "wizard_managed": 0,
                "manual_collision": 0,
                "host_local_route": 0,
                "unknown_unmanaged": 0,
            },
        },
    )
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["ok"] is True


def test_install_retry_accepts_temp_env_without_dokploy_admin_creds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    existing_raw = RawEnvInput(
        format_version=1,
        values={
            **parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env").values,
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
            "DOKPLOY_API_URL": "http://127.0.0.1:3000",
            "DOKPLOY_API_KEY": "dokp-key-123",
        },
    )
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )
    requested_raw = RawEnvInput(
        format_version=existing_raw.format_version,
        values={
            key: value
            for key, value in existing_raw.values.items()
            if key not in {"DOKPLOY_ADMIN_EMAIL", "DOKPLOY_ADMIN_PASSWORD"}
        },
    )
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.temp-no-admin.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=requested_raw,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["ok"] is True


def test_dokploy_mutation_auth_qualification_detects_write_auth_failure_after_list_projects() -> (
    None
):
    class FailingMutationClient:
        def __init__(self, *, api_url: str, api_key: str, **_: Any) -> None:
            assert api_url == "http://127.0.0.1:3000"
            assert api_key == "dokp-key-123"

        def list_projects(self) -> tuple[object, ...]:
            return ()

        def create_project(self, *, name: str, description: str | None, env: str | None) -> object:
            del name, description, env
            raise DokployApiError(
                'Dokploy API request failed with status 401: {"message":"Unauthorized"}.'
            )

    raw_env = RawEnvInput(
        format_version=1,
        values={
            **{
                key: value
                for key, value in parse_env_file(
                    FIXTURES_DIR / "lifecycle-headscale.env"
                ).values.items()
                if key not in {"DOKPLOY_BOOTSTRAP_MOCK_API_KEY", "DOKPLOY_MOCK_API_MODE"}
            },
            "DOKPLOY_API_URL": "http://127.0.0.1:3000",
            "DOKPLOY_API_KEY": "dokp-key-123",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(cli, "DokployApiClient", FailingMutationClient)
        monkeypatch.setattr(cli, "_build_dokploy_session_client", lambda **_: None)

        with pytest.raises(
            StateValidationError,
            match="Dokploy mutation auth qualification failed during project.create",
        ):
            cli._qualify_dokploy_mutation_auth(
                raw_env=raw_env,
                desired_state=desired_state,
                dry_run=False,
                require_real_dokploy_auth=True,
            )


def test_dokploy_mutation_auth_qualification_reuses_existing_probe_when_create_conflicts() -> None:
    recorded: list[str] = []

    class DuplicateThenUpdateClient:
        def __init__(self, *, api_url: str, api_key: str, **_: Any) -> None:
            assert api_url == "http://127.0.0.1:3000"
            assert api_key == "dokp-key-123"

        def list_projects(self) -> tuple[object, ...]:
            return (
                DokployProjectSummary(
                    project_id="proj-1",
                    name="wizard-stack-dokploy-wizard-auth-probe",
                    environments=(
                        DokployEnvironmentSummary(
                            environment_id="env-1",
                            name="default",
                            is_default=True,
                            composes=(
                                DokployComposeSummary(
                                    compose_id="cmp-1",
                                    name="wizard-stack-dokploy-wizard-auth-probe",
                                    status="done",
                                ),
                            ),
                        ),
                    ),
                ),
            )

        def create_project(self, *, name: str, description: str | None, env: str | None) -> object:
            del name, description, env
            raise DokployApiError(
                "Dokploy API request failed with status 409: compose already exists."
            )

        def update_compose(self, *, compose_id: str, compose_file: str) -> object:
            del compose_file
            recorded.append(f"update:{compose_id}")
            return DokployComposeRecord(
                compose_id=compose_id, name="wizard-stack-dokploy-wizard-auth-probe"
            )

        def deploy_compose(
            self, *, compose_id: str, title: str | None, description: str | None
        ) -> object:
            del title, description
            recorded.append(f"deploy:{compose_id}")
            return DokployDeployResult(success=True, compose_id=compose_id, message=None)

    raw_env = RawEnvInput(
        format_version=1,
        values={
            **{
                key: value
                for key, value in parse_env_file(
                    FIXTURES_DIR / "lifecycle-headscale.env"
                ).values.items()
                if key not in {"DOKPLOY_BOOTSTRAP_MOCK_API_KEY", "DOKPLOY_MOCK_API_MODE"}
            },
            "STACK_NAME": "wizard-stack",
            "DOKPLOY_API_URL": "http://127.0.0.1:3000",
            "DOKPLOY_API_KEY": "dokp-key-123",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(cli, "DokployApiClient", DuplicateThenUpdateClient)
        monkeypatch.setattr(cli, "_build_dokploy_session_client", lambda **_: None)
        cli._qualify_dokploy_mutation_auth(
            raw_env=raw_env,
            desired_state=desired_state,
            dry_run=False,
            require_real_dokploy_auth=True,
        )

    assert recorded == ["update:cmp-1", "deploy:cmp-1"]


def test_dokploy_mutation_auth_qualification_fails_fast_when_deploy_remains_unauthorized() -> None:
    class DeployUnauthorizedClient:
        def __init__(self, *, api_url: str, api_key: str, **_: Any) -> None:
            assert api_url == "http://127.0.0.1:3000"
            assert api_key == "dokp-key-123"

        def list_projects(self) -> tuple[object, ...]:
            return ()

        def create_project(self, *, name: str, description: str | None, env: str | None) -> object:
            del name, description, env
            return type("_Created", (), {"project_id": "proj-1", "environment_id": "env-1"})()

        def create_compose(
            self, *, name: str, environment_id: str, compose_file: str, app_name: str
        ) -> object:
            del name, environment_id, compose_file, app_name
            return DokployComposeRecord(
                compose_id="cmp-1", name="wizard-stack-dokploy-wizard-auth-probe"
            )

        def update_compose(self, *, compose_id: str, compose_file: str) -> object:
            del compose_file
            return DokployComposeRecord(
                compose_id=compose_id, name="wizard-stack-dokploy-wizard-auth-probe"
            )

        def deploy_compose(
            self, *, compose_id: str, title: str | None, description: str | None
        ) -> object:
            del compose_id, title, description
            raise DokployApiError(
                'Dokploy API request failed with status 401: {"message":"Unauthorized"}.'
            )

    raw_env = RawEnvInput(
        format_version=1,
        values={
            **{
                key: value
                for key, value in parse_env_file(
                    FIXTURES_DIR / "lifecycle-headscale.env"
                ).values.items()
                if key not in {"DOKPLOY_BOOTSTRAP_MOCK_API_KEY", "DOKPLOY_MOCK_API_MODE"}
            },
            "STACK_NAME": "wizard-stack",
            "DOKPLOY_API_URL": "http://127.0.0.1:3000",
            "DOKPLOY_API_KEY": "dokp-key-123",
        },
    )
    desired_state = resolve_desired_state(raw_env)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(cli, "DokployApiClient", DeployUnauthorizedClient)
        monkeypatch.setattr(cli, "_build_dokploy_session_client", lambda **_: None)
        with pytest.raises(
            StateValidationError,
            match="Dokploy mutation auth qualification failed during compose.deploy",
        ):
            cli._qualify_dokploy_mutation_auth(
                raw_env=raw_env,
                desired_state=desired_state,
                dry_run=False,
                require_real_dokploy_auth=True,
            )


def test_live_drift_entry_blocks_mutation_allows_missing_managed_service() -> None:
    assert (
        cli._live_drift_entry_blocks_mutation(
            {
                "classification": "wizard_managed",
                "pack": "openclaw",
                "health": "missing",
                "live_name": "openmerge-openclaw",
            }
        )
        is False
    )
    assert (
        cli._live_drift_entry_blocks_mutation(
            {
                "classification": "wizard_managed",
                "pack": "openclaw",
                "health": "unhealthy",
                "live_name": "openmerge-openclaw",
            }
        )
        is True
    )
    assert (
        cli._live_drift_entry_blocks_mutation(
            {
                "classification": "wizard_managed",
                "pack": "openclaw",
                "health": "unknown",
                "live_name": "openmerge-openclaw",
            }
        )
        is True
    )


def test_install_allows_missing_managed_service_drift_to_proceed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts())
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr(
        cli,
        "build_live_drift_report",
        lambda **_: {
            "detected": True,
            "entries": [
                {
                    "classification": "wizard_managed",
                    "detail": "managed openclaw missing",
                    "expected_service_name": "wizard-stack-openclaw",
                    "health": "missing",
                    "live_name": "wizard-stack-openclaw",
                    "managed": True,
                    "pack": "openclaw",
                    "scope": "stack:wizard-stack:openclaw",
                }
            ],
            "inspection": {
                "docker": {"available": True, "detail": "docker inspected"},
                "host_routes": {"available": True, "detail": "routes inspected"},
            },
            "status": "drift_detected",
            "summary": {
                "wizard_managed": 1,
                "manual_collision": 0,
                "host_local_route": 0,
                "unknown_unmanaged": 0,
            },
        },
    )
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["ok"] is True


def test_docker_hub_auth_no_keys_skips_login(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fail_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        raise AssertionError("docker login should not run without Docker Hub credentials")

    monkeypatch.setattr(cli.subprocess, "run", fail_run)

    credentials = cli._docker_hub_credentials_from_env(
        _raw_input({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"})
    )
    cli._docker_login_if_configured(credentials)

    assert credentials is None
    assert calls == []


def test_docker_hub_auth_runs_login_with_pat_on_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "test-docker-token-not-for-output"
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(
            {
                "command": command,
                "input": input,
                "text": text,
                "capture_output": capture_output,
                "check": check,
                "timeout": timeout,
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout="Login Succeeded", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    credentials = cli._docker_hub_credentials_from_env(
        _raw_input({"DOCKER_USERNAME": "operator", "DOCKER_PAT": token})
    )
    cli._docker_login_if_configured(credentials)

    assert captured["command"] == [
        "docker",
        "login",
        "--username",
        "operator",
        "--password-stdin",
    ]
    assert captured["input"] == token
    assert captured["text"] is True
    assert captured["capture_output"] is True
    assert captured["check"] is False
    assert captured["timeout"] == 30
    assert token not in " ".join(cast(list[str], captured["command"]))


def test_docker_hub_auth_partial_keys_fail_before_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "test-docker-token-not-for-output"
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("partial Docker Hub credentials must fail early"),
    )

    with pytest.raises(StateValidationError) as exc_info:
        cli._docker_hub_credentials_from_env(_raw_input({"DOCKER_PAT": token}))

    message = str(exc_info.value)
    assert "DOCKER_USERNAME and DOCKER_PAT" in message
    assert "Missing: DOCKER_USERNAME" in message
    assert token not in message


def test_docker_hub_auth_failure_redacts_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "plain-test-docker-token-not-matching-generic-redactor"

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del input, text, capture_output, check, timeout
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=f"stdout leaked {token}",
            stderr=f"stderr leaked {token}",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    credentials = cli._docker_hub_credentials_from_env(
        _raw_input({"DOCKER_USERNAME": "operator", "DOCKER_PAT": token})
    )
    with pytest.raises(StateValidationError) as exc_info:
        cli._docker_login_if_configured(credentials)

    message = str(exc_info.value)
    assert "Docker Hub login failed for username 'operator'" in message
    assert token not in message
    assert "<REDACTED>" in message


def test_docker_hub_auth_is_removed_from_persisted_raw_input() -> None:
    raw_env = _raw_input(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOCKER_USERNAME": "operator",
            "DOCKER_PAT": "plain-test-docker-token-not-for-state",
        }
    )

    persistable_raw_env = cli._state_persistable_raw_env_input(raw_env)

    assert "DOCKER_USERNAME" not in persistable_raw_env.values
    assert "DOCKER_PAT" not in persistable_raw_env.values
    assert persistable_raw_env.values == {
        "STACK_NAME": "wizard-stack",
        "ROOT_DOMAIN": "example.com",
    }


def test_install_runs_docker_hub_login_before_lifecycle_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base_raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    raw_env = RawEnvInput(
        format_version=base_raw_env.format_version,
        values={
            **base_raw_env.values,
            "DOCKER_USERNAME": "operator",
            "DOCKER_PAT": "test-docker-token-not-for-output",
        },
    )
    order: list[str] = []

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts())
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    _stub_install_flow_after_preflight(monkeypatch)
    monkeypatch.setattr(
        cli,
        "_docker_login_if_configured",
        lambda credentials: order.append("docker-login"),
    )
    monkeypatch.setattr(
        cli,
        "execute_lifecycle_plan",
        lambda **kwargs: order.append("execute-lifecycle") or {"ok": True},
    )

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert summary["ok"] is True
    assert order == ["docker-login", "execute-lifecycle"]


def test_install_does_not_persist_docker_hub_auth_to_raw_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base_raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    token = "plain-test-docker-token-not-for-state"
    raw_env = RawEnvInput(
        format_version=base_raw_env.format_version,
        values={
            **base_raw_env.values,
            "DOCKER_USERNAME": "operator",
            "DOCKER_PAT": token,
        },
    )

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts())
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_docker_login_if_configured", lambda credentials: None)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    raw_input_json = (tmp_path / "state" / "raw-input.json").read_text(encoding="utf-8")
    persisted_raw = load_state_dir(tmp_path / "state").raw_input
    assert summary["ok"] is True
    assert persisted_raw is not None
    assert "DOCKER_USERNAME" not in persisted_raw.values
    assert "DOCKER_PAT" not in persisted_raw.values
    assert token not in raw_input_json


def test_install_bootstraps_missing_docker_before_strict_preflight_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(docker_installed=False, docker_daemon_reachable=False)
    remediated_host = _host_facts(docker_installed=True, docker_daemon_reachable=True)
    collected: list[HostFacts] = []
    remediation_calls: list[dict[str, object]] = []

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            return package_name != "docker.io"

        def docker_daemon_reachable(self) -> bool:
            return False

    host_fact_sequence = iter((initial_host, remediated_host))
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: next(host_fact_sequence))
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda *, assessment, backend: remediation_calls.append(
            {
                "backend": backend,
                "missing_packages": assessment.missing_packages,
                "outcome": assessment.outcome,
            }
        ),
    )
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})

    def record_preflight(
        desired_state: object,
        host_facts: HostFacts,
        *,
        allow_memory_shortfall: bool = False,
    ) -> PreflightReport:
        del desired_state
        del allow_memory_shortfall
        collected.append(host_facts)
        return PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        )

    monkeypatch.setattr(cli, "run_preflight", record_preflight)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert collected == [remediated_host]
    assert remediation_calls == [
        {
            "backend": remediation_calls[0]["backend"],
            "missing_packages": (),
            "outcome": "missing_prerequisites",
        }
    ]
    assessment = summary["host_prerequisites"]["assessment"]
    assert assessment["outcome"] == "missing_prerequisites"
    assert assessment["docker_bootstrap_required"] is True
    assert assessment["missing_packages"] == []
    assert summary["host_prerequisites"]["post_remediation_host_facts"] == remediated_host.to_dict()
    assert summary["host_prerequisites"]["remediation_actions"] == [
        {
            "action": "bootstrap_docker_engine",
            "packages": [
                "docker-ce",
                "docker-ce-cli",
                "containerd.io",
                "docker-buildx-plugin",
                "docker-compose-plugin",
            ],
            "repository": "official_docker_apt_repository",
        },
        {"action": "ensure_docker_daemon"},
    ]
    assert summary["host_prerequisites"]["remediation_attempted"] is True


def test_install_bootstraps_missing_docker_on_supported_ubuntu_patch_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(
        distribution_id="ubuntu",
        version_id="24.04.2 LTS",
        docker_installed=False,
        docker_daemon_reachable=False,
    )
    remediated_host = _host_facts(
        distribution_id="ubuntu",
        version_id="24.04.2 LTS",
        docker_installed=True,
        docker_daemon_reachable=True,
    )
    remediation_calls: list[tuple[str, ...]] = []

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            return package_name != "docker.io"

        def docker_daemon_reachable(self) -> bool:
            return False

    host_fact_sequence = iter((initial_host, remediated_host))
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: next(host_fact_sequence))
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda *, assessment, backend: remediation_calls.append(assessment.missing_packages),
    )
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda desired_state, host_facts, *, allow_memory_shortfall=False: PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
    )

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert remediation_calls == [()]
    assert (
        summary["host_prerequisites"]["post_remediation_host_facts"]["version_id"] == "24.04.2 LTS"
    )


def test_install_waits_for_docker_readiness_after_remediation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(docker_installed=False, docker_daemon_reachable=False)
    delayed_host = _host_facts(docker_installed=True, docker_daemon_reachable=False)
    ready_host = _host_facts(docker_installed=True, docker_daemon_reachable=True)
    sleep_calls: list[float] = []

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            return package_name != "docker.io"

        def docker_daemon_reachable(self) -> bool:
            return False

    host_fact_sequence = iter((initial_host, delayed_host, ready_host))
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: next(host_fact_sequence))
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(cli, "time", cast(Any, type("_Clock", (), {"sleep": sleep_calls.append})()))
    monkeypatch.setattr(cli, "remediate_host_prerequisites", lambda **_: None)
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(
        cli,
        "run_preflight",
        lambda desired_state, host_facts, *, allow_memory_shortfall=False: PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
    )

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert sleep_calls == [1.0]
    assert summary["host_prerequisites"]["post_remediation_host_facts"] == ready_host.to_dict()


def test_install_on_unsupported_host_refuses_remediation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    unsupported_host = _host_facts(distribution_id="debian", version_id="12")

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: unsupported_host)
    monkeypatch.setattr(
        cli,
        "UbuntuAptHostPrerequisiteBackend",
        lambda _: pytest.fail("unsupported host should fail before backend construction"),
    )
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda **_: pytest.fail("unsupported host should not attempt remediation"),
    )

    with pytest.raises(PreflightError, match="unsupported host OS 'debian 12'"):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
        )


def test_install_attempts_docker_service_readiness_before_strict_preflight_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(docker_installed=True, docker_daemon_reachable=False)
    remediated_host = _host_facts(docker_installed=True, docker_daemon_reachable=True)
    collected: list[HostFacts] = []
    remediation_calls: list[tuple[str, ...]] = []

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            del package_name
            return True

        def docker_daemon_reachable(self) -> bool:
            return False

    host_fact_sequence = iter((initial_host, remediated_host))
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: next(host_fact_sequence))
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda *, assessment, backend: remediation_calls.append(assessment.missing_packages),
    )
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})

    def record_preflight(
        desired_state: object,
        host_facts: HostFacts,
        *,
        allow_memory_shortfall: bool = False,
    ) -> PreflightReport:
        del desired_state
        del allow_memory_shortfall
        collected.append(host_facts)
        return PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        )

    monkeypatch.setattr(cli, "run_preflight", record_preflight)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert collected == [remediated_host]
    assert remediation_calls == [()]
    assert summary["host_prerequisites"]["remediation_actions"] == [
        {"action": "ensure_docker_daemon"}
    ]


def test_install_leaves_supported_host_prerequisites_as_idempotent_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    ready_host = _host_facts()
    collect_calls: list[RawEnvInput] = []
    preflight_hosts: list[HostFacts] = []

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            del package_name
            return True

        def docker_daemon_reachable(self) -> bool:
            return True

    def collect_ready_host(raw: RawEnvInput) -> HostFacts:
        collect_calls.append(raw)
        return ready_host

    def record_preflight(
        desired_state: object,
        host_facts: HostFacts,
        *,
        allow_memory_shortfall: bool = False,
    ) -> PreflightReport:
        del desired_state
        del allow_memory_shortfall
        preflight_hosts.append(host_facts)
        return PreflightReport(
            host_facts=host_facts,
            required_profile=derive_required_profile(resolve_desired_state(raw_env)),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        )

    monkeypatch.setattr(cli, "collect_host_facts", collect_ready_host)
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda **_: pytest.fail("satisfied host prerequisites should not trigger remediation"),
    )
    monkeypatch.setattr(cli, "run_preflight", record_preflight)
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
    )

    assert collect_calls == [raw_env]
    assert preflight_hosts == [ready_host]
    assert summary["host_prerequisites"]["assessment"]["outcome"] == "noop"
    assert summary["host_prerequisites"]["assessment"]["missing_packages"] == []
    assert summary["host_prerequisites"]["assessment"]["notes"] == [
        "Baseline Ubuntu 24.04 host prerequisites are already satisfied."
    ]
    assert summary["host_prerequisites"]["assessment"]["docker_bootstrap_required"] is False
    assert [check["name"] for check in summary["host_prerequisites"]["assessment"]["checks"]] == [
        "os_support",
        "git",
        "curl",
        "ca_certificates",
        "docker_cli",
        "docker_daemon",
    ]
    assert summary["host_prerequisites"]["remediation_actions"] == []
    assert summary["host_prerequisites"]["remediation_attempted"] is False


def test_run_lifecycle_flow_reuses_one_dokploy_session_client_across_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "full.env")
    values = dict(raw_env.values)
    values.pop("DOKPLOY_BOOTSTRAP_MOCK_API_KEY", None)
    values["DOKPLOY_API_URL"] = "http://127.0.0.1:3000"
    values["DOKPLOY_API_KEY"] = "dokp-key-123"
    values["DOKPLOY_ADMIN_EMAIL"] = "admin@example.com"
    values["DOKPLOY_ADMIN_PASSWORD"] = "secret-123"
    values["ENABLE_SEAWEEDFS"] = "true"
    values["SEAWEEDFS_ACCESS_KEY"] = "seaweed-access"
    values["SEAWEEDFS_SECRET_KEY"] = "seaweed-secret"
    raw_env = RawEnvInput(format_version=raw_env.format_version, values=values)
    seen_session_clients: list[object] = []
    sentinel_session_client = object()

    class FakeLoadedState:
        raw_input = None
        desired_state = None
        applied_state = None
        ownership_ledger = None

    required_profile = derive_required_profile(resolve_desired_state(raw_env))

    monkeypatch.setattr(cli, "load_state_dir", lambda state_dir: FakeLoadedState())
    monkeypatch.setattr(cli, "parse_env_file", lambda env_file: raw_env)
    monkeypatch.setattr(cli, "collect_host_facts", lambda raw: _host_facts())
    monkeypatch.setattr(
        cli,
        "_run_preflight_report",
        lambda **_: PreflightReport(
            host_facts=_host_facts(),
            required_profile=required_profile,
            checks=(PreflightCheck(name="preflight", status="pass", detail="ok"),),
            advisories=(),
        ),
    )
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "_qualify_dokploy_mutation_auth", lambda **kwargs: None)
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(
        cli,
        "_build_dokploy_session_client",
        lambda **kwargs: sentinel_session_client,
    )

    def record_backend(**kwargs: Any) -> object:
        seen_session_clients.append(kwargs["session_client"])
        return cast(Any, object())

    monkeypatch.setattr(cli, "_build_shared_core_backend", record_backend)
    monkeypatch.setattr(cli, "_build_headscale_backend", record_backend)
    monkeypatch.setattr(cli, "_build_matrix_backend", record_backend)
    monkeypatch.setattr(cli, "_build_nextcloud_backend", record_backend)
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", record_backend)
    monkeypatch.setattr(cli, "_build_coder_backend", record_backend)
    monkeypatch.setattr(cli, "_build_openclaw_backend", record_backend)
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})

    cli._run_lifecycle_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
        tailscale_backend=None,
        networking_backend=None,
        shared_core_backend=None,
        headscale_backend=None,
        matrix_backend=None,
        nextcloud_backend=None,
        seaweedfs_backend=None,
        coder_backend=None,
        openclaw_backend=None,
        allow_modify=False,
        remediate_install_host_prereqs=False,
        allow_memory_shortfall=True,
        prompt_for_memory_shortfall=False,
        enforce_live_run_contamination_check=False,
    )

    assert len(seen_session_clients) == 7
    assert all(client is sentinel_session_client for client in seen_session_clients)


def test_run_lifecycle_flow_passes_state_dir_to_nextcloud_moodle_and_docuseal_builders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "moodle-docuseal.env")
    state_dir = tmp_path / "state"

    class FakeLoadedState:
        raw_input = None
        desired_state = None
        applied_state = None
        ownership_ledger = None

    required_profile = derive_required_profile(resolve_desired_state(raw_env))
    seen: dict[str, Path] = {}

    monkeypatch.setattr(cli, "load_state_dir", lambda state_dir: FakeLoadedState())
    monkeypatch.setattr(cli, "parse_env_file", lambda env_file: raw_env)
    monkeypatch.setattr(cli, "collect_host_facts", lambda raw: _host_facts())
    monkeypatch.setattr(
        cli,
        "_run_preflight_report",
        lambda **_: PreflightReport(
            host_facts=_host_facts(),
            required_profile=required_profile,
            checks=(PreflightCheck(name="preflight", status="pass", detail="ok"),),
            advisories=(),
        ),
    )
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "_qualify_dokploy_mutation_auth", lambda **kwargs: None)
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_dokploy_session_client", lambda **kwargs: object())
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_coder_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_openclaw_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})

    monkeypatch.setattr(
        cli,
        "_build_nextcloud_backend",
        lambda **kwargs: seen.setdefault("nextcloud", kwargs["state_dir"]) or cast(Any, object()),
    )
    monkeypatch.setattr(
        cli,
        "_build_moodle_backend",
        lambda **kwargs: seen.setdefault("moodle", kwargs["state_dir"]) or cast(Any, object()),
    )
    monkeypatch.setattr(
        cli,
        "_build_docuseal_backend",
        lambda **kwargs: seen.setdefault("docuseal", kwargs["state_dir"]) or cast(Any, object()),
    )

    cli._run_lifecycle_flow(
        env_file=tmp_path / "install.env",
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
        tailscale_backend=None,
        networking_backend=None,
        shared_core_backend=None,
        headscale_backend=None,
        matrix_backend=None,
        nextcloud_backend=None,
        seaweedfs_backend=None,
        coder_backend=None,
        openclaw_backend=None,
        allow_modify=False,
        remediate_install_host_prereqs=False,
        allow_memory_shortfall=True,
        prompt_for_memory_shortfall=False,
        enforce_live_run_contamination_check=False,
    )

    assert seen == {
        "nextcloud": state_dir,
        "moodle": state_dir,
        "docuseal": state_dir,
    }


def test_install_prompts_before_continuing_on_memory_only_shortfall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(memory_gb=3))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr("builtins.input", fake_input)
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
        prompt_for_memory_shortfall=True,
    )

    assert prompts == [
        "Memory shortfall warning: insufficient memory for Core: need 4 GB, found 3 GB. "
        "This host is below the recommended memory target for the selected scope "
        "and may be unstable or underprovisioned. "
        "Proceed anyway? [y/N] "
    ]
    assert summary["ok"] is True


def test_install_allows_non_interactive_memory_shortfall_with_explicit_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(memory_gb=3))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail(
            f"unexpected prompt with explicit memory override flag: {prompt}"
        ),
    )
    _stub_install_flow_after_preflight(monkeypatch)

    summary = cli.run_install_flow(
        env_file=tmp_path / "install.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=_FakeBootstrapBackend(),
        allow_memory_shortfall=True,
    )

    assert summary["ok"] is True


def test_install_requires_allow_memory_shortfall_flag_for_non_interactive_memory_only_shortfall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(memory_gb=3))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    _stub_install_flow_after_preflight(monkeypatch)

    with pytest.raises(
        PreflightError,
        match="Rerun install with --allow-memory-shortfall to continue non-interactively",
    ):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
        )


def test_install_does_not_allow_cpu_shortfall_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(cpu_count=1, memory_gb=16))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail(f"unexpected prompt for hard-stop preflight failure: {prompt}"),
    )
    _stub_install_flow_after_preflight(monkeypatch)

    with pytest.raises(PreflightError, match="insufficient CPU for Core: need 2 vCPU, found 1"):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
            allow_memory_shortfall=True,
            prompt_for_memory_shortfall=True,
        )


def test_install_does_not_allow_disk_shortfall_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: _host_facts(disk_gb=20, memory_gb=16))
    monkeypatch.setattr(cli, "_host_supports_prerequisite_remediation", lambda _: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail(f"unexpected prompt for hard-stop preflight failure: {prompt}"),
    )
    _stub_install_flow_after_preflight(monkeypatch)

    with pytest.raises(PreflightError, match="insufficient disk for Core: need 40 GB, found 20 GB"):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
            allow_memory_shortfall=True,
            prompt_for_memory_shortfall=True,
        )


def test_install_reports_explicit_rerun_with_sudo_guidance_on_apt_privilege_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_env = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    initial_host = _host_facts(docker_installed=False, docker_daemon_reachable=False)

    class FakeHostPrereqBackend:
        def package_installed(self, package_name: str) -> bool:
            return package_name != "docker.io"

        def docker_daemon_reachable(self) -> bool:
            return False

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: initial_host)
    monkeypatch.setattr(cli, "UbuntuAptHostPrerequisiteBackend", lambda _: FakeHostPrereqBackend())
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda **_: (_ for _ in ()).throw(
            StateValidationError(
                "Baseline host prerequisite remediation requires apt/systemd privileges; "
                "rerun dokploy-wizard install as root or with sudo."
            )
        ),
    )

    with pytest.raises(
        StateValidationError,
        match="rerun dokploy-wizard install as root or with sudo",
    ):
        cli.run_install_flow(
            env_file=tmp_path / "install.env",
            state_dir=tmp_path / "state",
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_FakeBootstrapBackend(),
        )


def test_modify_rejects_mock_contamination_before_auth_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    existing_raw = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )
    modify_env_path = tmp_path / "modify.env"
    modify_env_path.write_text(
        (FIXTURES_DIR / "lifecycle-headscale.env")
        .read_text(encoding="utf-8")
        .replace("ROOT_DOMAIN=example.com", "ROOT_DOMAIN=example.net"),
        encoding="utf-8",
    )

    def fail_if_called(**_: object) -> RawEnvInput:
        raise AssertionError("_ensure_dokploy_api_auth should not be reached")

    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", fail_if_called)

    with pytest.raises(SystemExit, match="live/pre-live runs require real integrations") as error:
        cli._handle_modify(
            argparse.Namespace(
                env_file=modify_env_path,
                state_dir=state_dir,
                dry_run=False,
                non_interactive=True,
            )
        )

    message = str(error.value)
    assert "CLOUDFLARE_MOCK_ACCOUNT_OK" in message
    assert "CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID" in message
    assert "DOKPLOY_BOOTSTRAP_HEALTHY" in message
    assert "DOKPLOY_BOOTSTRAP_MOCK_API_KEY" in message
    assert "DOKPLOY_MOCK_API_MODE" in message
    assert "HEADSCALE_MOCK_HEALTHY" in message


def test_modify_rejects_host_local_route_drift_before_auth_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    clean_env = "\n".join(
        line
        for line in (FIXTURES_DIR / "lifecycle-headscale.env")
        .read_text(encoding="utf-8")
        .splitlines()
        if not line.startswith(
            (
                "CLOUDFLARE_MOCK_",
                "DOKPLOY_BOOTSTRAP_",
                "DOKPLOY_MOCK_",
                "HEADSCALE_MOCK_",
            )
        )
    )
    clean_env_path = tmp_path / "existing.env"
    clean_env_path.write_text(clean_env + "\n", encoding="utf-8")
    existing_raw = parse_env_file(clean_env_path)
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )
    modify_env_path = tmp_path / "modify.env"
    modify_env_path.write_text(
        clean_env.replace("ROOT_DOMAIN=example.com", "ROOT_DOMAIN=example.net"),
        encoding="utf-8",
    )

    def fail_if_called(**_: object) -> RawEnvInput:
        raise AssertionError("_ensure_dokploy_api_auth should not be reached")

    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", fail_if_called)
    monkeypatch.setattr(
        cli,
        "build_live_drift_report",
        lambda **_: {
            "detected": True,
            "entries": [
                {
                    "classification": "host_local_route",
                    "detail": "manual my-farm route file",
                    "hostname": "farm.example.net",
                    "pack": "my-farm-advisor",
                    "path": "/etc/traefik/dynamic/openmerge-farm.yml",
                },
                {
                    "classification": "host_local_route",
                    "detail": "manual onlyoffice route file",
                    "hostname": "office.example.net",
                    "pack": "onlyoffice",
                    "path": "/etc/traefik/dynamic/openmerge-onlyoffice.yml",
                },
            ],
            "inspection": {
                "docker": {"available": True, "detail": "docker inspected"},
                "host_routes": {"available": True, "detail": "routes inspected"},
            },
            "status": "drift_detected",
            "summary": {
                "wizard_managed": 0,
                "manual_collision": 0,
                "host_local_route": 2,
                "unknown_unmanaged": 0,
            },
        },
    )

    with pytest.raises(SystemExit, match="Live drift is not allowed") as error:
        cli._handle_modify(
            argparse.Namespace(
                env_file=modify_env_path,
                state_dir=state_dir,
                dry_run=False,
                non_interactive=True,
            )
        )

    message = str(error.value)
    assert "/etc/traefik/dynamic/openmerge-farm.yml" in message
    assert "/etc/traefik/dynamic/openmerge-onlyoffice.yml" in message
    assert "Dokploy-managed ingress" in message


def test_modify_does_not_gain_host_prerequisite_remediation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    existing_raw = parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env")
    existing_desired = resolve_desired_state(existing_raw)
    write_target_state(state_dir, existing_raw, existing_desired)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=existing_desired.format_version,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=applicable_phases_for(existing_desired),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=existing_desired.format_version, resources=()),
    )

    missing_docker_host = _host_facts(docker_installed=False, docker_daemon_reachable=False)
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: missing_docker_host)
    monkeypatch.setattr(
        cli,
        "remediate_host_prerequisites",
        lambda **_: pytest.fail("modify should not attempt host prerequisite remediation"),
    )

    with pytest.raises(PreflightError, match="Docker is not installed"):
        cli.run_modify_flow(
            env_file=tmp_path / "modify.env",
            state_dir=state_dir,
            dry_run=False,
            raw_env=existing_raw,
            bootstrap_backend=_FakeBootstrapBackend(),
        )


def test_modify_help_lists_task_eleven_flags() -> None:
    result = run_cli("modify", "--help")

    assert result.returncode == 0
    assert "--env-file" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--non-interactive" in result.stdout
    assert result.stderr == ""


def test_uninstall_help_lists_task_twelve_flags() -> None:
    result = run_cli("uninstall", "--help")

    assert result.returncode == 0
    assert "--retain-data" in result.stdout
    assert "--destroy-data" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--confirm-file" in result.stdout
    assert "--non-interactive" in result.stdout
    assert result.stderr == ""


def test_pack_disable_plan_uninstalls_only_farm_resources_when_openclaw_remains() -> None:
    existing_raw = _raw_input(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "PACKS": "nextcloud,openclaw,my-farm-advisor",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
        }
    )
    requested_raw = _raw_input(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "PACKS": "nextcloud,openclaw",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
        }
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                "openclaw_service",
                "svc-openclaw",
                f"stack:{existing_desired.stack_name}:openclaw",
            ),
            OwnedResource(
                "my_farm_advisor_service",
                "svc-farm",
                f"stack:{existing_desired.stack_name}:my-farm-advisor",
            ),
            OwnedResource(
                "cloudflare_access_application",
                "app-openclaw",
                f"account:account-123:access-app:{existing_desired.hostnames['openclaw']}",
            ),
            OwnedResource(
                "cloudflare_access_policy",
                "policy-openclaw",
                f"account:account-123:access-policy:{existing_desired.hostnames['openclaw']}",
            ),
            OwnedResource(
                "cloudflare_access_application",
                "app-farm",
                f"account:account-123:access-app:{existing_desired.hostnames['my-farm-advisor']}",
            ),
            OwnedResource(
                "cloudflare_access_policy",
                "policy-farm",
                f"account:account-123:access-policy:{existing_desired.hostnames['my-farm-advisor']}",
            ),
        ),
    )

    plan = cli.build_pack_disable_plan(
        existing_desired=existing_desired,
        requested_desired=requested_desired,
        ownership_ledger=ledger,
    )

    deleted_ids = {item.resource.resource_id for item in plan.deletions}
    assert deleted_ids == {"svc-farm", "app-farm", "policy-farm"}
    assert {
        item.resource.resource_id for item in plan.deletions if item.phase == "openclaw"
    } == set()


def test_pack_disable_plan_uninstalls_both_advisors_when_both_removed() -> None:
    existing_raw = _raw_input(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "PACKS": "openclaw,my-farm-advisor",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
        }
    )
    requested_raw = _raw_input(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
        }
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource("openclaw_service", "svc-openclaw", "stack:wizard-stack:openclaw"),
            OwnedResource(
                "openclaw_mem0_service",
                "svc-mem0",
                "stack:wizard-stack:openclaw-sidecar:mem0",
            ),
            OwnedResource(
                "openclaw_qdrant_service",
                "svc-qdrant",
                "stack:wizard-stack:openclaw-sidecar:qdrant",
            ),
            OwnedResource(
                "openclaw_runtime_service",
                "svc-runtime",
                "stack:wizard-stack:openclaw-sidecar:nexa-runtime",
            ),
            OwnedResource(
                "my_farm_advisor_service",
                "svc-farm",
                "stack:wizard-stack:my-farm-advisor",
            ),
        ),
    )

    plan = cli.build_pack_disable_plan(
        existing_desired=existing_desired,
        requested_desired=requested_desired,
        ownership_ledger=ledger,
    )

    assert {item.resource.resource_id for item in plan.deletions} == {
        "svc-openclaw",
        "svc-mem0",
        "svc-qdrant",
        "svc-runtime",
        "svc-farm",
    }


def test_pack_disable_plan_uninstalls_openclaw_only_and_preserves_farm() -> None:
    existing_raw = _raw_input(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "PACKS": "openclaw,my-farm-advisor",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
        }
    )
    requested_raw = _raw_input(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "PACKS": "my-farm-advisor",
            "AI_DEFAULT_API_KEY": "shared-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "operator@example.com",
        }
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource("openclaw_service", "svc-openclaw", "stack:wizard-stack:openclaw"),
            OwnedResource(
                "my_farm_advisor_service",
                "svc-farm",
                "stack:wizard-stack:my-farm-advisor",
            ),
        ),
    )

    plan = cli.build_pack_disable_plan(
        existing_desired=existing_desired,
        requested_desired=requested_desired,
        ownership_ledger=ledger,
    )

    assert {item.resource.resource_id for item in plan.deletions} == {"svc-openclaw"}


def test_cloudflare_access_names_use_user_visible_advisor_labels() -> None:
    class _FakeAccessBackend:
        def get_access_application(
            self, account_id: str, app_id: str
        ) -> CloudflareAccessApplication | None:
            del account_id, app_id
            return None

        def find_access_application_by_domain(
            self, account_id: str, hostname: str
        ) -> CloudflareAccessApplication | None:
            del account_id, hostname
            return None

        def create_access_application(
            self, *args: object, **kwargs: object
        ) -> CloudflareAccessApplication:
            raise AssertionError("dry-run test should not create access apps")

        def get_access_policy(
            self, account_id: str, app_id: str, policy_id: str
        ) -> CloudflareAccessPolicy | None:
            del account_id, app_id, policy_id
            return None

        def find_access_policy_by_name(
            self, account_id: str, app_id: str, name: str
        ) -> CloudflareAccessPolicy | None:
            del account_id, app_id, name
            return None

        def create_access_policy(self, *args: object, **kwargs: object) -> CloudflareAccessPolicy:
            raise AssertionError("dry-run test should not create access policies")

    backend = cast(Any, _FakeAccessBackend())

    openclaw_app, _ = networking_planner._resolve_access_application(
        dry_run=True,
        account_id="account-123",
        pack_name="openclaw",
        hostname="openclaw.example.com",
        provider_id="provider-1",
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )
    farm_app, _ = networking_planner._resolve_access_application(
        dry_run=True,
        account_id="account-123",
        pack_name="my-farm-advisor",
        hostname="farm.example.com",
        provider_id="provider-1",
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )
    openclaw_policy, _ = networking_planner._resolve_access_policy(
        dry_run=True,
        account_id="account-123",
        pack_name="openclaw",
        hostname="openclaw.example.com",
        app_id="app-1",
        emails=("operator@example.com",),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )
    farm_policy, _ = networking_planner._resolve_access_policy(
        dry_run=True,
        account_id="account-123",
        pack_name="my-farm-advisor",
        hostname="farm.example.com",
        app_id="app-2",
        emails=("operator@example.com",),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert openclaw_app.name == "Nexa Claw protected"
    assert farm_app.name == "Nexa Farm protected"
    assert openclaw_policy.name == "Allow Nexa Claw"
    assert farm_policy.name == "Allow Nexa Farm"


def test_invalid_subcommand_fails_cleanly() -> None:
    result = run_cli("unknown-command")

    assert result.returncode != 0
    combined_output = f"{result.stdout}{result.stderr}"
    assert "usage:" in combined_output
    assert "invalid choice" in combined_output
    assert "unknown-command" in combined_output


def test_build_nextcloud_backend_passes_all_selected_advisor_workspace_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, desired_state, backend_kwargs, _ = _capture_nextcloud_backend_kwargs(
        monkeypatch,
        packs="nextcloud,openclaw,my-farm-advisor",
        include_nexa_env=True,
    )

    mounts = backend_kwargs["advisor_workspace_mounts"]
    assert len(mounts) == 3
    assert [mount.volume_name for mount in mounts] == [
        f"{desired_state.stack_name}-openclaw-data",
        f"{desired_state.stack_name}-my-farm-advisor-data",
        f"{desired_state.stack_name}-my-farm-advisor-data",
    ]
    assert [mount.external_mount_name for mount in mounts] == [
        "/OpenClaw",
        "/Nexa Farm",
        "/Nexa Farm Data Pipeline",
    ]
    assert [mount.external_mount_path for mount in mounts] == [
        "/mnt/openclaw/workspace",
        "/mnt/my-farm-advisor/field-operations",
        "/mnt/my-farm-advisor/data-pipeline",
    ]
    assert [mount.visible_root for mount in mounts] == [
        "/mnt/openclaw/workspace/nexa",
        "/mnt/my-farm-advisor/field-operations/workspace",
        "/mnt/my-farm-advisor/data-pipeline/workspace",
    ]


def test_build_nextcloud_backend_passes_farm_mounts_without_openclaw_nexa_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, desired_state, backend_kwargs, _ = _capture_nextcloud_backend_kwargs(
        monkeypatch,
        packs="nextcloud,my-farm-advisor",
        include_nexa_env=False,
    )

    mounts = backend_kwargs["advisor_workspace_mounts"]
    assert len(mounts) == 2
    assert {mount.volume_name for mount in mounts} == {
        f"{desired_state.stack_name}-my-farm-advisor-data"
    }
    assert [mount.external_mount_name for mount in mounts] == [
        "/Nexa Farm",
        "/Nexa Farm Data Pipeline",
    ]
    assert [mount.external_mount_path for mount in mounts] == [
        "/mnt/my-farm-advisor/field-operations",
        "/mnt/my-farm-advisor/data-pipeline",
    ]
    assert backend_kwargs["nexa_agent_user_id"] is None
    assert backend_kwargs["nexa_agent_display_name"] is None
    assert backend_kwargs["nexa_agent_password"] is None
    assert backend_kwargs["nexa_agent_email"] is None


def test_build_nextcloud_backend_preserves_openclaw_only_workspace_contract_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, desired_state, backend_kwargs, _ = _capture_nextcloud_backend_kwargs(
        monkeypatch,
        packs="nextcloud,openclaw",
        include_nexa_env=True,
    )

    mounts = backend_kwargs["advisor_workspace_mounts"]
    assert len(mounts) == 1
    contract = mounts[0]
    assert contract.advisor_id == "openclaw"
    assert contract.volume_name == f"{desired_state.stack_name}-openclaw-data"
    assert contract.external_mount_name == "/OpenClaw"
    assert contract.external_mount_path == "/mnt/openclaw/workspace"
    assert contract.visible_root == "/mnt/openclaw/workspace/nexa"
    assert contract.contract_path == "/mnt/openclaw/workspace/nexa/contract.json"
    assert contract.runtime_state_source == "server-owned env + durable state JSON"


def test_build_nextcloud_backend_passes_zero_advisor_mounts_when_no_advisor_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, backend_kwargs, _ = _capture_nextcloud_backend_kwargs(
        monkeypatch,
        packs="nextcloud",
        include_nexa_env=False,
    )

    assert backend_kwargs["advisor_workspace_mounts"] == ()
    assert backend_kwargs["openclaw_volume_name"] is None


def test_build_nextcloud_backend_passes_state_dir_to_dokploy_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, backend_kwargs, state_dir = _capture_nextcloud_backend_kwargs(
        monkeypatch,
        packs="nextcloud",
        include_nexa_env=False,
    )

    assert backend_kwargs["state_dir"] == state_dir


def test_build_moodle_backend_passes_state_dir_to_dokploy_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = parse_env_file(FIXTURES_DIR / "moodle-docuseal.env")
    raw_env = RawEnvInput(
        format_version=parsed.format_version,
        values={
            **parsed.values,
            "DOKPLOY_MOCK_API_MODE": "false",
            "DOKPLOY_API_URL": "https://dokploy.example.com/api",
            "DOKPLOY_API_KEY": "dokploy-key",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    recorded: dict[str, Any] = {}
    sentinel_backend = object()
    sentinel_client = object()
    state_dir = Path("/tmp/test-state")

    def record_backend(**kwargs: Any) -> object:
        recorded.update(kwargs)
        return sentinel_backend

    monkeypatch.setattr(cli, "DokployMoodleBackend", record_backend)
    monkeypatch.setattr(cli, "_build_dokploy_api_client", lambda **_: sentinel_client)

    backend = cli._build_moodle_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
    )

    assert backend is sentinel_backend
    assert recorded["client"] is sentinel_client
    assert recorded["state_dir"] == state_dir


def test_build_docuseal_backend_passes_state_dir_to_dokploy_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = parse_env_file(FIXTURES_DIR / "moodle-docuseal.env")
    raw_env = RawEnvInput(
        format_version=parsed.format_version,
        values={
            **parsed.values,
            "DOKPLOY_MOCK_API_MODE": "false",
            "DOKPLOY_API_URL": "https://dokploy.example.com/api",
            "DOKPLOY_API_KEY": "dokploy-key",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    recorded: dict[str, Any] = {}
    sentinel_backend = object()
    sentinel_client = object()
    state_dir = Path("/tmp/test-state")

    def record_backend(**kwargs: Any) -> object:
        recorded.update(kwargs)
        return sentinel_backend

    monkeypatch.setattr(cli, "DokployDocuSealBackend", record_backend)
    monkeypatch.setattr(cli, "_build_dokploy_api_client", lambda **_: sentinel_client)

    backend = cli._build_docuseal_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
    )

    assert backend is sentinel_backend
    assert recorded["client"] is sentinel_client
    assert recorded["state_dir"] == state_dir


class _LifecyclePhaseResult:
    def __init__(self, phase: str, **payload: object) -> None:
        self.phase = phase
        for key, value in payload.items():
            setattr(self, key, value)

    def to_dict(self) -> dict[str, object]:
        return {"phase": self.phase}


class _FakeLifecycleNextcloudBackend:
    def __init__(self, *, refresh_calls: list[str], events: list[str]) -> None:
        self.refresh_calls = refresh_calls
        self.events = events

    def refresh_openclaw_external_storage(self, *, admin_user: str) -> None:
        self.refresh_calls.append(admin_user)
        self.events.append("refresh")


def _lifecycle_refresh_values(**overrides: str) -> dict[str, str]:
    values = _farm_modify_values(DOKPLOY_ADMIN_EMAIL="operator@example.com")
    values.update(overrides)
    return values


def _build_lifecycle_test_plan(
    *, desired_state: Any, mode: str, phases_to_run: tuple[str, ...]
) -> LifecyclePlan:
    applicable_phases = applicable_phases_for(desired_state)
    ordered_phases = tuple(phase for phase in applicable_phases if phase in phases_to_run)
    return LifecyclePlan(
        mode=mode,
        reasons=("test",),
        applicable_phases=applicable_phases,
        phases_to_run=ordered_phases,
        preserved_phases=(),
        initial_completed_steps=(),
        start_phase=ordered_phases[0] if ordered_phases else None,
        raw_equivalent=False,
        desired_equivalent=False,
    )


def _run_lifecycle_refresh(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    values: dict[str, str],
    lifecycle_plan: LifecyclePlan,
) -> tuple[dict[str, Any], list[str], list[str]]:
    raw_env = _raw_input(values)
    desired_state = resolve_desired_state(raw_env)
    state_dir = tmp_path / "state"
    events: list[str] = []
    refresh_calls: list[str] = []
    nextcloud_backend = _FakeLifecycleNextcloudBackend(
        refresh_calls=refresh_calls,
        events=events,
    )

    monkeypatch.setattr(
        lifecycle_engine,
        "reconcile_shared_core",
        lambda **kwargs: (
            events.append("shared_core")
            or SimpleNamespace(
                result=_LifecyclePhaseResult("shared_core"),
                network_resource_id="network-1",
                postgres_resource_id="postgres-1",
                redis_resource_id="redis-1",
                mail_relay_resource_id="mail-1",
                litellm_resource_id="litellm-1",
            )
        ),
    )
    monkeypatch.setattr(
        lifecycle_engine,
        "reconcile_nextcloud",
        lambda **kwargs: (
            events.append("nextcloud")
            or SimpleNamespace(
                result=_LifecyclePhaseResult("nextcloud"),
                nextcloud_service_resource_id="nextcloud-service-1",
                onlyoffice_service_resource_id="onlyoffice-service-1",
                nextcloud_volume_resource_id="nextcloud-volume-1",
                onlyoffice_volume_resource_id="onlyoffice-volume-1",
            )
        ),
    )
    monkeypatch.setattr(
        lifecycle_engine,
        "reconcile_openclaw",
        lambda **kwargs: (
            events.append("openclaw")
            or SimpleNamespace(
                result=_LifecyclePhaseResult("openclaw"),
                service_resource_id="openclaw-service-1",
            )
        ),
    )
    monkeypatch.setattr(
        lifecycle_engine,
        "reconcile_my_farm_advisor",
        lambda **kwargs: (
            events.append("my-farm-advisor")
            or SimpleNamespace(
                result=_LifecyclePhaseResult("my-farm-advisor"),
                service_resource_id="farm-service-1",
            )
        ),
    )
    monkeypatch.setattr(
        lifecycle_engine,
        "reconcile_cloudflare_access",
        lambda **kwargs: (
            events.append("cloudflare_access")
            or SimpleNamespace(
                result=_LifecyclePhaseResult("cloudflare_access", account_id="account-123"),
                provider_resource_id="provider-1",
                application_resource_ids=("app-1",),
                policy_resource_ids=("policy-1",),
            )
        ),
    )
    monkeypatch.setattr(
        lifecycle_engine,
        "build_shared_core_ledger",
        lambda **kwargs: kwargs["existing_ledger"],
    )
    monkeypatch.setattr(
        lifecycle_engine,
        "build_nextcloud_ledger",
        lambda **kwargs: kwargs["existing_ledger"],
    )
    monkeypatch.setattr(
        lifecycle_engine,
        "build_openclaw_ledger",
        lambda **kwargs: kwargs["existing_ledger"],
    )
    monkeypatch.setattr(
        lifecycle_engine,
        "build_my_farm_advisor_ledger",
        lambda **kwargs: kwargs["existing_ledger"],
    )
    monkeypatch.setattr(
        lifecycle_engine,
        "build_access_ledger",
        lambda **kwargs: kwargs["existing_ledger"],
    )

    summary = lifecycle_engine.execute_lifecycle_plan(
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        preflight_report=PreflightReport(
            host_facts=_host_facts(),
            required_profile=derive_required_profile(desired_state),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
        lifecycle_plan=lifecycle_plan,
        backends=lifecycle_engine.LifecycleBackends(
            bootstrap=cast(Any, object()),
            tailscale=cast(Any, object()),
            networking=cast(Any, object()),
            cloudflared=None,
            shared_core=cast(Any, object()),
            headscale=cast(Any, object()),
            matrix=cast(Any, object()),
            nextcloud=cast(Any, nextcloud_backend),
            moodle=cast(Any, object()),
            docuseal=cast(Any, object()),
            seaweedfs=cast(Any, object()),
            coder=cast(Any, object()),
            openclaw=cast(Any, object()),
        ),
    )

    return summary, refresh_calls, events


def test_execute_lifecycle_plan_initializes_checkpoint_before_compose_hash_persistence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_env = _raw_input(_lifecycle_refresh_values())
    desired_state = resolve_desired_state(raw_env)
    applicable_phases = applicable_phases_for(desired_state)
    lifecycle_plan = LifecyclePlan(
        mode="install",
        reasons=("resume shared core",),
        applicable_phases=applicable_phases,
        phases_to_run=("shared_core",),
        preserved_phases=(),
        initial_completed_steps=("preflight", "dokploy_bootstrap", "networking"),
        start_phase="shared_core",
        raw_equivalent=False,
        desired_equivalent=False,
    )
    state_dir = tmp_path / "state"
    service_key = f"{desired_state.stack_name}-shared-core"
    rendered_compose = "services:\n  postgres:\n    image: postgres:16\n"

    def _reconcile_shared_core(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        persist_compose_artifact_hash(
            state_dir=state_dir,
            service_key=service_key,
            rendered_compose=rendered_compose,
        )
        return SimpleNamespace(
            result=_LifecyclePhaseResult("shared_core"),
            network_resource_id="network-1",
            postgres_resource_id="postgres-1",
            redis_resource_id="redis-1",
            mail_relay_resource_id="mail-1",
            litellm_resource_id="litellm-1",
        )

    monkeypatch.setattr(lifecycle_engine, "reconcile_shared_core", _reconcile_shared_core)
    monkeypatch.setattr(
        lifecycle_engine,
        "build_shared_core_ledger",
        lambda **kwargs: kwargs["existing_ledger"],
    )

    lifecycle_engine.execute_lifecycle_plan(
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        preflight_report=PreflightReport(
            host_facts=_host_facts(),
            required_profile=derive_required_profile(desired_state),
            checks=(PreflightCheck(name="preflight", status="pass", detail="passed"),),
            advisories=(),
        ),
        lifecycle_plan=lifecycle_plan,
        backends=lifecycle_engine.LifecycleBackends(
            bootstrap=cast(Any, object()),
            tailscale=cast(Any, object()),
            networking=cast(Any, object()),
            cloudflared=None,
            shared_core=cast(Any, object()),
            headscale=cast(Any, object()),
            matrix=cast(Any, object()),
            nextcloud=cast(Any, object()),
            moodle=cast(Any, object()),
            docuseal=cast(Any, object()),
            seaweedfs=cast(Any, object()),
            coder=cast(Any, object()),
            openclaw=cast(Any, object()),
        ),
    )

    stderr = capsys.readouterr().err

    assert "[dokploy-wizard] Lifecycle phase 'shared_core' starting." in stderr
    assert "[dokploy-wizard] Lifecycle phase 'shared_core' finished." in stderr

    applied_state = load_state_dir(state_dir).applied_state
    assert applied_state is not None
    assert applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
    )
    assert applied_state.compose_artifact_hashes[service_key] == (
        ComposeArtifactHashState.from_rendered_compose(
            service_id=service_key,
            rendered_compose=rendered_compose,
        )
    )


def _nextcloud_backend_raw_env(*, packs: str, include_nexa_env: bool) -> RawEnvInput:
    values = {
        **parse_env_file(FIXTURES_DIR / "lifecycle-headscale.env").values,
        "DOKPLOY_MOCK_API_MODE": "false",
        "PACKS": packs,
    }
    if "my-farm-advisor" in packs:
        values.update(
            {
                "AI_DEFAULT_API_KEY": "shared-key",
                "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
                "MY_FARM_ADVISOR_PRIMARY_MODEL": "anthropic/claude-sonnet-4",
            }
        )
    if include_nexa_env:
        values.update(
            {
                "OPENCLAW_NEXA_AGENT_PASSWORD": "nexa-password-123",
                "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-user-123",
                "OPENCLAW_NEXA_AGENT_DISPLAY_NAME": "Nexa",
                "OPENCLAW_NEXA_AGENT_EMAIL": "nexa@example.com",
            }
        )
    return RawEnvInput(format_version=1, values=values)


def _capture_nextcloud_backend_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    packs: str,
    include_nexa_env: bool,
) -> tuple[RawEnvInput, Any, dict[str, Any], Path]:
    raw_env = _nextcloud_backend_raw_env(packs=packs, include_nexa_env=include_nexa_env)
    desired_state = resolve_desired_state(raw_env)
    recorded: dict[str, Any] = {}
    sentinel_backend = object()
    sentinel_client = object()
    state_dir = Path("/tmp/test-state")

    def record_backend(**kwargs: Any) -> object:
        recorded.update(kwargs)
        return sentinel_backend

    monkeypatch.setattr(cli, "DokployNextcloudBackend", record_backend)
    monkeypatch.setattr(cli, "_build_dokploy_api_client", lambda **_: sentinel_client)

    backend = cli._build_nextcloud_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
    )

    assert backend is sentinel_backend
    assert recorded["client"] is sentinel_client
    return raw_env, desired_state, recorded, state_dir


class _FakeBootstrapBackend:
    def is_healthy(self) -> bool:
        return True

    def install(self) -> None:
        raise AssertionError("install should not be called")


def _stub_install_flow_after_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "persist_install_scaffold", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", lambda **kwargs: kwargs["raw_env"])
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "write_target_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "ShellTailscaleBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_shared_core_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_headscale_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_matrix_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_nextcloud_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "_build_seaweedfs_backend", lambda **_: cast(Any, object()))
    monkeypatch.setattr(cli, "ShellOpenClawBackend", lambda _: cast(Any, object()))
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **kwargs: {"ok": True})


def _host_facts(
    *,
    distribution_id: str = "ubuntu",
    version_id: str = "24.04",
    cpu_count: int = 8,
    memory_gb: int = 16,
    disk_gb: int = 200,
    docker_installed: bool = True,
    docker_daemon_reachable: bool = True,
) -> HostFacts:
    return HostFacts(
        distribution_id=distribution_id,
        version_id=version_id,
        cpu_count=cpu_count,
        memory_gb=memory_gb,
        disk_gb=disk_gb,
        disk_path="/var/lib/docker",
        docker_installed=docker_installed,
        docker_daemon_reachable=docker_daemon_reachable,
        ports_in_use=(),
        environment_classification="vps",
        hostname="test-host",
    )
