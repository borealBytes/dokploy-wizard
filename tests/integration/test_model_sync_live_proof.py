# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib import error

import pytest

from dokploy_wizard.dokploy.coder import _litellm_workspace_fallback_models_json
from dokploy_wizard.proof import (
    model_sync_artifacts,
    model_sync_baseline,
    model_sync_cli,
    model_sync_host_b,
    model_sync_remote,
    model_sync_results,
)
from dokploy_wizard.proof.model_sync_cli import main
from dokploy_wizard.proof.model_sync_env import ProofNamespace
from dokploy_wizard.proof.model_sync_host_a import (
    ProofRecoveryPaths,
    begin_proof_recovery,
    recover_interrupted_proof,
)
from dokploy_wizard.proof.model_sync_host_b import (
    HostIdentity,
    assert_followup_proof_contract,
    assert_namespace_identity,
)
from dokploy_wizard.proof.model_sync_state import process_start_time_ticks, read_abort_guard


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


def test_baseline_host_a_requires_all_named_environment_inputs_without_artifact(
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.json"

    exit_code = main(
        [
            "baseline-host-a",
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


def _sha(character: str) -> str:
    return character * 64


def _image_ref(repository: str, character: str) -> str:
    return f"{repository}@sha256:{_sha(character)}"


def _legacy_sha(target: str, pointer: str) -> str:
    return hashlib.sha256(f"{target}\0{pointer}\0expected-render\n".encode()).hexdigest()


def _preflight_wire(machine_id: str) -> str:
    return json.dumps(
        {
            "architecture": "x86_64",
            "machine_id": machine_id,
            "planes": {
                plane: {"resources": [], "state": "absent"}
                for plane in ("cloudflare", "coder", "docker", "dokploy", "tailscale")
            },
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
        model_sync_remote._parse_preflight(wire, namespace, "ssh-a")


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
            if failure == "docker-malformed":
                return subprocess.CompletedProcess(command, 0, b"{", b"")
            rows = [] if empty else [
                {
                    "ID": "container-other",
                    "Image": "busybox:latest",
                    "Labels": "",
                    "Names": "other-container",
                }
            ]
            return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, rows)).encode(), b"")
        if key[:2] == ("docker", "info"):
            return subprocess.CompletedProcess(command, 0, b"active\n", b"")
        if key[:3] == ("docker", "service", "ls"):
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


def _cloudflare_list(items: list[dict[str, Any]], *, page: int = 1, pages: int = 1) -> dict[str, Any]:
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
) -> Any:
    def open_request(request: Any, *, timeout: int) -> _WireResponse:
        assert timeout <= 30
        url = request.full_url
        if failure == "cloudflare-unauthorized" and "api.cloudflare.com" in url:
            raise error.HTTPError(url, 401, "unauthorized", Message(), None)
        if "/cfd_tunnel?" in url:
            if failure == "cloudflare-malformed":
                return _WireResponse({"success": True})
            tunnels = [] if empty else [{"id": "tunnel-other", "name": "other-tunnel"}]
            if matching:
                tunnels.append({"id": "tunnel-proof", "name": "proof-stack-cloudflared"})
            if failure == "cloudflare-partial":
                return _WireResponse(_cloudflare_list(tunnels, pages=2))
            return _WireResponse(_cloudflare_list(tunnels))
        if "/cfd_tunnel/" in url and url.endswith("/configurations"):
            ingress = [{"hostname": "other.example.test", "service": "http://other"}]
            if matching:
                ingress.append({"hostname": "coder.example.test", "service": "http://coder"})
            return _WireResponse({"result": {"config": {"ingress": ingress}}, "success": True})
        if "/dns_records?" in url:
            records = [] if empty else [{"id": "dns-other", "name": "other.example.test"}]
            if matching:
                records.append({"id": "dns-proof", "name": "coder.example.test"})
            return _WireResponse(_cloudflare_list(records))
        if "/access/apps?" in url:
            apps = [] if empty else [{"domain": "other.example.test", "id": "app-other", "name": "Other"}]
            if matching:
                apps.append({"domain": "coder.example.test", "id": "app-proof", "name": "Coder"})
            return _WireResponse(_cloudflare_list(apps))
        if "/access/apps/" in url and "/policies?" in url:
            policies = (
                [{"id": "policy-proof", "name": "proof-stack-access"}]
                if matching and "/app-proof/" in url
                else [{"id": "policy-other", "name": "Other policy"}]
            )
            return _WireResponse(_cloudflare_list(policies))
        if url.endswith("/api/project.all"):
            if failure == "dokploy-unauthorized":
                raise error.HTTPError(url, 401, "unauthorized", Message(), None)
            if failure == "dokploy-malformed":
                return _WireResponse({"data": {}})
            projects: list[dict[str, Any]] = [] if empty else [
                {"environments": [], "name": "other-project", "projectId": "project-other"}
            ]
            if matching:
                projects.append(
                    {
                        "environments": [
                            {
                                "applications": [
                                    {"applicationId": "application-proof", "name": "proof-stack-app"}
                                ],
                                "compose": [{"composeId": "compose-proof", "name": "proof-stack-coder"}],
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
                    [{"id": f"template-{index}", "name": f"template-{index}"} for index in range(100)]
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
    missing_tailscale: bool = False,
    template_count: int | None = None,
    template_malformed: bool = False,
    template_requests: list[str] | None = None,
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
    )
    scope["_which"] = lambda command: (
        None
        if (missing_tailscale and command == "tailscale")
        or (failure == "docker-missing" and command == "docker")
        else command
    )
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

    result = model_sync_remote._parse_preflight(wire, namespace, "ssh-a")

    assert result.namespace_clean is False
    assert {resource.kind for resource in result.inventory["cloudflare"]} == {
        "access_application",
        "access_policy",
        "dns_record",
        "hostname_route",
        "tunnel",
    }
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
        ("docker-missing", "docker"),
        ("docker-malformed", "docker"),
        ("tailscale-malformed", "tailscale"),
    ],
)
def test_authoritative_collectors_turn_failures_into_blocking_plane_errors(
    failure: str, failed_plane: str
) -> None:
    planes = _collect_planes(matching=True, failure=failure)

    assert planes[failed_plane] == {"resources": [], "state": "error"}


def test_authoritative_tailscale_collector_blocks_missing_required_command() -> None:
    planes = _collect_planes(missing_tailscale=True)

    assert planes["tailscale"] == {"resources": [], "state": "error"}


def test_preflight_parser_rejects_error_state_and_duplicate_ids() -> None:
    payload = json.loads(_preflight_wire("machine-a"))
    payload["planes"]["cloudflare"] = {"resources": [], "state": "error"}
    namespace = ProofNamespace("proof-stack", (), (), (), (), ())
    with pytest.raises(model_sync_remote.RemoteProofError, match="cloudflare plane collection failed"):
        model_sync_remote._parse_preflight(json.dumps(payload), namespace, "ssh-a")

    payload["planes"]["cloudflare"] = {
        "resources": [
            {"id": "duplicate", "kind": "tunnel", "name": "one"},
            {"id": "duplicate", "kind": "dns_record", "name": "two"},
        ],
        "state": "present",
    }
    with pytest.raises(model_sync_remote.RemoteProofError, match="IDs must be unique"):
        model_sync_remote._parse_preflight(json.dumps(payload), namespace, "ssh-a")


def test_coder_workspace_pagination_accepts_authoritative_empty_response() -> None:
    assert model_sync_results.collect_coder_workspace_pages(
        lambda _path: {"count": 0, "workspaces": []}
    ) == []


def test_remote_wrapper_password_is_sent_only_through_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    secret = "SECRET-REMOTE-PASSWORD"

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    model_sync_cli._run_wrapper(Path("wrapper"), "host-a", secret, Path("install.env"))

    assert secret not in " ".join(captured["command"])
    assert captured["input"] == secret + "\n"
    assert "--password-stdin" in captured["command"]


def test_coder_pointer_session_token_is_sent_only_through_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    secret = "SECRET-CODER-SESSION"

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(command, 0, '{"scope":"pointer"}', "")

    monkeypatch.setattr(subprocess, "run", run)

    model_sync_host_b._primary_pointer(
        "coder-container", secret, "workspace", "ubuntu-vscode", "version-1",
        model_sync_host_b.LegacyRenderer("http://proof-stack-shared-litellm:4000/v1", "key", "openrouter/example", (), ("openrouter/example",)),
    )

    assert secret not in " ".join(captured["command"])
    assert captured["input"] == secret + "\n"


def test_legacy_renderer_hash_is_independent_from_observed_pointer_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://proof-stack-shared-litellm:4000/v1", "apiKey": "SECRET-LEGACY-KEY"}, "models": {"openrouter/example": {}}}
    drifted = {**original, "models": {"openrouter/changed": {}}}

    def captured_pointer(value: dict[str, Any]) -> str:
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return json.dumps(
            {
                "target": "/home/coder/.config/opencode/opencode.json",
                "pointer": "/provider/litellm",
                "mode": "0644",
                "shape": "json-pointer",
                "base_url": value["options"]["baseURL"],
                "credential_value_sha256": hashlib.sha256(value["options"]["apiKey"].encode()).hexdigest(),
                "pointer_sha256": digest,
                "scope": "pointer",
            }
        )

    outputs = iter((captured_pointer(original), captured_pointer(drifted)))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, next(outputs), ""),
    )

    first = model_sync_host_b._primary_pointer(
        "coder-container", "session", "workspace", "ubuntu-vscode", "version-1",
        model_sync_host_b.LegacyRenderer("http://proof-stack-shared-litellm:4000/v1", "SECRET-LEGACY-KEY", "openrouter/example", (), ("openrouter/example",)),
    )
    second = model_sync_host_b._primary_pointer(
        "coder-container", "session", "workspace", "ubuntu-vscode", "version-1",
        model_sync_host_b.LegacyRenderer("http://proof-stack-shared-litellm:4000/v1", "SECRET-LEGACY-KEY", "openrouter/example", (), ("openrouter/example",)),
    )

    assert first["pointer_sha256"] != second["pointer_sha256"]
    assert first["independent_renderer_sha256"] == second["independent_renderer_sha256"]


