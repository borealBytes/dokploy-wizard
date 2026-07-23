# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib import error

import pytest

import dokploy_wizard.proof as model_sync_proof
from dokploy_wizard.dokploy import coder as coder_module
from dokploy_wizard.dokploy.coder import _litellm_workspace_fallback_models_json
from dokploy_wizard.proof import (
    ProofRecoveryPaths,
    canonical_json_bytes,
    model_sync_artifacts,
    model_sync_baseline,
    model_sync_cli,
    model_sync_host_b,
    model_sync_preflight_payload,
    model_sync_remote,
    model_sync_results,
    open_protected_directory,
    output_paths,
    protected_bytes,
    verify_attestation,
    verify_protected_artifacts,
)
from dokploy_wizard.proof.model_sync_cli import main
from dokploy_wizard.proof.model_sync_env import ProofNamespace
from dokploy_wizard.proof.model_sync_host_a import (
    begin_proof_recovery,
    complete_resumable_finalization,
    recover_interrupted_proof,
)
from dokploy_wizard.proof.model_sync_host_b import (
    HostIdentity,
    assert_followup_proof_contract,
    assert_namespace_identity,
)
from dokploy_wizard.proof.model_sync_state import (
    AbortGuardError,
    process_start_time_ticks,
    read_abort_guard,
)


def test_namespace_identity_rejects_same_machine_and_mismatched_architecture() -> None:
    host_a = HostIdentity(machine_sha256="a" * 64, ssh_sha256="b" * 64, architecture="amd64")
    same_host = HostIdentity(machine_sha256="a" * 64, ssh_sha256="c" * 64, architecture="amd64")
    wrong_architecture = HostIdentity(
        machine_sha256="d" * 64,
        ssh_sha256="e" * 64,
        architecture="arm64",
    )

    with pytest.raises(ValueError):
        assert_namespace_identity(host_a=host_a, host_b=same_host)
    with pytest.raises(ValueError):
        assert_namespace_identity(host_a=host_a, host_b=wrong_architecture)


@pytest.mark.parametrize(
    "contract_name",
    ["upgrade_host_a_contract", "final_proof_contract", "reseed_pair_contract"],
)
def test_followup_proof_contract_rejects_missing_required_receipt(contract_name: str) -> None:
    with pytest.raises(ValueError):
        assert_followup_proof_contract(contract_name=contract_name, receipts=())


def test_single_host_followup_contract_requires_complete_receipt_chain() -> None:
    # Given
    complete = (
        "single-host-lifecycle-host-a",
        "single-host-lifecycle-teardown",
        "single-host-lifecycle-final",
    )

    # When / Then
    assert_followup_proof_contract(
        contract_name="final_proof_contract",
        receipts=complete,
        host_identity_mode="single_sequential",
    )
    with pytest.raises(ValueError):
        assert_followup_proof_contract(
            contract_name="final_proof_contract",
            receipts=complete[:-1],
            host_identity_mode="single_sequential",
        )
    with pytest.raises(ValueError):
        assert_followup_proof_contract(
            contract_name="reseed_pair_contract",
            receipts=complete,
            host_identity_mode="single_sequential",
        )


def test_baseline_host_a_requires_all_named_environment_inputs_without_artifact(
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.json"

    exit_code = main(
        [
            "baseline-host-a",
            "--active-root",
            str(tmp_path / "repository"),
            "--wrapper",
            "/workspaces/model-sync/bin/dokploy-wizard-remote",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--external-backup",
            str(tmp_path / "backup"),
            "--abort-guard",
            str(tmp_path / "guard.json"),
            "--host-env",
            "MISSING_HOST",
            "--password-env",
            "MISSING_PASSWORD",
            "--host-b-env",
            "MISSING_HOST_B",
            "--host-b-password-env",
            "MISSING_PASSWORD_B",
            "--source-base-commit",
            "a" * 40,
            "--proof-commit",
            "b" * 40,
            "--artifact-dir",
            str(tmp_path),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert not output.exists()


def test_model_sync_cli_import_and_parser_commands_remain_available() -> None:
    parser = model_sync_cli._build_parser()
    commands = {
        parser.parse_args(["atomic-finalize", "--temp", "temp", "--output", "output"]).command,
        parser.parse_args(["abort-status", "--guard", "guard", "--output", "output"]).command,
        parser.parse_args(
            [
                "baseline-host-a",
                "--active-root",
                "active-root",
                "--wrapper",
                "wrapper",
                "--env-file",
                "env",
                "--external-backup",
                "backup",
                "--abort-guard",
                "guard",
                "--host-env",
                "HOST_A",
                "--password-env",
                "PASSWORD_A",
                "--host-b-env",
                "HOST_B",
                "--host-b-password-env",
                "PASSWORD_B",
                "--source-base-commit",
                "a" * 40,
                "--proof-commit",
                "b" * 40,
                "--artifact-dir",
                "artifacts",
                "--output",
                "result",
            ]
        ).command,
    }

    assert commands == {"abort-status", "atomic-finalize", "baseline-host-a"}


def test_baseline_parser_requires_explicit_active_root(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    parser = model_sync_cli._build_parser()

    # When / Then
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "baseline-host-a",
                "--wrapper",
                "wrapper",
                "--env-file",
                "env",
                "--external-backup",
                "backup",
                "--abort-guard",
                "guard",
                "--host-env",
                "HOST_A",
                "--password-env",
                "PASSWORD_A",
                "--host-b-env",
                "HOST_B",
                "--host-b-password-env",
                "PASSWORD_B",
                "--source-base-commit",
                "a" * 40,
                "--proof-commit",
                "b" * 40,
                "--artifact-dir",
                "artifacts",
                "--output",
                "result",
            ]
        )
    assert "--active-root" in capsys.readouterr().err


def test_default_baseline_rejects_same_host_mapping_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    arguments = _baseline_arguments(tmp_path)
    monkeypatch.setenv("FIXTURE_HOST_A", "one-vps")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "one-password")
    monkeypatch.setenv("FIXTURE_HOST_B", "one-vps")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "one-password")
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda _wrapper, _paths: pytest.fail("default same-host mapping reached proof recovery"),
    )

    # When
    exit_code = main(arguments)

    # Then
    assert exit_code == 1
    assert not (tmp_path / "abort-guard.json").exists()
    assert not (tmp_path / "artifacts" / "result.json").exists()


def _sha(character: str) -> str:
    return character * 64


def _image_ref(repository: str, character: str) -> str:
    return f"{repository}@sha256:{_sha(character)}"


def _legacy_sha(target: str, pointer: str) -> str:
    return hashlib.sha256(f"{target}\0{pointer}\0expected-render\n".encode()).hexdigest()


def _preflight_wire(machine_id: str) -> str:
    planes = {
        plane: {"resources": [], "state": "absent"}
        for plane in ("cloudflare", "coder", "docker", "dokploy", "tailscale")
    }
    planes["docker"]["provenance"] = "docker_available_inventory"
    return json.dumps(
        {
            "architecture": "x86_64",
            "machine_id": machine_id,
            "planes": planes,
            "schema_version": 2,
        }
    )


def test_preflight_rejects_docker_as_each_resource_plane() -> None:
    duplicated_docker_inventory = ["proof-stack", "proof-stack-coder"]
    wire = json.dumps(
        {
            "architecture": "x86_64",
            "machine_id": "machine-a",
            "planes": {
                "cloudflare": duplicated_docker_inventory,
                "coder": duplicated_docker_inventory,
                "docker": duplicated_docker_inventory,
                "dokploy": duplicated_docker_inventory,
                "tailscale": duplicated_docker_inventory,
            },
            "schema_version": 2,
        }
    )
    namespace = ProofNamespace(
        stack_name="unrelated-stack",
        docker=(),
        dokploy=(),
        cloudflare=(),
        tailscale=(),
        coder_templates=(),
    )

    with pytest.raises(model_sync_remote.RemoteProofError, match="resource objects"):
        model_sync_remote.parse_preflight(
            wire,
            namespace,
            ssh_key="ssh-a",
            boot_id="11111111-1111-1111-1111-111111111111",
        )


class _WireResponse:
    def __init__(self, payload: Any) -> None:
        self.status = 200
        self._payload = json.dumps(payload).encode()

    def __enter__(self) -> _WireResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._payload


def _transport_fixture(*, tailscale_required: bool = False) -> dict[str, Any]:
    return {
        "cloudflare_account_id": "account-proof",
        "cloudflare_token": "SECRET-CLOUDFLARE-TOKEN",
        "cloudflare_zone_id": "zone-proof",
        "cloudflare_zone_name": "example.test",
        "coder_email": "operator@example.test",
        "coder_hostname": "coder.example.test",
        "coder_password": "SECRET-CODER-PASSWORD",
        "dokploy_api_key": "SECRET-DOKPLOY-KEY",
        "dokploy_api_url": "https://dokploy.example.test",
        "tailscale_required": tailscale_required,
    }


def _command_fixture(
    *,
    empty: bool = False,
    matching: bool = False,
    missing_tailscale: bool = False,
    failure: str | None = None,
) -> Any:
    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        key = tuple(command)
        if key[:2] == ("docker", "ps"):
            if failure == "docker-timeout":
                raise subprocess.TimeoutExpired(command, 20)
            if failure == "docker-permission":
                return subprocess.CompletedProcess(command, 1, b"", b"permission denied")
            if failure == "docker-malformed":
                return subprocess.CompletedProcess(command, 0, b"{", b"")
            rows = (
                []
                if empty
                else [
                    {
                        "ID": "container-other",
                        "Image": "busybox:latest",
                        "Labels": "",
                        "Names": "other-container",
                    }
                ]
            )
            return subprocess.CompletedProcess(
                command, 0, "\n".join(map(json.dumps, rows)).encode(), b""
            )
        if key[:2] == ("docker", "info"):
            return subprocess.CompletedProcess(command, 0, b"active\n", b"")
        if key[:3] == ("docker", "service", "ls"):
            if failure == "docker-service-unreadable":
                return subprocess.CompletedProcess(
                    command, 125, b"", b"service inventory unavailable"
                )
            service_rows = [] if empty else ["service-other\tother-service\tbusybox:latest"]
            if matching:
                service_rows.extend(
                    (
                        "service-dokploy\tdokploy\tdokploy/dokploy:latest",
                        "service-coder\tproof-stack-coder\tghcr.io/coder/coder:latest",
                    )
                )
            return subprocess.CompletedProcess(command, 0, "\n".join(service_rows).encode(), b"")
        if key[:3] == ("docker", "network", "ls"):
            output = b"" if empty else b"network-other\tother-network\n"
            return subprocess.CompletedProcess(command, 0, output, b"")
        if key[:3] == ("docker", "volume", "ls"):
            output = b"" if empty else b"volume-other\tother-volume\n"
            return subprocess.CompletedProcess(command, 0, output, b"")
        if key == ("tailscale", "status", "--json"):
            if missing_tailscale:
                raise FileNotFoundError
            if failure == "tailscale-malformed":
                return subprocess.CompletedProcess(command, 0, b"{", b"")
            payload: dict[str, Any] = {"BackendState": "NeedsLogin", "Peer": {}}
            if matching:
                payload = {
                    "BackendState": "Running",
                    "Peer": {
                        "nodekey:peer": {
                            "HostName": "other-tailnet-node",
                            "ID": "peer-stable-id",
                        }
                    },
                    "Self": {"HostName": "proof-tailnet-node", "ID": "self-stable-id"},
                }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b"")
        raise AssertionError(command)

    return run


def _cloudflare_list(
    items: list[dict[str, Any]], *, page: int = 1, pages: int = 1
) -> dict[str, Any]:
    total_count = len(items) if pages == 1 else 101
    return {
        "result": items,
        "result_info": {
            "count": len(items),
            "page": page,
            "per_page": 100,
            "total_count": total_count,
        },
        "success": True,
    }


def _wire_fixture(
    *,
    empty: bool = False,
    many_coder: bool = False,
    matching: bool = False,
    failure: str | None = None,
    template_count: int | None = None,
    template_malformed: bool = False,
    template_requests: list[str] | None = None,
    access_fixture: dict[str, Any] | None = None,
) -> Any:
    def open_request(request: Any, *, timeout: int) -> _WireResponse:
        assert timeout <= 30
        url = request.full_url
        if failure == "cloudflare-unauthorized" and "api.cloudflare.com" in url:
            raise error.HTTPError(url, 401, "unauthorized", Message(), None)
        if "/cfd_tunnel?" in url:
            if failure == "cloudflare-malformed":
                return _WireResponse({"success": True})
            tunnels = (
                []
                if empty
                else [
                    {
                        "config_src": "cloudflare",
                        "created_at": "2026-01-01T00:00:00Z",
                        "deleted_at": None,
                        "id": "tunnel-other",
                        "name": "other-tunnel",
                    }
                ]
            )
            if matching:
                tunnels.append(
                    {
                        "config_src": "cloudflare",
                        "created_at": "2026-01-01T00:00:00Z",
                        "deleted_at": None,
                        "id": "tunnel-proof",
                        "name": "proof-stack-cloudflared",
                    }
                )
            if failure == "cloudflare-partial":
                return _WireResponse(_cloudflare_list(tunnels, pages=2))
            return _WireResponse(_cloudflare_list(tunnels))
        if "/cfd_tunnel/" in url and url.endswith("/configurations"):
            ingress = [
                {"hostname": "other.example.test", "originRequest": {}, "service": "http://other"}
            ]
            if matching and "/tunnel-proof/" in url:
                ingress.append(
                    {
                        "hostname": "coder.example.test",
                        "originRequest": {},
                        "service": "http://coder",
                    }
                )
            return _WireResponse({"result": {"config": {"ingress": ingress}}, "success": True})
        if "/dns_records?" in url:
            records: list[dict[str, Any]] = (
                []
                if empty
                else [
                    {
                        "comment": None,
                        "content": "198.51.100.1",
                        "id": "dns-other",
                        "name": "other.example.test",
                        "proxied": True,
                        "tags": [],
                        "ttl": 1,
                        "type": "CNAME",
                    }
                ]
            )
            if matching:
                records.append(
                    {
                        "comment": None,
                        "content": "198.51.100.2",
                        "id": "dns-proof",
                        "name": "coder.example.test",
                        "proxied": True,
                        "tags": [],
                        "ttl": 1,
                        "type": "CNAME",
                    }
                )
            return _WireResponse(_cloudflare_list(records))
        if "/access/apps?" in url:
            apps = (
                []
                if empty
                else [
                    {
                        "allowed_identity_providers": [],
                        "app_launcher_visible": True,
                        "auto_redirect_to_identity": False,
                        "domain": "other.example.test",
                        "id": "app-other",
                        "name": "Other",
                        "session_duration": "24h",
                        "type": "self_hosted",
                    }
                ]
            )
            if matching:
                apps.append(
                    access_fixture["app"]
                    if access_fixture
                    else {
                        "allowed_identity_providers": [],
                        "app_launcher_visible": True,
                        "auto_redirect_to_identity": False,
                        "domain": "coder.example.test",
                        "id": "app-proof",
                        "name": "Coder",
                        "session_duration": "24h",
                        "type": "self_hosted",
                    }
                )
            return _WireResponse(_cloudflare_list(apps))
        if "/access/apps/" in url and "/policies?" in url:
            policies = (
                access_fixture["policy"]
                if access_fixture and "/policies?" in url
                else (
                    [
                        {
                            "decision": "allow",
                            "exclude": [],
                            "id": "policy-proof",
                            "include": [{"email": "redacted@example.test"}],
                            "name": "Policy",
                            "precedence": 1,
                            "require": [],
                        }
                    ]
                    if matching and "/app-proof/" in url
                    else [
                        {
                            "decision": "allow",
                            "exclude": [],
                            "id": "policy-other",
                            "include": [],
                            "name": "Other policy",
                            "precedence": 1,
                            "require": [],
                        }
                    ]
                )
            )
            return _WireResponse(_cloudflare_list(policies))
        if url.endswith("/api/project.all"):
            if failure == "dokploy-unauthorized":
                raise error.HTTPError(url, 401, "unauthorized", Message(), None)
            if failure == "dokploy-malformed":
                return _WireResponse({"data": {}})
            projects: list[dict[str, Any]] = (
                []
                if empty
                else [{"environments": [], "name": "other-project", "projectId": "project-other"}]
            )
            if matching:
                projects.append(
                    {
                        "environments": [
                            {
                                "applications": [
                                    {
                                        "applicationId": "application-proof",
                                        "name": "proof-stack-app",
                                    }
                                ],
                                "compose": [
                                    {"composeId": "compose-proof", "name": "proof-stack-coder"}
                                ],
                            }
                        ],
                        "name": "proof-stack",
                        "projectId": "project-proof",
                    }
                )
            return _WireResponse({"data": projects})
        if "/api/schedule.list?" in url:
            schedules = []
            if matching:
                schedules.append({"name": "proof-stack-schedule", "scheduleId": "schedule-proof"})
            return _WireResponse({"data": schedules})
        if url.endswith("/api/v2/users/login"):
            if failure == "coder-unauthorized":
                raise error.HTTPError(url, 401, "unauthorized", Message(), None)
            if failure == "coder-malformed":
                return _WireResponse({})
            return _WireResponse({"session_token": "SECRET-CODER-SESSION"})
        if url.endswith("/api/v2/users/me"):
            return _WireResponse({"id": "coder-user"})
        if "/api/v2/templates" in url:
            if template_requests is not None:
                template_requests.append(url)
            if "?" in url:
                return _WireResponse({"unexpected_template_query": True})
            if template_malformed:
                return _WireResponse({"templates": []})
            if template_count is not None:
                return _WireResponse(
                    [
                        {"id": f"template-{index}", "name": f"template-{index}"}
                        for index in range(template_count)
                    ]
                )
            if failure == "coder-partial" and "offset=0" in url:
                return _WireResponse(
                    [
                        {"id": f"template-{index}", "name": f"template-{index}"}
                        for index in range(100)
                    ]
                )
            if failure == "coder-partial":
                raise error.URLError("second page unavailable")
            if many_coder:
                return _WireResponse(
                    [
                        {"id": f"template-{index}", "name": f"template-{index}"}
                        for index in range(101)
                    ]
                )
            templates = [{"id": "template-other", "name": "other-template"}]
            if matching:
                templates.append({"id": "template-proof", "name": "ubuntu-vscode"})
            return _WireResponse(templates)
        if "/api/v2/workspaces?" in url:
            if many_coder:
                offset = 100 if "offset=100" in url else 0
                workspaces = [
                    {"id": f"workspace-{index}", "name": f"workspace-{index}"}
                    for index in range(101)[offset : offset + 100]
                ]
                return _WireResponse({"count": 101, "workspaces": workspaces})
            workspaces = [{"id": "workspace-other", "name": "other-workspace"}]
            if matching:
                workspaces.append({"id": "workspace-proof", "name": "proof-stack-workspace"})
            return _WireResponse({"count": len(workspaces), "workspaces": workspaces})
        if "/api/v2/users/coder-user/secrets?" in url:
            if many_coder:
                offset = 100 if "offset=100" in url else 0
                return _WireResponse(
                    [
                        {"id": f"secret-{index}", "name": f"secret-{index}"}
                        for index in range(101)[offset : offset + 100]
                    ]
                )
            secrets = [{"id": "secret-other", "name": "other-secret"}]
            if matching:
                secrets.append({"id": "secret-proof", "name": "proof-stack-secret"})
            return _WireResponse(secrets)
        raise AssertionError(url)

    return open_request


