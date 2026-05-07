# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

from dokploy_wizard.packs.openclaw.nexa_ingress import (
    handle_onlyoffice_callback,
    handle_talk_webhook,
)
from dokploy_wizard.packs.openclaw.nexa_mem0_client import NexaMem0Client
from dokploy_wizard.packs.openclaw.nexa_memory import (
    NexaMemoryWriteRequest,
    build_nexa_mem0_config,
    build_nexa_memory_scopes,
    evaluate_memory_write_policy,
)
from dokploy_wizard.packs.openclaw.nexa_onlyoffice import (
    NexaOnlyofficeAgentIdentity,
    build_onlyoffice_save_signal,
    evaluate_onlyoffice_reconcile_policy,
)
from dokploy_wizard.packs.openclaw.nexa_retrieval import (
    NexaCanonicalFileSnapshot,
    NexaFtsBootstrapState,
    NexaFtsCandidateHit,
    evaluate_retrieval_gate,
    resolve_retrieval_plan,
)
from dokploy_wizard.packs.openclaw.nexa_runtime import (
    NexaOnlyofficeActionResult,
    NexaOnlyofficeRuntimeResult,
    NexaPlannedTalkReply,
    NexaRuntimeDependencies,
    NexaTalkRuntimeResult,
    run_queued_nexa_job,
)
from dokploy_wizard.packs.openclaw.nexa_scope import NexaScopeContext, build_talk_scope
from dokploy_wizard.packs.openclaw.nexa_talk_reply import (
    NexaTalkReplyRequest,
    deliver_talk_reply,
)
from dokploy_wizard.state import (
    DurableQueueStore,
    load_job_queue_state,
    load_outbound_delivery_log,
)
from dokploy_wizard.state.queue_policy import sweep_expired_leases
from tests.nexa_mem0_test_server import mem0_base_url, run_recording_mem0_server

from .nexa_e2e_helpers import (
    ONLYOFFICE_CALLBACK_SECRET,
    TALK_SHARED_SECRET,
    TALK_SIGNING_SECRET,
    build_onlyoffice_headers,
    build_talk_headers,
    json_bytes,
    load_json_fixture,
    write_evidence,
)


def _mem0_env(base_url: str) -> dict[str, str]:
    return {
        "OPENCLAW_NEXA_MEM0_BASE_URL": base_url,
        "OPENCLAW_NEXA_MEM0_API_KEY": "mem0-api-key",
        "OPENCLAW_NEXA_MEM0_LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "OPENCLAW_NEXA_MEM0_LLM_API_KEY": "nvidia-api-key",
        "OPENCLAW_NEXA_MEM0_VECTOR_BACKEND": "qdrant",
        "OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL": "http://qdrant:6333",
        "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY": "vector-api-key",
    }


def _runtime_ts(hour: int, minute: int = 0) -> datetime:
    return datetime(datetime.now(tz=UTC).year + 1, 4, 20, hour, minute, tzinfo=UTC)


def _fresh_canonical_file(*, file_version: str, acl_complete: bool = True) -> NexaCanonicalFileSnapshot:
    return NexaCanonicalFileSnapshot(
        scope=NexaScopeContext(
            tenant_id="example.com",
            integration_surface="nextcloud-files",
            file_id="file-991",
            file_version=file_version,
        ),
        content="Action items from the canonical Q2 plan",
        etag=f'"etag-{file_version}"',
        acl_principals=("clay", "nexa") if acl_complete else (),
        acl_complete=acl_complete,
    )


def _record_document_action(
    document_actions: list[dict[str, str]],
    *,
    gate_action: str,
    save_reason: str,
    agent_identity: NexaOnlyofficeAgentIdentity,
    target_path: str | None,
) -> None:
    if gate_action != "proceed":
        return
    document_actions.append(
        {
            "actor_user_id": agent_identity.agent_user_id,
            "actor_display_name": agent_identity.display_name,
            "mode": "update_original_with_track_changes",
            "reason": save_reason,
            "target_path": target_path or "<unknown>",
        }
    )