@pytest.mark.parametrize(
    "changed",
    [
        model_sync_host_b.LegacyRenderer("http://proof-stack-shared-litellm:4000/v1", "changed-key", "openrouter/example", (), ("openrouter/example",)),
        model_sync_host_b.LegacyRenderer("http://proof-stack-shared-litellm:4000/v1", "expected-key", "openrouter/changed", (), ("openrouter/example",)),
        model_sync_host_b.LegacyRenderer("http://proof-stack-shared-litellm:4000/v1", "expected-key", "openrouter/example", ("openrouter/fallback",), ("openrouter/example",)),
        model_sync_host_b.LegacyRenderer("http://proof-stack-shared-litellm:4000/v1", "expected-key", "openrouter/example", (), ("openrouter/changed",)),
    ],
)
def test_observed_pointer_hash_is_independent_from_renderer_input_mutation(
    changed: model_sync_host_b.LegacyRenderer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://proof-stack-shared-litellm:4000/v1", "apiKey": "observed-key"}, "models": {"openrouter/example": {}}}
    observed_sha = hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    captured = json.dumps({"target": "/home/coder/.config/opencode/opencode.json", "pointer": "/provider/litellm", "mode": "0644", "shape": "json-pointer", "base_url": "http://proof-stack-shared-litellm:4000/v1", "credential_value_sha256": hashlib.sha256(b"observed-key").hexdigest(), "pointer_sha256": observed_sha, "scope": "pointer"})
    monkeypatch.setattr(subprocess, "run", lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, captured, ""))
    original = model_sync_host_b.LegacyRenderer("http://proof-stack-shared-litellm:4000/v1", "expected-key", "openrouter/example", (), ("openrouter/example",))

    first = model_sync_host_b._primary_pointer("coder", "session", "workspace", "ubuntu-vscode", "version", original)
    second = model_sync_host_b._primary_pointer("coder", "session", "workspace", "ubuntu-vscode", "version", changed)

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
    fallbacks = tuple(json.loads(_litellm_workspace_fallback_models_json(default_alias="openrouter/example/model")))
    renderer = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000/v1", credential, "openrouter/example/model", fallbacks, ("openrouter/example/model",)
    )
    observed = model_sync_host_b._render_legacy_pointer(renderer)
    pointer_sha = hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    calls: list[tuple[list[str], str | None]] = []

    def api(_hostname: str, _token: str | None, path: str, _body: dict[str, str] | None = None) -> Any:
        if path == "/api/v2/users/me":
            return {"id": "user-proof"}
        if path == "/api/v2/templates":
            return [{"id": "template-proof", "name": "ubuntu-vscode", "active_version_id": "version-proof", "active_version_name": "proof"}]
        if path.startswith("/api/v2/workspaces?"):
            return {"count": 1, "workspaces": [{"id": "workspace-proof", "name": "primary", "template_id": "template-proof", "template_version_id": "version-proof"}]}
        if "/builds?" in path:
            return [{"id": "build-proof", "build_number": 1, "status": "stopped", "transition": "stop"}]
        if "/secrets?" in path:
            return [{"id": "secret-proof", "name": "secret", "env_name": "OPENAI_API_KEY", "description": "credential"}]
        raise AssertionError(path)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs.get("input")))
        assert kwargs["timeout"] <= 30
        if kwargs.get("input") == "session\n":
            return subprocess.CompletedProcess(command, 0, json.dumps({"target": "/home/coder/.config/opencode/opencode.json", "pointer": "/provider/litellm", "mode": "0644", "shape": "json-pointer", "base_url": renderer.base_url, "credential_value_sha256": hashlib.sha256(credential.encode()).hexdigest(), "pointer_sha256": pointer_sha, "scope": "pointer"}), "")
        return subprocess.CompletedProcess(
            command, 0, json.dumps([{"id": "openrouter/example/model"}]), ""
        )

    probe = model_sync_remote.RemoteProbe("a" * 64, "b" * 64, "amd64", False, {"cloudflare": (), "tailscale": (), "coder": (), "docker": (), "dokploy": ()}, {plane: "absent" for plane in ("cloudflare", "tailscale", "coder", "docker", "dokploy")})
    monkeypatch.setattr(model_sync_host_b, "_api", api)
    monkeypatch.setattr(model_sync_host_b, "_coder_login", lambda *_args: "session")
    monkeypatch.setattr(model_sync_host_b, "_coder_container_name", lambda *_args: "coder")
    monkeypatch.setattr(model_sync_host_b, "_image_inventory", lambda: [])
    monkeypatch.setattr(model_sync_host_b, "_state_inventory", lambda _state_dir: {})
    monkeypatch.setattr(model_sync_host_b, "capture_local_authoritative_inventory", lambda *_args: probe)
    monkeypatch.setattr(model_sync_host_b, "load_litellm_generated_keys", lambda _state_dir: SimpleNamespace(virtual_keys={"coder-hermes": credential}))
    monkeypatch.setattr(subprocess, "run", run)

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
    assert [input_value for command, input_value in calls if "ssh" in command] == ["session\n" + credential, "session\n"]
    assert [input_value for _command, input_value in calls if input_value != "session\n"] == ["session\n" + credential]
    captured = capsys.readouterr()
    assert credential not in captured.out + captured.err


