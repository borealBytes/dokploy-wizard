# mypy: ignore-errors
# pyright: reportIndexIssue=false
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dokploy_wizard.packs.openclaw.nexa_ingress import (
    handle_onlyoffice_callback,
    handle_talk_webhook,
)
from dokploy_wizard.packs.openclaw.nexa_onlyoffice import NexaOnlyofficeAgentIdentity
from dokploy_wizard.packs.openclaw.nexa_retrieval import NexaCanonicalFileSnapshot
from dokploy_wizard.packs.openclaw.nexa_runtime import (
    NexaOnlyofficeActionResult,
    NexaOnlyofficeRuntimeResult,
    NexaPlannedTalkReply,
    NexaRuntimeDependencies,
    NexaTalkRuntimeResult,
    run_queued_nexa_job,
)
from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _ensure_nexa_openclaw_session
from dokploy_wizard.packs.openclaw.nexa_scope import NexaScopeContext
from dokploy_wizard.state import DurableQueueStore
from tests.integration.nexa_e2e_helpers import (
    ONLYOFFICE_CALLBACK_SECRET,
    TALK_SHARED_SECRET,
    TALK_SIGNING_SECRET,
    build_onlyoffice_headers,
    build_talk_headers,
    json_bytes,
    load_json_fixture,
)

_OPENCLAW_FAIL_CLOSED_FALLBACK = (
    "OpenClaw did not return a grounded response in time. "
    "Please try again instead of trusting a guessed answer."
)


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 20, hour, minute, tzinfo=UTC)


def test_talk_runtime_degrades_cleanly_when_mem0_is_misconfigured(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)
    payload = load_json_fixture("nexa-talk-webhook-room-message.json")
    body = json_bytes(payload)
    ack = handle_talk_webhook(
        body=body,
        headers=build_talk_headers(body),
        talk_shared_secret=TALK_SHARED_SECRET,
        talk_signing_secret=TALK_SIGNING_SECRET,
        store=store,
    )
    job = store.lease_next_job(lease_owner="worker-talk", now=datetime.now(UTC))
    assert job is not None
    captured_memory_statuses: list[str] = []

    def planner(payload: dict[str, object], memory: object) -> NexaPlannedTalkReply:
        captured_memory_statuses.append(memory.status)  # type: ignore[attr-defined]
        return NexaPlannedTalkReply(
            text="Visible room reply.",
            memory_content="Visible room reply summarized for shared memory.",
        )

    result = run_queued_nexa_job(
        job,
        store=store,
        env={},
        dependencies=NexaRuntimeDependencies(
            talk_reply_planner=planner,
            talk_sender=lambda outbound_payload: {"messageId": "sent-900", "requestId": "request-42"},
            onlyoffice_agent_identity=NexaOnlyofficeAgentIdentity(
                agent_user_id="nexa-agent",
                display_name="Nexa",
            ),
            load_canonical_file=lambda save_signal: NexaCanonicalFileSnapshot(
                scope=NexaScopeContext(
                    tenant_id="example.com",
                    integration_surface="nextcloud-files",
                    file_id="file-991",
                    file_version="171",
                ),
                content="unused",
                etag='"etag-171"',
                acl_principals=("clay",),
                acl_complete=True,
            ),
            onlyoffice_reconcile_executor=lambda decision, save_signal, canonical_file, memory: NexaOnlyofficeActionResult(
                outcome="skipped",
                authoritative_write=False,
            ),
        ),
        now=_ts(12, 47),
    )

    assert ack == {"status_code": 202, "body": {"accepted": True}}
    assert result.status == "completed"
    talk_result = result.result
    assert isinstance(talk_result, NexaTalkRuntimeResult)
    assert talk_result.memory_read.status == "degraded"
    assert talk_result.memory_write.status == "degraded"
    assert captured_memory_statuses == ["degraded"]


