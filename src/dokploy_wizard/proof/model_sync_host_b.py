# ruff: noqa: E501, I001
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Final, Sequence
from urllib import request
from dokploy_wizard.dokploy.coder import _coder_container_name, _litellm_internal_base_url, _litellm_workspace_fallback_models_json
from dokploy_wizard.proof.model_sync_artifacts import CaptureSchemaError, JsonValue, normalize_image_repository, registry_manifest_digest, require_digest, require_list, require_mapping, require_sha256, require_text
from dokploy_wizard.proof.model_sync_env import resolve_proof_namespace
from dokploy_wizard.proof.model_sync_remote import RemoteProbe, capture_local_authoritative_inventory
from dokploy_wizard.proof.model_sync_results import HostIdentity as HostIdentity, assert_followup_proof_contract as assert_followup_proof_contract, assert_namespace_identity as assert_namespace_identity, collect_coder_array_pages, collect_coder_workspace_pages, run_bounded_process
from dokploy_wizard.state import load_litellm_generated_keys, parse_env_file, resolve_desired_state
_OUTPUT_LIMIT: Final = 2 * 1024 * 1024
_MODEL_LIMIT: Final = 1_000
_IMAGE_IDENTIFIERS: Final = (re.compile(r"^[0-9a-f]{12,64}$"), re.compile(r"^sha256:[0-9a-f]{64}$"))
_IMAGE_SPECS: Final = (("coder", "-coder"), ("litellm", "-shared-litellm"), ("pgvector", "-shared-postgres"), ("redis", "-shared-redis"), ("postfix", "-shared-postfix"))
_OPENCODE_TEMPLATES: Final = frozenset({"ubuntu-vscode", "ubuntu-vscode-opencode-web", "ubuntu-vscode-openwork"})
_KDENSE_TEMPLATE: Final = "ubuntu-vscode-kdense-byok"
_KDENSE_CATALOG: Final = (("Unsloth Active (local alias)", "local-model.internal/unsloth-active"), ("Claude Opus 4.7", "openrouter/anthropic/claude-opus-4.7"), ("Claude Sonnet 4.6", "openrouter/anthropic/claude-sonnet-4.6"), ("GPT-5.4 Pro", "openrouter/openai/gpt-5.4-pro"), ("GPT-5.4", "openrouter/openai/gpt-5.4"), ("GPT-5.4 Mini", "openrouter/openai/gpt-5.4-mini"), ("GPT-5.4 Nano", "openrouter/openai/gpt-5.4-nano"), ("Grok 4.20 Beta", "openrouter/x-ai/grok-4.20-beta"), ("Gemini 3.1 Pro Preview", "openrouter/google/gemini-3.1-pro-preview"), ("Gemini 3 Flash Preview", "openrouter/google/gemini-3-flash-preview"), ("Gemini 3.1 Flash Lite Preview", "openrouter/google/gemini-3.1-flash-lite-preview"), ("Qwen3 Max Thinking", "openrouter/qwen/qwen3-max-thinking"), ("Qwen3 Coder Next", "openrouter/qwen/qwen3-coder-next"), ("GLM 5 Turbo", "openrouter/z-ai/glm-5-turbo"), ("GLM 5", "openrouter/z-ai/glm-5"), ("MiniMax M2.5", "openrouter/minimax/minimax-m2.5"), ("MiniMax M2.5 (free)", "openrouter/minimax/minimax-m2.5:free"), ("Kimi K2.5", "openrouter/moonshotai/kimi-k2.5"), ("Nemotron 3 Super", "openrouter/nvidia/nemotron-3-super-120b-a12b"), ("Nemotron 3 Nano Omni (free)", "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"))
@dataclass(frozen=True, slots=True, repr=False)
class LegacyRenderer:
    base_url: str
    credential: str
    default_alias: str
    fallback_models: tuple[str, ...]
    model_inventory: tuple[str, ...]
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
    images = _image_inventory(desired.stack_name)
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
    workspaces = _workspaces(desired.hostnames["coder"], token, container, templates, raw_env, state_dir, desired.stack_name)
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
    headers = {"Accept": "application/json", "Host": hostname, **({"Coder-Session-Token": token} if token is not None else {})}
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
    return [{"id": _field(item, "id"), "name": _field(item, "name"), "active_version_id": _field(item, "active_version_id"), "active_version_name": _field(item, "active_version_name"), "rendered_source_sha256": _sha(item)} for item in raw]