def test_legacy_baseline_hashes_are_canonical_and_drift_sensitive() -> None:
    snapshot = json.loads(_snapshot_wire())
    captured = model_sync_baseline.parse_captured_baseline(json.dumps(snapshot), stack_name="proof-stack")
    legacy = model_sync_artifacts.require_list(
        captured.payload["legacy_workspace_managed_fingerprints"], "captured legacy fingerprints"
    )

    assert all(
        model_sync_artifacts.require_mapping(item, "captured legacy fingerprint")["legacy_exact"]
        is True
        for item in legacy
    )
    assert captured.legacy_workspace_managed_fingerprints_sha256 == model_sync_baseline.canonical_sha256(legacy)
    assert model_sync_host_b._sha(json.loads('{"b":2, "a":1}')) == model_sync_host_b._sha(json.loads('{ "a" : 1, "b" : 2 }'))
    snapshot["coder"]["workspaces"]["pages"][0]["items"][0]["legacy_pointers"][0]["independent_renderer_sha256"] = _sha("f")
    changed = model_sync_baseline.parse_captured_baseline(json.dumps(snapshot), stack_name="proof-stack")

    changed_legacy = model_sync_artifacts.require_list(
        changed.payload["legacy_workspace_managed_fingerprints"], "changed legacy fingerprints"
    )
    primary = next(
        fingerprint
        for item in changed_legacy
        if (fingerprint := model_sync_artifacts.require_mapping(item, "changed legacy fingerprint"))[
            "target"
        ]
        == "/home/coder/.config/opencode/opencode.json"
    )
    assert primary["legacy_exact"] is False
    assert changed.legacy_workspace_managed_fingerprints_sha256 != captured.legacy_workspace_managed_fingerprints_sha256
    observed_snapshot = json.loads(_snapshot_wire())
    observed_snapshot["coder"]["workspaces"]["pages"][0]["items"][0]["legacy_pointers"][0]["pointer_sha256"] = _sha("e")
    observed_changed = model_sync_baseline.parse_captured_baseline(json.dumps(observed_snapshot), stack_name="proof-stack")

    assert observed_changed.legacy_workspace_managed_fingerprints_sha256 != captured.legacy_workspace_managed_fingerprints_sha256