def _collect_planes(
    *,
    empty: bool = False,
    many_coder: bool = False,
    matching: bool = False,
    failure: str | None = None,
    docker_evidence: frozenset[str] = frozenset({"/var/run/docker.sock"}),
    docker_runtime: bool = False,
    missing_tailscale: bool = False,
    template_count: int | None = None,
    template_malformed: bool = False,
    template_requests: list[str] | None = None,
    access_fixture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scope: dict[str, Any] = {"__name__": "fixture"}
    exec(model_sync_results.PREFLIGHT_SCRIPT, scope)
    scope["_run_process"] = _command_fixture(
        empty=empty,
        matching=matching,
        missing_tailscale=missing_tailscale,
        failure=failure,
    )
    scope["_open_request"] = _wire_fixture(
        empty=empty,
        many_coder=many_coder,
        matching=matching,
        failure=failure,
        template_count=template_count,
        template_malformed=template_malformed,
        template_requests=template_requests,
        access_fixture=access_fixture,
    )

    def which(command: str) -> str | None:
        if command in {"dockerd", "containerd"}:
            return command if docker_runtime else None
        if (missing_tailscale and command == "tailscale") or (
            failure == "docker-missing" and command == "docker"
        ):
            return None
        return command

    scope["_which"] = which
    scope["_path_exists"] = lambda path: path in docker_evidence
    result = scope["_collect_planes"](
        _transport_fixture(tailscale_required=matching or missing_tailscale)
    )
    if not isinstance(result, dict):
        raise AssertionError("collector fixture did not return resource planes")
    return result


def test_authoritative_collectors_report_successful_empty_planes() -> None:
    planes = _collect_planes(empty=True)

    assert {name: plane["state"] for name, plane in planes.items()} == {
        "cloudflare": "absent",
        "coder": "absent",
        "docker": "absent",
        "dokploy": "absent",
        "tailscale": "absent",
    }
    assert planes["coder"]["resources"] == []
    assert planes["tailscale"]["resources"] == []


def test_authoritative_collectors_accept_complete_docker_absence() -> None:
    planes = _collect_planes(
        empty=True,
        failure="docker-missing",
        docker_evidence=frozenset(),
    )

    assert planes["docker"] == {
        "provenance": "docker_absent_clean",
        "resources": [],
        "state": "absent",
    }


def test_preflight_payload_decodes_authoritative_docker_absence_collector() -> None:
    payload_source = Path(model_sync_preflight_payload.__file__).read_text(encoding="utf-8")
    source = Path(model_sync_results.__file__).read_text(encoding="utf-8")

    assert payload_source.count("_PREFLIGHT_ENCODED =") == 1
    assert "_PREFLIGHT_ENCODED_V2" not in payload_source
    assert "with_cloudflare_" + "fingerprints" not in source
    assert ".replace(" not in source
    assert hashlib.sha256(model_sync_results.PREFLIGHT_SCRIPT.encode()).hexdigest() == (
        "f707056ee06c19af238c9cf0b53313cc72cf67130f3ec89fc12fe3b98fe9f86e"
    )
    assert "def _docker_absent_clean():" in model_sync_results.PREFLIGHT_SCRIPT
    assert '_which("dockerd") is None' in model_sync_results.PREFLIGHT_SCRIPT
    assert '_which("containerd") is None' in model_sync_results.PREFLIGHT_SCRIPT
    assert '"/var/run/docker.sock"' in model_sync_results.PREFLIGHT_SCRIPT
    assert '"/var/lib/docker"' in model_sync_results.PREFLIGHT_SCRIPT
    assert '"/etc/systemd/system/docker.service"' in model_sync_results.PREFLIGHT_SCRIPT
    assert 'return [], [], "docker_absent_clean"' in model_sync_results.PREFLIGHT_SCRIPT


def test_programmatic_probe_rejects_missing_docker_provenance() -> None:
    probe = model_sync_remote.RemoteProbe(
        machine_sha256="a" * 64,
        ssh_sha256="b" * 64,
        boot_sha256="c" * 64,
        architecture="amd64",
        namespace_clean=True,
        inventory={
            plane: () for plane in ("cloudflare", "coder", "docker", "dokploy", "tailscale")
        },
        plane_states={
            plane: "absent" for plane in ("cloudflare", "coder", "docker", "dokploy", "tailscale")
        },
        plane_provenance={},
    )

    with pytest.raises(model_sync_remote.RemoteProofError, match="docker plane provenance"):
        probe.to_dict()


@pytest.mark.parametrize(
    ("docker_evidence", "docker_runtime"),
    [
        (frozenset({"/var/run/docker.sock"}), False),
        (frozenset({"/usr/bin/docker"}), False),
        (frozenset({"/etc/systemd/system/docker.service"}), False),
        (frozenset({"/var/lib/docker"}), False),
        (frozenset({"/run/containerd/containerd.sock"}), False),
        (frozenset(), True),
    ],
)
def test_authoritative_collectors_reject_contradictory_docker_absence(
    docker_evidence: frozenset[str],
    docker_runtime: bool,
) -> None:
    planes = _collect_planes(
        empty=True,
        failure="docker-missing",
        docker_evidence=docker_evidence,
        docker_runtime=docker_runtime,
    )

    assert planes["docker"] == {
        "provenance": "docker_error",
        "resources": [],
        "state": "error",
    }


def test_authoritative_collectors_reject_docker_cli_without_socket() -> None:
    planes = _collect_planes(empty=True, docker_evidence=frozenset())

    assert planes["docker"] == {
        "provenance": "docker_error",
        "resources": [],
        "state": "error",
    }


def test_authoritative_collectors_report_matching_and_nonmatching_resources() -> None:
    planes = _collect_planes(matching=True)
    wire = json.dumps(
        {
            "architecture": "x86_64",
            "machine_id": "machine-a",
            "planes": planes,
            "schema_version": 2,
        }
    )
    namespace = ProofNamespace(
        stack_name="proof-stack",
        docker=("proof-stack-coder",),
        dokploy=("proof-stack", "proof-stack-coder"),
        cloudflare=("proof-stack-cloudflared", "coder.example.test"),
        tailscale=("proof-tailnet-node",),
        coder_templates=("ubuntu-vscode",),
    )

    result = model_sync_remote.parse_preflight(
        wire,
        namespace,
        ssh_key="ssh-a",
        boot_id="11111111-1111-1111-1111-111111111111",
    )

    assert result.namespace_clean is False
    assert {resource.kind for resource in result.inventory["cloudflare"]} == {
        "access_application",
        "access_policy",
        "dns_record",
        "hostname_route",
        "tunnel",
    }
    policy = next(
        resource
        for resource in result.inventory["cloudflare"]
        if resource.resource_id == "policy-proof"
    )
    assert policy.match == "exact"
    assert policy.provenance == "preexisting_unowned"
    assert "Policy" not in str(policy.to_dict())
    assert {resource.kind for resource in result.inventory["dokploy"]} == {
        "application",
        "compose",
        "project",
        "schedule",
    }
    assert {resource.kind for resource in result.inventory["coder"]} == {
        "secret",
        "template",
        "workspace",
    }
    assert result.plane_provenance["docker"] == "docker_available_inventory"
    assert any(resource.name == "proof-stack-coder" for resource in result.inventory["docker"])


def _access_policy_fingerprint(access_fixture: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    planes = _collect_planes(empty=True, matching=True, access_fixture=access_fixture)
    policy = next(
        item for item in planes["cloudflare"]["resources"] if item["kind"] == "access_policy"
    )
    return policy["fingerprint_sha256"], policy


def _access_fixture(
    *,
    app_id: str = "app-proof",
    domain: str = "coder.example.test",
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "app": {
            "allowed_identity_providers": [],
            "app_launcher_visible": True,
            "auto_redirect_to_identity": False,
            "domain": domain,
            "id": app_id,
            "name": "Coder",
            "session_duration": "24h",
            "type": "self_hosted",
        },
        "policy": [
            policy
            or {
                "decision": "allow",
                "exclude": [],
                "id": "policy-proof",
                "include": [{"email": "operator@example.test"}],
                "name": "Unrelated display name",
                "precedence": 1,
                "require": [],
            }
        ],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"decision": "deny"},
        {"precedence": 2},
        {"include": [{"email": "other@example.test"}]},
        {"exclude": [{"email": "blocked@example.test"}]},
        {"require": [{"email_domain": {"domain": "example.test"}}]},
        {"name": "Different unrelated display name"},
    ],
)
def test_access_policy_fingerprint_binds_each_policy_clause(mutation: dict[str, Any]) -> None:
    baseline = _access_fixture()
    base_fingerprint, _ = _access_policy_fingerprint(baseline)
    changed_policy = {**baseline["policy"][0], **mutation}

    changed_fingerprint, _ = _access_policy_fingerprint(_access_fixture(policy=changed_policy))

    assert changed_fingerprint != base_fingerprint


@pytest.mark.parametrize(
    "fixture",
    [
        _access_fixture(app_id="app-rebound"),
        _access_fixture(domain="other.example.test"),
    ],
)
def test_access_policy_fingerprint_binds_parent_identity(fixture: dict[str, Any]) -> None:
    base_fingerprint, evidence = _access_policy_fingerprint(_access_fixture())
    changed_fingerprint, changed = _access_policy_fingerprint(fixture)

    assert changed_fingerprint != base_fingerprint
    assert changed["id"] == evidence["id"]


def test_coder_preflight_collects_all_identifiers_beyond_first_page() -> None:
    template_requests: list[str] = []
    resources = _collect_planes(
        many_coder=True, matching=True, template_requests=template_requests
    )["coder"]["resources"]

    assert {item["id"] for item in resources if item["kind"] == "template"} == {
        f"template-{index}" for index in range(101)
    }
    assert {item["id"] for item in resources if item["kind"] == "workspace"} == {
        f"workspace-{index}" for index in range(101)
    }
    assert {item["id"] for item in resources if item["kind"] == "secret"} == {
        f"secret-{index}" for index in range(101)
    }
    assert template_requests == ["https://coder.example.test/api/v2/templates"]


@pytest.mark.parametrize("template_count", [100, 101])
def test_coder_preflight_accepts_unpaginated_template_arrays(
    template_count: int,
) -> None:
    template_requests: list[str] = []

    resources = _collect_planes(
        matching=True,
        template_count=template_count,
        template_requests=template_requests,
    )["coder"]["resources"]

    assert {item["id"] for item in resources if item["kind"] == "template"} == {
        f"template-{index}" for index in range(template_count)
    }
    assert template_requests == ["https://coder.example.test/api/v2/templates"]


def test_coder_preflight_fails_closed_for_malformed_template_response() -> None:
    template_requests: list[str] = []

    planes = _collect_planes(
        matching=True,
        template_malformed=True,
        template_requests=template_requests,
    )

    assert planes["coder"] == {"resources": [], "state": "error"}
    assert template_requests == ["https://coder.example.test/api/v2/templates"]


@pytest.mark.parametrize(
    ("failure", "failed_plane"),
    [
        ("cloudflare-unauthorized", "cloudflare"),
        ("cloudflare-malformed", "cloudflare"),
        ("cloudflare-partial", "cloudflare"),
        ("dokploy-unauthorized", "dokploy"),
        ("dokploy-malformed", "dokploy"),
        ("coder-unauthorized", "coder"),
        ("coder-malformed", "coder"),
        ("coder-partial", "coder"),
        ("docker-timeout", "docker"),
        ("docker-permission", "docker"),
        ("docker-malformed", "docker"),
        ("docker-service-unreadable", "docker"),
        ("tailscale-malformed", "tailscale"),
    ],
)
def test_authoritative_collectors_turn_failures_into_blocking_plane_errors(
    failure: str, failed_plane: str
) -> None:
    planes = _collect_planes(matching=True, failure=failure)

    expected = {"resources": [], "state": "error"}
    if failed_plane == "docker":
        expected["provenance"] = "docker_error"
    assert planes[failed_plane] == expected


def test_authoritative_tailscale_collector_blocks_missing_required_command() -> None:
    planes = _collect_planes(missing_tailscale=True)

    assert planes["tailscale"] == {"resources": [], "state": "error"}


@pytest.mark.parametrize(
    ("state", "provenance"),
    [
        ("absent", "docker_error"),
        ("error", "docker_available_inventory"),
        ("present", "docker_absent_clean"),
    ],
)
def test_preflight_parser_rejects_inconsistent_docker_provenance(
    state: str,
    provenance: str,
) -> None:
    payload = json.loads(_preflight_wire("machine-a"))
    payload["planes"]["docker"] = {
        "provenance": provenance,
        "resources": []
        if state != "present"
        else [{"id": "docker", "kind": "container", "name": "docker"}],
        "state": state,
    }
    namespace = ProofNamespace("proof-stack", (), (), (), (), ())

    with pytest.raises(
        model_sync_remote.RemoteProofError, match="docker plane provenance is inconsistent"
    ):
        model_sync_remote.parse_preflight(
            json.dumps(payload),
            namespace,
            ssh_key="ssh-a",
            boot_id="11111111-1111-1111-1111-111111111111",
        )


def test_preflight_parser_rejects_error_state_and_duplicate_ids() -> None:
    payload = json.loads(_preflight_wire("machine-a"))
    payload["planes"]["cloudflare"] = {"resources": [], "state": "error"}
    namespace = ProofNamespace("proof-stack", (), (), (), (), ())
    with pytest.raises(
        model_sync_remote.RemoteProofError, match="cloudflare plane collection failed"
    ):
        model_sync_remote.parse_preflight(
            json.dumps(payload),
            namespace,
            ssh_key="ssh-a",
            boot_id="11111111-1111-1111-1111-111111111111",
        )

    payload = json.loads(_preflight_wire("machine-a"))
    payload["planes"]["docker"].pop("provenance")
    with pytest.raises(model_sync_remote.RemoteProofError, match="docker resource objects"):
        model_sync_remote.parse_preflight(
            json.dumps(payload),
            namespace,
            ssh_key="ssh-a",
            boot_id="11111111-1111-1111-1111-111111111111",
        )

    payload = json.loads(_preflight_wire("machine-a"))
    payload["planes"]["docker"]["provenance"] = "docker_error"
    with pytest.raises(
        model_sync_remote.RemoteProofError, match="docker plane provenance is inconsistent"
    ):
        model_sync_remote.parse_preflight(
            json.dumps(payload),
            namespace,
            ssh_key="ssh-a",
            boot_id="11111111-1111-1111-1111-111111111111",
        )

    payload = json.loads(_preflight_wire("machine-a"))
    payload["planes"]["cloudflare"] = {
        "resources": [
            {
                "fingerprint_sha256": hashlib.sha256(b"duplicate-a").hexdigest(),
                "id": "duplicate",
                "kind": "tunnel",
                "name": "one",
            },
            {
                "fingerprint_sha256": hashlib.sha256(b"duplicate-b").hexdigest(),
                "id": "duplicate",
                "kind": "dns_record",
                "name": "two",
            },
        ],
        "state": "present",
    }
    with pytest.raises(model_sync_remote.RemoteProofError, match="duplicate identities"):
        model_sync_remote.parse_preflight(
            json.dumps(payload),
            namespace,
            ssh_key="ssh-a",
            boot_id="11111111-1111-1111-1111-111111111111",
        )


def test_coder_workspace_pagination_accepts_authoritative_empty_response() -> None:
    assert (
        model_sync_results.collect_coder_workspace_pages(
            lambda _path: {"count": 0, "workspaces": []}
        )
        == []
    )


def test_remote_wrapper_password_is_sent_only_through_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    secret = "SECRET-REMOTE-PASSWORD"

    def run(command: list[str], **kwargs: Any) -> bytes:
        captured["command"] = command
        captured["input"] = kwargs.get("stdin")
        return b""

    monkeypatch.setattr(model_sync_cli, "run_bounded_process", run)

    model_sync_cli._run_wrapper(Path("wrapper"), "host-a", secret, Path("install.env"))

    assert secret not in " ".join(captured["command"])
    assert captured["input"] == (secret + "\n").encode()
    assert "--password-stdin" in captured["command"]


def test_coder_pointer_session_token_is_sent_only_through_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    secret = "SECRET-CODER-SESSION"

    def run(command: list[str], **kwargs: Any) -> bytes:
        captured["command"] = command
        captured["input"] = kwargs.get("stdin")
        return b'{"scope":"pointer"}'

    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", run)

    model_sync_host_b._primary_pointer(
        "coder-container",
        secret,
        "workspace",
        "ubuntu-vscode",
        "version-1",
        model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "key",
            "openrouter/example",
            (),
            ("openrouter/example",),
        ),
    )

    assert secret not in " ".join(captured["command"])
    assert captured["input"] == (secret + "\n").encode()


def test_legacy_renderer_hash_is_independent_from_observed_pointer_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {
        "npm": "@ai-sdk/openai-compatible",
        "options": {
            "baseURL": "http://proof-stack-shared-litellm:4000",
            "apiKey": "SECRET-LEGACY-KEY",
        },
        "models": {"openrouter/example": {}},
    }
    drifted = {**original, "models": {"openrouter/changed": {}}}

    def captured_pointer(value: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return json.dumps(
            {
                "target": "/home/coder/.config/opencode/opencode.json",
                "pointer": "/provider/litellm",
                "mode": "0644",
                "shape": "json-pointer",
                "base_url": value["options"]["baseURL"],
                "credential_value_sha256": hashlib.sha256(
                    value["options"]["apiKey"].encode()
                ).hexdigest(),
                "pointer_sha256": digest,
                "scope": "pointer",
            }
        )

    outputs = iter((captured_pointer(original), captured_pointer(drifted)))
    monkeypatch.setattr(
        model_sync_host_b,
        "run_bounded_process",
        lambda _command, **_kwargs: next(outputs).encode(),
    )

    first = model_sync_host_b._primary_pointer(
        "coder-container",
        "session",
        "workspace",
        "ubuntu-vscode",
        "version-1",
        model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "SECRET-LEGACY-KEY",
            "openrouter/example",
            (),
            ("openrouter/example",),
        ),
    )
    second = model_sync_host_b._primary_pointer(
        "coder-container",
        "session",
        "workspace",
        "ubuntu-vscode",
        "version-1",
        model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "SECRET-LEGACY-KEY",
            "openrouter/example",
            (),
            ("openrouter/example",),
        ),
    )

    assert first["pointer_sha256"] != second["pointer_sha256"]
    assert first["independent_renderer_sha256"] == second["independent_renderer_sha256"]


@pytest.mark.parametrize(
    "changed",
    [
        model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "changed-key",
            "openrouter/example",
            (),
            ("openrouter/example",),
        ),
        model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "expected-key",
            "openrouter/changed",
            (),
            ("openrouter/example",),
        ),
        model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "expected-key",
            "openrouter/example",
            ("openrouter/fallback",),
            ("openrouter/example",),
        ),
        model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "expected-key",
            "openrouter/example",
            (),
            ("openrouter/changed",),
        ),
    ],
)
def test_observed_pointer_hash_is_independent_from_renderer_input_mutation(
    changed: model_sync_host_b.LegacyRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": "http://proof-stack-shared-litellm:4000", "apiKey": "observed-key"},
        "models": {"openrouter/example": {}},
    }
    observed_sha = hashlib.sha256(
        json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    captured = json.dumps(
        {
            "target": "/home/coder/.config/opencode/opencode.json",
            "pointer": "/provider/litellm",
            "mode": "0644",
            "shape": "json-pointer",
            "base_url": "http://proof-stack-shared-litellm:4000",
            "credential_value_sha256": hashlib.sha256(b"observed-key").hexdigest(),
            "pointer_sha256": observed_sha,
            "scope": "pointer",
        }
    )
    monkeypatch.setattr(
        model_sync_host_b, "run_bounded_process", lambda _command, **_kwargs: captured.encode()
    )
    original = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000",
        "expected-key",
        "openrouter/example",
        (),
        ("openrouter/example",),
    )

    first = model_sync_host_b._primary_pointer(
        "coder", "session", "workspace", "ubuntu-vscode", "version", original
    )
    second = model_sync_host_b._primary_pointer(
        "coder", "session", "workspace", "ubuntu-vscode", "version", changed
    )

    assert first["pointer_sha256"] == second["pointer_sha256"]
    assert first["independent_renderer_sha256"] != second["independent_renderer_sha256"]


