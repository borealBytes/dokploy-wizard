# pyright: reportMissingImports=false

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dokploy_wizard.packs.openclaw.nexa_scope import build_talk_scope
from dokploy_wizard.packs.openclaw.nexa_talk_reply import (
    NexaTalkReplyRequest,
    build_talk_reply_payload,
    deliver_talk_reply,
)
from dokploy_wizard.state import DurableQueueStore, load_outbound_delivery_log

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _ts(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 4, 19, hour, minute, second, tzinfo=UTC)


def _load_talk_payload() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "nexa-talk-webhook-room-message.json").read_text(encoding="utf-8"))


def _build_request(payload: dict[str, Any], *, delivery_key: str = "talk-reply:room-42:msg-845") -> NexaTalkReplyRequest:
    return NexaTalkReplyRequest(
        scope=build_talk_scope(payload),
        delivery_key=delivery_key,
        conversation_id=payload["conversation"]["id"],
        reply_to_message_id=payload["message"]["id"],
        text="Nexa reply for the room.",
        capabilities=payload.get("capabilities", {}),
        context=payload.get("context"),
    )


def test_build_talk_reply_payload_degrades_to_room_reply_when_thread_context_is_not_supported() -> None:
    payload = _load_talk_payload()
    payload["capabilities"]["threads"] = False

    reply_payload, thread_mode = build_talk_reply_payload(_build_request(payload))

    assert thread_mode == "room"
    assert reply_payload == {
        "conversationId": "room-42",
        "message": "Nexa reply for the room.",
        "replyTo": {"messageId": "msg-845"},
    }


def test_deliver_talk_reply_records_pending_delivery_before_sender_returns_and_marks_sent(
    tmp_path: Path,
) -> None:
    payload = _load_talk_payload()
    request = _build_request(payload)
    store = DurableQueueStore(tmp_path)
    observed_statuses: list[str | None] = []

    def sender(outbound_payload: dict[str, Any]) -> dict[str, Any]:
        recorded = store.get_outbound_delivery(channel="nextcloud-talk", delivery_key=request.delivery_key)
        observed_statuses.append(None if recorded is None else recorded.status)
        assert outbound_payload["context"] == {"threadId": "thread-room-42-msg-840"}
        return {
            "messageId": "sent-msg-900",
            "requestId": "request-42",
        }

    dispatch = deliver_talk_reply(request, store=store, sender=sender, now=_ts(17, 0))
    delivery_log = load_outbound_delivery_log(tmp_path)

    assert observed_statuses == ["pending"]
    assert dispatch.outcome == "sent"
    assert dispatch.visible_send is True
    assert dispatch.thread_mode == "thread"
    assert len(delivery_log.records) == 1
    assert delivery_log.records[0].status == "sent"
    assert delivery_log.records[0].remote_message_id == "sent-msg-900"
    assert delivery_log.records[0].remote_request_id == "request-42"
    assert delivery_log.records[0].thread_mode == "thread"
    assert delivery_log.records[0].thread_id == "thread-room-42-msg-840"


def test_deliver_talk_reply_skips_duplicate_visible_send_after_prior_delivery_was_recorded(
    tmp_path: Path,
) -> None:
    payload = _load_talk_payload()
    request = _build_request(payload, delivery_key="talk-reply:room-42:msg-845:dedupe")
    store = DurableQueueStore(tmp_path)
    sent_payloads: list[dict[str, Any]] = []

    def sender(outbound_payload: dict[str, Any]) -> dict[str, Any]:
        sent_payloads.append(outbound_payload)
        return {
            "messageId": "sent-msg-901",
            "requestId": "request-43",
        }

    first_dispatch = deliver_talk_reply(request, store=store, sender=sender, now=_ts(17, 5))
    second_dispatch = deliver_talk_reply(request, store=store, sender=sender, now=_ts(17, 6))

    assert first_dispatch.outcome == "sent"
    assert first_dispatch.visible_send is True
    assert second_dispatch.outcome == "duplicate"
    assert second_dispatch.visible_send is False
    assert len(sent_payloads) == 1
    assert second_dispatch.delivery_record.remote_message_id == "sent-msg-901"
    assert second_dispatch.payload == sent_payloads[0]


def test_deliver_talk_reply_downgrades_to_room_delivery_when_request_context_thread_does_not_match_scope(
    tmp_path: Path,
) -> None:
    payload = _load_talk_payload()
    request = NexaTalkReplyRequest(
        scope=build_talk_scope(payload),
        delivery_key="talk-reply:room-42:msg-845:room-mode",
        conversation_id=payload["conversation"]["id"],
        reply_to_message_id=payload["message"]["id"],
        text="Nexa reply for the room.",
        capabilities=payload.get("capabilities", {}),
        context={"threadId": "other-thread"},
    )
    store = DurableQueueStore(tmp_path)

    dispatch = deliver_talk_reply(
        request,
        store=store,
        sender=lambda outbound_payload: {"messageId": "sent-msg-902", "requestId": "request-44"},
        now=_ts(17, 10),
    )
    delivery_log = load_outbound_delivery_log(tmp_path)

    assert dispatch.thread_mode == "room"
    assert dispatch.payload == {
        "conversationId": "room-42",
        "message": "Nexa reply for the room.",
        "replyTo": {"messageId": "msg-845"},
    }
    assert delivery_log.records[0].thread_mode == "room"
    assert delivery_log.records[0].thread_id is None