def test_legacy_value_drift_is_nonexact_without_rejecting_valid_metadata() -> None:
    snapshot = json.loads(_snapshot_wire())
    pointer = snapshot["coder"]["workspaces"]["pages"][0]["items"][0]["legacy_pointers"][0]
    pointer["credential_value_sha256"] = "d" * 64
    pointer["pointer_sha256"] = "e" * 64

    captured = model_sync_baseline.parse_captured_baseline(json.dumps(snapshot), stack_name="proof-stack")
    legacy = model_sync_artifacts.require_list(
        captured.payload["legacy_workspace_managed_fingerprints"], "drifted legacy fingerprints"
    )
    first = next(
        item
        for value in legacy
        if (item := model_sync_artifacts.require_mapping(value, "legacy fingerprint"))["scope"] == "pointer"
    )

    assert first["legacy_exact"] is False
    assert first["credential_value_sha256"] == "d" * 64


def test_legacy_observed_base_url_drift_fails_closed() -> None:
    snapshot = json.loads(_snapshot_wire())
    snapshot["coder"]["workspaces"]["pages"][0]["items"][0]["legacy_pointers"][0][
        "base_url"
    ] = "https://observed.example.invalid/v1"

    with pytest.raises(model_sync_baseline.BaselineCaptureError):
        model_sync_baseline.parse_captured_baseline(json.dumps(snapshot), stack_name="proof-stack")


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

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload["data"]), "")

    monkeypatch.setattr(subprocess, "run", run)

    models = model_sync_host_b._model_inventory("coder", "session", "workspace", credential, "proof-stack")

    assert models == ("openrouter/one", "opencode-go/two")
    assert credential not in " ".join(captured["command"])
    assert captured["input"] == "session\n" + credential


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
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "SECRET-REMOTE-ERROR"),
    )

    with pytest.raises(ValueError) as inventory_error:
        model_sync_host_b._legacy_renderer({}, tmp_path, "coder", "session", "workspace", "proof-stack", "ubuntu-vscode")

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


def test_model_inventory_rejects_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "[]", ""),
    )

    with pytest.raises(ValueError):
        model_sync_host_b._model_inventory("coder", "session", "workspace", "credential", "proof-stack")


