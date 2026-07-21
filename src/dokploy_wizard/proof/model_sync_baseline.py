# ruff: noqa: E501, I001
"""Strict, redacted normalization of the Host A Coder baseline snapshot."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from typing import Final
from dokploy_wizard.proof.model_sync_artifacts import (
    CaptureSchemaError,
    JsonValue,
    parse_resource_planes,
    require_keys,
    require_list,
    require_mapping,
    require_sha256,
    require_text,
)
REQUIRED_TEMPLATE_NAMES: Final = (
    "ubuntu-vscode",
    "ubuntu-vscode-opencode-web",
    "ubuntu-vscode-openwork",
    "ubuntu-vscode-kdense-byok",
    "ubuntu-vscode-hermes",
    "ubuntu-vscode-pi-web",
)
_BUILD_STATUSES: Final = frozenset({"pending", "starting", "running", "stopping", "stopped", "failed", "canceled", "deleting", "deleted"})
_BUILD_TRANSITIONS: Final = frozenset({"start", "stop", "delete"})
_LEGACY_RULES: Final = {
    "ubuntu-vscode": ("pointer", "/home/coder/.config/opencode/opencode.json", "/provider/litellm", "0644", "json-pointer"),
    "ubuntu-vscode-opencode-web": ("pointer", "/home/coder/.config/opencode/opencode.json", "/provider/litellm", "0644", "json-pointer"),
    "ubuntu-vscode-openwork": ("pointer", "/home/coder/.config/opencode/opencode.json", "/provider/litellm", "0644", "json-pointer"),
    "ubuntu-vscode-kdense-byok": ("target-and-symlink", "/home/coder/.cache/kdense-byok-src/web/src/data/models.json", "/home/coder/.local/state/dokploy-wizard/model-sync/current", "0644", "json-target-and-symlink"),
}
BaselineCaptureError = CaptureSchemaError
@dataclass(frozen=True, slots=True)
class CapturedBaseline:
    """Value-free material required to write the Task 1 baseline and result payloads."""

    payload: dict[str, JsonValue]
    images: dict[str, str]
    coder_secret_inventory_sha256: str
    legacy_workspace_managed_fingerprints_sha256: str
def parse_captured_baseline(raw_snapshot: str, *, stack_name: str) -> CapturedBaseline:
    """Parse a fully paginated remote snapshot before any local artifact is finalized."""
    try:
        raw = json.loads(raw_snapshot)
    except json.JSONDecodeError as error:
        raise BaselineCaptureError("remote baseline snapshot is not valid JSON") from error
    snapshot = require_mapping(raw, "remote baseline snapshot")
    require_keys(snapshot, {"schema_version", "images", "wizard_state", "cloudflare", "tailscale", "coder"}, "snapshot")
    if snapshot["schema_version"] != 1:
        raise BaselineCaptureError("remote baseline snapshot schema is unsupported")
    resources = parse_resource_planes(snapshot)
    coder = require_mapping(snapshot["coder"], "coder")
    require_keys(coder, {"templates", "workspaces", "builds", "secrets"}, "coder")
    templates = _templates(coder["templates"])
    template_names = tuple(template["name"] for template in templates)
    if template_names != tuple(sorted(REQUIRED_TEMPLATE_NAMES)):
        raise BaselineCaptureError("baseline must contain exactly the six required Coder templates")
    workspaces = _workspaces(coder["workspaces"], templates, stack_name)
    _builds(coder["builds"], workspaces)
    secrets = _secrets(coder["secrets"])
    legacy: list[JsonValue] = []
    template_payload: list[JsonValue] = []
    workspace_payload: list[JsonValue] = []
    for workspace in workspaces:
        legacy.extend(require_list(workspace["legacy_fingerprints"], "legacy fingerprints"))
    for template in templates:
        template_payload.append(dict(template))
    for workspace in workspaces:
        workspace_payload.append({
            "id": workspace["id"],
            "name": workspace["name"],
            "template_id": workspace["template_id"],
            "template_version_id": workspace["template_version_id"],
            "legacy_fingerprints": workspace["legacy_fingerprints"],
        })
    payload: dict[str, JsonValue] = {
        "cloudflare": resources.cloudflare,
        "coder_secrets": secrets,
        "legacy_workspace_managed_fingerprints": legacy,
        "resource_inventories": {
            "tailscale": resources.tailscale,
            "wizard_state": resources.wizard_state,
        },
        "stack_name": stack_name,
        "templates": template_payload,
        "workspaces": workspace_payload,
    }
    return CapturedBaseline(
        payload=payload,
        images=resources.images,
        coder_secret_inventory_sha256=canonical_sha256(secrets),
        legacy_workspace_managed_fingerprints_sha256=canonical_sha256(legacy),
    )
def canonical_sha256(payload: JsonValue) -> str:
    """Fingerprint normalized JSON with deterministic bytes and no secret values."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def _templates(raw: JsonValue) -> tuple[dict[str, str], ...]:
    templates: list[dict[str, str]] = []
    for item in _pages(raw, "templates"):
        template = require_mapping(item, "template")
        require_keys(template, {"id", "name", "active_version_id", "active_version_name", "rendered_source_sha256"}, "template")
        templates.append({
            "active_version_id": require_text(template["active_version_id"], "template active version id"),
            "active_version_name": require_text(template["active_version_name"], "template active version name"),
            "id": require_text(template["id"], "template id"),
            "name": require_text(template["name"], "template name"),
            "rendered_source_sha256": require_sha256(template["rendered_source_sha256"], "template rendered source"),
        })
    _unique([template["id"] for template in templates], "template ids")
    _unique([template["name"] for template in templates], "template names")
    return tuple(sorted(templates, key=lambda item: item["name"]))