def test_onlyoffice_force_save_runtime_skips_memory_and_authoritative_write(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)
    payload = load_json_fixture("nexa-onlyoffice-callback-status-6.json")
    ack = handle_onlyoffice_callback(
        body=json_bytes(payload),
        headers=build_onlyoffice_headers(),
        callback_secret=ONLYOFFICE_CALLBACK_SECRET,
        store=store,
    )
    job = store.lease_next_job(lease_owner="worker-doc", now=datetime.now(UTC))
    assert job is not None
    called = {"executor": 0}

    def reconcile_executor(decision, save_signal, canonical_file, memory) -> NexaOnlyofficeActionResult:
        called["executor"] += 1
        return NexaOnlyofficeActionResult(outcome="applied", authoritative_write=True, memory_content="should not run")

    result = run_queued_nexa_job(
        job,
        store=store,
        env={},
        dependencies=NexaRuntimeDependencies(
            talk_reply_planner=lambda payload, memory: NexaPlannedTalkReply(
                text="unused",
                memory_content="unused",
            ),
            talk_sender=lambda outbound_payload: {"messageId": "unused"},
            onlyoffice_agent_identity=NexaOnlyofficeAgentIdentity(
                agent_user_id="nexa-agent",
                display_name="Nexa",
            ),
            load_canonical_file=lambda save_signal: NexaCanonicalFileSnapshot(
                scope=NexaScopeContext(
                    tenant_id="example.com",
                    integration_surface="nextcloud-files",
                    file_id="file-991",
                    file_version="171",
                ),
                content="unused",
                etag='"etag-171"',
                acl_principals=("clay",),
                acl_complete=True,
            ),
            onlyoffice_reconcile_executor=reconcile_executor,
        ),
        now=_ts(13, 1),
    )

    assert ack == {"status_code": 200, "body": {"error": 0}}
    assert result.status == "completed"
    onlyoffice_result = result.result
    assert isinstance(onlyoffice_result, NexaOnlyofficeRuntimeResult)
    assert onlyoffice_result.decision.action == "await_final_close"
    assert onlyoffice_result.memory_read.status == "skipped"
    assert onlyoffice_result.memory_write.status == "skipped"
    assert called["executor"] == 0


def test_sidecar_seeds_nexa_openclaw_session(tmp_path: Path) -> None:
    state_dir = tmp_path / ".nexa" / "state"
    state_dir.mkdir(parents=True)

    _ensure_nexa_openclaw_session(
        {
            "OPENCLAW_NEXA_AGENT_DISPLAY_NAME": "Nexa",
        },
        state_dir=state_dir,
    )

    session_index = tmp_path / "agents" / "nexa" / "sessions" / "sessions.json"
    assert session_index.exists()
    data = json.loads(session_index.read_text())
    assert "agent:nexa:main" in data
    entry = data["agent:nexa:main"]
    assert entry["chatType"] == "direct"
    assert entry["lastChannel"] == "nextcloud-talk"
    assert entry["origin"]["provider"] == "nextcloud-talk"
    assert entry["origin"]["label"] == "Nexa"
    assert Path(entry["sessionFile"]).exists()


def test_openclaw_session_key_uses_thread_context() -> None:
    from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _openclaw_session_key

    payload = {
        "conversation": {"id": "room-1"},
        "context": {"threadId": "thread-9"},
        "message": {"id": "123", "text": "hello"},
    }
    assert _openclaw_session_key(payload) == "agent:nexa:nextcloud-room-1-thread-thread-9"


def test_openclaw_responses_text_extracts_assistant_output() -> None:
    from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _extract_openclaw_responses_text

    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "First line."},
                    {"type": "output_text", "text": "Second line."},
                ],
            },
            {"type": "function_call", "name": "exec"},
        ]
    }

    assert _extract_openclaw_responses_text(payload) == "First line.\n\nSecond line."


@pytest.mark.parametrize(
    ("payload", "expected_text"),
    (
        ({"output_text": "Top-level response text."}, "Top-level response text."),
        (
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"text": "Content text without output_text type."}],
                    }
                ]
            },
            "Content text without output_text type.",
        ),
    ),
)
def test_openclaw_responses_text_extracts_additional_response_shapes(
    payload: dict[str, object], expected_text: str
) -> None:
    from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _extract_openclaw_responses_text

    assert _extract_openclaw_responses_text(payload) == expected_text