@pytest.mark.parametrize("extra_byte", [False, True])
def test_workspace_pointer_stdout_has_exact_byte_limit(
    extra_byte: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = 2 * 1024 * 1024
    payload = '{"scope":"pointer"}'
    stdout = payload + " " * (limit - len(payload) + int(extra_byte))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout, ""),
    )
    renderer = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000/v1",
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
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout, ""),
    )

    if extra_byte:
        with pytest.raises(ValueError):
            model_sync_host_b._model_inventory("coder", "session", "workspace", "credential", "proof-stack")
    else:
        assert model_sync_host_b._model_inventory("coder", "session", "workspace", "credential", "proof-stack") == (
            "openrouter/example",
        )


@pytest.mark.parametrize("extra_record", [False, True])
def test_model_inventory_has_exact_record_limit(
    extra_record: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = 1_000 + int(extra_record)
    stdout = json.dumps([{"id": f"p/{index}"} for index in range(count)], separators=(",", ":"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout, ""),
    )

    if extra_record:
        with pytest.raises(ValueError):
            model_sync_host_b._model_inventory("coder", "session", "workspace", "credential", "proof-stack")
    else:
        assert len(model_sync_host_b._model_inventory("coder", "session", "workspace", "credential", "proof-stack")) == count


def test_model_inventory_uses_workspace_python_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(command, 0, '[{"id":"openrouter/example"}]', "")

    monkeypatch.setattr(subprocess, "run", run)

    model_sync_host_b._model_inventory("coder", "session", "workspace", "credential", "proof-stack")

    assert "ssh" in captured["command"]
    assert "python3" not in captured["command"]
    node_index = captured["command"].index("node")
    assert captured["command"][node_index : node_index + 2] == ["node", "-e"]


def test_kdense_capture_binds_whole_target_and_current_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = model_sync_host_b.LegacyRenderer(
        "http://proof-stack-shared-litellm:4000/v1",
        "credential",
        "openrouter/example",
        (),
        ("openrouter/example",),
    )
    target_path = "/home/coder/.cache/kdense-byok-src/web/src/data/models.json"
    symlink_target = target_path
    target_sha = model_sync_host_b._sha([{"id": "openrouter/example"}])
    symlink_sha = model_sync_host_b._sha(symlink_target)
    aggregate_sha = model_sync_host_b._sha(
        {"base_url": renderer.base_url, "credential_value_sha256": model_sync_host_b._sha(renderer.credential.encode()), "symlink_sha256": symlink_sha, "target_sha256": target_sha}
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
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, json.dumps(observed), ""),
    )

    pointer = model_sync_host_b._primary_pointer(
        "coder", "session", "workspace", "ubuntu-vscode-kdense-byok", "historical", renderer
    )

    assert pointer["pointer_sha256"] == pointer["independent_renderer_sha256"]
    assert pointer["target_sha256"] == target_sha
    assert pointer["symlink_sha256"] == symlink_sha


def test_renderer_rejects_missing_generated_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_sync_host_b, "load_litellm_generated_keys", lambda _state_dir: None)

    with pytest.raises(ValueError):
        model_sync_host_b._legacy_renderer({}, tmp_path, "coder", "session", "workspace", "proof-stack", "ubuntu-vscode")


@pytest.mark.parametrize("template", ["ubuntu-vscode-hermes", "ubuntu-vscode-pi-web"])
def test_workspace_templates_without_exact_legacy_renderer_fail_closed(
    template: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("unsupported template executed"))

    with pytest.raises(ValueError):
        model_sync_host_b._primary_pointer(
            "coder", "session", "workspace", template, "version",
            model_sync_host_b.LegacyRenderer(
                "http://proof-stack-shared-litellm:4000/v1",
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
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("pointer capture executed"))

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
        "workspaces": [{
            "id": "workspace-proof",
            "name": "primary",
            "template_id": "template-proof",
            "template_version_id": "historical-version",
        }],
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
            "http://proof-stack-shared-litellm:4000/v1",
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
    snapshot["coder"]["templates"]["pages"][0]["items"][0]["active_version_id"] = "new-active-version"

    captured = model_sync_baseline.parse_captured_baseline(json.dumps(snapshot), stack_name="proof-stack")
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
        {"base_url": pointer["base_url"], "credential_value_sha256": pointer["credential_value_sha256"], "symlink_sha256": None, "target_sha256": pointer["target_sha256"]}
    )

    captured = model_sync_baseline.parse_captured_baseline(json.dumps(snapshot), stack_name="proof-stack")
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