def _workspaces(raw: JsonValue, templates: tuple[dict[str, str], ...], stack_name: str) -> tuple[dict[str, JsonValue], ...]:
    template_records = {template["id"]: template for template in templates}
    workspaces: list[dict[str, JsonValue]] = []
    for item in _pages(raw, "workspaces"):
        workspace = require_mapping(item, "workspace")
        require_keys(workspace, {"id", "name", "template_id", "template_version_id", "legacy_pointers"}, "workspace")
        template_id = require_text(workspace["template_id"], "workspace template id")
        template = template_records.get(template_id)
        if template is None:
            raise BaselineCaptureError("workspace references an uncaptured Coder template")
        version_id = require_text(workspace["template_version_id"], "workspace template version id")
        pointers = _legacy_pointers(workspace["legacy_pointers"], template["name"], stack_name, version_id)
        workspaces.append({
            "id": require_text(workspace["id"], "workspace id"),
            "legacy_fingerprints": pointers,
            "name": require_text(workspace["name"], "workspace name"),
            "template_id": template_id,
            "template_version_id": version_id,
        })
    _unique([require_text(workspace["id"], "workspace id") for workspace in workspaces], "workspace ids")
    return tuple(sorted(workspaces, key=lambda item: require_text(item["id"], "workspace id")))
def _legacy_pointers(raw: JsonValue, template_name: str, stack_name: str, version_id: str) -> list[dict[str, JsonValue]]:
    pointers: list[dict[str, JsonValue]] = []
    rule = _LEGACY_RULES.get(template_name)
    if rule is None:
        raise BaselineCaptureError("workspace template has no supported legacy renderer contract")
    expected_scope, expected_target, expected_pointer, expected_mode, expected_shape = rule
    expected_base = f"http://{stack_name}-shared-litellm:4000/v1"
    entries = require_list(raw, "legacy pointers")
    if not entries:
        raise BaselineCaptureError("workspace has no independently rendered legacy fingerprint")
    seen: set[tuple[str, str]] = set()
    for item in entries:
        pointer = require_mapping(item, "legacy pointer")
        keys = {"template_version_id", "target", "pointer", "mode", "shape", "base_url", "credential_value_sha256", "pointer_sha256", "independent_renderer_sha256", "scope"}
        if expected_scope == "target-and-symlink":
            keys.update({"target_sha256", "symlink_state", "symlink_target", "symlink_sha256", "renderer_source_revision", "renderer_source_path"})
        require_keys(pointer, keys, "legacy pointer")
        if require_text(pointer["scope"], "legacy pointer scope") != expected_scope:
            raise BaselineCaptureError("legacy pointer scope does not match its template contract")
        if require_text(pointer["base_url"], "legacy pointer base URL") != expected_base:
            raise BaselineCaptureError("legacy pointer base URL does not match resolved LiteLLM")
        target, path = require_text(pointer["target"], "legacy pointer target"), require_text(pointer["pointer"], "legacy pointer path")
        if (target, path, require_text(pointer["mode"], "legacy pointer mode"), require_text(pointer["shape"], "legacy pointer shape")) != (expected_target, expected_pointer, expected_mode, expected_shape) or require_text(pointer["template_version_id"], "legacy pointer version") != version_id or (target, path) in seen:
            raise BaselineCaptureError("legacy pointer does not match its captured template renderer contract")
        seen.add((target, path))
        pointer_sha = require_sha256(pointer["pointer_sha256"], "legacy pointer")
        renderer_sha = require_sha256(pointer["independent_renderer_sha256"], "legacy renderer")
        exact = pointer_sha == renderer_sha
        normalized: dict[str, JsonValue] = {
            "base_url": expected_base,
            "credential_value_sha256": require_sha256(pointer["credential_value_sha256"], "legacy credential"),
            "independent_renderer_sha256": renderer_sha,
            "legacy_exact": exact,
            "mode": expected_mode,
            "pointer": path,
            "pointer_sha256": pointer_sha,
            "scope": expected_scope,
            "shape": expected_shape,
            "target": target,
            "template_version_id": version_id,
        }
        if expected_scope == "target-and-symlink":
            target_sha = require_sha256(pointer["target_sha256"], "legacy target")
            revision, source_path = require_text(pointer["renderer_source_revision"], "legacy renderer revision"), require_text(pointer["renderer_source_path"], "legacy renderer path")
            if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision) or source_path != "web/src/data/models.json":
                raise BaselineCaptureError("legacy renderer source binding is invalid")
            state = require_text(pointer["symlink_state"], "legacy symlink state")
            if state not in {"present", "absent"}:
                raise BaselineCaptureError("legacy symlink state is invalid")
            symlink_target = pointer["symlink_target"]
            symlink_sha = pointer["symlink_sha256"]
            if state == "present":
                symlink_target = require_text(symlink_target, "legacy symlink target")
                symlink_sha = require_sha256(symlink_sha, "legacy symlink")
            elif symlink_target is not None or symlink_sha is not None:
                raise BaselineCaptureError("absent legacy symlink must not have target evidence")
            if pointer_sha != canonical_sha256({"base_url": expected_base, "credential_value_sha256": require_sha256(pointer["credential_value_sha256"], "legacy credential"), "symlink_sha256": symlink_sha, "target_sha256": target_sha}):
                raise BaselineCaptureError("legacy target and symlink aggregate hash is invalid")
            normalized.update({"legacy_exact": exact and state == "present", "renderer_source_path": source_path, "renderer_source_revision": revision, "symlink_sha256": symlink_sha, "symlink_state": state, "symlink_target": symlink_target, "target_sha256": target_sha})
        pointers.append(normalized)
    return pointers