def test_snapshot_uses_independent_renderer_inputs_without_persisting_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "install.env"
    env_file.write_text(
        "ROOT_DOMAIN=proof.example.test\nSTACK_NAME=proof-stack\nPACKS=coder\nAI_DEFAULT_PROVIDER=openrouter\nAI_DEFAULT_MODEL=example/model\nDOKPLOY_ADMIN_EMAIL=operator@example.test\nDOKPLOY_ADMIN_PASSWORD=SECRET-ADMIN\n",
        encoding="utf-8",
    )
    credential = "SECRET-CODER-HERMES-KEY"
    fallbacks = tuple(
        json.loads(
            _litellm_workspace_fallback_models_json(default_alias="openrouter/example/model")
        )
    )
    renderer = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000",
        credential,
        "openrouter/example/model",
        fallbacks,
        ("openrouter/example/model",),
    )
    observed = model_sync_host_b._render_legacy_pointer(renderer)
    pointer_sha = hashlib.sha256(
        json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    calls: list[tuple[list[str], bytes | None]] = []

    def api(
        _hostname: str, _token: str | None, path: str, _body: dict[str, str] | None = None
    ) -> Any:
        if path == "/api/v2/users/me":
            return {"id": "user-proof"}
        if path == "/api/v2/templates":
            return [
                {
                    "id": "template-proof",
                    "name": "ubuntu-vscode",
                    "active_version_id": "version-proof",
                    "active_version_name": "proof",
                }
            ]
        if path.startswith("/api/v2/workspaces?"):
            return {
                "count": 1,
                "workspaces": [
                    {
                        "id": "workspace-proof",
                        "name": "primary",
                        "template_id": "template-proof",
                        "template_version_id": "version-proof",
                    }
                ],
            }
        if "/builds?" in path:
            return [
                {"id": "build-proof", "build_number": 1, "status": "stopped", "transition": "stop"}
            ]
        if "/secrets?" in path:
            return [
                {
                    "id": "secret-proof",
                    "name": "secret",
                    "env_name": "OPENAI_API_KEY",
                    "description": "credential",
                }
            ]
        raise AssertionError(path)

    def run(command: list[str], **kwargs: Any) -> bytes:
        calls.append((command, kwargs.get("stdin")))
        assert kwargs["timeout_seconds"] <= 30
        if kwargs.get("stdin") == b"session\n":
            return json.dumps(
                {
                    "target": "/home/coder/.config/opencode/opencode.json",
                    "pointer": "/provider/litellm",
                    "mode": "0644",
                    "shape": "json-pointer",
                    "base_url": renderer.base_url,
                    "credential_value_sha256": hashlib.sha256(credential.encode()).hexdigest(),
                    "pointer_sha256": pointer_sha,
                    "scope": "pointer",
                }
            ).encode()
        return json.dumps([{"id": "openrouter/example/model"}]).encode()

    probe = model_sync_remote.RemoteProbe(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "amd64",
        False,
        {"cloudflare": (), "tailscale": (), "coder": (), "docker": (), "dokploy": ()},
        {plane: "absent" for plane in ("cloudflare", "tailscale", "coder", "docker", "dokploy")},
    )
    monkeypatch.setattr(model_sync_host_b, "_api", api)
    monkeypatch.setattr(model_sync_host_b, "_coder_login", lambda *_args: "session")
    monkeypatch.setattr(model_sync_host_b, "_coder_container_name", lambda *_args: "coder")
    monkeypatch.setattr(model_sync_host_b, "_image_inventory", lambda _stack_name: [])
    monkeypatch.setattr(model_sync_host_b, "_state_inventory", lambda _state_dir: {})
    monkeypatch.setattr(
        model_sync_host_b, "capture_local_authoritative_inventory", lambda *_args: probe
    )
    monkeypatch.setattr(
        model_sync_host_b,
        "load_litellm_generated_keys",
        lambda _state_dir: SimpleNamespace(virtual_keys={"coder-hermes": credential}),
    )
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", run)

    snapshot = model_sync_host_b._snapshot(env_file, tmp_path)

    coder = model_sync_artifacts.require_mapping(snapshot["coder"], "snapshot coder")
    workspaces = model_sync_artifacts.require_mapping(coder["workspaces"], "snapshot workspaces")
    pages = model_sync_artifacts.require_list(workspaces["pages"], "snapshot workspace pages")
    page = model_sync_artifacts.require_mapping(pages[0], "snapshot workspace page")
    items = model_sync_artifacts.require_list(page["items"], "snapshot workspace items")
    workspace = model_sync_artifacts.require_mapping(items[0], "snapshot workspace")
    pointers = model_sync_artifacts.require_list(workspace["legacy_pointers"], "snapshot pointers")
    pointer = model_sync_artifacts.require_mapping(pointers[0], "snapshot pointer")
    assert pointer["pointer_sha256"] == pointer["independent_renderer_sha256"]
    assert credential not in json.dumps(snapshot)
    assert all(credential not in " ".join(command) for command, _input_value in calls)
    assert [input_value for command, input_value in calls if "ssh" in command] == [
        ("session\n" + credential).encode(),
        b"session\n",
    ]
    assert [input_value for _command, input_value in calls if input_value != b"session\n"] == [
        ("session\n" + credential).encode()
    ]
    captured = capsys.readouterr()
    assert credential not in captured.out + captured.err


def test_legacy_baseline_hashes_are_canonical_and_drift_sensitive() -> None:
    snapshot = json.loads(_snapshot_wire())
    captured = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )
    legacy = model_sync_artifacts.require_list(
        captured.payload["legacy_workspace_managed_fingerprints"], "captured legacy fingerprints"
    )

    exact_by_scope = {
        item["scope"]: item["legacy_exact"]
        for value in legacy
        if (item := model_sync_artifacts.require_mapping(value, "captured legacy fingerprint"))
    }
    assert exact_by_scope == {"pointer": True, "target-and-symlink": False}
    assert (
        captured.legacy_workspace_managed_fingerprints_sha256
        == model_sync_baseline.canonical_sha256(legacy)
    )
    assert model_sync_host_b._sha(json.loads('{"b":2, "a":1}')) == model_sync_host_b._sha(
        json.loads('{ "a" : 1, "b" : 2 }')
    )
    snapshot["coder"]["workspaces"]["pages"][0]["items"][0]["legacy_pointers"][0][
        "independent_renderer_sha256"
    ] = _sha("f")
    changed = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )

    changed_legacy = model_sync_artifacts.require_list(
        changed.payload["legacy_workspace_managed_fingerprints"], "changed legacy fingerprints"
    )
    primary = next(
        fingerprint
        for item in changed_legacy
        if (
            fingerprint := model_sync_artifacts.require_mapping(item, "changed legacy fingerprint")
        )["target"]
        == "/home/coder/.config/opencode/opencode.json"
    )
    assert primary["legacy_exact"] is False
    assert (
        changed.legacy_workspace_managed_fingerprints_sha256
        != captured.legacy_workspace_managed_fingerprints_sha256
    )
    observed_snapshot = json.loads(_snapshot_wire())
    observed_snapshot["coder"]["workspaces"]["pages"][0]["items"][0]["legacy_pointers"][0][
        "pointer_sha256"
    ] = _sha("e")
    observed_changed = model_sync_baseline.parse_captured_baseline(
        json.dumps(observed_snapshot), stack_name="proof-stack"
    )

    assert (
        observed_changed.legacy_workspace_managed_fingerprints_sha256
        != captured.legacy_workspace_managed_fingerprints_sha256
    )


def test_legacy_value_drift_is_nonexact_without_rejecting_valid_metadata() -> None:
    snapshot = json.loads(_snapshot_wire())
    pointer = snapshot["coder"]["workspaces"]["pages"][0]["items"][0]["legacy_pointers"][0]
    pointer["credential_value_sha256"] = "d" * 64
    pointer["pointer_sha256"] = "e" * 64

    captured = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )
    legacy = model_sync_artifacts.require_list(
        captured.payload["legacy_workspace_managed_fingerprints"], "drifted legacy fingerprints"
    )
    first = next(
        item
        for value in legacy
        if (item := model_sync_artifacts.require_mapping(value, "legacy fingerprint"))["scope"]
        == "pointer"
    )

    assert first["legacy_exact"] is False
    assert first["credential_value_sha256"] == "d" * 64


def test_legacy_observed_safe_base_url_drift_is_nonexact() -> None:
    snapshot = json.loads(_snapshot_wire())
    snapshot["coder"]["workspaces"]["pages"][0]["items"][0]["legacy_pointers"][0]["base_url"] = (
        "https://observed.example.invalid/v1"
    )
    snapshot["coder"]["workspaces"]["pages"][0]["items"][0]["legacy_pointers"][0][
        "pointer_sha256"
    ] = _sha("d")

    captured = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )

    pointer = next(
        item
        for value in model_sync_artifacts.require_list(
            captured.payload["legacy_workspace_managed_fingerprints"], "legacy fingerprints"
        )
        if (item := model_sync_artifacts.require_mapping(value, "legacy fingerprint"))["scope"]
        == "pointer"
    )
    assert pointer["base_url"] == "https://observed.example.invalid/v1"
    assert pointer["legacy_exact"] is False


@pytest.mark.parametrize(
    "value",
    [
        "http://proof-stack-shared-litellm:4000",
        "https://opencode.ai/zen/go/v1",
        "http://127.0.0.1:4000",
        "http://[::1]:4000",
        "https://example.test/path",
    ],
)
def test_safe_base_url_accepts_current_template_contracts(value: str) -> None:
    assert model_sync_artifacts.require_safe_base_url(value) == value


@pytest.mark.parametrize(
    "value",
    [
        " http://example.test",
        "http://example.test ",
        "http://exa mple.test",
        "http://example.test/path with space",
        "http://exa\tmple.test",
        "http://example.test/\tpath",
        "http://example.test/\rpath",
        "http://example.test/\npath",
        "http://exa\x00mple.test",
        "http://example.test/\x01path",
        "http://example.test/\x7fpath",
        "http://example.test/\x85path",
        "http://exa\u00a0mple.test",
        "http://example.test/\u2003path",
        "http://example.test/\u200bpath",
        "\nhttp://[bad",
        "\x00http://[bad",
        "http://[::1\x00",
    ],
)
def test_safe_base_url_rejects_raw_whitespace_and_controls(value: str) -> None:
    with pytest.raises(model_sync_artifacts.CaptureSchemaError, match="base URL is unsafe"):
        model_sync_artifacts.require_safe_base_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.test",
        "//example.test/path",
        "http:///path",
        "http://example.test:invalid/path",
        "http://user:secret@example.test/path",
        "http://example.test/path?query=value",
        "http://example.test/path#fragment",
        "http://example.test/../path",
        "http://example.test/./path",
    ],
)
def test_safe_base_url_preserves_existing_fail_closed_contract(value: str) -> None:
    with pytest.raises(model_sync_artifacts.CaptureSchemaError, match="base URL is unsafe"):
        model_sync_artifacts.require_safe_base_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/%2e%2e/escape",
        "https://example.test/%2E%2e/escape",
        "https://example.test/.%2e/escape",
        "https://example.test/%2e./escape",
        "https://example.test/a%2fb",
        "https://example.test/a%2Fb",
        "https://example.test/a%5cb",
        "https://example.test/a%5Cb",
        "https://example.test/%00path",
        "https://example.test/%09path",
        "https://example.test/%7fpath",
        "https://example.test/%41",
        "https://example.test/%",
        "https://example.test/%2",
        "https://example.test/%GG",
    ],
)
def test_safe_base_url_rejects_percent_encoded_or_malformed_aliases(value: str) -> None:
    with pytest.raises(model_sync_artifacts.CaptureSchemaError, match="base URL is unsafe"):
        model_sync_artifacts.require_safe_base_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test\\evil/path",
        "https:\\example.test\\path",
        "https://example.test/path\\evil",
        "https://example.test\\@evil.test/path",
        "https://ｅxample.test/path",
        "https://éxample.test/path",
        "https://example.test/路径",
        "https://Example.test/path",
        "https://example.test./path",
        "https://example..test/path",
        "https://-example.test/path",
        "https://example-.test/path",
        "https://exam_ple.test/path",
        "https://xn--example.test/path",
        "https://999.999.999.999/path",
        "https://127.000.000.001/path",
        "https://[0:0:0:0:0:0:0:1]/path",
    ],
)
def test_safe_base_url_rejects_noncanonical_authority_aliases(value: str) -> None:
    with pytest.raises(model_sync_artifacts.CaptureSchemaError, match="base URL is unsafe"):
        model_sync_artifacts.require_safe_base_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://example.test:0/path",
        "http://example.test:080/path",
        "http://example.test:80/path",
        "https://example.test:443/path",
        "https://example.test:0443/path",
        "https://example.test:/path",
        "https://example.test/path/",
        "https://example.test//path",
    ],
)
def test_safe_base_url_rejects_noncanonical_port_or_path_aliases(value: str) -> None:
    with pytest.raises(model_sync_artifacts.CaptureSchemaError, match="base URL is unsafe"):
        model_sync_artifacts.require_safe_base_url(value)


def test_model_inventory_normalizes_like_legacy_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = "SECRET-EXPECTED-CREDENTIAL"
    payload = {
        "data": [
            {"id": " openrouter/one "},
            {"id": "openrouter/one"},
            {"id": ""},
            {"id": "slashless"},
            {"id": "openrouter/*"},
            {"id": "openai/hidden"},
            {"id": 7},
            "invalid",
            {"id": "opencode-go/two"},
        ]
    }
    captured: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> bytes:
        captured["command"] = command
        captured["input"] = kwargs.get("stdin")
        return json.dumps(payload["data"]).encode()

    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", run)

    models = model_sync_host_b._model_inventory(
        "coder", "session", "workspace", credential, "proof-stack"
    )

    assert models == ("openrouter/one", "opencode-go/two")
    assert credential not in " ".join(captured["command"])
    assert captured["input"] == ("session\n" + credential).encode()


def test_renderer_inputs_fail_closed_without_secret_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    credential = "SECRET-EXPECTED-CREDENTIAL"
    monkeypatch.setattr(
        model_sync_host_b,
        "load_litellm_generated_keys",
        lambda _state_dir: SimpleNamespace(virtual_keys={"coder-hermes": credential}),
    )
    monkeypatch.setattr(
        model_sync_host_b,
        "run_bounded_process",
        lambda _command, **_kwargs: (_ for _ in ()).throw(RuntimeError("model inventory failed")),
    )

    with pytest.raises(ValueError) as inventory_error:
        model_sync_host_b._legacy_renderer(
            {}, tmp_path, "coder", "session", "workspace", "proof-stack", "ubuntu-vscode"
        )

    captured = capsys.readouterr()
    assert credential not in str(inventory_error.value) + captured.out + captured.err
    assert "SECRET-REMOTE-ERROR" not in str(inventory_error.value) + captured.out + captured.err


def test_renderer_rejects_missing_base_url() -> None:
    with pytest.raises(ValueError):
        model_sync_host_b._render_legacy_pointer(
            model_sync_host_b.LegacyRenderer(
                "", "credential", "openrouter/example", (), ("openrouter/example",)
            )
        )


def test_legacy_renderer_uses_authoritative_internal_base_without_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_sync_host_b,
        "load_litellm_generated_keys",
        lambda _state_dir: SimpleNamespace(virtual_keys={"coder-hermes": "credential"}),
    )
    monkeypatch.setattr(
        model_sync_host_b, "_model_inventory", lambda *_args: ("openrouter/example",)
    )

    renderer = model_sync_host_b._legacy_renderer(
        {}, tmp_path, "coder", "session", "workspace", "proof-stack", "ubuntu-vscode"
    )

    assert renderer.base_url == coder_module._litellm_internal_base_url("proof-stack")
    assert renderer.base_url == "http://proof-stack-shared-litellm:4000"


def test_real_templates_preserve_authoritative_base_url_contracts() -> None:
    root = Path(__file__).parents[2]
    primary_templates = (
        "default-ubuntu-code-server",
        "default-ubuntu-code-server-opencode-web",
        "default-ubuntu-code-server-openwork",
    )
    for template in primary_templates:
        source = (root / f"templates/coder/{template}/main.tf").read_text(encoding="utf-8")
        assert "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__" in source
        assert 'base_url = os.environ["AI_DEFAULT_BASE_URL"].rstrip("/")' in source
        assert 'f"{base_url}/v1/models"' in source
    kdense = (root / "templates/coder/default-ubuntu-code-server-kdense-byok/main.tf").read_text(
        encoding="utf-8"
    )
    assert 'default      = "https://opencode.ai/zen/go/v1"' in kdense
    assert (
        "KDENSE_OPENCODE_GO_BASE_URL=${data.coder_parameter.kdense_opencode_go_base_url.value}"
        in kdense
    )


def test_model_inventory_rejects_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", lambda _command, **_kwargs: b"[]")

    with pytest.raises(ValueError):
        model_sync_host_b._model_inventory(
            "coder", "session", "workspace", "credential", "proof-stack"
        )


@pytest.mark.parametrize("extra_byte", [False, True])
def test_workspace_pointer_stdout_has_exact_byte_limit(
    extra_byte: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = 2 * 1024 * 1024
    payload = '{"scope":"pointer"}'
    stdout = payload + " " * (limit - len(payload) + int(extra_byte))

    def run(_command: list[str], **kwargs: Any) -> bytes:
        if len(stdout.encode()) > kwargs["output_limit"]:
            raise RuntimeError("legacy pointer exceeded output limit")
        return stdout.encode()

    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", run)
    renderer = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000",
        "credential",
        "openrouter/example",
        (),
        ("openrouter/example",),
    )

    if extra_byte:
        with pytest.raises(ValueError):
            model_sync_host_b._primary_pointer(
                "coder", "session", "workspace", "ubuntu-vscode", "version", renderer
            )
    else:
        model_sync_host_b._primary_pointer(
            "coder", "session", "workspace", "ubuntu-vscode", "version", renderer
        )


@pytest.mark.parametrize("extra_byte", [False, True])
def test_model_inventory_stdout_has_exact_byte_limit(
    extra_byte: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = 2 * 1024 * 1024
    payload = '[{"id":"openrouter/example"}]'
    stdout = payload + " " * (limit - len(payload) + int(extra_byte))

    def run(_command: list[str], **kwargs: Any) -> bytes:
        if len(stdout.encode()) > kwargs["output_limit"]:
            raise RuntimeError("model inventory exceeded output limit")
        return stdout.encode()

    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", run)

    if extra_byte:
        with pytest.raises(ValueError):
            model_sync_host_b._model_inventory(
                "coder", "session", "workspace", "credential", "proof-stack"
            )
    else:
        assert model_sync_host_b._model_inventory(
            "coder", "session", "workspace", "credential", "proof-stack"
        ) == ("openrouter/example",)


@pytest.mark.parametrize("extra_record", [False, True])
def test_model_inventory_has_exact_record_limit(
    extra_record: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = 1_000 + int(extra_record)
    stdout = json.dumps([{"id": f"p/{index}"} for index in range(count)], separators=(",", ":"))
    monkeypatch.setattr(
        model_sync_host_b, "run_bounded_process", lambda _command, **_kwargs: stdout.encode()
    )

    if extra_record:
        with pytest.raises(ValueError):
            model_sync_host_b._model_inventory(
                "coder", "session", "workspace", "credential", "proof-stack"
            )
    else:
        assert (
            len(
                model_sync_host_b._model_inventory(
                    "coder", "session", "workspace", "credential", "proof-stack"
                )
            )
            == count
        )


def test_model_inventory_uses_workspace_python_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> bytes:
        captured["command"] = command
        captured["input"] = kwargs.get("stdin")
        return b'[{"id":"openrouter/example"}]'

    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", run)

    model_sync_host_b._model_inventory("coder", "session", "workspace", "credential", "proof-stack")

    assert "ssh" in captured["command"]
    assert "python3" not in captured["command"]
    node_index = captured["command"].index("node")
    assert captured["command"][node_index : node_index + 2] == ["node", "-e"]


@pytest.mark.parametrize(
    ("stream", "extra_byte"),
    [("stdout", False), ("stdout", True), ("stderr", False), ("stderr", True)],
)
def test_bounded_process_enforces_real_stdout_and_stderr_limits(
    stream: str,
    extra_byte: bool,
) -> None:
    limit = 65_536
    descriptor = 1 if stream == "stdout" else 2
    script = "import os,sys;n=int(sys.argv[1]);fd=int(sys.argv[2]);chunk=b'x'*4096\nwhile n: w=min(n,len(chunk));os.write(fd,chunk[:w]);n-=w"
    command = [sys.executable, "-c", script, str(limit + int(extra_byte)), str(descriptor)]

    if extra_byte:
        with pytest.raises(RuntimeError, match="bounded fixture exceeded output limit"):
            model_sync_results.run_bounded_process(
                command, stdin=b"", output_limit=limit, timeout_seconds=5, label="bounded fixture"
            )
    else:
        output = model_sync_results.run_bounded_process(
            command, stdin=b"", output_limit=limit, timeout_seconds=5, label="bounded fixture"
        )
        assert output == (b"x" * limit if stream == "stdout" else b"")


def test_bounded_process_supports_binary_stdin_and_timeout() -> None:
    payload = b"\x00\xffbinary-input"
    output = model_sync_results.run_bounded_process(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin=payload,
        output_limit=1024,
        timeout_seconds=5,
        label="binary fixture",
    )

    assert output == payload
    with pytest.raises(RuntimeError, match="timeout fixture timed out"):
        model_sync_results.run_bounded_process(
            [sys.executable, "-c", "import time;time.sleep(10)"],
            stdin=b"",
            output_limit=1024,
            timeout_seconds=0.05,
            label="timeout fixture",
        )


def test_bounded_process_child_failure_redacts_stderr_and_stdin() -> None:
    secret = b"SECRET-BOUNDED-PAYLOAD"
    with pytest.raises(RuntimeError, match="failure fixture failed") as error:
        model_sync_results.run_bounded_process(
            [
                sys.executable,
                "-c",
                "import os,sys;data=sys.stdin.buffer.read();os.write(2,data);raise SystemExit(7)",
            ],
            stdin=secret,
            output_limit=1024,
            timeout_seconds=5,
            label="failure fixture",
        )

    assert secret.decode() not in str(error.value)


@pytest.mark.parametrize("mode", ["overflow", "timeout"])
def test_bounded_process_kills_and_reaps_failed_children(tmp_path: Path, mode: str) -> None:
    pid_file = tmp_path / "child.pid"
    action = "os.write(1,b'x'*2048)" if mode == "overflow" else "time.sleep(10)"
    script = "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));" + action

    with pytest.raises(RuntimeError):
        model_sync_results.run_bounded_process(
            [sys.executable, "-c", script, str(pid_file)],
            stdin=b"",
            output_limit=1024,
            timeout_seconds=0.1 if mode == "timeout" else 5,
            label=f"{mode} fixture",
        )

    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text(encoding="utf-8")), 0)


def test_local_authoritative_inventory_uses_bounded_binary_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET-LOCAL-INVENTORY"
    captured: dict[str, Any] = {}
    transport = model_sync_results.ProofTransport(
        None,
        None,
        "example.test",
        None,
        None,
        None,
        "operator@example.test",
        "coder.example.test",
        secret,
        False,
    )

    def run(command: list[str], **kwargs: Any) -> bytes:
        captured["command"] = command
        captured["stdin"] = kwargs["stdin"]
        captured["limit"] = kwargs["output_limit"]
        return _preflight_wire("machine-local").encode()

    monkeypatch.setattr(model_sync_remote, "resolve_proof_transport", lambda _path: transport)
    monkeypatch.setattr(model_sync_remote, "run_bounded_process", run)

    probe = model_sync_remote.capture_local_authoritative_inventory(
        tmp_path / "install.env", ProofNamespace("proof-stack", (), (), (), (), ())
    )

    assert probe.architecture == "amd64"
    assert secret not in " ".join(captured["command"])
    assert secret.encode() in captured["stdin"]
    assert captured["limit"] == 2 * 1024 * 1024


_IMAGE_LOGICAL = ("coder", "litellm", "pgvector", "redis", "postfix")
_IMAGE_SERVICES = {
    "coder": "proof-stack-coder",
    "litellm": "proof-stack-shared-litellm",
    "pgvector": "proof-stack-shared-postgres",
    "redis": "proof-stack-shared-redis",
    "postfix": "proof-stack-shared-postfix",
}
_IMAGE_REFERENCES = {
    "coder": "ghcr.io/coder/coder:latest",
    "litellm": "ghcr.io/berriai/litellm:main-latest",
    "pgvector": "pgvector/pgvector:pg16",
    "redis": "redis:7-alpine",
    "postfix": "boky/postfix:latest",
}
_IMAGE_REPOSITORIES = {
    "coder": "ghcr.io/coder/coder",
    "litellm": "ghcr.io/berriai/litellm",
    "pgvector": "docker.io/pgvector/pgvector",
    "redis": "docker.io/library/redis",
    "postfix": "docker.io/boky/postfix",
}