@pytest.mark.parametrize("field", ["target_sha256", "symlink_sha256", "credential_value_sha256"])
def test_kdense_target_symlink_and_credential_drift_are_nonexact(field: str) -> None:
    snapshot = json.loads(_snapshot_wire())
    pointer = snapshot["coder"]["workspaces"]["pages"][0]["items"][1]["legacy_pointers"][0]
    pointer[field] = "9" * 64
    pointer["pointer_sha256"] = model_sync_host_b._sha({
        "base_url": pointer["base_url"],
        "credential_value_sha256": pointer["credential_value_sha256"],
        "symlink_sha256": pointer["symlink_sha256"],
        "target_sha256": pointer["target_sha256"],
    })

    captured = model_sync_baseline.parse_captured_baseline(json.dumps(snapshot), stack_name="proof-stack")
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

    captured = model_sync_baseline.parse_captured_baseline(json.dumps(snapshot), stack_name="proof-stack")

    assert captured.payload["workspaces"] == []
    assert captured.payload["legacy_workspace_managed_fingerprints"] == []


@pytest.mark.parametrize("mutation", ["empty", "duplicate", "mode", "shape", "scope", "target", "path", "base", "version", "unsupported", "hash", "credential_hash", "renderer_hash"])
def test_legacy_baseline_rejects_missing_or_malformed_renderer_evidence(mutation: str) -> None:
    snapshot = json.loads(_snapshot_wire())
    primary = snapshot["coder"]["workspaces"]["pages"][0]["items"][0]
    pointer = primary["legacy_pointers"][0]
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
        pointer["base_url"] = "https://observed.example.invalid/v1"
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

    def api(_hostname: str, _token: str | None, path: str, _body: dict[str, str] | None = None) -> Any:
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
            "http://proof-stack-shared-litellm:4000/v1", "key", "openrouter/example", (), ("openrouter/example",)
        ),
    )

    captured_templates = model_sync_host_b._templates("coder.example.test", "session")
    captured_workspaces = model_sync_host_b._workspaces(
        "coder.example.test", "session", "coder-container", captured_templates, {}, Path("."), "proof-stack",
    )
    captured_builds: Any = model_sync_host_b._builds("coder.example.test", "session", captured_workspaces)
    captured_secrets = model_sync_host_b._secrets("coder.example.test", "session", "user-1")

    assert {item["id"] for item in captured_templates} == {f"template-{index}" for index in range(101)}
    assert {item["id"] for item in captured_workspaces} == {f"workspace-{index}" for index in range(101)}
    assert {
        item["id"]
        for page in captured_builds[0]["pages"]
        for item in page["items"]
    } == {f"build-{index}" for index in range(101)}
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
    kdense_target_sha = model_sync_host_b._sha([{"id": "openrouter/example"}])
    kdense_symlink_sha = model_sync_host_b._sha(kdense_target)
    kdense_sha = model_sync_host_b._sha({"base_url": "http://proof-stack-shared-litellm:4000/v1", "credential_value_sha256": _sha("c"), "symlink_sha256": kdense_symlink_sha, "target_sha256": kdense_target_sha})
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
                                        "status": "not-a-coder-status" if malformed_build else "stopped",
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
                                            "base_url": "http://proof-stack-shared-litellm:4000/v1",
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
                                            "base_url": "http://proof-stack-shared-litellm:4000/v1",
                                            "credential_value_sha256": _sha("c"),
                                            "independent_renderer_sha256": kdense_sha,
                                            "mode": "0644",
                                            "pointer": kdense_pointer,
                                            "pointer_sha256": kdense_sha,
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
                {"container_image": _image_ref("ghcr.io/coder/coder", "1"), "logical_name": "coder", "registry_image": _image_ref("ghcr.io/coder/coder", "1")},
                {"container_image": _image_ref("ghcr.io/berriai/litellm", "2"), "logical_name": "litellm", "registry_image": _image_ref("ghcr.io/berriai/litellm", "2")},
                {"container_image": _image_ref("pgvector/pgvector", "3"), "logical_name": "pgvector", "registry_image": _image_ref("pgvector/pgvector", "3")},
                {"container_image": _image_ref("redis", "4"), "logical_name": "redis", "registry_image": _image_ref("redis", "4")},
                {"container_image": _image_ref("boky/postfix", "5"), "logical_name": "postfix", "registry_image": _image_ref("boky/postfix", "5")},
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
    def __init__(self, *, machine_id: str, fingerprint: bytes, snapshot: str) -> None:
        self._machine_id = machine_id
        self._fingerprint = fingerprint
        self._snapshot = snapshot
        self.commands: list[str] = []
        self.stdins: list[_FixtureStdin] = []

    def exec_command(self, command: str, *, timeout: int) -> tuple[_FixtureStdin, _FixtureStream, _FixtureStream]:
        del timeout
        payload = self._snapshot if "model-sync-snapshot" in command else _preflight_wire(self._machine_id)
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
                client = _FixtureRemoteClient(machine_id="machine-a", fingerprint=b"ssh-a", snapshot=snapshot)
            case "host-b":
                client = _FixtureRemoteClient(machine_id="machine-b", fingerprint=b"ssh-b", snapshot=snapshot)
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
    return [
        "baseline-host-a",
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
            model_sync_remote.ObservedResource("app-proof", "coder.example.test", "access_application"),
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
        architecture="amd64",
        namespace_clean=False,
        inventory=observed,
        plane_states={plane: "present" for plane in observed},
    )
    monkeypatch.setattr(model_sync_host_b, "capture_local_authoritative_inventory", lambda *_args: probe)
    monkeypatch.setattr(model_sync_host_b, "_image_inventory", lambda: [])
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
            "http://proof-stack-shared-litellm:4000/v1",
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
    monkeypatch.setattr(model_sync_cli, "_require_active_workspace_root", lambda _wrapper: None)
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-b")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")

    exit_code = main(arguments)

    artifact_dir = tmp_path / "artifacts"
    result = json.loads((artifact_dir / "result.json").read_text(encoding="utf-8"))
    baseline = json.loads((artifact_dir / "baseline.json").read_text(encoding="utf-8"))
    manifest = (artifact_dir / "protected-artifacts-before.txt").read_text(encoding="utf-8")
    guard_sha256 = hashlib.sha256((tmp_path / "abort-guard.json").read_bytes()).hexdigest()
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
    assert result["abort_guard_sha256"] == guard_sha256
    assert f"{guard_sha256}  abort-guard.json" in manifest
    assert "baseline.json" in manifest
    assert "password-a" not in (artifact_dir / "baseline.json").read_text(encoding="utf-8")
    sentinels = (
        "SECRET-CLOUDFLARE-TOKEN",
        "SECRET-DOKPLOY-KEY",
        "SECRET-CODER-PASSWORD",
    )
    assert all(secret not in command for client in clients for command in client.commands for secret in sentinels)
    assert all(
        secret not in path.read_text(encoding="utf-8")
        for path in artifact_dir.iterdir()
        for secret in sentinels
    )
    assert all(
        secret in b"".join(stdin.payload for client in clients for stdin in client.stdins).decode()
        for secret in sentinels
    )


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
    monkeypatch.setattr(model_sync_cli, "_require_active_workspace_root", lambda _wrapper: None)
    monkeypatch.setenv("FIXTURE_HOST_A", "host-a")
    monkeypatch.setenv("FIXTURE_PASSWORD_A", "password-a")
    monkeypatch.setenv("FIXTURE_HOST_B", "host-b")
    monkeypatch.setenv("FIXTURE_PASSWORD_B", "password-b")

    exit_code = main(arguments)

    artifact_dir = tmp_path / "artifacts"
    assert exit_code == 1
    assert not (artifact_dir / "result.json").exists()
    assert not (artifact_dir / "baseline.json").exists()
    assert not (artifact_dir / "protected-artifacts-before.txt").exists()


