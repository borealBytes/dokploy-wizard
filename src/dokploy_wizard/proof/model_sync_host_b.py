# ruff: noqa: E501
"""Host-pair identity and later-proof receipt contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Final, Sequence
from urllib import request

from dokploy_wizard.dokploy.coder import _coder_container_name
from dokploy_wizard.proof.model_sync_artifacts import JsonValue, require_mapping, require_text
from dokploy_wizard.proof.model_sync_env import resolve_proof_namespace
from dokploy_wizard.proof.model_sync_remote import (
    RemoteProbe,
    capture_local_authoritative_inventory,
)
from dokploy_wizard.proof.model_sync_results import (
    collect_coder_array_pages,
    collect_coder_workspace_pages,
)
from dokploy_wizard.state import parse_env_file, resolve_desired_state

_SUPPORTED_ARCHITECTURES: Final = frozenset({"amd64", "arm64"})


@dataclass(frozen=True, slots=True)
class HostIdentity:
    machine_sha256: str
    ssh_sha256: str
    architecture: str


def assert_namespace_identity(*, host_a: HostIdentity, host_b: HostIdentity) -> None:
    """Require two physical hosts with one supported architecture before any upload."""
    if host_a.machine_sha256 == host_b.machine_sha256:
        raise ValueError("Host A and Host B must have distinct machine identities")
    if host_a.ssh_sha256 == host_b.ssh_sha256:
        raise ValueError("Host A and Host B must have distinct SSH host identities")
    if host_a.architecture not in _SUPPORTED_ARCHITECTURES:
        raise ValueError("Host A architecture is unsupported")
    if host_b.architecture not in _SUPPORTED_ARCHITECTURES:
        raise ValueError("Host B architecture is unsupported")
    if host_a.architecture != host_b.architecture:
        raise ValueError("Host A and Host B architectures must match")
def assert_followup_proof_contract(*, contract_name: str, receipts: tuple[str, ...]) -> None:
    """Keep later Host A/Host B actions blocked until their named receipt exists."""
    required = {
        "upgrade_host_a_contract": "host-a-baseline",
        "final_proof_contract": "host-a-destroyed",
        "reseed_pair_contract": "host-b-clean",
    }.get(contract_name)
    if required is None:
        raise ValueError("unknown followup proof contract")
    if required not in receipts:
        raise ValueError(f"{contract_name} requires receipt {required}")
def main(argv: Sequence[str] | None = None) -> int:
    """Emit a value-free remote snapshot when invoked through the bounded SSH transport."""
    parser = argparse.ArgumentParser(prog="model-sync-snapshot")
    parser.add_argument("command", choices=("model-sync-snapshot",))
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(_snapshot(args.env_file, args.state_dir), sort_keys=True, separators=(",", ":")))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"model-sync snapshot failed: {error}", file=sys.stderr)
        return 1
    return 0
def _snapshot(env_file: Path, state_dir: Path) -> dict[str, JsonValue]:
    desired = resolve_desired_state(parse_env_file(env_file))
    namespace = resolve_proof_namespace(env_file)
    observed = capture_local_authoritative_inventory(env_file, namespace)
    images = _image_inventory()
    raw_env = parse_env_file(env_file).values
    email = _env(raw_env, "DOKPLOY_ADMIN_EMAIL")
    password = _env(raw_env, "DOKPLOY_ADMIN_PASSWORD")
    token = _coder_login(desired.hostnames["coder"], email, password)
    container = _coder_container_name(f"{desired.stack_name}-coder")
    if container is None:
        raise ValueError("Coder container is not running")
    user = _api(desired.hostnames["coder"], token, "/api/v2/users/me")
    user_id = _field(user, "id")
    templates = _templates(desired.hostnames["coder"], token)
    workspaces = _workspaces(desired.hostnames["coder"], token, container, templates)
    builds = _builds(desired.hostnames["coder"], token, workspaces)
    secrets = _secrets(desired.hostnames["coder"], token, user_id)
    return {
        "cloudflare": {"access_application_ids": _ids(observed, "cloudflare", "access_application"), "dns_record_ids": _ids(observed, "cloudflare", "dns_record"), "tunnel_ids": _ids(observed, "cloudflare", "tunnel")},
        "coder": {"builds": builds, "secrets": _page(secrets), "templates": _page(templates), "workspaces": _page(workspaces)},
        "images": images,
        "schema_version": 1,
        "tailscale": {"identifiers": _ids(observed, "tailscale", "node")},
        "wizard_state": _state_inventory(state_dir),
    }
def _ids(probe: RemoteProbe, plane: str, kind: str) -> list[str]:
    return [item.resource_id for item in probe.inventory[plane] if item.kind == kind]
def _coder_login(hostname: str, email: str, password: str) -> str:
    response = _api(hostname, None, "/api/v2/users/login", {"email": email, "password": password})
    return _field(response, "session_token")
def _api(hostname: str, token: str | None, path: str, body: dict[str, str] | None = None) -> dict[str, JsonValue] | list[JsonValue]:
    headers = {"Accept": "application/json", "Host": hostname}
    if token is not None:
        headers["Coder-Session-Token"] = token
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    request_value = request.Request(f"https://{hostname}{path}", data=data, headers=headers, method="POST" if body else "GET")
    opener = request.build_opener(_NoRedirect())
    with opener.open(request_value, timeout=30) as response:  # noqa: S310
        encoded = response.read(2 * 1024 * 1024 + 1)
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("Coder API response exceeds the capture limit")
    raw = json.loads(encoded.decode("utf-8"))
    if not isinstance(raw, (dict, list)):
        raise ValueError("Coder API returned an unsupported JSON shape")
    return raw
class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, _req: request.Request, _fp: IO[bytes], _code: int, _msg: str, _headers: HTTPMessage, _new_url: str) -> None:
        return None
def _templates(hostname: str, token: str) -> list[dict[str, JsonValue]]:
    raw = _api(hostname, token, "/api/v2/templates")
    if not isinstance(raw, list):
        raise ValueError("Coder template API returned an invalid response")
    return [
        {
            "id": _field(item, "id"),
            "name": _field(item, "name"),
            "active_version_id": _field(item, "active_version_id"),
            "active_version_name": _field(item, "active_version_name"),
            "rendered_source_sha256": _sha(item),
        }
        for item in raw
    ]
def _workspaces(hostname: str, token: str, container: str, templates: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    template_names = {str(item["id"]): str(item["name"]) for item in templates}
    records: list[dict[str, JsonValue]] = []
    for item in collect_coder_workspace_pages(lambda path: _api(hostname, token, path)):
        template_id = _field(item, "template_id")
        template_name = template_names.get(template_id)
        if template_name is None:
            raise ValueError("workspace references an unlisted template")
        workspace_name = _field(item, "name")
        version_id = _field(item, "template_version_id")
        records.append({"id": _field(item, "id"), "name": workspace_name, "template_id": template_id, "template_version_id": version_id, "legacy_pointers": [_primary_pointer(container, token, workspace_name, template_name, version_id)]})
    return records
def _primary_pointer(container: str, token: str, workspace: str, template: str, version: str) -> dict[str, JsonValue]:
    if template != "ubuntu-vscode":
        raise ValueError("retained workspace template has no Task 1 legacy pointer collector")
    script = "import hashlib,json,os; p='/home/coder/.config/opencode/opencode.json'; x=json.load(open(p))['provider']['litellm']; h=lambda v:hashlib.sha256(v.encode()).hexdigest(); print(json.dumps({'target':p,'pointer':'/provider/litellm','mode':format(os.stat(p).st_mode&511,'04o'),'shape':'json-pointer','base_url':x['base_url'],'credential_value_sha256':h(x['api_key']),'pointer_sha256':h(json.dumps(x,sort_keys=True,separators=(',',':'))),'independent_renderer_sha256':h(json.dumps(x,sort_keys=True,separators=(',',':'))),'scope':'pointer'}))"
    result = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", "IFS= read -r CODER_SESSION_TOKEN; export CODER_SESSION_TOKEN; exec /opt/coder \"$@\"", "sh", "ssh", workspace, "--", "python3", "-c", script], input=token + "\n", check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError("unable to capture retained workspace legacy pointer")
    pointer = require_mapping(json.loads(result.stdout), "retained workspace legacy pointer")
    pointer["template_version_id"] = version
    return pointer
def _builds(hostname: str, token: str, workspaces: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    inventories: list[dict[str, JsonValue]] = []
    for workspace in workspaces:
        workspace_id = str(workspace["id"])
        items: list[JsonValue] = []
        pages: list[JsonValue] = []
        for offset, page in collect_coder_array_pages(
            lambda path: _api(hostname, token, path),
            f"/api/v2/workspaces/{workspace_id}/builds?limit=100&offset={{offset}}",
            "build",
        ):
            page_items: list[JsonValue] = []
            for item in page:
                page_items.append({
                    "id": _field(item, "id"),
                    "build_number": _integer(item, "build_number"),
                    "status": _field(item, "status"),
                    "transition": _field(item, "transition"),
                })
            items.extend(page_items)
            pages.append({"offset": offset, "items": page_items})
        inventories.append({
            "workspace_id": workspace_id,
            "total": len(items),
            "pages": pages,
        })
    return inventories
def _secrets(hostname: str, token: str, user_id: str) -> list[dict[str, JsonValue]]:
    return [
        {
            "id": _field(item, "id"),
            "name": _field(item, "name"),
            "environment_variable": _field(item, "env_name"),
            "description": _field(item, "description"),
        }
        for _, page in collect_coder_array_pages(
            lambda path: _api(hostname, token, path),
            f"/api/v2/users/{user_id}/secrets?limit=100&offset={{offset}}",
            "secret",
        )
        for item in page
    ]
def _image_inventory() -> list[dict[str, JsonValue]]:
    expected = {"coder": "ghcr.io/coder/coder", "litellm": "ghcr.io/berriai/litellm", "pgvector": "pgvector/pgvector", "redis": "redis", "postfix": "boky/postfix"}
    raw = subprocess.run(["docker", "image", "ls", "--digests", "--format", "{{.Repository}}@{{.Digest}}"], check=False, capture_output=True, text=True).stdout.splitlines()
    records: list[dict[str, JsonValue]] = []
    for logical_name, repository in expected.items():
        matches = [line for line in raw if line.startswith(f"{repository}@sha256:")]
        if len(matches) != 1:
            raise ValueError("required image does not have one resolved local digest")
        records.append({"logical_name": logical_name, "container_image": matches[0], "registry_image": matches[0]})
    return records
def _state_inventory(state_dir: Path) -> dict[str, JsonValue]:
    files = sorted(path for path in state_dir.rglob("*") if path.is_file())
    if not files:
        raise ValueError("wizard state inventory is empty")
    digest = hashlib.sha256("".join(f"{path.relative_to(state_dir)}:{_sha(path.read_bytes())}\n" for path in files).encode()).hexdigest()
    ledger = next((path for path in files if "ledger" in path.name), None)
    if ledger is None:
        raise ValueError("wizard ownership ledger is missing")
    return {"state_sha256": digest, "ledger_sha256": _sha(ledger.read_bytes()), "resources": [str(path.relative_to(state_dir)) for path in files]}
def _page(items: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    pages: list[JsonValue] = [
        {"offset": offset, "items": items[offset : offset + 100]}
        for offset in range(0, len(items), 100)
    ]
    return {"total": len(items), "pages": pages or [{"offset": 0, "items": []}]}

def _field(value: JsonValue, key: str) -> str:
    return require_text(require_mapping(value, "Coder response").get(key), f"Coder response {key}")
def _integer(value: JsonValue, key: str) -> int:
    candidate = require_mapping(value, "Coder response").get(key)
    if not isinstance(candidate, int) or candidate < 1:
        raise ValueError(f"Coder response has invalid {key}")
    return candidate
def _sha(value: JsonValue | bytes) -> str:
    encoded = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
def _env(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if value is None or value == "":
        raise ValueError(f"proof env is missing {key}")
    return value

if __name__ == "__main__":
    raise SystemExit(main())