def _single_manifest_raw(marker: str) -> bytes:
    return json.dumps(
        {
            "config": {
                "digest": f"sha256:{marker * 64}",
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": 1,
            },
            "layers": [],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _index_raw(platforms: list[dict[str, str]]) -> bytes:
    return json.dumps(
        {
            "manifests": [
                {
                    "digest": f"sha256:{str(index + 1) * 64}",
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "platform": platform,
                    "size": 1,
                }
                for index, platform in enumerate(platforms)
            ],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


_IMAGE_RAW = {
    name: _single_manifest_raw(marker) for name, marker in zip(_IMAGE_LOGICAL, "abcde", strict=True)
}
_IMAGE_RAW["coder"] = _index_raw(
    [
        {"architecture": "amd64", "os": "linux"},
        {"architecture": "arm64", "os": "linux", "variant": "v8"},
    ]
)
_IMAGE_DIGESTS = {
    name: f"sha256:{hashlib.sha256(raw).hexdigest()}" for name, raw in _IMAGE_RAW.items()
}


def _image_inventory_runner(
    overrides: dict[str, Any] | None = None,
) -> tuple[Any, list[list[str]]]:
    values = {} if overrides is None else overrides
    calls: list[list[str]] = []
    ids = {name: character * 64 for name, character in zip(_IMAGE_LOGICAL, "abcde", strict=True)}
    image_ids = {
        name: f"sha256:{character * 64}"
        for name, character in zip(_IMAGE_LOGICAL, "12345", strict=True)
    }
    service_names = {service: name for name, service in _IMAGE_SERVICES.items()}
    container_ids = {identifier: name for name, identifier in ids.items()}
    local_ids = {identifier: name for name, identifier in image_ids.items()}
    references = {**_IMAGE_REFERENCES, **values.get("references", {})}
    repositories = {**_IMAGE_REPOSITORIES, **values.get("repositories", {})}
    registry_raw = {**_IMAGE_RAW, **values.get("registry_raw", {})}

    def run(command: list[str], **kwargs: Any) -> bytes:
        calls.append(command)
        assert kwargs["stdin"] == b""
        assert kwargs["output_limit"] == 2 * 1024 * 1024
        if values.get("failure_kind") in command:
            raise RuntimeError(str(values["failure_message"]))
        if command[1] == "ps":
            service = next(
                item.removeprefix("label=com.docker.compose.service=")
                for item in command
                if item.startswith("label=com.docker.compose.service=")
            )
            logical_name = service_names[service]
            if values.get("missing") == logical_name:
                return b""
            output = ids[logical_name]
            if values.get("duplicate") == logical_name:
                output += "\n" + "f" * 64
            return (output + "\n").encode()
        if command[1:4] == ["inspect", "--type", "container"]:
            logical_name = container_ids[command[-1]]
            return json.dumps(
                [{"Config": {"Image": references[logical_name]}, "Image": image_ids[logical_name]}]
            ).encode()
        if command[1:3] == ["image", "inspect"]:
            logical_name = local_ids[command[-1]]
            local_digest = values.get("local_digests", {}).get(
                logical_name, f"sha256:{hashlib.sha256(registry_raw[logical_name]).hexdigest()}"
            )
            local_evidence = values.get("local_evidence", {}).get(
                logical_name, f"{repositories[logical_name]}@{local_digest}"
            )
            return json.dumps(
                [
                    {
                        "RepoDigests": values.get("repo_digests", {}).get(
                            logical_name, [local_evidence]
                        )
                    }
                ]
            ).encode()
        if command[1:4] == ["buildx", "imagetools", "inspect"]:
            logical_name = next(
                name
                for name, repository in repositories.items()
                if command[-1].startswith(f"{repository}@")
            )
            raw = registry_raw[logical_name]
            assert isinstance(raw, bytes)
            return raw
        raise AssertionError(command)

    return run, calls


def _resource_plane_snapshot(images: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cloudflare": {"access_application_ids": [], "dns_record_ids": [], "tunnel_ids": []},
        "images": images,
        "tailscale": {"identifiers": []},
        "wizard_state": {"ledger_sha256": "a" * 64, "resources": [], "state_sha256": "b" * 64},
    }


def test_image_inventory_binds_five_running_services_to_independent_registry_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, calls = _image_inventory_runner()
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", runner)

    images = model_sync_host_b._image_inventory("proof-stack")
    parsed = model_sync_artifacts.parse_resource_planes(_resource_plane_snapshot(images))

    assert tuple(item["logical_name"] for item in images) == _IMAGE_LOGICAL
    assert parsed.images == {
        name: f"{_IMAGE_REPOSITORIES[name]}@{_IMAGE_DIGESTS[name]}" for name in _IMAGE_LOGICAL
    }
    assert sum(command[1] == "ps" for command in calls) == 5
    assert {
        next(item for item in command if item.startswith("label=com.docker.compose.service="))
        for command in calls
        if command[1] == "ps"
    } == {f"label=com.docker.compose.service={service}" for service in _IMAGE_SERVICES.values()}
    assert sum(command[1:3] == ["image", "inspect"] for command in calls) == 5
    registry_calls = [
        command for command in calls if command[1:4] == ["buildx", "imagetools", "inspect"]
    ]
    assert len(registry_calls) == 5
    assert [command[-1] for command in registry_calls] == [
        f"{_IMAGE_REPOSITORIES[name]}@{_IMAGE_DIGESTS[name]}" for name in _IMAGE_LOGICAL
    ]
    assert all(command[-2] == "--raw" for command in registry_calls)


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ("missing", "running container must resolve exactly once"),
        ("duplicate", "running container must resolve exactly once"),
    ],
)
def test_image_inventory_rejects_missing_or_duplicate_service(
    condition: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _calls = _image_inventory_runner({condition: "coder"})
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", runner)

    with pytest.raises((ValueError, model_sync_artifacts.CaptureSchemaError), match=message):
        model_sync_host_b._image_inventory("proof-stack")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"local_digests": {"coder": "sha256:INVALID"}}, "local image digest is invalid"),
        (
            {"local_evidence": {"coder": "ghcr.io/coder/coder:latest"}},
            "local image digest is invalid",
        ),
        (
            {"local_evidence": {"coder": f"ghcr.io/other/coder@{_IMAGE_DIGESTS['coder']}"}},
            "local image digest is missing or ambiguous",
        ),
        (
            {"repo_digests": {"coder": [f"ghcr.io/coder/coder@{_IMAGE_DIGESTS['coder']}"] * 2}},
            "local image digest is missing or ambiguous",
        ),
        (
            {"local_digests": {"coder": "sha256:" + "f" * 64}},
            "container and registry image digests disagree",
        ),
        ({"registry_raw": {"coder": b"{"}}, "registry manifest returned invalid JSON"),
        (
            {"registry_raw": {"coder": b'{"mediaType":"application/example","schemaVersion":2}'}},
            "registry manifest media type is unsupported",
        ),
    ],
)
def test_image_inventory_rejects_malformed_or_mismatched_digests(
    overrides: dict[str, Any],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _calls = _image_inventory_runner(overrides)
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", runner)

    with pytest.raises((ValueError, model_sync_artifacts.CaptureSchemaError), match=message):
        model_sync_host_b._image_inventory("proof-stack")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "config": {
                "digest": "sha256:" + "a" * 64,
                "mediaType": "application/vnd.oci.image.config.v1+json",
            },
            "layers": [],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        },
        {
            "config": {
                "digest": "sha256:" + "a" * 64,
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": 1,
            },
            "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "size": 1}],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        },
        {
            "manifests": [
                {
                    "digest": "sha256:" + "a" * 64,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        },
    ],
)
def test_image_inventory_rejects_incomplete_registry_descriptors(
    payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    runner, _calls = _image_inventory_runner({"registry_raw": {"coder": raw}})
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", runner)

    with pytest.raises(
        model_sync_artifacts.CaptureSchemaError, match="registry manifest descriptor"
    ):
        model_sync_host_b._image_inventory("proof-stack")


@pytest.mark.parametrize(
    "platforms",
    [
        [{"architecture": "amd64", "os": "linux"}, {"architecture": "amd64", "os": "linux"}],
        [{"architecture": "amd64"}],
    ],
)
def test_image_inventory_index_digest_does_not_project_platform_metadata(
    platforms: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _calls = _image_inventory_runner({"registry_raw": {"coder": _index_raw(platforms)}})
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", runner)

    images = model_sync_host_b._image_inventory("proof-stack")

    assert images[0]["registry_image"] == images[0]["container_image"]


def test_image_inventory_normalizes_docker_hub_and_registry_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = "registry.example.test:5000/team/coder"
    runner, _calls = _image_inventory_runner(
        {
            "references": {"coder": f"{repository}:release"},
            "repositories": {"coder": repository},
        }
    )
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", runner)

    images = model_sync_host_b._image_inventory("proof-stack")

    assert images[0]["container_image"] == f"{repository}@{_IMAGE_DIGESTS['coder']}"
    assert images[0]["registry_image"] == images[0]["container_image"]
    assert images[3]["container_image"] == f"docker.io/library/redis@{_IMAGE_DIGESTS['redis']}"


def test_image_inventory_accepts_documented_single_manifest_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _single_manifest_raw("f")
    runner, _calls = _image_inventory_runner({"registry_raw": {"coder": raw}})
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", runner)

    images = model_sync_host_b._image_inventory("proof-stack")

    assert images[0]["registry_image"] == images[0]["container_image"]


def test_image_inventory_compares_multi_platform_index_digest_not_child_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _calls = _image_inventory_runner()
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", runner)

    images = model_sync_host_b._image_inventory("proof-stack")

    assert images[0]["container_image"] == f"ghcr.io/coder/coder@{_IMAGE_DIGESTS['coder']}"
    assert _IMAGE_DIGESTS["coder"] not in _IMAGE_RAW["coder"].decode()


@pytest.mark.parametrize("failure", ["timed out", "exceeded output limit"])
def test_image_inventory_propagates_bounded_command_failures(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _calls = _image_inventory_runner(
        {
            "failure_kind": "buildx",
            "failure_message": f"coder registry image {failure}",
        }
    )
    monkeypatch.setattr(model_sync_host_b, "run_bounded_process", runner)

    with pytest.raises(RuntimeError, match=failure):
        model_sync_host_b._image_inventory("proof-stack")


_KDENSE_CATALOG = (
    ("Unsloth Active (local alias)", "local-model.internal/unsloth-active"),
    ("Claude Opus 4.7", "openrouter/anthropic/claude-opus-4.7"),
    ("Claude Sonnet 4.6", "openrouter/anthropic/claude-sonnet-4.6"),
    ("GPT-5.4 Pro", "openrouter/openai/gpt-5.4-pro"),
    ("GPT-5.4", "openrouter/openai/gpt-5.4"),
    ("GPT-5.4 Mini", "openrouter/openai/gpt-5.4-mini"),
    ("GPT-5.4 Nano", "openrouter/openai/gpt-5.4-nano"),
    ("Grok 4.20 Beta", "openrouter/x-ai/grok-4.20-beta"),
    ("Gemini 3.1 Pro Preview", "openrouter/google/gemini-3.1-pro-preview"),
    ("Gemini 3 Flash Preview", "openrouter/google/gemini-3-flash-preview"),
    ("Gemini 3.1 Flash Lite Preview", "openrouter/google/gemini-3.1-flash-lite-preview"),
    ("Qwen3 Max Thinking", "openrouter/qwen/qwen3-max-thinking"),
    ("Qwen3 Coder Next", "openrouter/qwen/qwen3-coder-next"),
    ("GLM 5 Turbo", "openrouter/z-ai/glm-5-turbo"),
    ("GLM 5", "openrouter/z-ai/glm-5"),
    ("MiniMax M2.5", "openrouter/minimax/minimax-m2.5"),
    ("MiniMax M2.5 (free)", "openrouter/minimax/minimax-m2.5:free"),
    ("Kimi K2.5", "openrouter/moonshotai/kimi-k2.5"),
    ("Nemotron 3 Super", "openrouter/nvidia/nemotron-3-super-120b-a12b"),
    (
        "Nemotron 3 Nano Omni (free)",
        "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    ),
)


def test_kdense_renderer_catalog_matches_terraform_static_options() -> None:
    terraform = (
        Path(__file__).parents[2] / "templates/coder/default-ubuntu-code-server-kdense-byok/main.tf"
    ).read_text(encoding="utf-8")
    block = terraform.split("kdense_model_options = [", 1)[1].split("\n  ]", 1)[0]
    names = [
        line.split('"', 2)[1] for line in block.splitlines() if line.strip().startswith("name  =")
    ]
    values = [
        line.split('"', 2)[1] for line in block.splitlines() if line.strip().startswith("value =")
    ]
    authoritative = tuple(zip(names, values, strict=True))

    assert authoritative == _KDENSE_CATALOG == model_sync_host_b._KDENSE_CATALOG


def _terraform_kdense_models(
    source_models: list[dict[str, Any]],
    default_model: str = "openai/anthropic/claude-opus-4.7",
    expert_model: str = "openai/google/gemini-3.1-pro-preview",
) -> list[dict[str, Any]]:
    source_by_id = {
        str(model.get("id", "")): {
            key: value for key, value in model.items() if key not in {"default", "expertDefault"}
        }
        for model in source_models
        if str(model.get("id", "")).startswith("openrouter/")
    }
    merged: list[dict[str, Any]] = []
    for label, option_value in _KDENSE_CATALOG:
        if not option_value.startswith("openrouter/"):
            continue
        model = dict(source_by_id.get(option_value, {}))
        if not model:
            model = {"id": option_value, "label": label, "provider": "OpenRouter"}
        model.pop("default", None)
        model.pop("expertDefault", None)
        model["id"] = "openai/" + option_value.removeprefix("openrouter/")
        model["label"] = label
        model["provider"] = "OpenCode Go"
        description = str(model.get("description", "")).strip()
        model["description"] = (
            description + "\n\n" if description else ""
        ) + "Available through the central LiteLLM OpenCode Go-compatible gateway."
        merged.append(model)
    deduped = list({str(model["id"]): model for model in reversed(merged)}.values())[::-1]
    for model in deduped:
        if model["id"] == default_model:
            model["default"] = True
        if model["id"] == expert_model:
            model["expertDefault"] = True
    if not any(model.get("default") for model in deduped):
        deduped[0]["default"] = True
    if not any(model.get("expertDefault") for model in deduped):
        next((model for model in deduped if model.get("default")), deduped[0])["expertDefault"] = (
            True
        )
    return deduped


def _kdense_source_fixture() -> list[dict[str, Any]]:
    return [
        {
            "id": "openrouter/anthropic/claude-opus-4.7",
            "label": "Superseded duplicate",
            "provider": "OpenRouter",
            "description": "This duplicate must not survive.",
            "contextWindow": 1,
        },
        {
            "id": "openrouter/anthropic/claude-opus-4.7",
            "label": "Upstream Opus",
            "provider": "OpenRouter",
            "description": "Upstream reasoning model.",
            "contextWindow": 200_000,
            "inputModalities": ["text", "image"],
            "default": True,
        },
        {
            "id": "openrouter/google/gemini-3.1-pro-preview",
            "label": "Upstream Gemini",
            "provider": "OpenRouter",
            "description": "Upstream multimodal expert.",
            "contextWindow": 1_048_576,
            "outputModalities": ["text"],
            "expertDefault": True,
        },
        {"id": "ollama/local-only", "label": "Ignored local model", "provider": "Ollama"},
    ]


def _commit_kdense_source(tmp_path: Path, source_models: list[dict[str, Any]]) -> tuple[Path, str]:
    repo = tmp_path / "kdense-source"
    target = repo / "web/src/data/models.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(source_models, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.test"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://github.com/K-Dense-AI/k-dense-byok.git",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "web/src/data/models.json"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", revision], check=True
    )
    return repo, revision


def _kdense_observed(
    renderer: model_sync_host_b.LegacyRenderer, target: list[dict[str, Any]]
) -> dict[str, Any]:
    target_path = "/home/coder/.cache/kdense-byok-src/web/src/data/models.json"
    target_sha = model_sync_host_b._sha(target)
    symlink_sha = model_sync_host_b._sha(target_path.encode())
    credential_sha = model_sync_host_b._sha(renderer.credential.encode())
    aggregate_sha = model_sync_host_b._sha(
        {
            "base_url": renderer.base_url,
            "credential_value_sha256": credential_sha,
            "symlink_sha256": symlink_sha,
            "target_sha256": target_sha,
        }
    )
    return {
        "base_url": renderer.base_url,
        "credential_value_sha256": credential_sha,
        "mode": "0644",
        "pointer": "/home/coder/.local/state/dokploy-wizard/model-sync/current",
        "pointer_sha256": aggregate_sha,
        "scope": "target-and-symlink",
        "shape": "json-target-and-symlink",
        "symlink_sha256": symlink_sha,
        "symlink_state": "present",
        "symlink_target": target_path,
        "target": target_path,
        "target_sha256": target_sha,
    }


@pytest.mark.parametrize(
    ("default_model", "expert_model", "default_index", "expert_index"),
    [
        ("openai/anthropic/claude-opus-4.7", "openai/google/gemini-3.1-pro-preview", 0, 7),
        ("openai/missing-default", "openai/missing-expert", 0, 0),
    ],
)
def test_kdense_renderer_reconstructs_terraform_catalog_from_git_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    default_model: str,
    expert_model: str,
    default_index: int,
    expert_index: int,
) -> None:
    source_models = _kdense_source_fixture()
    expected_target = _terraform_kdense_models(source_models, default_model, expert_model)
    repo, revision = _commit_kdense_source(tmp_path, source_models)
    env_file = repo / ".env"
    env_file.write_text(
        f"DEFAULT_AGENT_MODEL={default_model}\nDEFAULT_EXPERT_MODEL={expert_model}\n",
        encoding="utf-8",
    )
    (repo / "web/src/data/models.json").write_text(
        json.dumps(expected_target, indent=2) + "\n", encoding="utf-8"
    )
    renderer = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000", "credential", "unused", (), ()
    )
    observed = _kdense_observed(renderer, expected_target)
    real_run = subprocess.run

    def workspace_json(
        _container: str,
        _token: str,
        _workspace: str,
        script: str,
        payload: str,
        args: tuple[str, ...],
        label: str,
    ) -> model_sync_artifacts.JsonValue:
        if label == "legacy pointer":
            return observed
        result = real_run(
            ["node", "-e", script, str(repo), str(env_file), *args[2:]],
            input=payload,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError("unable to reconstruct K-Dense legacy target")
        return model_sync_artifacts.require_mapping(
            json.loads(result.stdout), "K-Dense renderer output"
        )

    monkeypatch.setattr(model_sync_host_b, "_workspace_json", workspace_json)

    pointer = model_sync_host_b._primary_pointer(
        "coder", "session", "workspace", "ubuntu-vscode-kdense-byok", "historical", renderer
    )

    assert pointer["pointer_sha256"] == pointer["independent_renderer_sha256"]
    assert pointer["renderer_source_revision"] == revision
    assert pointer["renderer_source_path"] == "web/src/data/models.json"
    assert len(expected_target) == 19
    assert expected_target[0]["id"] == "openai/anthropic/claude-opus-4.7"
    assert expected_target[0]["contextWindow"] == 200_000
    assert expected_target[default_index]["default"] is True
    assert expected_target[1] == {
        "id": "openai/anthropic/claude-sonnet-4.6",
        "label": "Claude Sonnet 4.6",
        "provider": "OpenCode Go",
        "description": "Available through the central LiteLLM OpenCode Go-compatible gateway.",
    }
    assert expected_target[expert_index]["expertDefault"] is True


def test_kdense_renderer_fails_closed_without_git_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "kdense-archive"
    repo.mkdir()
    env_file = repo / ".env"
    env_file.write_text(
        "DEFAULT_AGENT_MODEL=openai/anthropic/claude-opus-4.7\n"
        "DEFAULT_EXPERT_MODEL=openai/google/gemini-3.1-pro-preview\n",
        encoding="utf-8",
    )
    renderer = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000", "credential", "unused", (), ()
    )
    real_run = subprocess.run

    def workspace_json(
        _container: str,
        _token: str,
        _workspace: str,
        script: str,
        payload: str,
        args: tuple[str, ...],
        label: str,
    ) -> model_sync_artifacts.JsonValue:
        if label == "legacy pointer":
            return _kdense_observed(renderer, _terraform_kdense_models(_kdense_source_fixture()))
        result = real_run(
            ["node", "-e", script, str(repo), str(env_file), *args[2:]],
            input=payload,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError("unable to reconstruct K-Dense legacy target")
        return model_sync_artifacts.require_mapping(
            json.loads(result.stdout), "K-Dense renderer output"
        )

    monkeypatch.setattr(model_sync_host_b, "_workspace_json", workspace_json)

    with pytest.raises(ValueError, match="unable to reconstruct K-Dense legacy target"):
        model_sync_host_b._primary_pointer(
            "coder", "session", "workspace", "ubuntu-vscode-kdense-byok", "historical", renderer
        )


def test_kdense_renderer_rejects_local_head_not_bound_to_origin_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_models = _kdense_source_fixture()
    repo, _revision = _commit_kdense_source(tmp_path, source_models)
    env_file = repo / ".env"
    env_file.write_text(
        "DEFAULT_AGENT_MODEL=openai/anthropic/claude-opus-4.7\n"
        "DEFAULT_EXPERT_MODEL=openai/google/gemini-3.1-pro-preview\n",
        encoding="utf-8",
    )
    source_models[1]["contextWindow"] = 999
    (repo / "web/src/data/models.json").write_text(
        json.dumps(source_models, indent=2) + "\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", "web/src/data/models.json"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "untrusted local commit"], check=True)
    renderer = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000", "credential", "unused", (), ()
    )
    real_run = subprocess.run

    def workspace_json(
        _container: str,
        _token: str,
        _workspace: str,
        script: str,
        payload: str,
        args: tuple[str, ...],
        label: str,
    ) -> model_sync_artifacts.JsonValue:
        if label == "legacy pointer":
            return _kdense_observed(renderer, _terraform_kdense_models(_kdense_source_fixture()))
        result = real_run(
            ["node", "-e", script, str(repo), str(env_file), *args[2:]],
            input=payload,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError("unable to reconstruct K-Dense legacy target")
        return model_sync_artifacts.require_mapping(
            json.loads(result.stdout), "K-Dense renderer output"
        )

    monkeypatch.setattr(model_sync_host_b, "_workspace_json", workspace_json)

    with pytest.raises(ValueError, match="unable to reconstruct K-Dense legacy target"):
        model_sync_host_b._primary_pointer(
            "coder", "session", "workspace", "ubuntu-vscode-kdense-byok", "historical", renderer
        )


def test_kdense_capture_binds_whole_target_and_current_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000",
        "credential",
        "openrouter/example",
        (),
        ("openrouter/example",),
    )
    target_path = "/home/coder/.cache/kdense-byok-src/web/src/data/models.json"
    symlink_target = target_path
    target_sha = model_sync_host_b._sha(_terraform_kdense_models(_kdense_source_fixture()))
    symlink_sha = model_sync_host_b._sha(symlink_target.encode())
    aggregate_sha = model_sync_host_b._sha(
        {
            "base_url": renderer.base_url,
            "credential_value_sha256": model_sync_host_b._sha(renderer.credential.encode()),
            "symlink_sha256": symlink_sha,
            "target_sha256": target_sha,
        }
    )
    observed = {
        "base_url": renderer.base_url,
        "credential_value_sha256": model_sync_host_b._sha(renderer.credential.encode()),
        "mode": "0644",
        "pointer": "/home/coder/.local/state/dokploy-wizard/model-sync/current",
        "pointer_sha256": aggregate_sha,
        "scope": "target-and-symlink",
        "shape": "json-target-and-symlink",
        "symlink_sha256": symlink_sha,
        "symlink_state": "present",
        "symlink_target": symlink_target,
        "target": target_path,
        "target_sha256": target_sha,
    }
    monkeypatch.setattr(
        model_sync_host_b,
        "_workspace_json",
        lambda _container, _token, _workspace, _script, _payload, _args, label: observed
        if label == "legacy pointer"
        else {
            "independent_renderer_sha256": aggregate_sha,
            "renderer_source_path": "web/src/data/models.json",
            "renderer_source_revision": "a" * 40,
        },
    )

    pointer = model_sync_host_b._primary_pointer(
        "coder", "session", "workspace", "ubuntu-vscode-kdense-byok", "historical", renderer
    )

    assert pointer["pointer_sha256"] == pointer["independent_renderer_sha256"]
    assert pointer["target_sha256"] == target_sha
    assert pointer["symlink_sha256"] == symlink_sha


def test_renderer_rejects_missing_generated_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_sync_host_b, "load_litellm_generated_keys", lambda _state_dir: None)

    with pytest.raises(ValueError):
        model_sync_host_b._legacy_renderer(
            {}, tmp_path, "coder", "session", "workspace", "proof-stack", "ubuntu-vscode"
        )