def test_capture_finalization_rolls_back_outputs_when_system_exit_interrupts_second_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dokploy_wizard.proof import model_sync_artifacts

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    writes = 0
    original = model_sync_artifacts.atomic_write_bytes

    def write(path: Path, content: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise SystemExit(1)
        original(path, content)

    monkeypatch.setattr(model_sync_artifacts, "atomic_write_bytes", write)

    with pytest.raises(SystemExit):
        model_sync_artifacts.finalize_capture_outputs({first: b"one", second: b"two"})

    assert not first.exists()
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
    child = r'''
from __future__ import annotations
import argparse
import hashlib
import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from dokploy_wizard.proof import model_sync_cli, model_sync_env

boundary, env_name, backup_name, guard_name = sys.argv[1:]
env_file = Path(env_name)
backup = Path(backup_name)
guard = Path(guard_name)
original_write = model_sync_env.atomic_write_bytes
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

model_sync_env.atomic_write_bytes = write
model_sync_cli._required_inputs = lambda _args: ("host-a", "password-a", "host-b", "password-b")
model_sync_cli._require_active_workspace_root = lambda _wrapper: None
model_sync_cli.resolve_proof_namespace = lambda _env: SimpleNamespace(stack_name="proof-stack")
model_sync_cli.resolve_proof_transport = lambda _env: None
model_sync_cli.probe_host = lambda **_kwargs: SimpleNamespace(
    machine_sha256="a" * 64, ssh_sha256="b" * 64, architecture="amd64", namespace_clean=True,
    to_dict=lambda: {},
)
model_sync_cli.assert_namespace_identity = lambda **_kwargs: None
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
if boundary in ("before-restore", "between-restore"):
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
    wrapper=Path("wrapper"), env_file=env_file, external_backup=backup, abort_guard=guard,
    host_env="unused", password_env="unused", host_b_env="unused", host_b_password_env="unused",
    source_base_commit="a" * 40, proof_commit="b" * 40, artifact_dir=env_file.parent,
    output=env_file.parent / "result.json",
)
model_sync_cli._baseline_host_a(args)
'''
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
        assert status.env_receipt is not None and status.env_receipt.complete

        actual_hashes = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in task_outputs.items()
        }
        result = model_sync_artifacts.require_mapping(
            json.loads(task_outputs["result.json"].read_text(encoding="utf-8")),
            "result",
        )
        assert result["abort_guard_sha256"] == actual_hashes["abort-guard.json"]
        assert result["baseline_sha256"] == actual_hashes["baseline.json"]
        assert result["host_a_preflight_sha256"] == actual_hashes["host-a-preflight.json"]
        assert result["host_b_preflight_sha256"] == actual_hashes["host-b-preflight.json"]
        assert result["protected_artifacts_before_sha256"] == actual_hashes[
            "protected-artifacts-before.txt"
        ]
        assert result["env_original_sha256"] == hashlib.sha256(original).hexdigest()
        assert result["env_proof_sha256"] == hashlib.sha256(proof).hexdigest()

        manifest_hashes: dict[str, str] = {}
        for line in task_outputs["protected-artifacts-before.txt"].read_text(
            encoding="utf-8"
        ).splitlines():
            fingerprint, name = line.split("  ", 1)
            manifest_hashes[name] = fingerprint
        assert manifest_hashes == {
            "abort-guard.json": actual_hashes["abort-guard.json"],
            "baseline.json": actual_hashes["baseline.json"],
            "env-original": hashlib.sha256(original).hexdigest(),
            "env-proof": hashlib.sha256(proof).hexdigest(),
            "host-a-preflight.json": actual_hashes["host-a-preflight.json"],
            "host-b-preflight.json": actual_hashes["host-b-preflight.json"],
        }

        abort_status_output = tmp_path / "status" / "abort-status.json"
        assert main(
            [
                "abort-status",
                "--guard",
                str(guard),
                "--output",
                str(abort_status_output),
            ]
        ) == 0
        abort_status = model_sync_artifacts.require_mapping(
            json.loads(abort_status_output.read_text(encoding="utf-8")),
            "abort status",
        )
        assert abort_status == {
            "claim_token": "<REDACTED>",
            "claimant_kind": "plan",
            "pid": None,
            "start_time_ticks": None,
            "state": "armed",
        }
        mode_paths = (*task_outputs.values(), env_file, backup, abort_status_output)
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in mode_paths)
        assert not list(tmp_path.rglob("*.tmp"))
        assert "RECOVERY" not in stdout + stderr
        if boundary == "between-restore" and signum == signal.SIGINT:
            assert f"PREVIOUS:{signum}" in stdout
        else:
            assert "PREVIOUS:" not in stdout
    elif boundary == "before-handler":
        assert process.returncode == -signum
        recovery = begin_proof_recovery(
            paths=ProofRecoveryPaths(env_file, backup, guard),
            pid=os.getpid(),
            start_time_ticks=process_start_time_ticks(Path("/proc/self/stat").read_text(encoding="utf-8")),
        )
        recover_interrupted_proof(recovery)
    elif boundary == "after-finalize":
        assert process.returncode == 128 + signum
        result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        manifest = (tmp_path / "protected-artifacts-before.txt").read_text(encoding="utf-8")
        guard_bytes = guard.read_bytes()
        guard_sha256 = hashlib.sha256(guard_bytes).hexdigest()
        status = read_abort_guard(guard)
        assert env_file.read_bytes() != original
        assert backup.read_bytes() == original
        assert status.state == "armed"
        assert status.claimant_kind == "plan"
        assert status.env_receipt is not None and status.env_receipt.complete
        assert result["abort_guard_sha256"] == guard_sha256
        assert f"{guard_sha256}  abort-guard.json" in manifest
    else:
        assert process.returncode == 128 + signum
    if boundary not in ("after-finalize", "before-restore", "between-restore"):
        assert env_file.read_bytes() == original
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert not backup.exists()
        assert read_abort_guard(guard).claimant_kind == "plan"
        assert not (tmp_path / "result.json").exists()
    assert b"SECRET-SIGNAL-SENTINEL" not in (stdout + stderr).encode()


