"""Minimal deployed Nexa queue worker sidecar."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import subprocess
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib import error, parse, request
from xml.etree import ElementTree
import zipfile
from io import BytesIO
from typing import Any

from dokploy_wizard.state.store import DurableQueueStore

from .nexa_onlyoffice import NexaOnlyofficeAgentIdentity
from .nexa_retrieval import NexaCanonicalFileSnapshot
from .nexa_runtime import (
    NexaNextcloudFileCreateRequest,
    NexaNextcloudFileCreateResult,
    NexaOnlyofficeActionResult,
    NexaPlannedTalkReply,
    NexaRuntimeDependencies,
    NexaTerminalCommandResult,
    _diff_for_new_file,
    run_queued_nexa_job,
)
from .nexa_scope import NexaScopeContext

LOGGER = logging.getLogger("dokploy_wizard.nexa_runtime_sidecar")

_DEFAULT_POLL_SECONDS = 5.0
_DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 60.0
_DEFAULT_RUNTIME_CONTRACT_PATH = "/mnt/openclaw/.nexa/runtime-contract.json"
_DEFAULT_WORKSPACE_CONTRACT_PATH = "/mnt/openclaw/workspace/nexa/contract.json"
_DEFAULT_STATE_DIR = "/mnt/openclaw/.nexa/state"
_SUPPORTED_WORKER_MODE = "queue"
_DEFAULT_HTTP_TIMEOUT_SECONDS = 15
_DEFAULT_OPENCLAW_PASS_THROUGH_TIMEOUT_SECONDS = 90

_ENV_NEXTCLOUD_BASE_URL = "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL"
_ENV_TALK_SHARED_SECRET = "OPENCLAW_NEXA_TALK_SHARED_SECRET"
_ENV_TALK_SIGNING_SECRET = "OPENCLAW_NEXA_TALK_SIGNING_SECRET"
_ENV_WEBDAV_AUTH_USER = "OPENCLAW_NEXA_WEBDAV_AUTH_USER"
_ENV_WEBDAV_AUTH_PASSWORD = "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD"
_ENV_AGENT_USER_ID = "OPENCLAW_NEXA_AGENT_USER_ID"
_ENV_AGENT_DISPLAY_NAME = "OPENCLAW_NEXA_AGENT_DISPLAY_NAME"
_ENV_AGENT_PASSWORD = "OPENCLAW_NEXA_AGENT_PASSWORD"
_ENV_OPENCLAW_INTERNAL_URL = "DOKPLOY_WIZARD_OPENCLAW_INTERNAL_URL"
_ENV_PLANNER_MODEL = "DOKPLOY_WIZARD_NEXA_PLANNER_MODEL"
_ENV_PLANNER_MODEL_PROVIDER = "DOKPLOY_WIZARD_NEXA_PLANNER_MODEL_PROVIDER"
_ENV_PLANNER_LOCAL_BASE_URL = "DOKPLOY_WIZARD_NEXA_PLANNER_LOCAL_BASE_URL"
_ENV_PLANNER_LOCAL_API_KEY = "DOKPLOY_WIZARD_NEXA_PLANNER_LOCAL_API_KEY"
_ENV_PLANNER_OPENROUTER_API_KEY = "DOKPLOY_WIZARD_NEXA_PLANNER_OPENROUTER_API_KEY"
_ENV_PLANNER_NVIDIA_API_KEY = "DOKPLOY_WIZARD_NEXA_PLANNER_NVIDIA_API_KEY"
_ENV_PLANNER_NVIDIA_BASE_URL = "DOKPLOY_WIZARD_NEXA_PLANNER_NVIDIA_BASE_URL"
_ENV_PLANNER_OPENROUTER_BASE_URL = "DOKPLOY_WIZARD_NEXA_PLANNER_OPENROUTER_BASE_URL"

_DAV_NAMESPACE = "DAV:"
_OWNCLOUD_NAMESPACE = "http://owncloud.org/ns"
_NEXTCLOUD_NAMESPACE = "http://nextcloud.org/ns"
_XML_NAMESPACES = {
    "d": _DAV_NAMESPACE,
    "oc": _OWNCLOUD_NAMESPACE,
    "nc": _NEXTCLOUD_NAMESPACE,
}


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("DOKPLOY_WIZARD_NEXA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker_mode = os.environ.get("DOKPLOY_WIZARD_NEXA_WORKER_MODE", _SUPPORTED_WORKER_MODE)
    if worker_mode != _SUPPORTED_WORKER_MODE:
        msg = f"Unsupported Nexa worker mode '{worker_mode}'."
        raise RuntimeError(msg)

    runtime_contract_path = Path(
        os.environ.get(
            "DOKPLOY_WIZARD_NEXA_RUNTIME_CONTRACT_PATH",
            _DEFAULT_RUNTIME_CONTRACT_PATH,
        )
    )
    workspace_contract_path = Path(
        os.environ.get(
            "DOKPLOY_WIZARD_NEXA_WORKSPACE_CONTRACT_PATH",
            _DEFAULT_WORKSPACE_CONTRACT_PATH,
        )
    )
    state_dir = Path(os.environ.get("DOKPLOY_WIZARD_NEXA_STATE_DIR", _DEFAULT_STATE_DIR))
    state_dir.mkdir(parents=True, exist_ok=True)
    _wait_for_contracts(
        runtime_contract_path=runtime_contract_path,
        workspace_contract_path=workspace_contract_path,
    )

    store = DurableQueueStore(state_dir)
    _ensure_nexa_openclaw_session(os.environ, state_dir=state_dir)
    dependencies = _runtime_dependencies_from_env(os.environ)
    worker_id = os.environ.get("DOKPLOY_WIZARD_NEXA_WORKER_ID", socket.gethostname())
    poll_seconds = _float_env("DOKPLOY_WIZARD_NEXA_POLL_SECONDS", _DEFAULT_POLL_SECONDS)
    LOGGER.info(
        "Nexa runtime sidecar online: %s",
        json.dumps(
            {
                "runtime_contract_path": str(runtime_contract_path),
                "state_dir": str(state_dir),
                "worker_id": worker_id,
                "worker_mode": worker_mode,
                "workspace_contract_path": str(workspace_contract_path),
            },
            sort_keys=True,
        ),
    )
    _update_presence(os.environ, status="online", message="Ready")
    last_presence_at = time.monotonic()

    while True:
        leased_job = store.lease_next_job(
            lease_owner=f"nexa-runtime:{worker_id}",
            now=datetime.now(UTC),
        )
        if leased_job is None:
            try:
                queued = _poll_talk_messages(store=store, env=os.environ, now=datetime.now(UTC))
            except RuntimeError:
                LOGGER.warning("Nexa Talk polling failed", exc_info=True)
                queued = 0
            if time.monotonic() - last_presence_at >= 240:
                _update_presence(os.environ, status="online", message="Ready")
                last_presence_at = time.monotonic()
            if queued == 0:
                time.sleep(poll_seconds)
            continue
        _update_presence(os.environ, status="away", message=_presence_message_for_job(leased_job))
        last_presence_at = time.monotonic()
        result = run_queued_nexa_job(
            leased_job,
            store=store,
            env=os.environ,
            dependencies=dependencies,
            now=datetime.now(UTC),
        )
        _update_presence(os.environ, status="online", message="Ready")
        last_presence_at = time.monotonic()
        LOGGER.info(
            "Processed Nexa job: %s",
            json.dumps(
                {
                    "completed_at": result.completed_at,
                    "error_message": result.error_message,
                    "job_id": result.job_id,
                    "job_kind": result.job_kind,
                    "status": result.status,
                },
                sort_keys=True,
            ),
        )


def _ensure_nexa_openclaw_session(
    env: dict[str, str] | os._Environ[str],
    *,
    state_dir: Path,
) -> None:
    openclaw_root = state_dir.parent.parent
    session_dir = openclaw_root / "agents" / "nexa" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_index = session_dir / "sessions.json"
    if session_index.exists():
        try:
            data = json.loads(session_index.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}
    key = "agent:nexa:main"
    existing = data.get(key)
    if isinstance(existing, dict) and existing.get("sessionFile"):
        return
    session_id = str(uuid.uuid4())
    session_file = session_dir / f"{session_id}.jsonl"
    if not session_file.exists():
        session_file.write_text("", encoding="utf-8")
    now_ms = int(time.time() * 1000)
    display_name = env.get(_ENV_AGENT_DISPLAY_NAME, "Nexa") or "Nexa"
    data[key] = {
        "sessionId": session_id,
        "updatedAt": now_ms,
        "systemSent": True,
        "abortedLastRun": False,
        "chatType": "direct",
        "deliveryContext": {"channel": "nextcloud-talk"},
        "lastChannel": "nextcloud-talk",
        "origin": {
            "provider": "nextcloud-talk",
            "surface": "nextcloud-talk",
            "chatType": "direct",
            "label": display_name,
            "from": "nextcloud:nexa-agent",
            "to": "nextcloud:nexa-agent",
            "accountId": "default",
        },
        "sessionFile": str(session_file),
        "compactionCount": 0,
    }
    session_index.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _wait_for_contracts(
    *,
    runtime_contract_path: Path,
    workspace_contract_path: Path,
) -> None:
    timeout_seconds = _float_env(
        "DOKPLOY_WIZARD_NEXA_BOOTSTRAP_TIMEOUT_SECONDS",
        _DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if runtime_contract_path.exists() and workspace_contract_path.exists():
            return
        time.sleep(1.0)
    msg = (
        "Timed out waiting for seeded Nexa contract files: "
        f"runtime={runtime_contract_path} workspace={workspace_contract_path}"
    )
    raise RuntimeError(msg)


def _float_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        msg = f"Environment variable {name} must be numeric."
        raise RuntimeError(msg) from exc
    if value <= 0:
        msg = f"Environment variable {name} must be greater than zero."
        raise RuntimeError(msg)
    return value


def _runtime_dependencies_from_env(env: dict[str, str] | os._Environ[str]) -> NexaRuntimeDependencies:
    return NexaRuntimeDependencies(
        talk_reply_planner=lambda payload, memory: _provider_talk_reply_planner(payload, memory, env=env),
        talk_sender=lambda payload: _send_talk_reply(payload, env=env),
        nextcloud_file_creator=lambda request: _create_nextcloud_markdown_file(request, env=env),
        terminal_command_runner=lambda command: _run_terminal_command(command),
        onlyoffice_agent_identity=_onlyoffice_agent_identity_from_env(env),
        load_canonical_file=lambda save_signal: _load_canonical_file(save_signal, env=env),
        onlyoffice_reconcile_executor=lambda decision, save_signal, canonical_file, memory: _execute_onlyoffice_reconcile(
            decision,
            save_signal,
            canonical_file,
            memory,
            env=env,
        ),
    )


def _run_terminal_command(command: str) -> NexaTerminalCommandResult:
    completed = subprocess.run(
        ["sh", "-lc", command],
        capture_output=True,
        text=True,
        timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
        check=False,
    )
    return NexaTerminalCommandResult(
        command=command,
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=completed.returncode,
    )


def _minimal_talk_reply_planner(
    payload: dict[str, Any],
    memory: Any,
) -> NexaPlannedTalkReply:
    message_text = str(payload.get("message", {}).get("text", "")).strip()
    if message_text == "":
        message_text = "your message"
    memory_hits = getattr(memory, "hits", ())
    if memory_hits:
        first_hit = str(memory_hits[0].content).strip()
        reply_text = f"Nexa received {message_text!r}. Relevant memory: {first_hit}"
        memory_content = f"Nexa acknowledged the Talk request and surfaced one prior memory: {first_hit}"
    else:
        reply_text = (
            f"Nexa received {message_text!r}. The live sidecar path is healthy, but autonomous reply planning is still minimal in this first VPS loop."
        )
        memory_content = "Nexa sent a minimal live-sidecar acknowledgement reply for a Talk request."
    return NexaPlannedTalkReply(text=reply_text, memory_content=memory_content)


def _provider_talk_reply_planner(
    payload: dict[str, Any],
    memory: Any,
    *,
    env: dict[str, str] | os._Environ[str],
) -> NexaPlannedTalkReply:
    pass_through_enabled = env.get(_ENV_OPENCLAW_INTERNAL_URL, "").strip() != ""
    openclaw_reply = _openclaw_talk_reply_planner(payload, memory, env=env)
    if openclaw_reply is not None:
        return openclaw_reply
    if pass_through_enabled:
        return NexaPlannedTalkReply(
            text=(
                "OpenClaw did not return a grounded response in time. "
                "Please try again instead of trusting a guessed answer."
            ),
            memory_content="OpenClaw pass-through timed out before producing a grounded response.",
            memory_content_class="assistant_summary",
            memory_target_layer="shared",
        )
    model = env.get(_ENV_PLANNER_MODEL, "").strip()
    provider = env.get(_ENV_PLANNER_MODEL_PROVIDER, "").strip() or "openai"
    message_text = str(payload.get("message", {}).get("text", "")).strip() or "your message"
    recent_conversation = payload.get("recentConversation")
    conversation_summary = ""
    if isinstance(recent_conversation, list):
        turns: list[str] = []
        for entry in recent_conversation[-8:]:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role", "")).strip()
            text = str(entry.get("text", "")).strip()
            if role == "" or text == "":
                continue
            turns.append(f"{role}: {text}")
        if turns:
            conversation_summary = "\n\nRecent conversation:\n" + "\n".join(turns)
    memory_summary = ""
    memory_hits = getattr(memory, "hits", ())
    if memory_hits:
        memory_summary = "\n\nRelevant memory:\n" + "\n".join(
            f"- {str(hit.content).strip()}" for hit in memory_hits[:5]
        )
    if model == "":
        return _minimal_talk_reply_planner(payload, memory)
    if provider == "local" or model.startswith("local/"):
        base_url = env.get(_ENV_PLANNER_LOCAL_BASE_URL, "").strip()
        api_key = env.get(_ENV_PLANNER_LOCAL_API_KEY, "").strip() or "sk-no-key-required"
    elif provider == "nvidia" or model.startswith("nvidia/"):
        base_url = env.get(_ENV_PLANNER_NVIDIA_BASE_URL, "").strip() or "https://integrate.api.nvidia.com/v1"
        api_key = env.get(_ENV_PLANNER_NVIDIA_API_KEY, "").strip()
    else:
        base_url = env.get(_ENV_PLANNER_OPENROUTER_BASE_URL, "").strip() or "https://openrouter.ai/api/v1"
        api_key = env.get(_ENV_PLANNER_OPENROUTER_API_KEY, "").strip()
    if not api_key:
        return _minimal_talk_reply_planner(payload, memory)
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Nexa, a Nextcloud coworker agent. Reply briefly and directly to the latest user message. "
                    "Use recent conversation context to resolve pronouns and follow-up questions. "
                    "Ignore system noise and respond like a helpful teammate."
                ),
            },
            {"role": "user", "content": f"Incoming Nextcloud Talk message:\n{message_text}{conversation_summary}{memory_summary}"},
        ],
        "temperature": 0.2,
    }
    try:
        response_payload = _json_request(
            f"{base_url.rstrip('/')}/chat/completions",
            method="POST",
            body=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
    except RuntimeError:
        LOGGER.warning("Nexa provider-backed planner failed; using minimal planner", exc_info=True)
        return _minimal_talk_reply_planner(payload, memory)
    text = _extract_openclaw_chat_text(response_payload).strip()
    if text == "":
        return _minimal_talk_reply_planner(payload, memory)
    return NexaPlannedTalkReply(
        text=text,
        memory_content=f"Nexa replied through the provider-backed planner: {text}",
        memory_content_class="assistant_summary",
        memory_target_layer="shared",
    )


def _extract_openclaw_chat_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _openclaw_talk_reply_planner(
    payload: dict[str, Any],
    memory: Any,
    *,
    env: dict[str, str] | os._Environ[str],
) -> NexaPlannedTalkReply | None:
    base_url = env.get(_ENV_OPENCLAW_INTERNAL_URL, "").strip()
    if base_url == "":
        return None
    input_items: list[dict[str, object]] = []
    recent_conversation = payload.get("recentConversation")
    if isinstance(recent_conversation, list):
        for entry in recent_conversation[-8:]:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role", "")).strip()
            text = str(entry.get("text", "")).strip()
            if role not in {"user", "assistant"} or text == "":
                continue
            input_items.append({"type": "message", "role": role, "content": text})
    memory_hits = getattr(memory, "hits", ())
    if memory_hits:
        memory_summary = "Relevant memory:\n" + "\n".join(
            f"- {str(hit.content).strip()}" for hit in memory_hits[:5]
        )
        input_items.append({"type": "message", "role": "developer", "content": memory_summary})
    current_text = str(payload.get("message", {}).get("text", "")).strip() or "your message"
    input_items.append({"type": "message", "role": "user", "content": current_text})
    request_body = {
        "model": "openclaw/nexa",
        "input": input_items,
        "user": str(payload.get("initiator", {}).get("id", "nexa-user")),
        "stream": False,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "cf-access-authenticated-user-email": _required_env(env, _ENV_AGENT_USER_ID),
        "x-openclaw-agent-id": "nexa",
        "x-openclaw-message-channel": "nextcloud-talk",
        "x-openclaw-session-key": _openclaw_session_key(payload),
    }
    try:
        response_payload = _json_request(
            f"{base_url.rstrip('/')}/v1/responses",
            method="POST",
            body=request_body,
            headers=headers,
            timeout_seconds=_DEFAULT_OPENCLAW_PASS_THROUGH_TIMEOUT_SECONDS,
        )
    except RuntimeError:
        LOGGER.warning("OpenClaw pass-through planner failed", exc_info=True)
        return None
    text = _extract_openclaw_responses_text(response_payload).strip()
    if text == "":
        return None
    return NexaPlannedTalkReply(
        text=text,
        memory_content=f"Nexa replied through OpenClaw pass-through: {text}",
        memory_content_class="assistant_summary",
        memory_target_layer="shared",
    )


def _extract_openclaw_responses_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip() != "":
                chunks.append(text.strip())
    return "\n\n".join(chunks)


def _openclaw_session_key(payload: dict[str, Any]) -> str:
    conversation_id = str(payload.get("conversation", {}).get("id", "main")).strip() or "main"
    context = payload.get("context")
    thread = context.get("threadId") if isinstance(context, dict) else None
    if thread is not None and str(thread).strip() != "":
        return f"agent:nexa:nextcloud-{conversation_id}-thread-{str(thread).strip()}"
    return f"agent:nexa:nextcloud-{conversation_id}"


def _send_talk_reply(payload: dict[str, Any], *, env: dict[str, str] | os._Environ[str]) -> dict[str, Any]:
    base_url = _required_env(env, _ENV_NEXTCLOUD_BASE_URL)
    agent_user = _required_env(env, _ENV_AGENT_USER_ID)
    agent_password = _agent_password(env)
    conversation_token = _conversation_token(payload)
    signing_secret = _required_env(env, _ENV_TALK_SIGNING_SECRET)
    shared_secret = _required_env(env, _ENV_TALK_SHARED_SECRET)
    message = _required_string(payload, "message")
    body = {
        "message": message,
        "referenceId": hashlib.sha256(f"{conversation_token}:{message}".encode("utf-8")).hexdigest(),
    }
    reply_to = payload.get("replyTo")
    if isinstance(reply_to, dict) and reply_to.get("messageId") is not None:
        body["replyTo"] = reply_to["messageId"]
    if agent_password is not None:
        try:
            response_payload = _json_request(
                f"{base_url.rstrip('/')}/ocs/v2.php/apps/spreed/api/v1/chat/{parse.quote(conversation_token, safe='')}",
                method="POST",
                body=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "OCS-APIRequest": "true",
                },
                auth_user=agent_user,
                auth_password=agent_password,
            )
        except RuntimeError as exc:
            if "reply-to" in str(exc).lower() and "replyTo" in body:
                fallback_body = {key: value for key, value in body.items() if key != "replyTo"}
                response_payload = _json_request(
                    f"{base_url.rstrip('/')}/ocs/v2.php/apps/spreed/api/v1/chat/{parse.quote(conversation_token, safe='')}",
                    method="POST",
                    body=fallback_body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "OCS-APIRequest": "true",
                    },
                    auth_user=agent_user,
                    auth_password=agent_password,
                )
            else:
                raise
    else:
        random_header = secrets.token_hex(32)
        signature = hmac.new(
            signing_secret.encode("utf-8"),
            f"{random_header}{message}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        response_payload = _json_request(
            f"{base_url.rstrip('/')}/ocs/v2.php/apps/spreed/api/v1/bot/{parse.quote(conversation_token, safe='')}/message",
            method="POST",
            body=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "OCS-APIRequest": "true",
                "X-Nextcloud-Talk-Bot-Random": random_header,
                "X-Nextcloud-Talk-Bot-Signature": signature,
                "X-Nextcloud-Talk-Secret": shared_secret,
            },
        )
    message_id = _extract_talk_response_string(response_payload, ("messageId",), fallback_paths=(("ocs", "data", "id"), ("ocs", "data", "messageId")))
    request_id = _extract_talk_response_string(response_payload, ("requestId",), fallback_paths=(("ocs", "meta", "requestid"),), required=False)
    result = {"messageId": message_id}
    if request_id is not None:
        result["requestId"] = request_id
    return result


def _load_canonical_file(
    save_signal: Any,
    *,
    env: dict[str, str] | os._Environ[str],
) -> NexaCanonicalFileSnapshot:
    path = getattr(save_signal, "path", None)
    if not isinstance(path, str) or path.strip() == "":
        msg = "Live Nexa canonical file loading requires an explicit Nextcloud file path."
        raise RuntimeError(msg)
    base_url = _required_env(env, _ENV_NEXTCLOUD_BASE_URL)
    webdav_user = _required_env(env, _ENV_WEBDAV_AUTH_USER)
    webdav_password = _required_env(env, _ENV_WEBDAV_AUTH_PASSWORD)
    agent_user_id = _required_env(env, _ENV_AGENT_USER_ID)
    dav_url = _webdav_file_url(base_url=base_url, auth_user=webdav_user, path=path)
    propfind_payload = _propfind_file_metadata(
        url=dav_url,
        auth_user=webdav_user,
        auth_password=webdav_password,
    )
    etag = _require_xml_text(propfind_payload, ".//d:getetag")
    propfind_file_id = _optional_xml_text(propfind_payload, ".//oc:fileid")
    expected_file_id = getattr(save_signal.scope, "file_id", None)
    if isinstance(propfind_file_id, str) and expected_file_id is not None and propfind_file_id != expected_file_id:
        msg = f"Canonical WebDAV file id mismatch: expected {expected_file_id}, got {propfind_file_id}."
        raise RuntimeError(msg)
    file_bytes, content_type = _raw_request(
        dav_url,
        method="GET",
        headers={"Accept": "*/*"},
        auth_user=webdav_user,
        auth_password=webdav_password,
    )
    content = _decode_canonical_file_content(file_bytes, path=path, content_type=content_type)
    acl_principals = _extract_acl_principals(propfind_payload)
    acl_complete = bool(acl_principals)
    if not acl_complete:
        acl_principals = (agent_user_id,)
        acl_complete = True
    return NexaCanonicalFileSnapshot(
        scope=save_signal.scope,
        content=content,
        etag=etag,
        acl_principals=tuple(sorted({principal for principal in acl_principals if principal.strip() != ""})),
        acl_complete=acl_complete,
    )


def _create_nextcloud_markdown_file(
    file_request: NexaNextcloudFileCreateRequest,
    *,
    env: dict[str, str] | os._Environ[str],
) -> NexaNextcloudFileCreateResult:
    base_url = _required_env(env, _ENV_NEXTCLOUD_BASE_URL)
    webdav_user = _required_env(env, _ENV_WEBDAV_AUTH_USER)
    webdav_password = _required_env(env, _ENV_WEBDAV_AUTH_PASSWORD)
    file_url = _webdav_file_url(base_url=base_url, auth_user=webdav_user, path=file_request.relative_path)
    _raw_request(
        file_url,
        method="PUT",
        body=file_request.content.encode("utf-8"),
        headers={
            "Content-Type": "text/markdown; charset=utf-8",
            "X-NC-WebDAV-AutoMkcol": "1",
        },
        auth_user=webdav_user,
        auth_password=webdav_password,
    )
    share_payload = _json_request(
        f"{base_url.rstrip('/')}/ocs/v2.php/apps/files_sharing/api/v1/shares",
        method="POST",
        body={
            "path": file_request.relative_path,
            "shareType": 3,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "OCS-APIRequest": "true",
        },
        auth_user=webdav_user,
        auth_password=webdav_password,
    )
    share_url = _extract_talk_response_string(
        share_payload,
        (),
        fallback_paths=(("ocs", "data", "url"),),
    )
    if share_url is None:
        share_url = "share link unavailable"
    return NexaNextcloudFileCreateResult(
        filename=file_request.filename,
        relative_path=file_request.relative_path,
        share_url=share_url,
        diff_text=_diff_for_new_file(file_request.relative_path, file_request.content),
    )


def _execute_onlyoffice_reconcile(
    decision: Any,
    save_signal: Any,
    canonical_file: Any,
    memory: Any,
    *,
    env: dict[str, str] | os._Environ[str],
) -> NexaOnlyofficeActionResult:
    state_dir = Path(env.get("DOKPLOY_WIZARD_NEXA_STATE_DIR", _DEFAULT_STATE_DIR))
    state_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "action": decision.action,
        "reason": decision.reason,
        "authoritative": bool(decision.authoritative),
        "document_key": save_signal.document_key,
        "file_id": save_signal.scope.file_id,
        "file_version": save_signal.scope.file_version,
        "path": save_signal.path,
        "etag": canonical_file.etag,
        "content_length": len(canonical_file.content),
        "actor": {
            "user_id": _required_env(env, _ENV_AGENT_USER_ID),
            "display_name": _required_env(env, _ENV_AGENT_DISPLAY_NAME),
        },
        "result": "structured_noop",
        "document_mutation_performed": False,
        "memory_hits": len(getattr(memory, "hits", ())),
    }
    with (state_dir / "onlyoffice-reconcile-actions.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return NexaOnlyofficeActionResult(
        outcome="applied",
        authoritative_write=False,
        memory_content=None,
    )


def _onlyoffice_agent_identity_from_env(
    env: dict[str, str] | os._Environ[str],
) -> NexaOnlyofficeAgentIdentity:
    return NexaOnlyofficeAgentIdentity(
        agent_user_id=_required_env(env, _ENV_AGENT_USER_ID),
        display_name=_required_env(env, _ENV_AGENT_DISPLAY_NAME),
    )


def _agent_password(env: dict[str, str] | os._Environ[str]) -> str | None:
    value = env.get(_ENV_AGENT_PASSWORD)
    if value is not None and value.strip() != "":
        return value.strip()
    fallback = env.get(_ENV_WEBDAV_AUTH_PASSWORD)
    return fallback.strip() if fallback is not None and fallback.strip() != "" else None


def _presence_message_for_job(job: Any) -> str:
    if job.kind == "nexa.onlyoffice.reconcile_saved_document":
        return "Working on a document..."
    return "Thinking..."


def _presence_auth(env: dict[str, str] | os._Environ[str]) -> tuple[str, str] | None:
    user = env.get(_ENV_AGENT_USER_ID)
    password = _agent_password(env)
    if user is None or user.strip() == "" or password is None:
        return None
    return user.strip(), password


def _update_presence(env: dict[str, str] | os._Environ[str], *, status: str, message: str) -> None:
    auth = _presence_auth(env)
    if auth is None:
        return
    base_url = _required_env(env, _ENV_NEXTCLOUD_BASE_URL)
    user, password = auth
    form_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "OCS-APIRequest": "true",
    }
    try:
        _raw_request(
            f"{base_url.rstrip('/')}/ocs/v2.php/apps/user_status/api/v1/heartbeat",
            method="PUT",
            headers=form_headers,
            body=parse.urlencode({"status": status}).encode("utf-8"),
            auth_user=user,
            auth_password=password,
        )
    except RuntimeError as exc:
        if _is_http_429_runtime_error(exc):
            LOGGER.info("Nexa presence heartbeat rate-limited by Nextcloud; leaving current status unchanged.")
            return
        LOGGER.warning("Nexa presence update failed", exc_info=True)
        return
    try:
        _raw_request(
            f"{base_url.rstrip('/')}/ocs/v2.php/apps/user_status/api/v1/user_status/message/custom",
            method="PUT",
            headers=form_headers,
            body=parse.urlencode({"message": message, "statusIcon": "🤖"}).encode("utf-8"),
            auth_user=user,
            auth_password=password,
        )
    except RuntimeError as exc:
        if _is_http_429_runtime_error(exc):
            LOGGER.info("Nexa presence message rate-limited by Nextcloud; leaving current message unchanged.")
            return
        LOGGER.warning("Nexa presence update failed", exc_info=True)


def _is_http_429_runtime_error(error_obj: RuntimeError) -> bool:
    detail = str(error_obj).lower()
    return "http 429" in detail or "reached maximum delay" in detail


def _poll_talk_messages(store: DurableQueueStore, *, env: dict[str, str] | os._Environ[str], now: datetime) -> int:
    auth = _presence_auth(env)
    if auth is None:
        return 0
    base_url = _required_env(env, _ENV_NEXTCLOUD_BASE_URL)
    agent_user_id, agent_password = auth
    state_dir = Path(env.get("DOKPLOY_WIZARD_NEXA_STATE_DIR", _DEFAULT_STATE_DIR))
    cursor_path = state_dir / "talk-cursors.json"
    cursors: dict[str, str] = {}
    if cursor_path.exists():
        try:
            loaded = json.loads(cursor_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cursors = {str(key): str(value) for key, value in loaded.items()}
        except json.JSONDecodeError:
            cursors = {}
    payload = _json_request(
        f"{base_url.rstrip('/')}/ocs/v2.php/apps/spreed/api/v4/room",
        method="GET",
        body=None,
        headers={"Accept": "application/json", "OCS-APIRequest": "true"},
        auth_user=agent_user_id,
        auth_password=agent_password,
    )
    conversations = sorted(
        _ocs_data_list(payload),
        key=lambda item: int(str(item.get("unreadMessages", "0") or "0")),
        reverse=True,
    )
    enqueued = 0
    for conversation in conversations:
        token = conversation.get("token")
        if not isinstance(token, str) or token.strip() == "":
            continue
        last_seen = cursors.get(token)
        try:
            messages_payload = _json_request(
                f"{base_url.rstrip('/')}/ocs/v2.php/apps/spreed/api/v1/chat/{parse.quote(token, safe='')}?lookIntoFuture=0&includeLastKnown=1&setReadMarker=0&lastKnownMessageId=0",
                method="GET",
                body=None,
                headers={"Accept": "application/json", "OCS-APIRequest": "true"},
                auth_user=agent_user_id,
                auth_password=agent_password,
            )
        except RuntimeError:
            LOGGER.warning("Nexa Talk poll failed for conversation %s", token, exc_info=True)
            continue
        messages = sorted(
            _ocs_data_list(messages_payload),
            key=lambda item: int(str(item.get("id", "0")) or "0"),
        )
        latest_seen = last_seen
        for message in messages:
            message_id = str(message.get("id", "")).strip()
            actor_id = str(message.get("actorId", "")).strip()
            text = str(message.get("message", "")).strip()
            message_type = str(message.get("messageType", "comment")).strip()
            if message_id == "" or text == "":
                continue
            latest_seen = message_id
            if actor_id == agent_user_id:
                continue
            if message_type != "comment":
                continue
            if last_seen is not None and message_id <= last_seen:
                continue
            normalized = {
                "server": base_url,
                "webhookEventId": f"poll:{token}:{message_id}",
                "conversationToken": token,
                "conversation": {"id": token},
                "initiator": {"id": actor_id},
                "message": {"id": message_id, "text": text},
                "capabilities": {"threads": False},
            }
            event = store.persist_incoming_event(
                source="nextcloud-talk",
                idempotency_key=normalized["webhookEventId"],
                raw_body=json.dumps(message).encode("utf-8"),
                parsed_payload=normalized,
                received_at=now,
            )
            scope = NexaScopeContext(
                tenant_id=parse.urlsplit(base_url).hostname or "nextcloud",
                integration_surface="nextcloud-talk",
                user_id=actor_id,
                room_id=token,
                run_id=normalized["webhookEventId"],
            )
            store.enqueue_job(
                queue="foreground",
                job_type="nexa.talk.process_message",
                payload={"source": "nextcloud-talk", "event_id": event.event_id},
                idempotency_key=normalized["webhookEventId"],
                scope_key=scope.queue_scope_key(),
                supersession_key=scope.run_correlation_key(),
                now=now,
            )
            enqueued += 1
        if latest_seen is not None:
            cursors[token] = latest_seen
    cursor_path.write_text(json.dumps(cursors, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return enqueued


def _ocs_data_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ocs = payload.get("ocs")
    data = ocs.get("data") if isinstance(ocs, dict) else payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _required_env(env: dict[str, str] | os._Environ[str], key: str) -> str:
    value = env.get(key)
    if value is None or value.strip() == "":
        msg = f"Environment variable {key} is required for the live Nexa sidecar adapter path."
        raise RuntimeError(msg)
    return value.strip()


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value.strip() == "":
        msg = f"Talk sender payload requires a non-empty {key}."
        raise RuntimeError(msg)
    return value


def _conversation_token(payload: dict[str, Any]) -> str:
    token = payload.get("conversationToken")
    if isinstance(token, str) and token.strip() != "":
        return token
    conversation_id = payload.get("conversationId")
    if isinstance(conversation_id, str) and conversation_id.strip() != "":
        return conversation_id
    msg = "Talk sender payload requires a conversation token or conversation id."
    raise RuntimeError(msg)


def _extract_talk_response_string(
    payload: dict[str, Any],
    direct_keys: tuple[str, ...],
    *,
    fallback_paths: tuple[tuple[str, ...], ...],
    required: bool = True,
) -> str | None:
    for key in direct_keys:
        value = payload.get(key)
        if isinstance(value, (str, int)):
            normalized = str(value).strip()
            if normalized != "":
                return normalized
    for path in fallback_paths:
        value: Any = payload
        for segment in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(segment)
        if isinstance(value, (str, int)):
            normalized = str(value).strip()
            if normalized != "":
                return normalized
    if required:
        msg = f"Talk sender response is missing a usable value for {direct_keys[0]}."
        raise RuntimeError(msg)
    return None


def _webdav_file_url(*, base_url: str, auth_user: str, path: str) -> str:
    normalized_base = base_url.rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    encoded_segments = [parse.quote(segment, safe="") for segment in normalized_path.split("/") if segment != ""]
    encoded_path = "/".join(encoded_segments)
    return f"{normalized_base}/remote.php/dav/files/{parse.quote(auth_user, safe='')}/{encoded_path}"


def _propfind_file_metadata(*, url: str, auth_user: str, auth_password: str) -> ElementTree.Element:
    body = (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<d:propfind xmlns:d=\"DAV:\" xmlns:oc=\"http://owncloud.org/ns\" xmlns:nc=\"http://nextcloud.org/ns\">"
        "<d:prop>"
        "<d:getetag />"
        "<oc:fileid />"
        "<oc:permissions />"
        "<nc:acl />"
        "<nc:acl-list />"
        "</d:prop>"
        "</d:propfind>"
    ).encode("utf-8")
    response_bytes, _ = _raw_request(
        url,
        method="PROPFIND",
        body=body,
        headers={
            "Content-Type": "application/xml; charset=utf-8",
            "Depth": "0",
        },
        auth_user=auth_user,
        auth_password=auth_password,
    )
    try:
        return ElementTree.fromstring(response_bytes)
    except ElementTree.ParseError as exc:
        msg = "Canonical WebDAV PROPFIND did not return valid XML."
        raise RuntimeError(msg) from exc


def _require_xml_text(root: ElementTree.Element, xpath: str) -> str:
    value = _optional_xml_text(root, xpath)
    if value is None:
        msg = f"Canonical WebDAV metadata is missing required property {xpath}."
        raise RuntimeError(msg)
    return value


def _optional_xml_text(root: ElementTree.Element, xpath: str) -> str | None:
    node = root.find(xpath, _XML_NAMESPACES)
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value if value != "" else None


def _extract_acl_principals(root: ElementTree.Element) -> tuple[str, ...]:
    principals: list[str] = []
    for xpath in (
        ".//nc:acl/nc:acl-mapping-id",
        ".//nc:acl-list/nc:acl/nc:acl-mapping-id",
    ):
        for node in root.findall(xpath, _XML_NAMESPACES):
            if node.text is None:
                continue
            value = node.text.strip()
            if value != "":
                principals.append(value)
    return tuple(sorted(set(principals)))


def _decode_canonical_file_content(file_bytes: bytes, *, path: str, content_type: str | None) -> str:
    lowered_path = path.lower()
    lowered_type = "" if content_type is None else content_type.lower()
    if lowered_path.endswith(".docx") or "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in lowered_type:
        return _extract_docx_text(file_bytes)
    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"Canonical WebDAV loader could not decode file content for {path}; only UTF-8 text and .docx are supported in the first live loop."
        raise RuntimeError(msg) from exc


def _extract_docx_text(file_bytes: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as archive:
            document_xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        msg = "Canonical .docx loader could not read word/document.xml from the WebDAV response."
        raise RuntimeError(msg) from exc
    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        msg = "Canonical .docx loader could not parse word/document.xml."
        raise RuntimeError(msg) from exc
    text_nodes = [node.text.strip() for node in root.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t") if node.text and node.text.strip()]
    if not text_nodes:
        msg = "Canonical .docx loader extracted no text content from word/document.xml."
        raise RuntimeError(msg)
    return "\n".join(text_nodes)


def _json_request(
    url: str,
    *,
    method: str,
    body: dict[str, Any] | None,
    headers: dict[str, str],
    auth_user: str | None = None,
    auth_password: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    response_bytes, _ = _raw_request(
        url,
        method=method,
        body=None if body is None else json.dumps(body).encode("utf-8"),
        headers=headers,
        auth_user=auth_user,
        auth_password=auth_password,
        timeout_seconds=timeout_seconds,
    )
    try:
        payload = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"Expected JSON response from {url}."
        raise RuntimeError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"Expected JSON object response from {url}."
        raise RuntimeError(msg)
    return payload


def _raw_request(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: bytes | None = None,
    auth_user: str | None = None,
    auth_password: str | None = None,
    timeout_seconds: float | None = None,
) -> tuple[bytes, str | None]:
    request_headers = dict(headers)
    if auth_user is not None and auth_password is not None:
        token = _basic_auth_token(auth_user, auth_password)
        request_headers["Authorization"] = f"Basic {token}"
    http_request = request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with request.urlopen(http_request, timeout=timeout_seconds or _DEFAULT_HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.read(), response.headers.get("Content-Type")
    except error.HTTPError as exc:
        if exc.code == 304:
            return b"{}", exc.headers.get("Content-Type")
        detail = exc.read().decode("utf-8", errors="replace")
        msg = f"HTTP {exc.code} from {url}: {detail}"
        raise RuntimeError(msg) from exc
    except error.URLError as exc:
        msg = f"Request to {url} failed: {exc.reason}"
        raise RuntimeError(msg) from exc
    except Exception as exc:
        raise RuntimeError(f"Request to {url} failed: {exc}") from exc


def _basic_auth_token(user: str, password: str) -> str:
    credentials = f"{user}:{password}".encode("utf-8")
    return base64.b64encode(credentials).decode("ascii")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