@pytest.mark.parametrize("template", ["ubuntu-vscode-hermes", "ubuntu-vscode-pi-web"])
def test_workspace_templates_without_exact_legacy_renderer_fail_closed(
    template: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_sync_host_b,
        "run_bounded_process",
        lambda *_args, **_kwargs: pytest.fail("unsupported template executed"),
    )

    with pytest.raises(ValueError):
        model_sync_host_b._primary_pointer(
            "coder",
            "session",
            "workspace",
            template,
            "version",
            model_sync_host_b.LegacyRenderer(
                "http://proof-stack-shared-litellm:4000",
                "credential",
                "openrouter/example",
                (),
                ("openrouter/example",),
            ),
        )


@pytest.mark.parametrize("mutation", ["unsupported"])
def test_workspace_inventory_fails_closed_before_pointer_capture(
    mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    template: dict[str, model_sync_artifacts.JsonValue] = {
        "id": "template-proof",
        "name": "ubuntu-vscode",
        "active_version_id": "version-proof",
        "active_version_name": "proof",
    }
    workspace = {
        "id": "workspace-proof",
        "name": "primary",
        "template_id": "missing-template",
        "template_version_id": "version-proof",
    }
    response: dict[str, Any] = {
        "count": 1,
        "workspaces": [workspace],
    }
    monkeypatch.setattr(model_sync_host_b, "_api", lambda *_args: response)
    monkeypatch.setattr(
        model_sync_host_b,
        "run_bounded_process",
        lambda *_args, **_kwargs: pytest.fail("pointer capture executed"),
    )

    with pytest.raises(ValueError):
        model_sync_host_b._workspaces(
            "coder.example.test",
            "session",
            "coder",
            [template],
            {},
            Path("."),
            "proof-stack",
        )


def test_historical_workspace_version_remains_bound_without_active_version_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template: dict[str, model_sync_artifacts.JsonValue] = {
        "id": "template-proof",
        "name": "ubuntu-vscode",
        "active_version_id": "active-version",
        "active_version_name": "active",
    }
    response = {
        "count": 1,
        "workspaces": [
            {
                "id": "workspace-proof",
                "name": "primary",
                "template_id": "template-proof",
                "template_version_id": "historical-version",
            }
        ],
    }
    monkeypatch.setattr(model_sync_host_b, "_api", lambda *_args: response)
    monkeypatch.setattr(
        model_sync_host_b,
        "_primary_pointer",
        lambda *_args: {"template_version_id": "historical-version"},
    )
    monkeypatch.setattr(
        model_sync_host_b,
        "_legacy_renderer",
        lambda *_args: model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "credential",
            "openrouter/example",
            (),
            ("openrouter/example",),
        ),
    )

    workspaces = model_sync_host_b._workspaces(
        "coder.example.test",
        "session",
        "coder",
        [template],
        {},
        Path("."),
        "proof-stack",
    )

    assert workspaces[0]["template_version_id"] == "historical-version"


def test_historical_workspace_version_is_preserved_in_baseline() -> None:
    snapshot = json.loads(_snapshot_wire())
    snapshot["coder"]["templates"]["pages"][0]["items"][0]["active_version_id"] = (
        "new-active-version"
    )

    captured = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )
    workspaces = model_sync_artifacts.require_list(captured.payload["workspaces"], "workspaces")
    primary = next(
        item
        for value in workspaces
        if (item := model_sync_artifacts.require_mapping(value, "workspace"))["name"] == "primary"
    )

    assert primary["template_version_id"] == "version-0"


def test_kdense_absent_current_symlink_cannot_be_exact() -> None:
    snapshot = json.loads(_snapshot_wire())
    pointer = snapshot["coder"]["workspaces"]["pages"][0]["items"][1]["legacy_pointers"][0]
    pointer["symlink_state"] = "absent"
    pointer["symlink_target"] = None
    pointer["symlink_sha256"] = None
    pointer["pointer_sha256"] = model_sync_host_b._sha(
        {
            "base_url": pointer["base_url"],
            "credential_value_sha256": pointer["credential_value_sha256"],
            "symlink_sha256": None,
            "target_sha256": pointer["target_sha256"],
        }
    )

    captured = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )
    legacy = model_sync_artifacts.require_list(
        captured.payload["legacy_workspace_managed_fingerprints"], "legacy fingerprints"
    )
    kdense = next(
        item
        for value in legacy
        if (item := model_sync_artifacts.require_mapping(value, "legacy fingerprint"))["scope"]
        == "target-and-symlink"
    )

    assert kdense["legacy_exact"] is False
    assert kdense["symlink_state"] == "absent"


def test_kdense_truthful_parameter_base_url_drift_is_preserved_as_nonexact() -> None:
    snapshot = json.loads(_snapshot_wire())
    pointer = snapshot["coder"]["workspaces"]["pages"][0]["items"][1]["legacy_pointers"][0]
    pointer["base_url"] = "https://opencode.ai/zen/go/v1"
    pointer["pointer_sha256"] = model_sync_host_b._sha(
        {
            "base_url": pointer["base_url"],
            "credential_value_sha256": pointer["credential_value_sha256"],
            "symlink_sha256": pointer["symlink_sha256"],
            "target_sha256": pointer["target_sha256"],
        }
    )

    captured = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )
    legacy = model_sync_artifacts.require_list(
        captured.payload["legacy_workspace_managed_fingerprints"], "legacy fingerprints"
    )
    kdense = next(
        item
        for value in legacy
        if (item := model_sync_artifacts.require_mapping(value, "legacy fingerprint"))["scope"]
        == "target-and-symlink"
    )

    assert kdense["base_url"] == "https://opencode.ai/zen/go/v1"
    assert kdense["legacy_exact"] is False


@pytest.mark.parametrize("field", ["target_sha256", "symlink_sha256", "credential_value_sha256"])
def test_kdense_target_symlink_and_credential_drift_are_nonexact(field: str) -> None:
    snapshot = json.loads(_snapshot_wire())
    pointer = snapshot["coder"]["workspaces"]["pages"][0]["items"][1]["legacy_pointers"][0]
    pointer[field] = "9" * 64
    pointer["pointer_sha256"] = model_sync_host_b._sha(
        {
            "base_url": pointer["base_url"],
            "credential_value_sha256": pointer["credential_value_sha256"],
            "symlink_sha256": pointer["symlink_sha256"],
            "target_sha256": pointer["target_sha256"],
        }
    )

    captured = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )
    legacy = model_sync_artifacts.require_list(
        captured.payload["legacy_workspace_managed_fingerprints"], "legacy fingerprints"
    )
    kdense = next(
        item
        for value in legacy
        if (item := model_sync_artifacts.require_mapping(value, "legacy fingerprint"))["scope"]
        == "target-and-symlink"
    )

    assert kdense["legacy_exact"] is False


@pytest.mark.parametrize("mutation", ["metadata", "id", "default", "expert", "order"])
def test_kdense_terraform_target_semantic_drift_is_nonexact(mutation: str) -> None:
    snapshot = json.loads(_snapshot_wire())
    pointer = snapshot["coder"]["workspaces"]["pages"][0]["items"][1]["legacy_pointers"][0]
    target = _terraform_kdense_models(_kdense_source_fixture())
    if mutation == "metadata":
        target[0]["contextWindow"] += 1
    elif mutation == "id":
        target[0]["id"] = "openrouter/anthropic/claude-opus-4.7"
    elif mutation == "default":
        target[0].pop("default")
    elif mutation == "expert":
        target[7].pop("expertDefault")
    else:
        target.reverse()
    pointer["target_sha256"] = model_sync_host_b._sha(target)
    pointer["pointer_sha256"] = model_sync_host_b._sha(
        {
            "base_url": pointer["base_url"],
            "credential_value_sha256": pointer["credential_value_sha256"],
            "symlink_sha256": pointer["symlink_sha256"],
            "target_sha256": pointer["target_sha256"],
        }
    )

    captured = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )
    legacy = model_sync_artifacts.require_list(
        captured.payload["legacy_workspace_managed_fingerprints"], "legacy fingerprints"
    )
    kdense = next(
        item
        for value in legacy
        if (item := model_sync_artifacts.require_mapping(value, "legacy fingerprint"))["scope"]
        == "target-and-symlink"
    )

    assert kdense["legacy_exact"] is False


def test_zero_workspace_baseline_has_no_adoption_candidates() -> None:
    snapshot = json.loads(_snapshot_wire())
    snapshot["coder"]["workspaces"] = {"pages": [{"items": [], "offset": 0}], "total": 0}
    snapshot["coder"]["builds"] = []

    captured = model_sync_baseline.parse_captured_baseline(
        json.dumps(snapshot), stack_name="proof-stack"
    )

    assert captured.payload["workspaces"] == []
    assert captured.payload["legacy_workspace_managed_fingerprints"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "duplicate",
        "mode",
        "shape",
        "scope",
        "target",
        "path",
        "base",
        "version",
        "unsupported",
        "hash",
        "credential_hash",
        "renderer_hash",
        "renderer_source_revision",
        "renderer_source_path",
    ],
)
def test_legacy_baseline_rejects_missing_or_malformed_renderer_evidence(mutation: str) -> None:
    snapshot = json.loads(_snapshot_wire())
    primary = snapshot["coder"]["workspaces"]["pages"][0]["items"][0]
    pointer = primary["legacy_pointers"][0]
    if mutation.startswith("renderer_source_"):
        pointer = snapshot["coder"]["workspaces"]["pages"][0]["items"][1]["legacy_pointers"][0]
    if mutation == "empty":
        primary["legacy_pointers"] = []
    elif mutation == "duplicate":
        primary["legacy_pointers"].append(dict(pointer))
    elif mutation == "mode":
        pointer["mode"] = "0600"
    elif mutation == "shape":
        pointer["shape"] = "json-target-and-symlink"
    elif mutation == "scope":
        pointer["scope"] = "target-and-symlink"
    elif mutation == "target":
        pointer["target"] = "/home/coder/other.json"
    elif mutation == "path":
        pointer["pointer"] = "/provider/other"
    elif mutation == "base":
        pointer["base_url"] = "https://user:secret@observed.example.invalid/v1"
    elif mutation == "version":
        pointer["template_version_id"] = "wrong-version"
    elif mutation == "unsupported":
        primary["template_id"] = "template-4"
        primary["template_version_id"] = "version-4"
        pointer["template_version_id"] = "version-4"
    elif mutation == "hash":
        pointer["pointer_sha256"] = "not-a-sha256"
    elif mutation == "credential_hash":
        pointer["credential_value_sha256"] = "not-a-sha256"
    elif mutation == "renderer_source_revision":
        pointer["renderer_source_revision"] = "not-a-git-revision"
    elif mutation == "renderer_source_path":
        pointer["renderer_source_path"] = "web/src/data/mutated.json"
    else:
        pointer["independent_renderer_sha256"] = "not-a-sha256"

    with pytest.raises(model_sync_baseline.BaselineCaptureError):
        model_sync_baseline.parse_captured_baseline(json.dumps(snapshot), stack_name="proof-stack")


@pytest.mark.parametrize(
    "payload",
    [b"{", b"\xff", json.dumps({"unknown": "SECRET-INPUT-SENTINEL"}).encode()],
)
def test_preflight_script_rejects_malformed_or_unknown_transport_without_secret_output(
    payload: bytes,
) -> None:
    result = subprocess.run(
        ["python", "-c", model_sync_results.PREFLIGHT_SCRIPT],
        input=payload,
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert b"SECRET-INPUT-SENTINEL" not in result.stdout + result.stderr


def test_coder_inventory_paginates_workspace_build_and_secret_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    templates: list[dict[str, Any]] = [
        {
            "active_version_id": f"version-{index}",
            "active_version_name": f"version-{index}",
            "id": f"template-{index}",
            "name": f"template-{index}",
        }
        for index in range(101)
    ]
    workspaces: list[dict[str, Any]] = [
        {
            "id": f"workspace-{index}",
            "name": f"workspace-{index}",
            "template_id": f"template-{index}",
            "template_version_id": f"version-{index}",
        }
        for index in range(101)
    ]
    builds: list[dict[str, Any]] = [
        {
            "build_number": index + 1,
            "id": f"build-{index}",
            "status": "stopped",
            "transition": "stop",
        }
        for index in range(101)
    ]
    secrets: list[dict[str, Any]] = [
        {
            "description": f"secret-{index}",
            "env_name": f"SECRET_{index}",
            "id": f"secret-{index}",
            "name": f"secret-{index}",
        }
        for index in range(101)
    ]

    template_requests: list[str] = []

    def api(
        _hostname: str, _token: str | None, path: str, _body: dict[str, str] | None = None
    ) -> Any:
        offset = 100 if "offset=100" in path else 0
        if path.startswith("/api/v2/templates"):
            template_requests.append(path)
            if path != "/api/v2/templates":
                raise AssertionError(path)
            return templates
        if path.startswith("/api/v2/workspaces?"):
            return {"count": len(workspaces), "workspaces": workspaces[offset : offset + 100]}
        if "/builds?" in path:
            return builds[offset : offset + 100]
        if "/secrets" in path:
            return secrets[offset : offset + 100]
        raise AssertionError(path)

    monkeypatch.setattr(model_sync_host_b, "_api", api)
    monkeypatch.setattr(
        model_sync_host_b,
        "_primary_pointer",
        lambda *_args: {"template_version_id": "version"},
    )
    monkeypatch.setattr(
        model_sync_host_b,
        "_legacy_renderer",
        lambda *_args: model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "key",
            "openrouter/example",
            (),
            ("openrouter/example",),
        ),
    )

    captured_templates = model_sync_host_b._templates("coder.example.test", "session")
    captured_workspaces = model_sync_host_b._workspaces(
        "coder.example.test",
        "session",
        "coder-container",
        captured_templates,
        {},
        Path("."),
        "proof-stack",
    )
    captured_builds: Any = model_sync_host_b._builds(
        "coder.example.test", "session", captured_workspaces
    )
    captured_secrets = model_sync_host_b._secrets("coder.example.test", "session", "user-1")

    assert {item["id"] for item in captured_templates} == {
        f"template-{index}" for index in range(101)
    }
    assert {item["id"] for item in captured_workspaces} == {
        f"workspace-{index}" for index in range(101)
    }
    assert {item["id"] for page in captured_builds[0]["pages"] for item in page["items"]} == {
        f"build-{index}" for index in range(101)
    }
    assert {item["id"] for item in captured_secrets} == {f"secret-{index}" for index in range(101)}
    assert template_requests == ["/api/v2/templates"]


def _snapshot_wire(*, omit_template: bool = False, malformed_build: bool = False) -> str:
    template_names = (
        "ubuntu-vscode",
        "ubuntu-vscode-opencode-web",
        "ubuntu-vscode-openwork",
        "ubuntu-vscode-kdense-byok",
        "ubuntu-vscode-hermes",
        "ubuntu-vscode-pi-web",
    )
    templates = [
        {
            "active_version_id": f"version-{index}",
            "active_version_name": f"dokploy-wizard-{index}",
            "id": f"template-{index}",
            "name": name,
            "rendered_source_sha256": _sha(str(index + 1)),
        }
        for index, name in enumerate(template_names)
    ]
    if omit_template:
        templates.pop()
    pointer_target = "/home/coder/.config/opencode/opencode.json"
    pointer = "/provider/litellm"
    pointer_sha = _legacy_sha(pointer_target, pointer)
    kdense_target = "/home/coder/.cache/kdense-byok-src/web/src/data/models.json"
    kdense_pointer = "/home/coder/.local/state/dokploy-wizard/model-sync/current"
    kdense_target_sha = model_sync_host_b._sha(_terraform_kdense_models(_kdense_source_fixture()))
    kdense_symlink_sha = model_sync_host_b._sha(kdense_target.encode())
    kdense_expected_sha = model_sync_host_b._sha(
        {
            "base_url": "http://proof-stack-shared-litellm:4000",
            "credential_value_sha256": _sha("c"),
            "symlink_sha256": kdense_symlink_sha,
            "target_sha256": kdense_target_sha,
        }
    )
    kdense_observed_sha = model_sync_host_b._sha(
        {
            "base_url": "https://opencode.ai/zen/go/v1",
            "credential_value_sha256": _sha("c"),
            "symlink_sha256": kdense_symlink_sha,
            "target_sha256": kdense_target_sha,
        }
    )
    return json.dumps(
        {
            "cloudflare": {
                "access_application_ids": ["access-proof"],
                "dns_record_ids": ["dns-proof"],
                "tunnel_ids": ["tunnel-proof"],
            },
            "coder": {
                "builds": [
                    {
                        "pages": [
                            {
                                "items": [
                                    {
                                        "build_number": 1,
                                        "id": "build-primary",
                                        "status": "not-a-coder-status"
                                        if malformed_build
                                        else "stopped",
                                        "transition": "stop",
                                    }
                                ],
                                "offset": 0,
                            }
                        ],
                        "total": 1,
                        "workspace_id": "workspace-primary",
                    },
                    {
                        "pages": [
                            {
                                "items": [
                                    {
                                        "build_number": 3,
                                        "id": "build-kdense",
                                        "status": "running",
                                        "transition": "start",
                                    }
                                ],
                                "offset": 0,
                            }
                        ],
                        "total": 1,
                        "workspace_id": "workspace-kdense",
                    },
                ],
                "secrets": {
                    "pages": [
                        {
                            "items": [
                                {
                                    "description": "Hermes LiteLLM virtual key for wizard-managed workspaces.",
                                    "environment_variable": "OPENAI_API_KEY",
                                    "id": "secret-hermes-key",
                                    "name": "hermes-openai-api-key",
                                }
                            ],
                            "offset": 0,
                        }
                    ],
                    "total": 1,
                },
                "templates": {
                    "pages": [{"items": templates, "offset": 0}],
                    "total": len(templates),
                },
                "workspaces": {
                    "pages": [
                        {
                            "items": [
                                {
                                    "id": "workspace-primary",
                                    "legacy_pointers": [
                                        {
                                            "base_url": "http://proof-stack-shared-litellm:4000",
                                            "credential_value_sha256": _sha("b"),
                                            "independent_renderer_sha256": pointer_sha,
                                            "mode": "0644",
                                            "pointer": pointer,
                                            "pointer_sha256": pointer_sha,
                                            "scope": "pointer",
                                            "shape": "json-pointer",
                                            "target": pointer_target,
                                            "template_version_id": "version-0",
                                        }
                                    ],
                                    "name": "primary",
                                    "template_id": "template-0",
                                    "template_version_id": "version-0",
                                },
                                {
                                    "id": "workspace-kdense",
                                    "legacy_pointers": [
                                        {
                                            "base_url": "https://opencode.ai/zen/go/v1",
                                            "credential_value_sha256": _sha("c"),
                                            "independent_renderer_sha256": kdense_expected_sha,
                                            "mode": "0644",
                                            "pointer": kdense_pointer,
                                            "pointer_sha256": kdense_observed_sha,
                                            "renderer_source_path": "web/src/data/models.json",
                                            "renderer_source_revision": "a" * 40,
                                            "scope": "target-and-symlink",
                                            "shape": "json-target-and-symlink",
                                            "symlink_sha256": kdense_symlink_sha,
                                            "symlink_state": "present",
                                            "symlink_target": kdense_target,
                                            "target": kdense_target,
                                            "target_sha256": kdense_target_sha,
                                            "template_version_id": "version-3",
                                        }
                                    ],
                                    "name": "kdense",
                                    "template_id": "template-3",
                                    "template_version_id": "version-3",
                                },
                            ],
                            "offset": 0,
                        }
                    ],
                    "total": 2,
                },
            },
            "images": [
                {
                    "container_image": _image_ref("ghcr.io/coder/coder", "1"),
                    "logical_name": "coder",
                    "registry_image": _image_ref("ghcr.io/coder/coder", "1"),
                },
                {
                    "container_image": _image_ref("ghcr.io/berriai/litellm", "2"),
                    "logical_name": "litellm",
                    "registry_image": _image_ref("ghcr.io/berriai/litellm", "2"),
                },
                {
                    "container_image": _image_ref("pgvector/pgvector", "3"),
                    "logical_name": "pgvector",
                    "registry_image": _image_ref("pgvector/pgvector", "3"),
                },
                {
                    "container_image": _image_ref("redis", "4"),
                    "logical_name": "redis",
                    "registry_image": _image_ref("redis", "4"),
                },
                {
                    "container_image": _image_ref("boky/postfix", "5"),
                    "logical_name": "postfix",
                    "registry_image": _image_ref("boky/postfix", "5"),
                },
            ],
            "schema_version": 1,
            "tailscale": {"identifiers": ["proof-stack-tailnet"]},
            "wizard_state": {
                "ledger_sha256": _sha("d"),
                "resources": ["proof-stack-coder", "proof-stack-shared"],
                "state_sha256": _sha("e"),
            },
        }
    )