def test_talk_event_runs_through_queue_memory_retrieval_and_one_visible_reply(tmp_path: Path) -> None:
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

    leased = store.lease_next_job(lease_owner="worker-talk", now=_runtime_ts(12, 46))
    assert leased is not None
    talk_scope = build_talk_scope(payload)
    memory_scopes = build_nexa_memory_scopes(talk_scope)
    summary_memory = evaluate_memory_write_policy(
        NexaMemoryWriteRequest(
            scope=talk_scope,
            target_layer="shared",
            content="Action items: verify the Q2 edits and post the summary back to the thread.",
            content_class="assistant_summary",
            visibility="shared",
        )
    )
    transcript_memory = evaluate_memory_write_policy(
        NexaMemoryWriteRequest(
            scope=talk_scope,
            target_layer="durable_facts",
            content=payload["message"]["text"],
            content_class="raw_room_transcript",
            visibility="shared",
        )
    )
    retrieval_scope = NexaScopeContext(
        tenant_id=talk_scope.tenant_id,
        integration_surface="onlyoffice-document-server",
        file_id="file-991",
        file_version="171",
    )
    retrieval_plan = resolve_retrieval_plan(
        retrieval_scope,
        canonical_file=_fresh_canonical_file(file_version="171"),
        fts_bootstrap=NexaFtsBootstrapState(
            enabled=True,
            collection_initialized=True,
            privileged_auth=True,
            done_acknowledged=True,
            tombstones_enabled=True,
            content_encoding="base64",
        ),
        fts_candidate=NexaFtsCandidateHit(
            scope=NexaScopeContext(
                tenant_id="example.com",
                integration_surface="nextcloud-files",
                file_id="file-991",
                file_version="171",
            ),
            document_id="fts-doc-991",
            indexed_at="2026-04-19T12:45:30+00:00",
            acl_complete=True,
            acl_principals=("clay", "nexa"),
            content=base64.b64encode(b"Q2 edits mention two follow-ups.").decode("ascii"),
        ),
    )
    sent_payloads: list[dict[str, object]] = []
    with run_recording_mem0_server(
        search_results=[
            {
                "id": "mem-room-42-1",
                "memory": "The room expects a concise Q2 write-back summary after visible send.",
                "score": 0.88,
                "metadata": {
                    "namespace": memory_scopes.shared_memory.namespace if memory_scopes.shared_memory else "",
                    "layer": "shared",
                },
            }
        ]
    ) as mem0_server:
        runtime_result = run_queued_nexa_job(
            leased,
            store=store,
            env=_mem0_env(mem0_base_url(mem0_server)),
            dependencies=NexaRuntimeDependencies(
                talk_reply_planner=lambda payload, memory: NexaPlannedTalkReply(
                    text="Two action items: review the Q2 edits and confirm the write-back before close.",
                    memory_content="Shared room summary: review the Q2 edits and confirm the write-back before close.",
                ),
                talk_sender=lambda outbound_payload: (
                    sent_payloads.append(outbound_payload) or {"messageId": "talk-sent-900", "requestId": "request-42"}
                ),
                onlyoffice_agent_identity=NexaOnlyofficeAgentIdentity(
                    agent_user_id="nexa-agent",
                    display_name="Nexa",
                ),
                load_canonical_file=lambda save_signal: _fresh_canonical_file(file_version="171"),
                onlyoffice_reconcile_executor=lambda decision, save_signal, canonical_file, memory: NexaOnlyofficeActionResult(
                    outcome="skipped",
                    authoritative_write=False,
                ),
                mem0_client=NexaMem0Client(build_nexa_mem0_config(_mem0_env(mem0_base_url(mem0_server)))),
            ),
            now=_runtime_ts(12, 47),
        )
        second_dispatch = deliver_talk_reply(
            NexaTalkReplyRequest(
                scope=talk_scope,
                delivery_key="talk-reply:evt-talk-room-42-msg-845-v2",
                conversation_id=payload["conversation"]["id"],
                conversation_token=payload["conversation"].get("token"),
                reply_to_message_id=payload["message"]["id"],
                text="Two action items: review the Q2 edits and confirm the write-back before close.",
                capabilities=payload["capabilities"],
                context=payload["context"],
            ),
            store=store,
            sender=lambda outbound_payload: {"messageId": "talk-sent-901", "requestId": "request-43"},
            now=_runtime_ts(12, 48),
        )
    talk_runtime_result = runtime_result.result
    assert isinstance(talk_runtime_result, NexaTalkRuntimeResult)
    evidence_path = write_evidence(
        tmp_path,
        scenario="talk_flow",
        evidence={
            "ack": ack,
            "job_id": leased.job_id,
            "memory_namespace": memory_scopes.shared_memory.namespace if memory_scopes.shared_memory else None,
            "retrieval_reason": retrieval_plan.usage.reason,
            "retrieval_pointer_allowed": retrieval_plan.candidate_decision.allowed,
            "reply_outcomes": [talk_runtime_result.reply_dispatch.outcome, second_dispatch.outcome],
            "visible_send_count": len(sent_payloads),
            "memory_read_status": talk_runtime_result.memory_read.status,
            "memory_write_status": talk_runtime_result.memory_write.status,
            "mem0_paths": [request.path for request in mem0_server.requests],
        },
    )

    assert ack == {"status_code": 202, "body": {"accepted": True}}
    assert leased.kind == "nexa.talk.process_message"
    assert leased.scope_key == talk_scope.queue_scope_key()
    assert memory_scopes.shared_memory is not None
    assert summary_memory.allowed is True
    assert summary_memory.target_layer == "shared"
    assert transcript_memory.allowed is False
    assert transcript_memory.reason == "raw_room_transcript_is_not_durable"
    assert retrieval_plan.usage.action == "proceed"
    assert retrieval_plan.candidate_decision.allowed is True
    assert runtime_result.status == "completed"
    assert talk_runtime_result.memory_read.status == "ok"
    assert talk_runtime_result.memory_write.status == "ok"
    assert talk_runtime_result.reply_dispatch.outcome == "sent"
    assert talk_runtime_result.reply_dispatch.visible_send is True
    assert second_dispatch.outcome == "duplicate"
    assert second_dispatch.visible_send is False
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["context"] == {"threadId": "thread-room-42-msg-840"}
    assert load_outbound_delivery_log(tmp_path).records[0].remote_message_id == "talk-sent-900"
    assert evidence_path.exists()