def _workspaces(hostname: str, token: str, container: str, templates: list[dict[str, JsonValue]], raw_env: dict[str, str], state_dir: Path, stack_name: str) -> list[dict[str, JsonValue]]:
    template_records = {require_text(item["id"], "template id"): item for item in templates}
    records: list[dict[str, JsonValue]] = []
    for item in collect_coder_workspace_pages(lambda path: _api(hostname, token, path)):
        template_id = _field(item, "template_id")
        template = template_records.get(template_id)
        if template is None:
            raise ValueError("workspace references an unlisted template")
        workspace_name = _field(item, "name")
        version_id = _field(item, "template_version_id")
        template_name = require_text(template["name"], "template name")
        renderer = _legacy_renderer(raw_env, state_dir, container, token, workspace_name, stack_name, template_name)
        records.append({"id": _field(item, "id"), "name": workspace_name, "template_id": template_id, "template_version_id": version_id, "legacy_pointers": [_primary_pointer(container, token, workspace_name, template_name, version_id, renderer)]})
    return records
def _primary_pointer(container: str, token: str, workspace: str, template: str, version: str, renderer: LegacyRenderer) -> dict[str, JsonValue]:
    source: dict[str, JsonValue] = {}
    if template in _OPENCODE_TEMPLATES:
        script = "const f=require('fs'),c=require('crypto'),p='/home/coder/.config/opencode/opencode.json',s=v=>Array.isArray(v)?v.map(s):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,s(v[k])])):v,h=v=>c.createHash('sha256').update(typeof v==='string'?v:JSON.stringify(s(v))).digest('hex'),x=JSON.parse(f.readFileSync(p)).provider.litellm,o=x.options,r={target:p,pointer:'/provider/litellm',mode:(f.statSync(p).mode&511).toString(8).padStart(4,'0'),shape:'json-pointer',base_url:o.baseURL,credential_value_sha256:h(o.apiKey),pointer_sha256:h(x),scope:'pointer'},b=Buffer.from(JSON.stringify(r));if(b.length>Number(process.argv[1]))process.exit(2);process.stdout.write(b)"
        expected = _sha(_render_legacy_pointer(renderer))
    elif template == _KDENSE_TEMPLATE:
        script = "const f=require('fs'),c=require('crypto'),p='/home/coder/.cache/kdense-byok-src/web/src/data/models.json',l='/home/coder/.local/state/dokploy-wizard/model-sync/current',e=Object.fromEntries(f.readFileSync('/home/coder/.cache/kdense-byok-src/.env','utf8').split('\\n').filter(x=>x.includes('=')).map(x=>{let i=x.indexOf('=');return[x.slice(0,i),x.slice(i+1)]})),s=v=>Array.isArray(v)?v.map(s):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,s(v[k])])):v,h=v=>c.createHash('sha256').update(typeof v==='string'?v:JSON.stringify(s(v))).digest('hex'),x=JSON.parse(f.readFileSync(p)),t=h(x),k=h(e.OPENAI_API_KEY);let z=null,y=null,q='absent';try{if(f.lstatSync(l).isSymbolicLink()){y=f.readlinkSync(l);z=h(y);q='present'}}catch(a){if(a.code!=='ENOENT')throw a}const a=h({base_url:e.OPENAI_API_BASE,credential_value_sha256:k,symlink_sha256:z,target_sha256:t}),r={target:p,pointer:l,mode:(f.statSync(p).mode&511).toString(8).padStart(4,'0'),shape:'json-target-and-symlink',base_url:e.OPENAI_API_BASE,credential_value_sha256:k,target_sha256:t,symlink_state:q,symlink_target:y,symlink_sha256:z,pointer_sha256:a,scope:'target-and-symlink'},b=Buffer.from(JSON.stringify(r));if(b.length>Number(process.argv[1]))process.exit(2);process.stdout.write(b)"
        renderer_script = "const f=require('fs'),c=require('crypto'),cp=require('child_process'),d=process.argv[1],ep=process.argv[2],m=Number(process.argv[3]),n=Number(process.argv[4]),path='web/src/data/models.json',run=a=>cp.execFileSync('git',['-C',d,...a],{encoding:'utf8',maxBuffer:m}).trim(),s=v=>Array.isArray(v)?v.map(s):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,s(v[k])])):v,h=v=>c.createHash('sha256').update(typeof v==='string'?v:JSON.stringify(s(v))).digest('hex'),clear=x=>{const y={...x};delete y.default;delete y.expertDefault;return y};if(!f.existsSync(d+'/.git')||run(['remote','get-url','origin'])!=='https://github.com/K-Dense-AI/k-dense-byok.git')process.exit(2);const rev=run(['rev-parse','HEAD']),up=run(['rev-parse','refs/remotes/origin/main']);if(!/^[0-9a-f]{40}$/.test(rev)||rev!==up)process.exit(2);run(['cat-file','-e',rev+':'+path]);const raw=cp.execFileSync('git',['-C',d,'show',rev+':'+path],{maxBuffer:m});if(raw.length>m)process.exit(2);const models=JSON.parse(raw),cfg=JSON.parse(f.readFileSync(0,'utf8')),env=Object.fromEntries(f.readFileSync(ep,'utf8').split('\\n').filter(x=>x.includes('=')).map(x=>{const i=x.indexOf('=');return[x.slice(0,i),x.slice(i+1)]}));if(!Array.isArray(models)||models.length>n||!env.DEFAULT_AGENT_MODEL||!env.DEFAULT_EXPERT_MODEL)process.exit(2);const open=Object.fromEntries(models.filter(x=>String(x.id||'').startsWith('openrouter/')).map(x=>[String(x.id),clear(x)])),merged=cfg.catalog.filter(x=>String(x.value||'').trim().startsWith('openrouter/')).map(x=>{const v=String(x.value).trim(),label=String(x.name||'').trim(),y=Object.keys(open[v]||{}).length?clear(open[v]):{id:v,label:label||v.slice(11),provider:'OpenRouter'};y.id='openai/'+v.slice(11);if(label)y.label=label;y.provider='OpenCode Go';const z=String(y.description||'').trim();y.description=(z?z+'\\n\\n':'')+'Available through the central LiteLLM OpenCode Go-compatible gateway.';return y}),seen=new Set(),out=[];for(const x of merged){const id=String(x.id||'');if(!id||seen.has(id))continue;seen.add(id);if(id===env.DEFAULT_AGENT_MODEL)x.default=true;if(id===env.DEFAULT_EXPERT_MODEL)x.expertDefault=true;out.push(x)}if(out.length&&!out.some(x=>x.default))out[0].default=true;if(out.length&&!out.some(x=>x.expertDefault))(out.find(x=>x.default)||out[0]).expertDefault=true;const target=h(out),expected=h({base_url:cfg.base_url,credential_value_sha256:h(cfg.credential),symlink_sha256:h('/home/coder/.cache/kdense-byok-src/web/src/data/models.json'),target_sha256:target}),result=Buffer.from(JSON.stringify({independent_renderer_sha256:expected,renderer_source_path:path,renderer_source_revision:rev}));if(result.length>m)process.exit(2);process.stdout.write(result)"
        payload = json.dumps({"base_url": renderer.base_url, "credential": renderer.credential, "catalog": [{"name": name, "value": value} for name, value in _KDENSE_CATALOG]})
        rendered = require_mapping(_workspace_json(container, token, workspace, renderer_script, payload, ("/home/coder/.cache/kdense-byok-src", "/home/coder/.cache/kdense-byok-src/.env", str(_OUTPUT_LIMIT), str(_MODEL_LIMIT)), "K-Dense independent renderer"), "K-Dense independent renderer")
        expected = require_sha256(rendered["independent_renderer_sha256"], "K-Dense independent renderer")
        source = {"renderer_source_revision": require_text(rendered["renderer_source_revision"], "K-Dense renderer revision"), "renderer_source_path": "web/src/data/models.json"}
    else:
        raise ValueError("retained workspace template has no Task 1 legacy pointer collector")
    pointer = require_mapping(_workspace_json(container, token, workspace, script, "", (str(_OUTPUT_LIMIT),), "legacy pointer"), "retained workspace legacy pointer")
    pointer["independent_renderer_sha256"] = expected
    pointer["template_version_id"] = version
    pointer.update(source)
    return pointer
