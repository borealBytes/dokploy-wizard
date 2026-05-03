# ruff: noqa: E501
"""Narrow Talk outbound reply adapter with retry-safe delivery recording."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, Mapping

from .nexa_scope import NexaScopeContext

NexaTalkThreadMode = Literal["room", "thread"]
TalkReplySender = Callable[[dict[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class NexaTalkReplyRequest:
    """Normalized outbound Talk reply request for one queued work item."""

    scope: NexaScopeContext
    delivery_key: str
    conversation_id: str
    conversation_token: str | None
    reply_to_message_id: str
    text: str
    capabilities: Mapping[str, Any]
    context: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.scope.integration_surface != "nextcloud-talk":
            msg = "Talk replies require a nextcloud-talk scope context."
            raise ValueError(msg)
        if self.delivery_key.strip() == "":
            msg = "Talk replies require a non-empty delivery_key."
            raise ValueError(msg)
        if self.conversation_id.strip() == "":
            msg = "Talk replies require a non-empty conversation_id."
            raise ValueError(msg)
        if self.reply_to_message_id.strip() == "":
            msg = "Talk replies require a non-empty reply_to_message_id."
            raise ValueError(msg)
        if self.text.strip() == "":
            msg = "Talk replies require non-empty text."
            raise ValueError(msg)


@dataclass(frozen=True)
class NexaTalkReplyDispatch:
    """Result of one outbound Talk reply dispatch attempt."""

    outcome: Literal["sent", "duplicate"]
    visible_send: bool
    thread_mode: NexaTalkThreadMode
    payload: dict[str, Any]
    delivery_record: Any


def build_talk_reply_payload(request: NexaTalkReplyRequest) -> tuple[dict[str, Any], NexaTalkThreadMode]:
    """Build a JSON payload while degrading thread context safely when unsupported."""

    payload: dict[str, Any] = {
        "conversationId": request.conversation_id,
        "message": request.text,
        "replyTo": {
            "messageId": request.reply_to_message_id,
        },
    }
    if request.conversation_token is not None and request.conversation_token.strip() != "":
        payload["conversationToken"] = request.conversation_token
    thread_id = _resolve_thread_id(request)
    if thread_id is None:
        return payload, "room"
    payload["context"] = {"threadId": thread_id}
    return payload, "thread"


def deliver_talk_reply(
    request: NexaTalkReplyRequest,
    *,
    store: Any,
    sender: TalkReplySender,
    now: datetime | None = None,
) -> NexaTalkReplyDispatch:
    """Record outbound delivery state and suppress duplicate visible retries."""

    existing = store.get_outbound_delivery(channel="nextcloud-talk", delivery_key=request.delivery_key)
    payload, thread_mode = build_talk_reply_payload(request)
    if existing is not None and existing.status == "sent":
        return NexaTalkReplyDispatch(
            outcome="duplicate",
            visible_send=False,
            thread_mode=existing.thread_mode,
            payload=existing.payload,
            delivery_record=existing,
        )

    pending = store.record_outbound_delivery(
        channel="nextcloud-talk",
        delivery_key=request.delivery_key,
        transport="nextcloud-talk-reply",
        scope_key=request.scope.queue_scope_key(),
        conversation_id=request.conversation_id,
        reply_to_message_id=request.reply_to_message_id,
        thread_mode=thread_mode,
        thread_id=_payload_thread_id(payload),
        payload=payload,
        now=now,
    )
    if pending.status == "sent":
        return NexaTalkReplyDispatch(
            outcome="duplicate",
            visible_send=False,
            thread_mode=pending.thread_mode,
            payload=pending.payload,
            delivery_record=pending,
        )

    response = dict(sender(payload))
    sent_record = store.mark_outbound_delivery_sent(
        channel="nextcloud-talk",
        delivery_key=request.delivery_key,
        remote_message_id=_require_response_string(response, "messageId"),
        remote_request_id=_optional_response_string(response, "requestId"),
        now=now,
    )
    return NexaTalkReplyDispatch(
        outcome="sent",
        visible_send=True,
        thread_mode=sent_record.thread_mode,
        payload=payload,
        delivery_record=sent_record,
    )


def _resolve_thread_id(request: NexaTalkReplyRequest) -> str | None:
    if request.capabilities.get("threads") is not True:
        return None
    if request.scope.thread_id is None:
        return None
    if request.context is None:
        return None
    thread_id = request.context.get("threadId")
    if not isinstance(thread_id, str) or thread_id.strip() == "":
        return None
    if thread_id != request.scope.thread_id:
        return None
    return thread_id


def _payload_thread_id(payload: Mapping[str, Any]) -> str | None:
    context = payload.get("context")
    if not isinstance(context, Mapping):
        return None
    thread_id = context.get("threadId")
    return thread_id if isinstance(thread_id, str) and thread_id.strip() != "" else None


def _require_response_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value.strip() == "":
        msg = f"Talk reply sender must return a non-empty '{key}'."
        raise ValueError(msg)
    return value


def _optional_response_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or value.strip() == "":
        return None
    return value


__all__ = [
    "NexaTalkReplyDispatch",
    "NexaTalkReplyRequest",
    "build_talk_reply_payload",
    "deliver_talk_reply",
]