@pytest.mark.parametrize("signals", [(signal.SIGINT, signal.SIGTERM), (signal.SIGTERM, signal.SIGINT)])
def test_nested_signal_does_not_interrupt_active_recovery(
    tmp_path: Path, signals: tuple[signal.Signals, signal.Signals]
) -> None:
    env_file = tmp_path / "install.env"
    original = b"ROOT_DOMAIN=proof.example.test\nLITELLM_NVIDIA_API_KEY=SECRET-NESTED\n"
    env_file.write_bytes(original)
    env_file.chmod(0o600)
    backup = tmp_path / "backup.env"
    guard = tmp_path / "abort-guard.json"
    child = r'''
import os, signal, sys
from pathlib import Path
from dokploy_wizard.proof import model_sync_cli
from dokploy_wizard.proof.model_sync_env import prepare_proof_env
from dokploy_wizard.proof.model_sync_host_a import ProofRecoveryPaths, begin_proof_recovery, recover_interrupted_proof

env, backup, guard = map(Path, sys.argv[1:])
recovery = begin_proof_recovery(paths=ProofRecoveryPaths(env, backup, guard), pid=os.getpid(), start_time_ticks=model_sync_cli._self_start_time_ticks())
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
'''
    process = subprocess.Popen(
        [os.environ.get("PYTHON", "python"), "-c", child, str(env_file), str(backup), str(guard)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
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