def _legacy_renderer(raw_env: dict[str, str], state_dir: Path, container: str, token: str, workspace: str, stack_name: str, template: str) -> LegacyRenderer:
    keys = load_litellm_generated_keys(state_dir)
    consumer = "coder-kdense" if template == _KDENSE_TEMPLATE else "coder-hermes"
    credential = None if keys is None else keys.virtual_keys.get(consumer)
    if credential is None or credential == "":
        raise ValueError("expected Coder LiteLLM credential is unavailable")
    provider = raw_env.get("AI_DEFAULT_PROVIDER", "").strip().lower() or "opencode-go"
    provider = "opencode-go" if provider == "opencode" else provider
    model = raw_env.get("AI_DEFAULT_MODEL", "").strip() or "deepseek-v4-flash"
    default_alias = model if model.startswith(f"{provider}/") else f"{provider}/{model}"
    fallbacks = tuple(require_text(item, "expected fallback model") for item in require_list(json.loads(_litellm_workspace_fallback_models_json(default_alias=default_alias)), "expected fallback models"))
    return LegacyRenderer(_litellm_internal_base_url(stack_name), credential, default_alias, fallbacks, () if template == _KDENSE_TEMPLATE else _model_inventory(container, token, workspace, credential, stack_name))
def _model_inventory(container: str, token: str, workspace: str, credential: str, stack_name: str) -> tuple[str, ...]:
    script = "const f=require('fs'),h=require('http'),k=f.readFileSync(0,'utf8'),m=Number(process.argv[2]),n=Number(process.argv[3]),r=h.get(process.argv[1],{headers:{Accept:'application/json',Authorization:'Bearer '+k}},x=>{let z=0,a=[];x.on('data',b=>{z+=b.length;if(z>m)r.destroy();else a.push(b)});x.on('end',()=>{if(z>m)return;let p;try{p=JSON.parse(Buffer.concat(a))}catch(e){process.exit(2)}const d=p&&p.data;if(!Array.isArray(d)||d.length>n)process.exit(2);const o=Buffer.from(JSON.stringify(d));if(o.length>m)process.exit(2);process.stdout.write(o)})});r.setTimeout(5000,()=>r.destroy());r.on('error',()=>process.exit(2))"
    values = require_list(_workspace_json(container, token, workspace, script, credential, (f"{_litellm_internal_base_url(stack_name)}/v1/models", str(_OUTPUT_LIMIT), str(_MODEL_LIMIT)), "model inventory"), "expected LiteLLM model inventory")
    if len(values) > _MODEL_LIMIT:
        raise ValueError("expected LiteLLM model inventory exceeds the record limit")
    models: list[str] = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(candidate := item.get("id"), str):
            continue
        model = candidate.strip()
        if model and "/" in model and not model.endswith("/*") and not model.startswith("openai/") and model not in models:
            models.append(model)
    if not models:
        raise ValueError("expected LiteLLM model inventory is invalid")
    return tuple(models)