def test_openclaw_talk_reply_planner_uses_responses_endpoint_and_history(monkeypatch: pytest.MonkeyPatch) -> None:
    from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _openclaw_talk_reply_planner

    recorded: dict[str, object] = {}

    def fake_json_request(url: str, *, method: str, body: dict[str, object], headers: dict[str, str], auth_user=None, auth_password=None, timeout_seconds=None):
        recorded["url"] = url
        recorded["method"] = method
        recorded["body"] = body
        recorded["headers"] = headers
        recorded["timeout_seconds"] = timeout_seconds
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Ran through OpenClaw."}],
                }
            ]
        }

    monkeypatch.setattr(
        "dokploy_wizard.packs.openclaw.nexa_runtime_sidecar._json_request",
        fake_json_request,
    )

    payload = {
        "conversation": {"id": "room-1", "token": "room-token"},
        "initiator": {"id": "admin@example.com"},
        "message": {"id": "101", "text": "right now, but eastern"},
        "context": {"threadId": "thread-9"},
        "recentConversation": [
            {"role": "user", "text": "what time is in central standard usa time right now"},
            {"role": "assistant", "text": "Ran terminal command TZ=\"America/Chicago\" date +\"%H:%M:%S\"."},
        ],
    }
    memory = type("Memory", (), {"hits": ()})()
    env = {
        "DOKPLOY_WIZARD_OPENCLAW_INTERNAL_URL": "http://openclaw:18789",
        "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
    }

    result = _openclaw_talk_reply_planner(payload, memory, env=env)

    assert result is not None
    assert result.text == "Ran through OpenClaw."
    assert recorded["url"] == "http://openclaw:18789/v1/responses"
    assert recorded["method"] == "POST"
    assert recorded["timeout_seconds"] == 90
    body = recorded["body"]
    assert isinstance(body, dict)
    assert body["model"] == "openclaw/nexa"
    assert body["stream"] is False
    input_items = body["input"]
    assert isinstance(input_items, list)
    assert input_items[-1] == {"type": "message", "role": "user", "content": "right now, but eastern"}
    headers = recorded["headers"]
    assert headers["x-openclaw-agent-id"] == "nexa"
    assert headers["x-openclaw-message-channel"] == "nextcloud-talk"
    assert headers["x-openclaw-session-key"] == "agent:nexa:nextcloud-room-1-thread-thread-9"


def test_send_talk_reply_falls_back_to_bot_endpoint_when_basic_chat_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _send_talk_reply

    calls: list[dict[str, object]] = []

    def fake_json_request(url: str, *, method: str, body: dict[str, object], headers: dict[str, str], auth_user=None, auth_password=None, timeout_seconds=None):
        del timeout_seconds
        calls.append(
            {
                "url": url,
                "method": method,
                "body": body,
                "headers": headers,
                "auth_user": auth_user,
                "auth_password": auth_password,
            }
        )
        if "/chat/" in url:
            raise RuntimeError("HTTP 403 from https://nextcloud.example.com/ocs/v2.php/apps/spreed/api/v1/chat/room-token: forbidden")
        return {"ocs": {"data": {"id": "bot-message-101"}, "meta": {"requestid": "request-101"}}}

    monkeypatch.setattr(
        "dokploy_wizard.packs.openclaw.nexa_runtime_sidecar._json_request",
        fake_json_request,
    )

    result = _send_talk_reply(
        {
            "conversationToken": "room-token",
            "message": "Nexa reply from bot fallback.",
            "replyTo": {"messageId": "parent-100"},
        },
        env={
            "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL": "https://nextcloud.example.com",
            "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
            "OPENCLAW_NEXA_AGENT_PASSWORD": "app-password",
            "OPENCLAW_NEXA_TALK_SHARED_SECRET": "talk-shared-secret",
            "OPENCLAW_NEXA_TALK_SIGNING_SECRET": "talk-signing-secret",
        },
    )

    assert result == {"messageId": "bot-message-101", "requestId": "request-101"}
    assert len(calls) == 2
    chat_call, bot_call = calls
    assert chat_call["url"] == "https://nextcloud.example.com/ocs/v2.php/apps/spreed/api/v1/chat/room-token"
    assert chat_call["auth_user"] == "nexa-agent"
    assert chat_call["auth_password"] == "app-password"
    assert bot_call["url"] == "https://nextcloud.example.com/ocs/v2.php/apps/spreed/api/v1/bot/room-token/message"
    assert bot_call["auth_user"] is None
    assert bot_call["auth_password"] is None
    bot_headers = bot_call["headers"]
    assert isinstance(bot_headers, dict)
    assert bot_headers["X-Nextcloud-Talk-Secret"] == "talk-shared-secret"
    assert bot_headers["X-Nextcloud-Talk-Bot-Random"]
    assert bot_headers["X-Nextcloud-Talk-Bot-Signature"]
    assert bot_headers["X-Nextcloud-Talk-Bot-Signature"] != "talk-signing-secret"
    assert bot_call["body"] == chat_call["body"]


def test_send_talk_reply_does_not_fallback_to_bot_endpoint_for_non_forbidden_chat_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _send_talk_reply

    calls: list[str] = []

    def fake_json_request(url: str, **kwargs):
        del kwargs
        calls.append(url)
        raise RuntimeError("HTTP 500 from https://nextcloud.example.com/ocs/v2.php/apps/spreed/api/v1/chat/room-token: unavailable")

    monkeypatch.setattr(
        "dokploy_wizard.packs.openclaw.nexa_runtime_sidecar._json_request",
        fake_json_request,
    )

    with pytest.raises(RuntimeError, match="HTTP 500"):
        _send_talk_reply(
            {
                "conversationToken": "room-token",
                "message": "Nexa reply.",
            },
            env={
                "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL": "https://nextcloud.example.com",
                "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
                "OPENCLAW_NEXA_AGENT_PASSWORD": "app-password",
                "OPENCLAW_NEXA_TALK_SHARED_SECRET": "talk-shared-secret",
                "OPENCLAW_NEXA_TALK_SIGNING_SECRET": "talk-signing-secret",
            },
        )

    assert calls == ["https://nextcloud.example.com/ocs/v2.php/apps/spreed/api/v1/chat/room-token"]