def _builds(raw: JsonValue, workspaces: tuple[dict[str, JsonValue], ...]) -> None:
    expected_ids = {require_text(workspace["id"], "workspace id") for workspace in workspaces}
    seen: set[str] = set()
    for value in require_list(raw, "build inventories"):
        inventory = require_mapping(value, "build inventory")
        require_keys(inventory, {"workspace_id", "total", "pages"}, "build inventory")
        workspace_id = require_text(inventory["workspace_id"], "build workspace id")
        if workspace_id not in expected_ids or workspace_id in seen:
            raise BaselineCaptureError("build inventories must cover each retained workspace exactly once")
        seen.add(workspace_id)
        for build in _pages(
            {"total": inventory["total"], "pages": inventory["pages"]},
            f"builds for {workspace_id}",
        ):
            item = require_mapping(build, "build")
            require_keys(item, {"id", "build_number", "status", "transition"}, "build")
            if item["status"] not in _BUILD_STATUSES or item["transition"] not in _BUILD_TRANSITIONS:
                raise BaselineCaptureError("Coder build has an unrecognized status or transition")
            require_text(item["id"], "build id")
            if not isinstance(item["build_number"], int) or item["build_number"] < 1:
                raise BaselineCaptureError("Coder build number must be positive")
    if seen != expected_ids:
        raise BaselineCaptureError("build inventory is incomplete")
def _secrets(raw: JsonValue) -> list[dict[str, str]]:
    secrets: list[dict[str, str]] = []
    for value in _pages(raw, "secrets"):
        secret = require_mapping(value, "Coder secret")
        require_keys(secret, {"id", "name", "environment_variable", "description"}, "Coder secret")
        secrets.append({key: require_text(secret[key], f"Coder secret {key}") for key in sorted(secret)})
    _unique([secret["id"] for secret in secrets], "Coder secret ids")
    _unique([secret["name"] for secret in secrets], "Coder secret names")
    return sorted(secrets, key=lambda item: item["id"])
def _pages(raw: JsonValue, label: str) -> list[JsonValue]:
    page_set = require_mapping(raw, label)
    require_keys(page_set, {"total", "pages"}, label)
    total = page_set["total"]
    if not isinstance(total, int) or total < 0:
        raise BaselineCaptureError(f"{label} total must be a non-negative integer")
    items: list[JsonValue] = []
    offset = 0
    for page_raw in require_list(page_set["pages"], f"{label} pages"):
        page = require_mapping(page_raw, f"{label} page")
        require_keys(page, {"offset", "items"}, f"{label} page")
        if page["offset"] != offset:
            raise BaselineCaptureError(f"{label} pagination is incomplete or out of order")
        page_items = require_list(page["items"], f"{label} page items")
        if len(page_items) > 100:
            raise BaselineCaptureError(f"{label} page exceeds the bounded API limit")
        items.extend(page_items)
        offset += len(page_items)
    if offset != total:
        raise BaselineCaptureError(f"{label} pagination is incomplete")
    return items
def _unique(values: list[str], label: str) -> list[str]:
    if len(values) != len(set(values)):
        raise BaselineCaptureError(f"{label} must be unique")
    return values