def test_onlyoffice_force_save_defers_until_final_close_then_reconciles_with_pinned_identity(
    tmp_path: Path,
) -> None:
    store = DurableQueueStore(tmp_path)
    agent_identity = NexaOnlyofficeAgentIdentity(agent_user_id="nexa-agent", display_name="Nexa")
    force_payload = load_json_fixture("nexa-onlyoffice-callback-status-6.json")
    final_payload = load_json_fixture("nexa-onlyoffice-callback-status-2.json")
    final_payload["history"]["serverVersion"] = "172"
    final_payload["url"] = "https://office.example.com/cache/files/document-key-abc123/output-final.docx"

    force_ack = handle_onlyoffice_callback(
        body=json_bytes(force_payload),
        headers=build_onlyoffice_headers(),
        callback_secret=ONLYOFFICE_CALLBACK_SECRET,
        store=store,
    )
    final_ack = handle_onlyoffice_callback(
        body=json_bytes(final_payload),
        headers=build_onlyoffice_headers(),
        callback_secret=ONLYOFFICE_CALLBACK_SECRET,
        store=store,
    )

    authoritative_job = store.lease_next_job(lease_owner="worker-doc", now=_runtime_ts(13, 0))
    assert authoritative_job is not None
    authoritative_signal = build_onlyoffice_save_signal(final_payload)
    authoritative_decision = evaluate_onlyoffice_reconcile_policy(
        authoritative_signal,
        agent_identity=agent_identity,
    )
    authoritative_gate = evaluate_retrieval_gate(
        authoritative_signal.scope,
        canonical_file=_fresh_canonical_file(file_version="172"),
    )
    document_actions: list[dict[str, str]] = []
    with run_recording_mem0_server(
        search_results=[
            {
                "id": "mem-file-991-1",
                "memory": "Project file-991 expects in-place tracked write-back for authoritative final-close saves.",
                "score": 0.83,
                "metadata": {"file_id": "file-991", "layer": "durable_facts"},
            }
        ]
    ) as mem0_server:
        runtime_result = run_queued_nexa_job(
            authoritative_job,
            store=store,
            env=_mem0_env(mem0_base_url(mem0_server)),
            dependencies=NexaRuntimeDependencies(
                talk_reply_planner=lambda payload, memory: NexaPlannedTalkReply(
                    text="unused",
                    memory_content="unused",
                ),
                talk_sender=lambda outbound_payload: {"messageId": "unused"},
                onlyoffice_agent_identity=agent_identity,
                load_canonical_file=lambda save_signal: _fresh_canonical_file(file_version="172"),
                onlyoffice_reconcile_executor=lambda decision, save_signal, canonical_file, memory: (
                    _record_document_action(
                        document_actions,
                        gate_action="proceed",
                        save_reason=decision.reason,
                        agent_identity=agent_identity,
                        target_path=save_signal.path,
                    )
                    or NexaOnlyofficeActionResult(
                        outcome="applied",
                        authoritative_write=True,
                        memory_content="Authoritative reconcile applied to file-991 with tracked changes.",
                    )
                ),
                mem0_client=NexaMem0Client(build_nexa_mem0_config(_mem0_env(mem0_base_url(mem0_server)))),
            ),
            now=_runtime_ts(13, 1),
        )
        mem0_paths = [request.path for request in mem0_server.requests]
    onlyoffice_runtime_result = runtime_result.result
    assert isinstance(onlyoffice_runtime_result, NexaOnlyofficeRuntimeResult)
    deferred_signal = build_onlyoffice_save_signal(force_payload)
    deferred_decision = evaluate_onlyoffice_reconcile_policy(
        deferred_signal,
        agent_identity=agent_identity,
        finalized_versions=frozenset({"172"}),
    )
    queue_after_final_close = sweep_expired_leases(
        load_job_queue_state(tmp_path),
        now=_runtime_ts(13, 2).isoformat(),
    )
    evidence_path = write_evidence(
        tmp_path,
        scenario="onlyoffice_reconcile_flow",
        evidence={
            "force_ack": force_ack,
            "final_ack": final_ack,
            "authoritative_reason": authoritative_decision.reason,
            "authoritative_gate": authoritative_gate.reason,
            "deferred_reason": deferred_decision.reason,
            "deferred_queue_statuses": [job.status for job in queue_after_final_close.jobs],
            "document_actions": document_actions,
            "memory_read_status": onlyoffice_runtime_result.memory_read.status,
            "memory_write_status": onlyoffice_runtime_result.memory_write.status,
            "mem0_paths": mem0_paths,
        },
    )

    assert force_ack == {"status_code": 200, "body": {"error": 0}}
    assert final_ack == {"status_code": 200, "body": {"error": 0}}
    assert authoritative_job.kind == "nexa.onlyoffice.reconcile_saved_document"
    assert authoritative_job.lane == "foreground"
    assert authoritative_decision.action == "reconcile"
    assert authoritative_decision.authoritative is True
    assert authoritative_decision.requires_fresh_canonical_file is True
    assert authoritative_gate.action == "proceed"
    assert authoritative_decision.write_back_policy.mode == "update_original_with_track_changes"
    assert authoritative_decision.write_back_policy.agent_identity == agent_identity
    assert authoritative_decision.write_back_policy.comment_reply_attribution_source == "configured_agent_identity"
    assert runtime_result.status == "completed"
    assert onlyoffice_runtime_result.memory_read.status == "ok"
    assert onlyoffice_runtime_result.memory_write.status == "ok"
    assert deferred_decision.action == "await_final_close"
    assert deferred_decision.reason == "force_save_superseded_by_final_close"
    assert [job.status for job in queue_after_final_close.jobs] == ["superseded", "completed"]
    assert document_actions == [
        {
            "actor_display_name": "Nexa",
            "actor_user_id": "nexa-agent",
            "mode": "update_original_with_track_changes",
            "reason": "final_close_save_signal",
            "target_path": "/Projects/Q2 Plan.docx",
        }
    ]
    assert evidence_path.exists()