class _FixtureChannel:
    def close(self) -> None:
        return None

    def exit_status_ready(self) -> bool:
        return True

    def recv_exit_status(self) -> int:
        return 0


class _FixtureStream:
    def __init__(self, payload: str) -> None:
        self.channel = _FixtureChannel()
        self._payload = payload.encode()

    def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]


class _FixtureStdin:
    def __init__(self) -> None:
        self.payload = b""

    def write(self, payload: bytes) -> int:
        self.payload += payload
        return len(payload)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FixtureRemoteKey:
    def __init__(self, fingerprint: bytes) -> None:
        self._fingerprint = fingerprint

    def get_fingerprint(self) -> bytes:
        return self._fingerprint


class _FixtureTransportHandle:
    def __init__(self, fingerprint: bytes) -> None:
        self._key = _FixtureRemoteKey(fingerprint)

    def get_remote_server_key(self) -> _FixtureRemoteKey:
        return self._key


class _FixtureRemoteClient:
    def __init__(
        self,
        *,
        machine_id: str,
        boot_id: str,
        fingerprint: bytes,
        snapshot: str,
    ) -> None:
        self._machine_id = machine_id
        self._boot_id = boot_id
        self._fingerprint = fingerprint
        self._snapshot = snapshot
        self.commands: list[str] = []
        self.stdins: list[_FixtureStdin] = []

    def exec_command(
        self, command: str, *, timeout: int
    ) -> tuple[_FixtureStdin, _FixtureStream, _FixtureStream]:
        del timeout
        if "model-sync-snapshot" in command:
            payload = self._snapshot
        elif "model-sync-boot-id" in command:
            payload = self._boot_id
        else:
            payload = _preflight_wire(self._machine_id)
        stdin = _FixtureStdin()
        self.commands.append(command)
        self.stdins.append(stdin)
        return stdin, _FixtureStream(payload), _FixtureStream("")

    def get_transport(self) -> _FixtureTransportHandle:
        return _FixtureTransportHandle(self._fingerprint)

    def close(self) -> None:
        return None


class _FixtureParamikoTransport:
    def __init__(self, client: _FixtureRemoteClient) -> None:
        self.client = client

    def close(self) -> None:
        self.client.close()


def _install_fixture_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: str,
) -> list[_FixtureRemoteClient]:
    clients: list[_FixtureRemoteClient] = []

    def connect(**kwargs: Any) -> _FixtureParamikoTransport:
        host = kwargs["hostname"]
        match host:
            case "host-a":
                client = _FixtureRemoteClient(
                    machine_id="machine-a",
                    boot_id="11111111-1111-1111-1111-111111111111",
                    fingerprint=b"ssh-a",
                    snapshot=snapshot,
                )
            case "host-b":
                client = _FixtureRemoteClient(
                    machine_id="machine-b",
                    boot_id="22222222-2222-2222-2222-222222222222",
                    fingerprint=b"ssh-b",
                    snapshot=snapshot,
                )
            case unexpected:
                raise AssertionError(f"unexpected fixture host {unexpected}")
        clients.append(client)
        return _FixtureParamikoTransport(client)

    monkeypatch.setattr(model_sync_remote.ParamikoRemoteTransport, "connect", connect)
    return clients


def _baseline_arguments(tmp_path: Path) -> list[str]:
    env_file = tmp_path / "install.env"
    env_file.write_text(
        "\n".join(
            (
                "ROOT_DOMAIN=proof.example.test",
                "STACK_NAME=proof-stack",
                "PACKS=coder",
                "AI_DEFAULT_PROVIDER=openrouter",
                "AI_DEFAULT_MODEL=example/model",
                "CLOUDFLARE_ACCOUNT_ID=account-proof",
                "CLOUDFLARE_ZONE_ID=zone-proof",
                "CLOUDFLARE_API_TOKEN=SECRET-CLOUDFLARE-TOKEN",
                "DOKPLOY_API_URL=https://dokploy.example.test",
                "DOKPLOY_API_KEY=SECRET-DOKPLOY-KEY",
                "DOKPLOY_ADMIN_EMAIL=operator@example.test",
                "DOKPLOY_ADMIN_PASSWORD=SECRET-CODER-PASSWORD",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    protected = tmp_path / "repository" / ".omo" / "evidence" / "unrelated.txt"
    protected.parent.mkdir(parents=True)
    protected.write_bytes(b"unrelated")
    manifest = model_sync_artifacts.protected_manifest_bytes(
        {".omo/evidence/unrelated.txt": hashlib.sha256(b"unrelated").hexdigest()}
    )
    model_sync_artifacts.atomic_write_bytes(
        artifact_dir / "protected-artifacts-before.txt", manifest
    )
    model_sync_artifacts.atomic_write_bytes(
        artifact_dir / "protected-artifacts-before.sha256",
        f"{hashlib.sha256(manifest).hexdigest()}  protected-artifacts-before.txt\n".encode(),
    )
    return [
        "baseline-host-a",
        "--active-root",
        str(tmp_path / "repository"),
        "--wrapper",
        str(Path(__file__).parents[2] / "bin" / "dokploy-wizard-remote"),
        "--env-file",
        str(env_file),
        "--external-backup",
        str(tmp_path / "secrets" / "install.env.backup"),
        "--abort-guard",
        str(tmp_path / "abort-guard.json"),
        "--host-env",
        "FIXTURE_HOST_A",
        "--password-env",
        "FIXTURE_PASSWORD_A",
        "--host-b-env",
        "FIXTURE_HOST_B",
        "--host-b-password-env",
        "FIXTURE_PASSWORD_B",
        "--source-base-commit",
        "a" * 40,
        "--proof-commit",
        "b" * 40,
        "--artifact-dir",
        str(tmp_path / "artifacts"),
        "--output",
        str(tmp_path / "artifacts" / "result.json"),
    ]


def _active_root_fixture(tmp_path: Path, name: str) -> tuple[Path, ProofRecoveryPaths]:
    root = tmp_path / name
    wrapper = root / "bin" / "dokploy-wizard-remote"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    env_file = root / ".install-min.env"
    env_file.write_text("ROOT_DOMAIN=proof.example.test\n", encoding="utf-8")
    env_file.chmod(0o600)
    artifact_dir = root / ".omo" / "evidence" / "task-1"
    artifact_dir.mkdir(parents=True)
    return wrapper, ProofRecoveryPaths(
        env_file,
        tmp_path / f"{name}-secrets" / "install.env.backup",
        artifact_dir / "abort-guard.json",
        artifact_dir,
        artifact_dir / "result.json",
        root,
    )


def test_active_checkout_root_accepts_canonical_proof_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    wrapper, paths = _active_root_fixture(tmp_path, "active")
    monkeypatch.setattr(model_sync_proof, "_ACTIVE_REPOSITORY_ROOT", paths.repository_root)

    # When
    root = model_sync_cli._require_active_workspace_root(wrapper, paths)

    # Then
    assert root == paths.repository_root
    assert not paths.guard_path.exists()
    assert not paths.output.exists()


def test_active_checkout_root_rejects_unrelated_copy_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    _active_wrapper, active_paths = _active_root_fixture(tmp_path, "active")
    copied_wrapper, copied_paths = _active_root_fixture(tmp_path, "copied")
    monkeypatch.setattr(
        model_sync_proof,
        "_ACTIVE_REPOSITORY_ROOT",
        active_paths.repository_root,
    )

    # When / Then
    with pytest.raises(
        RuntimeError,
        match="active root does not match the running repository checkout",
    ):
        model_sync_cli._require_active_workspace_root(copied_wrapper, copied_paths)
    assert not copied_paths.guard_path.exists()
    assert not copied_paths.output.exists()


def test_active_checkout_root_rejects_symlink_alias_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    _wrapper, paths = _active_root_fixture(tmp_path, "active")
    alias = tmp_path / "active-alias"
    alias.symlink_to(paths.repository_root, target_is_directory=True)
    aliased_paths = ProofRecoveryPaths(
        alias / ".install-min.env",
        paths.backup_path,
        alias / ".omo" / "evidence" / "task-1" / "abort-guard.json",
        alias / ".omo" / "evidence" / "task-1",
        alias / ".omo" / "evidence" / "task-1" / "result.json",
        alias,
    )
    monkeypatch.setattr(model_sync_proof, "_ACTIVE_REPOSITORY_ROOT", paths.repository_root)

    # When / Then
    with pytest.raises(
        RuntimeError,
        match="active root must be an absolute canonical path without symlinks",
    ):
        model_sync_cli._require_active_workspace_root(
            alias / "bin" / "dokploy-wizard-remote",
            aliased_paths,
        )
    assert not aliased_paths.guard_path.exists()
    assert not aliased_paths.output.exists()


def test_active_checkout_root_rejects_wrapper_outside_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    _wrapper, paths = _active_root_fixture(tmp_path, "active")
    outside_wrapper = tmp_path / "dokploy-wizard-remote"
    outside_wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside_wrapper.chmod(0o755)
    monkeypatch.setattr(model_sync_proof, "_ACTIVE_REPOSITORY_ROOT", paths.repository_root)

    # When / Then
    with pytest.raises(
        RuntimeError,
        match="wrapper must be the canonical active-root remote wrapper",
    ):
        model_sync_cli._require_active_workspace_root(outside_wrapper, paths)
    assert not paths.guard_path.exists()
    assert not paths.output.exists()


def test_active_checkout_root_rejects_env_outside_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    wrapper, paths = _active_root_fixture(tmp_path, "active")
    outside_env = tmp_path / ".install-min.env"
    outside_env.write_text("ROOT_DOMAIN=proof.example.test\n", encoding="utf-8")
    outside_env.chmod(0o600)
    outside_paths = ProofRecoveryPaths(
        outside_env,
        paths.backup_path,
        paths.guard_path,
        paths.artifact_dir,
        paths.output,
        paths.repository_root,
    )
    monkeypatch.setattr(model_sync_proof, "_ACTIVE_REPOSITORY_ROOT", paths.repository_root)

    # When / Then
    with pytest.raises(
        RuntimeError,
        match="env file must be the canonical active-root .install-min.env",
    ):
        model_sync_cli._require_active_workspace_root(wrapper, outside_paths)
    assert not paths.guard_path.exists()
    assert not paths.output.exists()


def test_active_checkout_root_rejects_traversed_artifact_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    wrapper, paths = _active_root_fixture(tmp_path, "active")
    other = paths.repository_root / ".omo" / "evidence" / "other"
    other.mkdir()
    traversed = paths.artifact_dir / ".." / "other"
    traversed_paths = ProofRecoveryPaths(
        paths.env_file,
        paths.backup_path,
        traversed / "abort-guard.json",
        traversed,
        traversed / "result.json",
        paths.repository_root,
    )
    monkeypatch.setattr(model_sync_proof, "_ACTIVE_REPOSITORY_ROOT", paths.repository_root)

    # When / Then
    with pytest.raises(
        RuntimeError,
        match="artifact directory must be an absolute canonical path without symlinks",
    ):
        model_sync_cli._require_active_workspace_root(wrapper, traversed_paths)
    assert not traversed_paths.guard_path.exists()
    assert not traversed_paths.output.exists()


def test_cli_rejects_unrelated_active_root_before_guard_or_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    arguments = _baseline_arguments(tmp_path)
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-b")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")

    # When
    exit_code = main(arguments)

    # Then
    assert exit_code == 1
    assert "active root does not match the running repository checkout" in capsys.readouterr().err
    assert not (tmp_path / "abort-guard.json").exists()
    assert not (tmp_path / "artifacts" / "result.json").exists()


def _baseline_recovery_paths(arguments: list[str], tmp_path: Path) -> ProofRecoveryPaths:
    return ProofRecoveryPaths(
        Path(arguments[arguments.index("--env-file") + 1]),
        Path(arguments[arguments.index("--external-backup") + 1]),
        Path(arguments[arguments.index("--abort-guard") + 1]),
        Path(arguments[arguments.index("--artifact-dir") + 1]),
        Path(arguments[arguments.index("--output") + 1]),
        tmp_path / "repository",
    )


def _run_baseline_fixture(
    arguments: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> int:
    _install_fixture_transport(monkeypatch, snapshot=_snapshot_wire())
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", lambda *_args: None)
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda _wrapper, _paths: tmp_path / "repository",
    )
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-b")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")
    return main(arguments)


def test_explicit_single_host_baseline_uses_one_physical_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    arguments = [*_baseline_arguments(tmp_path), "--single-host-sequential"]
    clients = _install_fixture_transport(monkeypatch, snapshot=_snapshot_wire())
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", lambda *_args: None)
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda _wrapper, _paths: tmp_path / "repository",
    )
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-a")

    # When
    exit_code = main(arguments)

    # Then
    artifact_dir = tmp_path / "artifacts"
    result = json.loads((artifact_dir / "result.json").read_text(encoding="utf-8"))
    preflight = json.loads((artifact_dir / "host-a-preflight.json").read_text(encoding="utf-8"))
    lifecycle_receipt = json.loads(
        (artifact_dir / "single-host-lifecycle-baseline.json").read_text(encoding="utf-8")
    )
    assert exit_code == 0
    assert (
        sum("model-sync-preflight" in command for client in clients for command in client.commands)
        == 2
    )
    assert not (artifact_dir / "host-b-preflight.json").exists()
    assert (artifact_dir / "single-host-lifecycle-baseline.json").exists()
    assert result["host_identity_mode"] == "single_sequential"
    assert result["host_identities_distinct"] is False
    assert result["temporal_clean_epoch_evidence"] is False
    assert result["host_b_preflight_sha256"] is None
    assert lifecycle_receipt["evidence_sha256"] == result["host_a_preflight_sha256"]
    assert preflight["host_identity_mode"] == "single_sequential"
    assert preflight["provenance_role"] == "host_a"


def test_single_host_baseline_requires_exact_password_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    arguments = [*_baseline_arguments(tmp_path), "--single-host-sequential"]
    monkeypatch.setenv("FIXTURE_HOST_A", "one-vps")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "one-vps")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda _wrapper, _paths: pytest.fail("credential mismatch reached proof recovery"),
    )

    # When
    exit_code = main(arguments)

    # Then
    assert exit_code == 1
    assert not (tmp_path / "abort-guard.json").exists()


def test_post_install_snapshot_uses_authoritative_cloudflare_and_tailscale_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    env_file = Path(arguments[arguments.index("--env-file") + 1])
    observed = {
        "cloudflare": (
            model_sync_remote.ObservedResource("tunnel-proof", "proof-stack-cloudflared", "tunnel"),
            model_sync_remote.ObservedResource("dns-proof", "coder.example.test", "dns_record"),
            model_sync_remote.ObservedResource(
                "app-proof", "coder.example.test", "access_application"
            ),
        ),
        "tailscale": (
            model_sync_remote.ObservedResource("stable-node-proof", "proof-tailnet-node", "node"),
        ),
        "coder": (),
        "docker": (),
        "dokploy": (),
    }
    probe = model_sync_remote.RemoteProbe(
        machine_sha256="a" * 64,
        ssh_sha256="b" * 64,
        boot_sha256="c" * 64,
        architecture="amd64",
        namespace_clean=False,
        inventory=observed,
        plane_states={plane: "present" for plane in observed},
    )
    monkeypatch.setattr(
        model_sync_host_b, "capture_local_authoritative_inventory", lambda *_args: probe
    )
    monkeypatch.setattr(model_sync_host_b, "_image_inventory", lambda _stack_name: [])
    monkeypatch.setattr(model_sync_host_b, "_coder_login", lambda *_args: "session")
    monkeypatch.setattr(model_sync_host_b, "_coder_container_name", lambda *_args: "coder")
    monkeypatch.setattr(model_sync_host_b, "_api", lambda *_args: {"id": "user-proof"})
    monkeypatch.setattr(model_sync_host_b, "_templates", lambda *_args: [])
    monkeypatch.setattr(model_sync_host_b, "_workspaces", lambda *_args: [])
    monkeypatch.setattr(model_sync_host_b, "_builds", lambda *_args: [])
    monkeypatch.setattr(model_sync_host_b, "_secrets", lambda *_args: [])
    monkeypatch.setattr(model_sync_host_b, "_state_inventory", lambda *_args: {})
    monkeypatch.setattr(
        model_sync_host_b,
        "_legacy_renderer",
        lambda *_args: model_sync_host_b.LegacyRenderer(
            "http://proof-stack-shared-litellm:4000",
            "expected-credential",
            "openrouter/example/model",
            (),
            ("openrouter/example/model",),
        ),
    )

    snapshot = model_sync_host_b._snapshot(env_file, tmp_path)

    assert snapshot["cloudflare"] == {
        "access_application_ids": ["app-proof"],
        "dns_record_ids": ["dns-proof"],
        "tunnel_ids": ["tunnel-proof"],
    }
    assert snapshot["tailscale"] == {"identifiers": ["stable-node-proof"]}


def test_baseline_host_a_collects_complete_fixture_inventory_via_argparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    clients = _install_fixture_transport(monkeypatch, snapshot=_snapshot_wire())
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", lambda *_args: None)
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda _wrapper, _paths: tmp_path / "repository",
    )
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-b")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")

    exit_code = main(arguments)

    artifact_dir = tmp_path / "artifacts"
    result = json.loads((artifact_dir / "result.json").read_text(encoding="utf-8"))
    baseline = json.loads((artifact_dir / "baseline.json").read_text(encoding="utf-8"))
    host_a_preflight = json.loads(
        (artifact_dir / "host-a-preflight.json").read_text(encoding="utf-8")
    )
    host_b_preflight = json.loads(
        (artifact_dir / "host-b-preflight.json").read_text(encoding="utf-8")
    )
    manifest = (artifact_dir / "protected-artifacts-before.txt").read_text(encoding="utf-8")
    guard = read_abort_guard(tmp_path / "abort-guard.json")
    assert exit_code == 0
    assert [template["name"] for template in baseline["templates"]] == [
        "ubuntu-vscode",
        "ubuntu-vscode-hermes",
        "ubuntu-vscode-kdense-byok",
        "ubuntu-vscode-opencode-web",
        "ubuntu-vscode-openwork",
        "ubuntu-vscode-pi-web",
    ]
    assert result["coder_image_digest"] == "ghcr.io/coder/coder@sha256:" + _sha("1")
    assert result["coder_secret_inventory_sha256"] != "0" * 64
    assert result["legacy_workspace_managed_fingerprints_sha256"] != "0" * 64
    assert result["host_identity_mode"] == "distinct"
    assert result["host_identities_distinct"] is True
    assert result["temporal_clean_epoch_evidence"] is False
    assert result["single_host_lifecycle_sha256"] is None
    post_install_cloudflare = baseline["post_install_cloudflare"]
    assert isinstance(post_install_cloudflare, list)
    assert (
        result["post_install_cloudflare_sha256"]
        == hashlib.sha256(canonical_json_bytes(post_install_cloudflare)).hexdigest()
    )
    assert all(
        set(record) == {"fingerprint_sha256", "id", "kind", "match", "provenance"}
        for record in post_install_cloudflare
    )
    assert {record["provenance"] for record in post_install_cloudflare} <= {
        "preexisting_unowned",
        "created_candidate",
        "foreign",
    }
    assert host_a_preflight["host_identity_mode"] == "distinct"
    assert host_a_preflight["provenance_role"] == "host_a"
    assert host_b_preflight["host_identity_mode"] == "distinct"
    assert host_b_preflight["provenance_role"] == "host_b"
    assert guard.attestation is not None
    assert (
        result["abort_guard_sha256"]
        == hashlib.sha256(canonical_json_bytes(guard.attestation.to_payload())).hexdigest()
    )
    assert ".omo/evidence/unrelated.txt" in manifest
    assert "abort-guard.json" not in manifest
    assert "baseline.json" not in manifest
    assert "password-a" not in (artifact_dir / "baseline.json").read_text(encoding="utf-8")
    sentinels = (
        "SECRET-CLOUDFLARE-TOKEN",
        "SECRET-DOKPLOY-KEY",
        "SECRET-CODER-PASSWORD",
    )
    assert all(
        secret not in command
        for client in clients
        for command in client.commands
        for secret in sentinels
    )
    assert all(
        secret not in path.read_text(encoding="utf-8")
        for path in artifact_dir.iterdir()
        for secret in sentinels
    )
    assert all(
        secret in b"".join(stdin.payload for client in clients for stdin in client.stdins).decode()
        for secret in sentinels
    )


