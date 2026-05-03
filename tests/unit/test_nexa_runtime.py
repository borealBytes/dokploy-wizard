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
        "initiator": {"id": "clayton@superiorbyteworks.com"},
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