def test_stale_or_acl_incomplete_canonical_state_blocks_downstream_document_actions(
    tmp_path: Path,
) -> None:
    agent_identity = NexaOnlyofficeAgentIdentity(agent_user_id="nexa-agent", display_name="Nexa")
    save_signal = build_onlyoffice_save_signal(load_json_fixture("nexa-onlyoffice-callback-status-2.json"))
    reconcile_decision = evaluate_onlyoffice_reconcile_policy(
        save_signal,
        agent_identity=agent_identity,
    )
    stale_gate = evaluate_retrieval_gate(
        save_signal.scope,
        canonical_file=_fresh_canonical_file(file_version="172"),
    )
    acl_gate = evaluate_retrieval_gate(
        save_signal.scope,
        canonical_file=_fresh_canonical_file(file_version="171", acl_complete=False),
    )
    document_actions: list[dict[str, str]] = []
    _record_document_action(
        document_actions,
        gate_action=stale_gate.action,
        save_reason=reconcile_decision.reason,
        agent_identity=agent_identity,
        target_path=save_signal.path,
    )
    _record_document_action(
        document_actions,
        gate_action=acl_gate.action,
        save_reason=reconcile_decision.reason,
        agent_identity=agent_identity,
        target_path=save_signal.path,
    )
    evidence_path = write_evidence(
        tmp_path,
        scenario="onlyoffice_blocked_actions",
        evidence={
            "reconcile_reason": reconcile_decision.reason,
            "stale_gate": stale_gate.reason,
            "acl_gate": acl_gate.reason,
            "document_actions": document_actions,
        },
    )

    assert reconcile_decision.action == "reconcile"
    assert stale_gate.action == "reschedule"
    assert stale_gate.reason == "stale_file_version"
    assert acl_gate.action == "reschedule"
    assert acl_gate.reason == "canonical_acl_metadata_incomplete"
    assert document_actions == []
    assert evidence_path.exists()