def test_baseline_host_a_adopts_only_receipted_preexisting_protected_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    manifest_path = artifact_dir / "protected-artifacts-before.txt"
    manifest_bytes = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    _install_fixture_transport(monkeypatch, snapshot=_snapshot_wire())
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", lambda *_args: None)
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda _wrapper, _paths: tmp_path / "repository",
    )
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-b")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")

    exit_code = main(arguments)

    result = json.loads((artifact_dir / "result.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert manifest_path.read_bytes() == manifest_bytes
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    assert result["protected_artifacts_before_sha256"] == manifest_hash


def test_baseline_host_a_rejects_unknown_preexisting_protected_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    manifest_path = artifact_dir / "protected-artifacts-before.txt"
    receipt_path = artifact_dir / "protected-artifacts-before.sha256"
    unknown = b"unknown protected bytes\n"
    manifest_path.write_bytes(unknown)
    os.chmod(manifest_path, 0o600)
    receipt_path.unlink()
    _install_fixture_transport(monkeypatch, snapshot=_snapshot_wire())
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", lambda *_args: None)
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda _wrapper, _paths: tmp_path / "repository",
    )
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-b")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")

    exit_code = main(arguments)

    assert exit_code == 1
    assert manifest_path.read_bytes() == unknown
    assert not (artifact_dir / "result.json").exists()
    assert not (artifact_dir / "baseline.json").exists()


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "missing",
        "digest",
        "symlink",
        "intermediate-symlink",
        "directory",
        "fifo",
        "oversized",
        "entry-count",
    ],
)
def test_baseline_host_a_rejects_unverified_protected_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    protected = tmp_path / "repository" / ".omo" / "evidence" / "unrelated.txt"
    match invalid_kind:
        case "missing":
            protected.unlink()
        case "digest":
            protected.write_bytes(b"changed")
        case "symlink":
            protected.unlink()
            protected.symlink_to(tmp_path / "outside.txt")
        case "intermediate-symlink":
            protected.unlink()
            protected.parent.rmdir()
            outside = tmp_path / "outside"
            outside.mkdir()
            (outside / "unrelated.txt").write_bytes(b"unrelated")
            protected.parent.symlink_to(outside, target_is_directory=True)
        case "directory":
            protected.unlink()
            protected.mkdir()
        case "fifo":
            protected.unlink()
            os.mkfifo(protected)
        case "oversized":
            with protected.open("wb") as stream:
                stream.truncate(16 * 1024 * 1024 + 1)
        case "entry-count":
            entries = {
                f".omo/evidence/file-{index:04d}.txt": hashlib.sha256(b"unrelated").hexdigest()
                for index in range(1_025)
            }
            manifest = model_sync_artifacts.protected_manifest_bytes(entries)
            artifact_dir = tmp_path / "artifacts"
            model_sync_artifacts.atomic_write_bytes(
                artifact_dir / "protected-artifacts-before.txt", manifest
            )
            model_sync_artifacts.atomic_write_bytes(
                artifact_dir / "protected-artifacts-before.sha256",
                f"{hashlib.sha256(manifest).hexdigest()}  protected-artifacts-before.txt\n".encode(),
            )
        case unexpected:
            raise AssertionError(f"unexpected fixture kind {unexpected}")

    exit_code = _run_baseline_fixture(arguments, tmp_path, monkeypatch)

    assert exit_code == 1
    assert not (tmp_path / "artifacts" / "result.json").exists()


@pytest.mark.parametrize("mutation_kind", ["rewrite", "replace"])
def test_baseline_host_a_rejects_protected_artifact_mutation_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_kind: str,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    protected = tmp_path / "repository" / ".omo" / "evidence" / "unrelated.txt"
    identity = (protected.stat().st_dev, protected.stat().st_ino)
    original_read = os.read
    original_fstat = os.fstat
    mutated = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, size)
        metadata = original_fstat(descriptor)
        if chunk and not mutated and (metadata.st_dev, metadata.st_ino) == identity:
            mutated = True
            if mutation_kind == "rewrite":
                protected.write_bytes(b"changed")
            else:
                replacement = tmp_path / "replacement.txt"
                replacement.write_bytes(b"unrelated")
                os.replace(replacement, protected)
        return chunk

    monkeypatch.setattr(os, "read", mutate_after_read)

    exit_code = _run_baseline_fixture(arguments, tmp_path, monkeypatch)

    assert exit_code == 1
    assert mutated is True
    assert not (tmp_path / "artifacts" / "result.json").exists()


@pytest.mark.parametrize("replacement_kind", ["same", "different", "symlink"])
def test_baseline_host_a_rejects_intermediate_directory_replacement_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    evidence = tmp_path / "repository" / ".omo" / "evidence"
    protected = evidence / "unrelated.txt"
    identity = (protected.stat().st_dev, protected.stat().st_ino)
    original_read = os.read
    original_fstat = os.fstat
    replaced = False

    def replace_directory_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        metadata = original_fstat(descriptor)
        if chunk and not replaced and (metadata.st_dev, metadata.st_ino) == identity:
            replaced = True
            moved = evidence.with_name("evidence-original")
            evidence.rename(moved)
            if replacement_kind == "symlink":
                evidence.symlink_to(moved, target_is_directory=True)
            else:
                evidence.mkdir()
                content = b"unrelated" if replacement_kind == "same" else b"changed"
                (evidence / "unrelated.txt").write_bytes(content)
        return chunk

    monkeypatch.setattr(os, "read", replace_directory_after_read)

    exit_code = _run_baseline_fixture(arguments, tmp_path, monkeypatch)

    assert exit_code == 1
    assert replaced is True
    assert not (tmp_path / "artifacts" / "result.json").exists()


@pytest.mark.parametrize(
    "name", ["protected-artifacts-before.txt", "protected-artifacts-before.sha256"]
)
def test_baseline_host_a_rejects_symlinked_protected_manifest_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    protected_path = artifact_dir / name
    target = artifact_dir / f"{name}.target"
    protected_bytes = protected_path.read_bytes()
    protected_path.unlink()
    target.write_bytes(protected_bytes)
    os.chmod(target, 0o600)
    protected_path.symlink_to(target.name)
    _install_fixture_transport(monkeypatch, snapshot=_snapshot_wire())
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", lambda *_args: None)
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda _wrapper, _paths: tmp_path / "repository",
    )
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-b")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")

    exit_code = main(arguments)

    assert exit_code == 1
    assert target.read_bytes() == protected_bytes
    assert not (artifact_dir / "result.json").exists()


def test_protected_contract_reads_tolerate_arbitrary_short_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    paths = _baseline_recovery_paths(arguments, tmp_path)
    manifest_path = paths.artifact_dir / "protected-artifacts-before.txt"
    expected = manifest_path.read_bytes()
    original_path_read = Path.read_bytes
    original_read = os.read

    def truncated_path_read(path: Path) -> bytes:
        content = original_path_read(path)
        if path.name.startswith("protected-artifacts-before"):
            return content[:3]
        return content

    def short_read(descriptor: int, size: int) -> bytes:
        return original_read(descriptor, min(size, 3))

    monkeypatch.setattr(Path, "read_bytes", truncated_path_read)
    monkeypatch.setattr(os, "read", short_read)

    assert protected_bytes(paths) == expected


@pytest.mark.parametrize(
    "name", ["protected-artifacts-before.txt", "protected-artifacts-before.sha256"]
)
@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "mode"])
def test_protected_contract_rejects_unauthorized_file_kinds_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    kind: str,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    paths = _baseline_recovery_paths(arguments, tmp_path)
    target = paths.artifact_dir / name
    content = target.read_bytes()
    match kind:
        case "symlink":
            target.unlink()
            sibling = target.with_suffix(target.suffix + ".target")
            sibling.write_bytes(content)
            sibling.chmod(0o600)
            target.symlink_to(sibling.name)
        case "directory":
            target.unlink()
            target.mkdir()
        case "fifo":
            target.unlink()
            os.mkfifo(target, mode=0o600)
        case "mode":
            target.chmod(0o640)
        case unexpected:
            raise AssertionError(f"unexpected protected contract kind {unexpected}")

    original_path_read = Path.read_bytes

    def reject_unsafe_path_read(path: Path) -> bytes:
        if path == target:
            raise AssertionError("unauthorized contract reached Path.read_bytes")
        return original_path_read(path)

    monkeypatch.setattr(Path, "read_bytes", reject_unsafe_path_read)

    with pytest.raises(AbortGuardError):
        protected_bytes(paths)


@pytest.mark.parametrize(
    "name", ["protected-artifacts-before.txt", "protected-artifacts-before.sha256"]
)
def test_protected_contract_rejects_oversize_before_content_or_repository_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    paths = _baseline_recovery_paths(arguments, tmp_path)
    target = paths.artifact_dir / name
    if name.endswith(".txt"):
        target.write_bytes(b"a" * (256 * 1024 + 1))
    else:
        target.write_bytes(b"a" * 257)
    target.chmod(0o600)
    original_path_read = Path.read_bytes

    def reject_oversize_path_read(path: Path) -> bytes:
        if path == target:
            raise AssertionError("oversize contract reached Path.read_bytes")
        return original_path_read(path)

    monkeypatch.setattr(Path, "read_bytes", reject_oversize_path_read)

    with pytest.raises(AbortGuardError):
        protected_bytes(paths)


@pytest.mark.parametrize(
    "name", ["protected-artifacts-before.txt", "protected-artifacts-before.sha256"]
)
def test_protected_contract_rejects_trailing_bytes(tmp_path: Path, name: str) -> None:
    arguments = _baseline_arguments(tmp_path)
    paths = _baseline_recovery_paths(arguments, tmp_path)
    target = paths.artifact_dir / name
    with target.open("ab") as stream:
        stream.write(b"\n")

    with pytest.raises(AbortGuardError):
        protected_bytes(paths)


@pytest.mark.parametrize(
    "name", ["protected-artifacts-before.txt", "protected-artifacts-before.sha256"]
)
@pytest.mark.parametrize("mutation", ["growth", "replacement", "metadata"])
def test_protected_contract_rejects_post_read_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    mutation: str,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    paths = _baseline_recovery_paths(arguments, tmp_path)
    target = paths.artifact_dir / name
    identity = (target.stat().st_dev, target.stat().st_ino)
    original_read = os.read
    original_fstat = os.fstat
    mutated = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, size)
        metadata = original_fstat(descriptor)
        if chunk and not mutated and (metadata.st_dev, metadata.st_ino) == identity:
            mutated = True
            match mutation:
                case "growth":
                    with target.open("ab") as stream:
                        stream.write(b"x")
                case "replacement":
                    replacement = target.with_suffix(target.suffix + ".replacement")
                    replacement.write_bytes(target.read_bytes())
                    replacement.chmod(0o600)
                    os.replace(replacement, target)
                case "metadata":
                    current = target.stat()
                    os.utime(target, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
                case unexpected:
                    raise AssertionError(f"unexpected protected mutation {unexpected}")
        return chunk

    monkeypatch.setattr(os, "read", mutate_after_read)

    with pytest.raises(AbortGuardError):
        protected_bytes(paths)
    assert mutated is True


def test_open_directory_closes_descriptor_when_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_open = os.open
    original_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def tracked_open(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        descriptor = original_open(path, flags, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    def failing_fstat(descriptor: int) -> os.stat_result:
        if descriptor in opened:
            raise OSError("injected fstat failure")
        return os.fstat(descriptor)

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fstat", failing_fstat)
    monkeypatch.setattr(os, "close", tracked_close)
    try:
        with pytest.raises(OSError, match="injected fstat failure"):
            open_protected_directory(".", parent)
        assert opened == closed
    finally:
        for descriptor in set(opened) - set(closed):
            original_close(descriptor)
        original_close(parent)


def test_protected_file_growth_aborts_at_streaming_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    protected = root / ".omo" / "evidence" / "unrelated.txt"
    protected.parent.mkdir(parents=True)
    protected.write_bytes(b"x")
    entries = model_sync_artifacts.validate_protected_manifest_bytes(
        model_sync_artifacts.protected_manifest_bytes(
            {".omo/evidence/unrelated.txt": hashlib.sha256(b"x").hexdigest()}
        )
    )
    identity = (protected.stat().st_dev, protected.stat().st_ino)
    original_read = os.read
    original_fstat = os.fstat
    reads = 0

    def growing_read(descriptor: int, size: int) -> bytes:
        nonlocal reads
        metadata = original_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == identity:
            reads += 1
            if reads > 300:
                raise AssertionError("protected growth exceeded its streaming bound")
            return b"x" * size
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", growing_read)

    with pytest.raises(AbortGuardError):
        verify_protected_artifacts(root, entries)
    assert reads <= 257


def test_protected_file_rejects_premature_end_of_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    protected = root / ".omo" / "evidence" / "unrelated.txt"
    protected.parent.mkdir(parents=True)
    protected.write_bytes(b"xy")
    entries = model_sync_artifacts.validate_protected_manifest_bytes(
        model_sync_artifacts.protected_manifest_bytes(
            {".omo/evidence/unrelated.txt": hashlib.sha256(b"x").hexdigest()}
        )
    )
    identity = (protected.stat().st_dev, protected.stat().st_ino)
    original_read = os.read
    original_fstat = os.fstat
    read_once = False

    def premature_eof(descriptor: int, size: int) -> bytes:
        nonlocal read_once
        metadata = original_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != identity:
            return original_read(descriptor, size)
        if read_once:
            return b""
        read_once = True
        return original_read(descriptor, 1)

    monkeypatch.setattr(os, "read", premature_eof)

    with pytest.raises(AbortGuardError):
        verify_protected_artifacts(root, entries)


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_protected_aggregate_bound_accepts_exact_limit_and_rejects_overflow(
    tmp_path: Path,
    extra_bytes: int,
) -> None:
    root = tmp_path / "repository"
    evidence = root / ".omo" / "evidence"
    evidence.mkdir(parents=True)
    chunk_size = 16 * 1024 * 1024
    chunk_hash = hashlib.sha256(bytes(chunk_size)).hexdigest()
    entries = {}
    for index in range(4):
        path = evidence / f"chunk-{index}.bin"
        with path.open("wb") as stream:
            stream.truncate(chunk_size)
        entries[f".omo/evidence/{path.name}"] = chunk_hash
    if extra_bytes:
        (evidence / "overflow.bin").write_bytes(b"x")
        entries[".omo/evidence/overflow.bin"] = hashlib.sha256(b"x").hexdigest()
    manifest = model_sync_artifacts.protected_manifest_bytes(entries)
    parsed = model_sync_artifacts.validate_protected_manifest_bytes(manifest)

    if extra_bytes:
        with pytest.raises(AbortGuardError):
            verify_protected_artifacts(root, parsed)
    else:
        verify_protected_artifacts(root, parsed)


def test_terminal_recovery_tolerates_short_reads_of_generated_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    assert _run_baseline_fixture(arguments, tmp_path, monkeypatch) == 0
    paths = _baseline_recovery_paths(arguments, tmp_path)
    original_read = os.read

    def short_read(descriptor: int, size: int) -> bytes:
        return original_read(descriptor, min(size, 3))

    monkeypatch.setattr(os, "read", short_read)

    recovery = begin_proof_recovery(
        paths=paths,
        pid=os.getpid(),
        start_time_ticks=process_start_time_ticks(
            Path("/proc/self/stat").read_text(encoding="utf-8")
        ),
    )
    assert recovery.terminal is True


@pytest.mark.parametrize("name", ["baseline.json", "result.json"])
@pytest.mark.parametrize("mutation", ["growth", "replacement", "metadata"])
def test_terminal_recovery_rejects_generated_output_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    mutation: str,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    assert _run_baseline_fixture(arguments, tmp_path, monkeypatch) == 0
    paths = _baseline_recovery_paths(arguments, tmp_path)
    target = paths.artifact_dir / name
    identity = (target.stat().st_dev, target.stat().st_ino)
    original_read = os.read
    original_fstat = os.fstat
    mutated = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, size)
        metadata = original_fstat(descriptor)
        if chunk and not mutated and (metadata.st_dev, metadata.st_ino) == identity:
            mutated = True
            match mutation:
                case "growth":
                    with target.open("ab") as stream:
                        stream.write(b"x")
                case "replacement":
                    replacement = target.with_suffix(".replacement")
                    replacement.write_bytes(target.read_bytes())
                    replacement.chmod(0o600)
                    os.replace(replacement, target)
                case "metadata":
                    current = target.stat()
                    os.utime(target, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
                case unexpected:
                    raise AssertionError(f"unexpected output mutation {unexpected}")
        return chunk

    monkeypatch.setattr(os, "read", mutate_after_read)

    with pytest.raises(AbortGuardError):
        begin_proof_recovery(
            paths=paths,
            pid=os.getpid(),
            start_time_ticks=process_start_time_ticks(
                Path("/proc/self/stat").read_text(encoding="utf-8")
            ),
        )
    assert mutated is True


@pytest.mark.parametrize("name", ["baseline.json", "result.json"])
def test_terminal_recovery_bounds_oversize_generated_output_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    assert _run_baseline_fixture(arguments, tmp_path, monkeypatch) == 0
    paths = _baseline_recovery_paths(arguments, tmp_path)
    target = paths.artifact_dir / name
    with target.open("ab") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    identity = (target.stat().st_dev, target.stat().st_ino)
    original_read = os.read
    original_fstat = os.fstat
    guard = read_abort_guard(paths.guard_path)
    assert guard.attestation is not None
    result_limit = len(model_sync_results.result_bytes_from_attestation(guard.attestation)) + 1

    def bounded_read(descriptor: int, size: int) -> bytes:
        metadata = original_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == identity:
            limit = result_limit if name == "result.json" else 65_536
            if size > limit:
                raise AssertionError("generated output requested an unbounded read")
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", bounded_read)

    with pytest.raises(AbortGuardError):
        begin_proof_recovery(
            paths=paths,
            pid=os.getpid(),
            start_time_ticks=process_start_time_ticks(
                Path("/proc/self/stat").read_text(encoding="utf-8")
            ),
        )


@pytest.mark.parametrize("unknown_name", [None, "host-a-preflight.json", "result.json"])
def test_rollback_removes_only_exact_attested_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unknown_name: str | None,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    assert _run_baseline_fixture(arguments, tmp_path, monkeypatch) == 0
    paths = _baseline_recovery_paths(arguments, tmp_path)
    guard = read_abort_guard(paths.guard_path)
    assert guard.attestation is not None
    outputs = {
        **output_paths(paths),
        "result.json": paths.output,
    }
    if unknown_name is not None:
        outputs[unknown_name].write_bytes(b"unknown")
        outputs[unknown_name].chmod(0o600)

    if unknown_name is None:
        model_sync_results.remove_authorized_outputs(paths, guard.attestation)
        assert not any(path.exists() for path in outputs.values())
    else:
        with pytest.raises(AbortGuardError):
            model_sync_results.remove_authorized_outputs(paths, guard.attestation)
        assert outputs[unknown_name].read_bytes() == b"unknown"


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (_snapshot_wire(omit_template=True), "missing template"),
        (_snapshot_wire(malformed_build=True), "malformed build status"),
        (json.dumps({"schema_version": 1}), "partial snapshot"),
        ("{", "malformed JSON"),
    ],
)
def test_baseline_host_a_rejects_incomplete_fixture_without_finalized_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: str,
    reason: str,
) -> None:
    del reason
    arguments = _baseline_arguments(tmp_path)
    _install_fixture_transport(monkeypatch, snapshot=snapshot)
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", lambda *_args: None)
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda _wrapper, _paths: tmp_path / "repository",
    )
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-b")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")

    exit_code = main(arguments)

    artifact_dir = tmp_path / "artifacts"
    assert exit_code == 1
    assert not (artifact_dir / "result.json").exists()
    assert not (artifact_dir / "baseline.json").exists()
    assert (artifact_dir / "protected-artifacts-before.txt").exists()
    assert (artifact_dir / "protected-artifacts-before.sha256").exists()


