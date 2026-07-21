# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from email.message import Message
from pathlib import Path
from typing import Any
from urllib import error

import pytest

from dokploy_wizard.proof import (
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
        "coder-container", secret, "workspace", "ubuntu-vscode", "version-1"
    )

    assert secret not in " ".join(captured["command"])
    assert captured["input"] == secret + "\n"


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

    captured_templates = model_sync_host_b._templates("coder.example.test", "session")
    captured_workspaces = model_sync_host_b._workspaces(
        "coder.example.test", "session", "coder-container", captured_templates
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
    kdense_target = "/home/coder/kdense/models.json"
    kdense_pointer = "/home/coder/kdense/current"
    kdense_sha = _legacy_sha(kdense_target, kdense_pointer)
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
                                            "target": kdense_target,
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
    ["before-backup", "after-backup", "after-replace", "before-handler", "after-handler"],
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
    machine_sha256="a" * 64, ssh_sha256="b" * 64, architecture="amd64", namespace_clean=True
)
model_sync_cli.assert_namespace_identity = lambda **_kwargs: None
model_sync_cli._run_wrapper = lambda *_args: None
original_install = model_sync_cli._install_recovery_handlers
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
    assert process.stdout.readline().strip() == "READY"

    process.send_signal(signum)
    stdout, stderr = process.communicate()

    if boundary == "before-handler":
        assert process.returncode == -signum
        recovery = begin_proof_recovery(
            paths=ProofRecoveryPaths(env_file, backup, guard),
            pid=os.getpid(),
            start_time_ticks=process_start_time_ticks(Path("/proc/self/stat").read_text(encoding="utf-8")),
        )
        recover_interrupted_proof(recovery)
    else:
        assert process.returncode == 128 + signum
    assert env_file.read_bytes() == original
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert not backup.exists()
    assert read_abort_guard(guard).claimant_kind == "plan"
    assert not (tmp_path / "result.json").exists()
    assert b"SECRET-SIGNAL-SENTINEL" not in (stdout + stderr).encode()