def test_queue_fairness_and_backpressure_preserve_background_turn_and_scope_serialization(
    tmp_path: Path,
) -> None:
    store = DurableQueueStore(tmp_path)
    talk_one = load_json_fixture("nexa-talk-webhook-room-message.json")
    talk_two_same_scope = load_json_fixture("nexa-talk-webhook-room-message.json")
    talk_two_same_scope["webhookEventId"] = "evt-talk-room-42-msg-846-v1"
    talk_two_same_scope["message"]["id"] = "msg-846"
    talk_two_same_scope["message"]["text"] = "Another follow-up for the same room thread."
    talk_three_other_scope = load_json_fixture("nexa-talk-webhook-room-message.json")
    talk_three_other_scope["webhookEventId"] = "evt-talk-room-43-msg-901-v1"
    talk_three_other_scope["conversation"]["id"] = "room-43"
    talk_three_other_scope["context"]["roomId"] = "room-43"
    talk_three_other_scope["message"]["id"] = "msg-901"
    talk_three_other_scope["message"]["parent"]["id"] = "msg-900"
    talk_three_other_scope["context"]["threadId"] = "thread-room-43-msg-900"
    background_callback = load_json_fixture("nexa-onlyoffice-callback-status-6.json")

    for payload in (talk_one, talk_two_same_scope, talk_three_other_scope):
        body = json_bytes(payload)
        ack = handle_talk_webhook(
            body=body,
            headers=build_talk_headers(body),
            talk_shared_secret=TALK_SHARED_SECRET,
            talk_signing_secret=TALK_SIGNING_SECRET,
            store=store,
        )
        assert ack == {"status_code": 202, "body": {"accepted": True}}

    background_ack = handle_onlyoffice_callback(
        body=json_bytes(background_callback),
        headers=build_onlyoffice_headers(),
        callback_secret=ONLYOFFICE_CALLBACK_SECRET,
        store=store,
    )

    lease_one = store.lease_next_job(lease_owner="worker-a", now=_runtime_ts(14, 0))
    blocked_cross_scope_while_active = store.lease_next_job(lease_owner="worker-b", now=_runtime_ts(14, 1))
    assert lease_one is not None
    assert blocked_cross_scope_while_active is None

    store.mark_job_completed(job_id=lease_one.job_id, now=_runtime_ts(14, 2))
    lease_two = store.lease_next_job(lease_owner="worker-b", now=_runtime_ts(14, 3))
    assert lease_two is not None
    blocked_background_while_active = store.lease_next_job(lease_owner="worker-c", now=_runtime_ts(14, 4))
    assert blocked_background_while_active is None

    store.mark_job_completed(job_id=lease_two.job_id, now=_runtime_ts(14, 5))
    lease_three = store.lease_next_job(lease_owner="worker-c", now=_runtime_ts(14, 6))
    assert lease_three is not None
    queue_after_background_turn = load_job_queue_state(tmp_path)
    blocked_while_scope_active = store.lease_next_job(lease_owner="worker-d", now=_runtime_ts(14, 7))
    assert blocked_while_scope_active is None

    store.mark_job_completed(job_id=lease_three.job_id, now=_runtime_ts(14, 8))
    resumed_same_scope = store.lease_next_job(lease_owner="worker-d", now=_runtime_ts(14, 9))
    queue_state = load_job_queue_state(tmp_path)
    evidence_path = write_evidence(
        tmp_path,
        scenario="queue_fairness_backpressure",
        evidence={
            "background_ack": background_ack,
            "lease_sequence": [
                lease_one.kind,
                lease_two.kind,
                lease_three.kind,
            ],
            "blocked_cross_scope_while_active": blocked_cross_scope_while_active is None,
            "blocked_background_while_active": blocked_background_while_active is None,
            "lease_one_scope": lease_one.scope_key,
            "lease_two_scope": lease_two.scope_key,
            "background_turn_streak": queue_after_background_turn.foreground_streak,
            "resumed_scope": None if resumed_same_scope is None else resumed_same_scope.scope_key,
            "foreground_streak": queue_state.foreground_streak,
        },
    )

    assert background_ack == {"status_code": 200, "body": {"error": 0}}
    assert lease_one.kind == "nexa.talk.process_message"
    assert lease_two.kind == "nexa.talk.process_message"
    assert lease_two.scope_key == lease_one.scope_key
    assert lease_three.kind == "nexa.onlyoffice.reconcile_saved_document"
    assert lease_three.lane == "background"
    assert blocked_while_scope_active is None
    assert queue_after_background_turn.foreground_streak == 0
    assert resumed_same_scope is not None
    assert resumed_same_scope.kind == "nexa.talk.process_message"
    assert resumed_same_scope.scope_key != lease_one.scope_key
    assert resumed_same_scope.scope_key == build_talk_scope(talk_three_other_scope).queue_scope_key()
    assert queue_state.foreground_streak == 1
    assert evidence_path.exists()
