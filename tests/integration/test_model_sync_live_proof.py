# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from dokploy_wizard.proof import model_sync_cli, model_sync_remote
from dokploy_wizard.proof.model_sync_cli import main
from dokploy_wizard.proof.model_sync_host_b import (
    HostIdentity,
    assert_followup_proof_contract,
    assert_namespace_identity,
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
                "cloudflare": [],
                "coder": [],
                "docker": [],
                "dokploy": [],
                "tailscale": [],
            },
            "schema_version": 1,
        }
    )


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

    def read(self) -> bytes:
        return self._payload


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

    def exec_command(self, command: str, *, timeout: int) -> tuple[None, _FixtureStream, _FixtureStream]:
        del timeout
        payload = self._snapshot if "model-sync-snapshot" in command else _preflight_wire(self._machine_id)
        return None, _FixtureStream(payload), _FixtureStream("")

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
) -> None:
    def connect(**kwargs: Any) -> _FixtureParamikoTransport:
        host = kwargs["hostname"]
        match host:
            case "host-a":
                return _FixtureParamikoTransport(
                    _FixtureRemoteClient(machine_id="machine-a", fingerprint=b"ssh-a", snapshot=snapshot)
                )
            case "host-b":
                return _FixtureParamikoTransport(
                    _FixtureRemoteClient(machine_id="machine-b", fingerprint=b"ssh-b", snapshot=snapshot)
                )
            case unexpected:
                raise AssertionError(f"unexpected fixture host {unexpected}")

    monkeypatch.setattr(model_sync_remote.ParamikoRemoteTransport, "connect", connect)


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


def test_baseline_host_a_collects_complete_fixture_inventory_via_argparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _baseline_arguments(tmp_path)
    _install_fixture_transport(monkeypatch, snapshot=_snapshot_wire())
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
    assert "baseline.json" in manifest
    assert "password-a" not in (artifact_dir / "baseline.json").read_text(encoding="utf-8")


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
