# pyright: reportMissingImports=false

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dokploy_wizard.packs.openclaw.nexa_ingress import handle_onlyoffice_callback, handle_talk_webhook
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
    job = store.lease_next_job(lease_owner="worker-talk", now=_ts(12, 46))
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
    job = store.lease_next_job(lease_owner="worker-doc", now=_ts(13, 0))
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