def test_talk_runtime_passes_recent_conversation_to_planner(tmp_path: Path) -> None:
    store = DurableQueueStore(tmp_path)

    first_payload = load_json_fixture("nexa-talk-webhook-room-message.json")
    first_payload["message"]["id"] = "100"
    first_payload["message"]["text"] = "get it in central usa time"
    first_body = json_bytes(first_payload)
    handle_talk_webhook(
        body=first_body,
        headers=build_talk_headers(first_body),
        talk_shared_secret=TALK_SHARED_SECRET,
        talk_signing_secret=TALK_SIGNING_SECRET,
        store=store,
    )
    first_job = store.lease_next_job(lease_owner="worker-talk", now=datetime.now(UTC))
    assert first_job is not None
    first_outbound: list[dict[str, object]] = []
    first_result = run_queued_nexa_job(
        first_job,
        store=store,
        env={},
        dependencies=NexaRuntimeDependencies(
            talk_reply_planner=lambda payload, memory: NexaPlannedTalkReply(
                text="Central time is 11:34:56.",
                memory_content="Converted the current time to central time for the user.",
            ),
            talk_sender=lambda outbound_payload: first_outbound.append(outbound_payload) or {"messageId": "sent-100"},
            onlyoffice_agent_identity=NexaOnlyofficeAgentIdentity(
                agent_user_id="nexa-agent",
                display_name="Nexa",
            ),
            load_canonical_file=lambda save_signal: NexaCanonicalFileSnapshot(
                scope=NexaScopeContext(
                    tenant_id="example.com",
                    integration_surface="nextcloud-files",
                    file_id="file-991",
                    file_version="171",
                ),
                content="unused",
                etag='"etag-171"',
                acl_principals=("clay",),
                acl_complete=True,
            ),
            onlyoffice_reconcile_executor=lambda decision, save_signal, canonical_file, memory: NexaOnlyofficeActionResult(
                outcome="skipped",
                authoritative_write=False,
            ),
        ),
        now=_ts(12, 47),
    )
    assert first_result.status == "completed"
    assert first_outbound

    second_payload = load_json_fixture("nexa-talk-webhook-room-message.json")
    second_payload["webhookEventId"] = "webhook-event-101"
    second_payload["message"]["id"] = "101"
    second_payload["message"]["text"] = "right now"
    second_body = json_bytes(second_payload)
    handle_talk_webhook(
        body=second_body,
        headers=build_talk_headers(second_body),
        talk_shared_secret=TALK_SHARED_SECRET,
        talk_signing_secret=TALK_SIGNING_SECRET,
        store=store,
    )
    second_job = store.lease_next_job(lease_owner="worker-talk", now=datetime.now(UTC))
    assert second_job is not None
    planner_payloads: list[dict[str, object]] = []
    second_result = run_queued_nexa_job(
        second_job,
        store=store,
        env={},
        dependencies=NexaRuntimeDependencies(
            talk_reply_planner=lambda payload, memory: planner_payloads.append(payload) or NexaPlannedTalkReply(
                text="Right now in Central Time it's 11:34:56.",
                memory_content="Answered follow-up current-time question using recent conversation context.",
            ),
            talk_sender=lambda outbound_payload: {"messageId": "sent-101"},
            onlyoffice_agent_identity=NexaOnlyofficeAgentIdentity(
                agent_user_id="nexa-agent",
                display_name="Nexa",
            ),
            load_canonical_file=lambda save_signal: NexaCanonicalFileSnapshot(
                scope=NexaScopeContext(
                    tenant_id="example.com",
                    integration_surface="nextcloud-files",
                    file_id="file-991",
                    file_version="171",
                ),
                content="unused",
                etag='"etag-171"',
                acl_principals=("clay",),
                acl_complete=True,
            ),
            onlyoffice_reconcile_executor=lambda decision, save_signal, canonical_file, memory: NexaOnlyofficeActionResult(
                outcome="skipped",
                authoritative_write=False,
            ),
        ),
        now=_ts(12, 48),
    )
    assert second_result.status == "completed"
    assert planner_payloads
    recent = planner_payloads[0].get("recentConversation")
    assert isinstance(recent, list)
    assert any(item.get("role") == "user" and item.get("text") == "get it in central usa time" for item in recent)
    assert any(item.get("role") == "assistant" and "Central time is 11:34:56." in str(item.get("text")) for item in recent)


