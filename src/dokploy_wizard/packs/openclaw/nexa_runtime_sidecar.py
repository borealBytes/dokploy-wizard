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
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib import error, parse, request
from xml.etree import ElementTree
import zipfile
from io import BytesIO
from typing import Any

from dokploy_wizard.state import DurableQueueStore

from .nexa_onlyoffice import NexaOnlyofficeAgentIdentity
from .nexa_retrieval import NexaCanonicalFileSnapshot
from .nexa_runtime import (
    NexaOnlyofficeActionResult,
    NexaPlannedTalkReply,
    NexaRuntimeDependencies,
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

_ENV_NEXTCLOUD_BASE_URL = "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL"
_ENV_TALK_SHARED_SECRET = "OPENCLAW_NEXA_TALK_SHARED_SECRET"
_ENV_TALK_SIGNING_SECRET = "OPENCLAW_NEXA_TALK_SIGNING_SECRET"
_ENV_WEBDAV_AUTH_USER = "OPENCLAW_NEXA_WEBDAV_AUTH_USER"
_ENV_WEBDAV_AUTH_PASSWORD = "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD"
_ENV_AGENT_USER_ID = "OPENCLAW_NEXA_AGENT_USER_ID"
_ENV_AGENT_DISPLAY_NAME = "OPENCLAW_NEXA_AGENT_DISPLAY_NAME"

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

    while True:
        leased_job = store.lease_next_job(
            lease_owner=f"nexa-runtime:{worker_id}",
            now=datetime.now(UTC),
        )
        if leased_job is None:
            time.sleep(poll_seconds)
            continue
        result = run_queued_nexa_job(
            leased_job,
            store=store,
            env=os.environ,
            dependencies=dependencies,
            now=datetime.now(UTC),
        )
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
        talk_reply_planner=_minimal_talk_reply_planner,
        talk_sender=lambda payload: _send_talk_reply(payload, env=env),
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


def _send_talk_reply(payload: dict[str, Any], *, env: dict[str, str] | os._Environ[str]) -> dict[str, Any]:
    base_url = _required_env(env, _ENV_NEXTCLOUD_BASE_URL)
    signing_secret = _required_env(env, _ENV_TALK_SIGNING_SECRET)
    shared_secret = _required_env(env, _ENV_TALK_SHARED_SECRET)
    conversation_token = _conversation_token(payload)
    message = _required_string(payload, "message")
    body = {
        "message": message,
        "referenceId": hashlib.sha256(f"{conversation_token}:{message}".encode("utf-8")).hexdigest(),
    }
    reply_to = payload.get("replyTo")
    if isinstance(reply_to, dict) and reply_to.get("messageId") is not None:
        body["replyTo"] = reply_to["messageId"]
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
    body: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    response_bytes, _ = _raw_request(
        url,
        method=method,
        body=json.dumps(body).encode("utf-8"),
        headers=headers,
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
) -> tuple[bytes, str | None]:
    request_headers = dict(headers)
    if auth_user is not None and auth_password is not None:
        token = _basic_auth_token(auth_user, auth_password)
        request_headers["Authorization"] = f"Basic {token}"
    http_request = request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with request.urlopen(http_request, timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.read(), response.headers.get("Content-Type")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        msg = f"HTTP {exc.code} from {url}: {detail}"
        raise RuntimeError(msg) from exc
    except error.URLError as exc:
        msg = f"Request to {url} failed: {exc.reason}"
        raise RuntimeError(msg) from exc


def _basic_auth_token(user: str, password: str) -> str:
    credentials = f"{user}:{password}".encode("utf-8")
    return base64.b64encode(credentials).decode("ascii")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