def test_exact_output_writer_never_removes_prior_authorized_output_when_later_write_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dokploy_wizard.proof import model_sync_artifacts

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    original = model_sync_artifacts.atomic_write_bytes

    def write(path: Path, content: bytes) -> None:
        if content == b"two":
            raise SystemExit(1)
        original(path, content)

    monkeypatch.setattr(model_sync_artifacts, "atomic_write_bytes", write)
    model_sync_artifacts.write_or_verify_exact_bytes(first, b"one")

    with pytest.raises(SystemExit):
        model_sync_artifacts.write_or_verify_exact_bytes(second, b"two")

    assert first.read_bytes() == b"one"
    assert not second.exists()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize(
    "boundary",
    [
        "before-backup",
        "after-backup",
        "after-replace",
        "before-handler",
        "after-handler",
        "after-finalize",
        "before-restore",
        "between-restore",
    ],
)
def test_baseline_host_a_recovers_exact_env_for_each_signal_boundary(
    tmp_path: Path,
    boundary: str,
    signum: signal.Signals,
) -> None:
    env_file = tmp_path / "install.env"
    original = (
        b"ROOT_DOMAIN=proof.example.test\nPACKS=coder\nAI_DEFAULT_PROVIDER=openrouter\n"
        b"AI_DEFAULT_MODEL=example/model\nLITELLM_NVIDIA_API_KEY=SECRET-SIGNAL-SENTINEL\n"
    )
    env_file.write_bytes(original)
    env_file.chmod(0o600)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    child = r"""
from __future__ import annotations
import argparse
import hashlib
import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from dokploy_wizard.proof import model_sync_artifacts, model_sync_cli, model_sync_env

boundary, env_name, backup_name, guard_name = sys.argv[1:]
env_file = Path(env_name)
backup = Path(backup_name)
guard = Path(guard_name)
repository = env_file.parent / "repository"
protected = repository / ".omo" / "evidence" / "unrelated.txt"
protected.parent.mkdir(parents=True)
protected.write_bytes(b"unrelated")
manifest = model_sync_artifacts.protected_manifest_bytes(
    {".omo/evidence/unrelated.txt": hashlib.sha256(b"unrelated").hexdigest()}
)
model_sync_artifacts.atomic_write_bytes(env_file.parent / "protected-artifacts-before.txt", manifest)
model_sync_artifacts.atomic_write_bytes(
    env_file.parent / "protected-artifacts-before.sha256",
    f"{hashlib.sha256(manifest).hexdigest()}  protected-artifacts-before.txt\n".encode(),
)
original_write = model_sync_env.artifacts.atomic_write_bytes
paused = False

def pause() -> None:
    global paused
    paused = True
    print("READY", flush=True)
    sys.stdin.buffer.read(1)

def write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    if boundary == "before-backup" and path == backup and not paused:
        pause()
    original_write(path, content, mode=mode)
    if boundary == "after-backup" and path == backup and not paused:
        pause()
    if boundary == "after-replace" and path == env_file and not paused:
        pause()

model_sync_env.artifacts.atomic_write_bytes = write
model_sync_cli.resolve_host_inputs = lambda *_args: ("host-a", "password-a", "host-b", "password-b")
model_sync_cli._require_active_workspace_root = lambda _wrapper, _paths: repository
model_sync_cli.resolve_proof_namespace = lambda _env: SimpleNamespace(stack_name="proof-stack", cloudflare=())
model_sync_cli.resolve_proof_transport = lambda _env: None
synthetic_probe = SimpleNamespace(
    machine_sha256="a" * 64, ssh_sha256="b" * 64, boot_sha256="c" * 64,
    architecture="amd64", namespace_clean=True,
    inventory={"cloudflare": ()}, preexisting_cloudflare_sha256="8" * 64,
    verify_preexisting_cloudflare_unchanged=lambda _post: None,
    to_dict=lambda: {},
)
model_sync_cli.probe_baseline_hosts = lambda **_kwargs: (synthetic_probe, synthetic_probe)
model_sync_cli.probe_host = lambda **_kwargs: synthetic_probe
model_sync_cli._run_wrapper = lambda *_args: None
model_sync_cli.capture_host_a_snapshot = lambda **_kwargs: None
model_sync_cli.parse_captured_baseline = lambda *_args, **_kwargs: SimpleNamespace(
    payload={},
    images={"coder": "coder@sha256:" + "1" * 64, "litellm": "litellm@sha256:" + "2" * 64,
            "pgvector": "pgvector@sha256:" + "3" * 64, "redis": "redis@sha256:" + "4" * 64,
            "postfix": "postfix@sha256:" + "5" * 64},
    coder_secret_inventory_sha256="6" * 64,
    legacy_workspace_managed_fingerprints_sha256="7" * 64,
)
if boundary in ("after-finalize", "before-restore", "between-restore"):
    def previous_handler(signum, _frame):
        print(f"PREVIOUS:{signum}", flush=True)
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGINT, previous_handler)
    signal.signal(signal.SIGTERM, previous_handler)
    original_recover = model_sync_cli.recover_interrupted_proof
    def recover(value):
        print("RECOVERY", flush=True)
        original_recover(value)
    model_sync_cli.recover_interrupted_proof = recover
original_install = model_sync_cli._install_recovery_handlers
original_finalize = model_sync_cli.finalize_baseline_artifacts
original_restore = model_sync_cli._restore_recovery_handlers
if boundary == "after-finalize":
    def finalize(inputs):
        original_finalize(inputs)
        pause()
    model_sync_cli.finalize_baseline_artifacts = finalize
if boundary == "before-handler":
    def install(recovery, *_args):
        pause()
        return original_install(recovery)
    model_sync_cli._install_recovery_handlers = install
if boundary == "after-handler":
    def install(recovery, *_args):
        previous = original_install(recovery)
        pause()
        return previous
    model_sync_cli._install_recovery_handlers = install
if boundary == "before-restore":
    def restore(previous):
        pause()
        original_restore(previous)
    model_sync_cli._restore_recovery_handlers = restore
if boundary == "between-restore":
    def restore(previous):
        signal.signal(signal.SIGINT, previous[0])
        pause()
        signal.signal(signal.SIGTERM, previous[1])
    model_sync_cli._restore_recovery_handlers = restore
args = argparse.Namespace(
    active_root=repository, wrapper=Path("wrapper"), env_file=env_file,
    external_backup=backup, abort_guard=guard,
    host_env="unused", password_env="unused", host_b_env="unused", host_b_password_env="unused",
    single_host_sequential=False,
    source_base_commit="a" * 40, proof_commit="b" * 40, artifact_dir=env_file.parent,
    output=env_file.parent / "result.json",
)
model_sync_cli._baseline_host_a(args)
"""
    process = subprocess.Popen(
        [
            os.environ.get("PYTHON", "python"),
            "-c",
            child,
            boundary,
            str(env_file),
            str(backup),
            str(guard),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert process.stdout is not None
    assert process.stdin is not None
    assert process.stderr is not None
    assert process.stdout.readline().strip() == "READY", process.stderr.read()

    process.send_signal(signum)
    stdout, stderr = process.communicate()

    if boundary in ("before-restore", "between-restore"):
        assert process.returncode == 128 + signum
        proof = env_file.read_bytes()
        task_outputs = {
            "abort-guard.json": guard,
            "baseline.json": tmp_path / "baseline.json",
            "host-a-preflight.json": tmp_path / "host-a-preflight.json",
            "host-b-preflight.json": tmp_path / "host-b-preflight.json",
            "protected-artifacts-before.txt": tmp_path / "protected-artifacts-before.txt",
            "result.json": tmp_path / "result.json",
        }
        assert all(path.exists() for path in task_outputs.values())
        assert proof != original
        assert backup.read_bytes() == original
        status = read_abort_guard(guard)
        assert status.state == "armed"
        assert status.claimant_kind == "plan"
        assert status.phase == "complete"
        assert status.attestation is not None

        actual_hashes = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in task_outputs.items()
        }
        result = model_sync_artifacts.require_mapping(
            json.loads(task_outputs["result.json"].read_text(encoding="utf-8")),
            "result",
        )
        assert (
            result["abort_guard_sha256"]
            == hashlib.sha256(canonical_json_bytes(status.attestation.to_payload())).hexdigest()
        )
        assert result["baseline_sha256"] == actual_hashes["baseline.json"]
        assert result["host_a_preflight_sha256"] == actual_hashes["host-a-preflight.json"]
        assert result["host_b_preflight_sha256"] == actual_hashes["host-b-preflight.json"]
        assert (
            result["protected_artifacts_before_sha256"]
            == actual_hashes["protected-artifacts-before.txt"]
        )
        assert result["env_original_sha256"] == hashlib.sha256(original).hexdigest()
        assert result["env_proof_sha256"] == hashlib.sha256(proof).hexdigest()

        manifest_hashes: dict[str, str] = {}
        for line in (
            task_outputs["protected-artifacts-before.txt"].read_text(encoding="utf-8").splitlines()
        ):
            fingerprint, name = line.split("  ", 1)
            manifest_hashes[name] = fingerprint
        assert manifest_hashes == {
            ".omo/evidence/unrelated.txt": hashlib.sha256(b"unrelated").hexdigest(),
        }

        abort_status_output = tmp_path / "status" / "abort-status.json"
        assert (
            main(
                [
                    "abort-status",
                    "--guard",
                    str(guard),
                    "--output",
                    str(abort_status_output),
                ]
            )
            == 0
        )
        abort_status = model_sync_artifacts.require_mapping(
            json.loads(abort_status_output.read_text(encoding="utf-8")),
            "abort status",
        )
        assert abort_status == {
            "claimant_kind": "plan",
            "host_identities_distinct": True,
            "host_identity_mode": "distinct",
            "phase": "complete",
            "pid": None,
            "start_time_ticks": None,
            "state": "armed",
            "temporal_clean_epoch_evidence": False,
        }
        mode_paths = (*task_outputs.values(), env_file, backup, abort_status_output)
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in mode_paths)
        assert not list(tmp_path.rglob("*.tmp"))
        if boundary == "between-restore" and signum == signal.SIGINT:
            assert "RECOVERY" not in stdout + stderr
            assert f"PREVIOUS:{signum}" in stdout
        else:
            assert "RECOVERY" in stdout
            assert "PREVIOUS:" not in stdout
    elif boundary == "before-handler":
        assert process.returncode == -signum
        recovery = begin_proof_recovery(
            paths=ProofRecoveryPaths(
                env_file,
                backup,
                guard,
                tmp_path,
                tmp_path / "result.json",
                tmp_path / "repository",
            ),
            pid=os.getpid(),
            start_time_ticks=process_start_time_ticks(
                Path("/proc/self/stat").read_text(encoding="utf-8")
            ),
        )
        recover_interrupted_proof(recovery)
    elif boundary == "after-finalize":
        assert process.returncode == 128 + signum
        result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        manifest = (tmp_path / "protected-artifacts-before.txt").read_text(encoding="utf-8")
        status = read_abort_guard(guard)
        assert env_file.read_bytes() != original
        assert backup.read_bytes() == original
        assert status.state == "armed"
        assert status.claimant_kind == "plan"
        assert status.phase == "complete"
        assert status.attestation is not None
        assert (
            result["abort_guard_sha256"]
            == hashlib.sha256(canonical_json_bytes(status.attestation.to_payload())).hexdigest()
        )
        assert ".omo/evidence/unrelated.txt" in manifest
        assert "RECOVERY" in stdout
    else:
        assert process.returncode == 128 + signum
    if boundary not in ("after-finalize", "before-restore", "between-restore"):
        assert env_file.read_bytes() == original
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert not backup.exists()
        assert read_abort_guard(guard).claimant_kind == "plan"
        assert not (tmp_path / "result.json").exists()
    assert b"SECRET-SIGNAL-SENTINEL" not in (stdout + stderr).encode()


@pytest.mark.parametrize(
    "signals", [(signal.SIGINT, signal.SIGTERM), (signal.SIGTERM, signal.SIGINT)]
)
def test_nested_signal_does_not_interrupt_active_recovery(
    tmp_path: Path, signals: tuple[signal.Signals, signal.Signals]
) -> None:
    env_file = tmp_path / "install.env"
    original = b"ROOT_DOMAIN=proof.example.test\nPACKS=coder\nAI_DEFAULT_PROVIDER=openrouter\nAI_DEFAULT_MODEL=example/model\nLITELLM_NVIDIA_API_KEY=SECRET-NESTED\n"
    env_file.write_bytes(original)
    env_file.chmod(0o600)
    backup = tmp_path / "backup.env"
    guard = tmp_path / "abort-guard.json"
    child = r"""
import os, signal, sys
from pathlib import Path
from dokploy_wizard.proof import ProofRecoveryPaths, model_sync_artifacts, model_sync_cli
from dokploy_wizard.proof.model_sync_env import prepare_proof_env
from dokploy_wizard.proof.model_sync_host_a import begin_proof_recovery, recover_interrupted_proof

env, backup, guard = map(Path, sys.argv[1:])
repository = env.parent / "repository"
protected = repository / ".omo" / "evidence" / "unrelated.txt"
protected.parent.mkdir(parents=True)
protected.write_bytes(b"unrelated")
manifest = model_sync_artifacts.protected_manifest_bytes({".omo/evidence/unrelated.txt": __import__("hashlib").sha256(b"unrelated").hexdigest()})
model_sync_artifacts.atomic_write_bytes(env.parent / "protected-artifacts-before.txt", manifest)
model_sync_artifacts.atomic_write_bytes(env.parent / "protected-artifacts-before.sha256", f"{__import__('hashlib').sha256(manifest).hexdigest()}  protected-artifacts-before.txt\n".encode())
recovery = begin_proof_recovery(paths=ProofRecoveryPaths(env, backup, guard, env.parent, env.parent / "result.json", repository), pid=os.getpid(), start_time_ticks=model_sync_cli._self_start_time_ticks())
prepare_proof_env(env_file=env, backup_path=backup, guard_path=guard, claim_token=recovery.claim.token)
original = model_sync_cli.recover_interrupted_proof
def paused(value):
    print("READY", flush=True)
    sys.stdin.buffer.read(1)
    original(value)
model_sync_cli.recover_interrupted_proof = paused
model_sync_cli._install_recovery_handlers(recovery)
print("ARMED", flush=True)
signal.pause()
"""
    process = subprocess.Popen(
        [os.environ.get("PYTHON", "python"), "-c", child, str(env_file), str(backup), str(guard)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert process.stdout is not None and process.stdin is not None
    assert process.stdout.readline().strip() == "ARMED"
    process.send_signal(signals[0])
    assert process.stdout.readline().strip() == "READY"
    process.send_signal(signals[1])
    process.stdin.write("x")
    process.stdin.flush()
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 128 + signals[0]
    assert env_file.read_bytes() == original
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert not backup.exists()
    assert read_abort_guard(guard).claimant_kind == "plan"
    assert not list(tmp_path.glob(".*.tmp"))
    assert b"SECRET-NESTED" not in (stdout + stderr).encode()


@pytest.mark.parametrize(
    ("boundary", "terminal"),
    [
        ("finalize-intent", "ready"),
        ("baseline-published", "ready"),
        ("host-a-preflight-published", "ready"),
        ("host-b-preflight-published", "complete"),
        ("result-published", "complete"),
        ("complete-guard", "complete"),
        ("rollback-output-unlinked", "ready"),
        ("rollback-env-restored", "ready"),
        ("rollback-backup-unlinked", "ready"),
    ],
)
def test_sigkill_after_named_finalization_boundary_converges_from_disk(
    tmp_path: Path,
    boundary: str,
    terminal: str,
) -> None:
    env_file = tmp_path / "install.env"
    original = (
        b"ROOT_DOMAIN=proof.example.test\nPACKS=coder\nAI_DEFAULT_PROVIDER=openrouter\n"
        b"AI_DEFAULT_MODEL=example/model\nLITELLM_NVIDIA_API_KEY=SECRET-KILL\n"
    )
    env_file.write_bytes(original)
    env_file.chmod(0o600)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    repository = tmp_path / "repository"
    protected = repository / ".omo" / "evidence" / "unrelated.txt"
    protected.parent.mkdir(parents=True)
    protected.write_bytes(b"unrelated")
    manifest = model_sync_artifacts.protected_manifest_bytes(
        {".omo/evidence/unrelated.txt": hashlib.sha256(b"unrelated").hexdigest()}
    )
    model_sync_artifacts.atomic_write_bytes(
        artifact_dir / "protected-artifacts-before.txt", manifest
    )
    model_sync_artifacts.atomic_write_bytes(
        artifact_dir / "protected-artifacts-before.sha256",
        f"{hashlib.sha256(manifest).hexdigest()}  protected-artifacts-before.txt\n".encode(),
    )
    marker = tmp_path / "boundary.marker"
    child = r"""
import os
import sys
from pathlib import Path
from dokploy_wizard.proof import ProofRecoveryPaths, model_sync_artifacts, model_sync_state
from dokploy_wizard.proof.model_sync_baseline import CapturedBaseline
from dokploy_wizard.proof.model_sync_env import prepare_proof_env
from dokploy_wizard.proof.model_sync_host_a import (
    BaselineArtifactInputs,
    begin_proof_recovery,
    finalize_baseline_artifacts,
    recover_interrupted_proof,
)
from dokploy_wizard.proof.model_sync_remote import RemoteProbe

boundary, env_name, backup_name, guard_name, artifact_name, repository_name, marker_name = sys.argv[1:]
env, backup, guard, artifact_dir, repository, marker = map(
    Path,
    (env_name, backup_name, guard_name, artifact_name, repository_name, marker_name),
)
paths = ProofRecoveryPaths(env, backup, guard, artifact_dir, artifact_dir / "result.json", repository)
recovery = begin_proof_recovery(paths=paths, pid=os.getpid(), start_time_ticks=model_sync_state.process_start_time_ticks(Path("/proc/self/stat").read_text()))
if recovery.claim is None:
    raise RuntimeError("fixture recovery has no claim")
prepared = prepare_proof_env(
    env_file=env,
    backup_path=backup,
    guard_path=guard,
    claim_token=recovery.claim.token,
)
planes = ("cloudflare", "coder", "docker", "dokploy", "tailscale")
host_a = RemoteProbe("a" * 64, "b" * 64, "e" * 64, "amd64", True, {name: () for name in planes}, {name: "absent" for name in planes})
host_b = RemoteProbe("c" * 64, "d" * 64, "f" * 64, "amd64", True, {name: () for name in planes}, {name: "absent" for name in planes})
baseline = CapturedBaseline(
    payload={"fixture": "baseline"},
    images={
        "coder": "coder@sha256:" + "1" * 64,
        "litellm": "litellm@sha256:" + "2" * 64,
        "pgvector": "pgvector@sha256:" + "3" * 64,
        "redis": "redis@sha256:" + "4" * 64,
        "postfix": "postfix@sha256:" + "5" * 64,
    },
    coder_secret_inventory_sha256="6" * 64,
    legacy_workspace_managed_fingerprints_sha256="7" * 64,
)
inputs = BaselineArtifactInputs(
    repository,
    artifact_dir,
    artifact_dir / "result.json",
    "a" * 40,
    "b" * 40,
    prepared,
    guard,
    recovery.claim,
    "distinct",
    host_a,
        host_b,
        baseline,
        (),
    )

def boundary_name(value):
    return value.value

def pause_at_target(value):
    if boundary_name(value) != boundary:
        return
    model_sync_artifacts.atomic_write_bytes(marker, (boundary + "\n").encode())
    print(boundary, flush=True)
    sys.stdin.buffer.read(1)

if boundary.startswith("rollback-"):
    def interrupt_after_baseline(value):
        if boundary_name(value) == "baseline-published":
            raise RuntimeError("enter rollback fixture")
    try:
        finalize_baseline_artifacts(inputs, boundary_hook=interrupt_after_baseline)
    except RuntimeError as error:
        if str(error) != "enter rollback fixture":
            raise
    recover_interrupted_proof(recovery, boundary_hook=pause_at_target)
else:
    finalize_baseline_artifacts(inputs, boundary_hook=pause_at_target)
"""
    process = subprocess.Popen(
        [
            os.environ.get("PYTHON", "python"),
            "-c",
            child,
            boundary,
            str(env_file),
            str(backup),
            str(guard),
            str(artifact_dir),
            str(repository),
            str(marker),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert process.stdout is not None
    assert process.stdin is not None
    assert process.stderr is not None
    assert process.stdout.readline().strip() == boundary, process.stderr.read()
    assert marker.read_bytes() == f"{boundary}\n".encode()
    pre_kill_guard = read_abort_guard(guard)
    if boundary == "complete-guard":
        assert (pre_kill_guard.phase, pre_kill_guard.claimant_kind) == ("complete", "plan")
    elif boundary.startswith("rollback-"):
        assert (pre_kill_guard.phase, pre_kill_guard.claimant_kind) == ("rollback", "process")
    else:
        assert (pre_kill_guard.phase, pre_kill_guard.claimant_kind) == (
            "finalize_intent",
            "process",
        )
    process.send_signal(signal.SIGKILL)
    _stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == -signal.SIGKILL, stderr

    paths = ProofRecoveryPaths(
        env_file,
        backup,
        guard,
        artifact_dir,
        artifact_dir / "result.json",
        repository,
    )
    recovery = begin_proof_recovery(
        paths=paths,
        pid=os.getpid(),
        start_time_ticks=process_start_time_ticks(
            Path("/proc/self/stat").read_text(encoding="utf-8")
        ),
    )
    if recovery.resumable:
        complete_resumable_finalization(recovery)
    elif not recovery.terminal:
        recover_interrupted_proof(recovery)

    status = read_abort_guard(guard)
    assert (status.phase, status.claimant_kind) == (terminal, "plan")
    if terminal == "complete":
        verify_attestation(paths, status, require_result=True)
        assert env_file.read_bytes() != original
        assert backup.read_bytes() == original
    else:
        assert env_file.read_bytes() == original
        assert not backup.exists()
        assert status.attestation is None
        assert not any(path.exists() for path in output_paths(paths).values())
        assert not paths.output.exists()