@pytest.mark.parametrize(
    ("json_response", "runtime_error", "expected_diagnostic"),
    (
        (None, RuntimeError("OpenClaw pass-through timed out"), "timeout"),
        (None, RuntimeError("HTTP 401 Unauthorized"), "auth_rejected"),
        (None, RuntimeError("HTTP 502 Bad Gateway"), "http_error"),
        (None, RuntimeError("OpenClaw returned non-JSON response"), "non_json"),
        (None, RuntimeError("Request to http://openclaw:18789/v1/responses failed: [Errno -3] Temporary failure in name resolution"), "dns_connectivity"),
        ({"output": []}, None, "empty_output"),
        ({"output_text": "I don't know; not enough information to answer from grounded context."}, None, "grounding_no_answer"),
    ),
)
def test_provider_talk_reply_planner_preserves_fallback_and_records_openclaw_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    json_response: dict[str, object] | None,
    runtime_error: RuntimeError | None,
    expected_diagnostic: str,
) -> None:
    from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _provider_talk_reply_planner

    def fake_json_request(*args, **kwargs):
        del args, kwargs
        if runtime_error is not None:
            raise runtime_error
        assert json_response is not None
        return json_response

    monkeypatch.setattr(
        "dokploy_wizard.packs.openclaw.nexa_runtime_sidecar._json_request",
        fake_json_request,
    )

    payload = {
        "conversation": {"id": "room-1"},
        "initiator": {"id": "adminuser@example.com"},
        "message": {"id": "101", "text": "Use the OpenClaw tools for this."},
        "context": {"threadId": "thread-9"},
    }
    memory = type("Memory", (), {"hits": ()})()
    env = {
        "DOKPLOY_WIZARD_OPENCLAW_INTERNAL_URL": "http://openclaw:18789",
        "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
    }

    reply = _provider_talk_reply_planner(payload, memory, env=env)

    assert reply.text == _OPENCLAW_FAIL_CLOSED_FALLBACK
    if expected_diagnostic not in reply.memory_content:
        raise AssertionError(
            f"OpenClaw fallback evidence did not include diagnostic category {expected_diagnostic!r}."
        )
    diagnostic_json = reply.memory_content.split("Redacted diagnostic: ", 1)[1]
    diagnostic = json.loads(diagnostic_json)
    assert diagnostic["category"] == expected_diagnostic
    assert payload["message"]["text"] not in reply.memory_content


def test_nexa_bridge_verifier_classifies_http_500_without_exposing_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import (
        _OPENCLAW_FAIL_CLOSED_FALLBACK,
        verify_openclaw_nexa_bridge,
    )

    def fake_json_request(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("HTTP 500 from http://openclaw:18789/v1/responses: raw-secret-body")

    monkeypatch.setattr(
        "dokploy_wizard.packs.openclaw.nexa_runtime_sidecar._json_request",
        fake_json_request,
    )

    result = verify_openclaw_nexa_bridge(env=_nexa_bridge_env())
    rendered = json.dumps(result, sort_keys=True)

    assert result["passed"] is False
    assert result["reply_status"] == "fail_closed"
    assert result["fail_closed_fallback"] == _OPENCLAW_FAIL_CLOSED_FALLBACK
    diagnostic = result["diagnostic"]
    assert isinstance(diagnostic, dict)
    assert diagnostic["category"] == "http_error"
    assert diagnostic["http_status"] == 500
    assert "raw-secret-body" not in rendered


def test_nexa_bridge_verifier_classifies_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import verify_openclaw_nexa_bridge

    def fake_json_request(*args, **kwargs):
        del args, kwargs
        return {"output": []}

    monkeypatch.setattr(
        "dokploy_wizard.packs.openclaw.nexa_runtime_sidecar._json_request",
        fake_json_request,
    )

    result = verify_openclaw_nexa_bridge(env=_nexa_bridge_env())

    assert result["passed"] is False
    assert result["reply_status"] == "fail_closed"
    diagnostic = result["diagnostic"]
    assert isinstance(diagnostic, dict)
    assert diagnostic == {"category": "empty_output", "reason": "empty_response_schema"}


def _nexa_bridge_env() -> dict[str, str]:
    return {
        "DOKPLOY_WIZARD_OPENCLAW_INTERNAL_URL": "http://openclaw:18789",
        "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
    }