def _render_legacy_pointer(renderer: LegacyRenderer) -> dict[str, JsonValue]:
    if not renderer.base_url or not renderer.credential or not renderer.default_alias or not renderer.model_inventory:
        raise ValueError("expected legacy renderer inputs are incomplete")
    models = list(dict.fromkeys((*renderer.model_inventory, *renderer.fallback_models)))
    if renderer.default_alias not in models:
        models.insert(0, renderer.default_alias)
    return {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": renderer.base_url, "apiKey": renderer.credential}, "models": {model: {} for model in models}}
def _workspace_json(container: str, token: str, workspace: str, script: str, payload: str, args: tuple[str, ...], label: str) -> JsonValue:
    command = ["docker", "exec", "-i", container, "sh", "-c", "IFS= read -r CODER_SESSION_TOKEN; export CODER_SESSION_TOKEN; exec /opt/coder \"$@\"", "sh", "ssh", workspace, "--", "node", "-e", script, *args]
    try:
        raw = json.loads(run_bounded_process(command, stdin=(token + "\n" + payload).encode(), output_limit=_OUTPUT_LIMIT, timeout_seconds=30, label=f"retained workspace {label}"))
    except (RuntimeError, ValueError) as error:
        raise ValueError(f"unable to capture retained workspace {label}") from error
    if not isinstance(raw, (dict, list)):
        raise ValueError(f"retained workspace {label} has invalid JSON shape")
    return raw
def _builds(hostname: str, token: str, workspaces: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    inventories: list[dict[str, JsonValue]] = []
    for workspace in workspaces:
        workspace_id = str(workspace["id"])
        items: list[JsonValue] = []
        pages: list[JsonValue] = []
        path = f"/api/v2/workspaces/{workspace_id}/builds?limit=100&offset={{offset}}"
        for offset, page in collect_coder_array_pages(lambda path: _api(hostname, token, path), path, "build"):
            page_items: list[JsonValue] = [{"id": _field(item, "id"), "build_number": _integer(item, "build_number"), "status": _field(item, "status"), "transition": _field(item, "transition")} for item in page]
            items.extend(page_items)
            pages.append({"offset": offset, "items": page_items})
        inventories.append({"workspace_id": workspace_id, "total": len(items), "pages": pages})
    return inventories
def _secrets(hostname: str, token: str, user_id: str) -> list[dict[str, JsonValue]]:
    path = f"/api/v2/users/{user_id}/secrets?limit=100&offset={{offset}}"
    return [{"id": _field(item, "id"), "name": _field(item, "name"), "environment_variable": _field(item, "env_name"), "description": _field(item, "description")} for _, page in collect_coder_array_pages(lambda path: _api(hostname, token, path), path, "secret") for item in page]
def _image_inventory(stack_name: str) -> list[dict[str, JsonValue]]:
    records: list[dict[str, JsonValue]] = []
    for logical_name, suffix in _IMAGE_SPECS:
        label = f"{logical_name} running container"
        raw_id = run_bounded_process(["docker", "ps", "--filter", f"label=com.docker.compose.service={stack_name}{suffix}", "--filter", "status=running", "--format", "{{.ID}}"], stdin=b"", output_limit=_OUTPUT_LIMIT, timeout_seconds=60, label=label)
        identifiers = raw_id.decode("utf-8").splitlines()
        if len(identifiers) != 1 or _IMAGE_IDENTIFIERS[0].fullmatch(identifiers[0]) is None:
            raise ValueError(f"{label} must resolve exactly once")
        container = _docker_object(["docker", "inspect", "--type", "container", identifiers[0]], f"{logical_name} running container inspect")
        config = require_mapping(container.get("Config"), f"{logical_name} container config")
        reference = require_text(config.get("Image"), f"{logical_name} container image reference")
        repository = normalize_image_repository(reference, f"{logical_name} container image reference")
        image_id = require_text(container.get("Image"), f"{logical_name} container image ID")
        if _IMAGE_IDENTIFIERS[1].fullmatch(image_id) is None:
            raise ValueError(f"{logical_name} container image ID is invalid")
        image = _docker_object(["docker", "image", "inspect", image_id], f"{logical_name} local image inspect")
        try:
            digests = [require_digest(value, f"{logical_name} local image digest") for value in require_list(image.get("RepoDigests"), f"{logical_name} local image digests")]
        except CaptureSchemaError as error:
            raise ValueError(f"{logical_name} local image digest is invalid") from error
        matches = [digest for digest in digests if digest.partition("@")[0] == repository]
        if len(matches) != 1:
            raise ValueError(f"{logical_name} local image digest is missing or ambiguous")
        raw_registry = run_bounded_process(["docker", "buildx", "imagetools", "inspect", "--raw", matches[0]], stdin=b"", output_limit=_OUTPUT_LIMIT, timeout_seconds=60, label=f"{logical_name} registry image")
        registry = registry_manifest_digest(raw_registry, repository, f"{logical_name} registry manifest")
        if matches[0] != registry:
            raise ValueError(f"{logical_name} container and registry image digests disagree")
        records.append({"logical_name": logical_name, "container_image": matches[0], "registry_image": registry})
    return records
def _docker_object(command: list[str], label: str) -> dict[str, JsonValue]:
    values = require_list(json.loads(run_bounded_process(command, stdin=b"", output_limit=_OUTPUT_LIMIT, timeout_seconds=60, label=label)), label)
    if len(values) != 1:
        raise ValueError(f"{label} must return exactly one object")
    return require_mapping(values[0], label)
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
    return {"total": len(items), "pages": [{"offset": offset, "items": items[offset : offset + 100]} for offset in range(0, len(items), 100)] or [{"offset": 0, "items": []}]}
def _field(value: JsonValue, key: str) -> str:
    return require_text(require_mapping(value, "Coder response").get(key), f"Coder response {key}")
def _integer(value: JsonValue, key: str) -> int:
    candidate = require_mapping(value, "Coder response").get(key)
    if not isinstance(candidate, int) or candidate < 1:
        raise ValueError(f"Coder response has invalid {key}")
    return candidate
def _sha(value: JsonValue | bytes) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def _env(values: dict[str, str], key: str) -> str:
    return require_text(values.get(key), f"proof env {key}")
if __name__ == "__main__":
    raise SystemExit(main())
