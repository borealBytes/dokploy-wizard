"""Minimal Nexa ingress receivers for queue-first webhook handling."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from typing import Any

from .nexa_scope import (  # pyright: ignore[reportMissingImports]
    build_onlyoffice_scope,
    build_talk_scope,
)


def handle_talk_webhook(
    *,
    body: bytes,
    headers: dict[str, str],
    talk_shared_secret: str,
    talk_signing_secret: str,
    store: Any,
) -> dict[str, Any]:
    """Validate, persist, enqueue, and immediately acknowledge Talk webhooks."""

    normalized_headers = _normalize_headers(headers)
    if normalized_headers.get("x-nextcloud-talk-secret") != talk_shared_secret:
        return _ack(status_code=401, body={"error": "unauthorized"})

    if not _has_valid_talk_signature(
        body=body,
        provided_signature=normalized_headers.get("x-nextcloud-talk-signature"),
        signing_secret=talk_signing_secret,
    ):
        return _ack(status_code=401, body={"error": "invalid_signature"})

    payload = _parse_json_object(body)
    idempotency_key = f"talk:{payload['webhookEventId']}"
    talk_scope = build_talk_scope(payload)

    _persist_incoming_event(
        store,
        source="nextcloud-talk",
        idempotency_key=idempotency_key,
        raw_body=body,
        parsed_payload=payload,
    )
    _enqueue_job(
        store,
        queue="foreground",
        job_type="nexa.talk.process_message",
        payload={
            "conversation_id": payload["conversation"]["id"],
            "message_id": payload["message"]["id"],
            "thread_id": talk_scope.thread_id,
            "user_id": payload["initiator"]["id"],
        },
        idempotency_key=idempotency_key,
        scope_key=talk_scope.queue_scope_key(),
    )
    return _ack(status_code=202, body={"accepted": True})


def handle_onlyoffice_callback(
    *,
    body: bytes,
    headers: dict[str, str],
    callback_secret: str,
    store: Any,
) -> dict[str, Any]:
    """Treat ONLYOFFICE callbacks as thin change signals and queue work."""

    normalized_headers = _normalize_headers(headers)
    if normalized_headers.get("x-onlyoffice-callback-secret") != callback_secret:
        return _ack(status_code=401, body={"error": 1})

    payload = _parse_json_object(body)
    idempotency_key = (
        f"onlyoffice:{payload['key']}:status:{payload['status']}:"
        f"version:{payload['history']['serverVersion']}"
    )
    onlyoffice_scope = build_onlyoffice_scope(payload)

    _persist_incoming_event(
        store,
        source="onlyoffice-document-server",
        idempotency_key=idempotency_key,
        raw_body=body,
        parsed_payload=payload,
    )

    status = payload["status"]
    if status == 2:
        job_payload = {
            "authoritative": True,
            "document_key": payload["key"],
            "download_url": payload["url"],
            "status": status,
        }
        queue = "foreground"
    elif status == 6:
        job_payload = {
            "authoritative": False,
            "document_key": payload["key"],
            "download_url": payload["url"],
            "force_save_type": payload.get("forcesavetype"),
            "status": status,
        }
        queue = "background"
    else:
        return _ack(status_code=200, body={"error": 0})

    _enqueue_job(
        store,
        queue=queue,
        job_type="nexa.onlyoffice.reconcile_saved_document",
        payload=job_payload,
        idempotency_key=idempotency_key,
        scope_key=onlyoffice_scope.queue_scope_key(),
        supersession_key="reconcile_saved_document",
    )
    return _ack(status_code=200, body={"error": 0})


def _ack(*, status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"status_code": status_code, "body": body}


def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {str(key).lower(): value for key, value in headers.items()}


def _has_valid_talk_signature(
    *, body: bytes, provided_signature: str | None, signing_secret: str
) -> bool:
    if provided_signature is None:
        return False
    prefix = "sha256="
    if not provided_signature.startswith(prefix):
        return False
    expected = hmac.new(signing_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_signature[len(prefix) :], expected)


def _parse_json_object(body: bytes) -> dict[str, Any]:
    payload = json.loads(body)
    if not isinstance(payload, dict):
        msg = "Expected JSON object payload."
        raise ValueError(msg)
    return payload


def _persist_incoming_event(
    store: Any,
    *,
    source: str,
    idempotency_key: str,
    raw_body: bytes,
    parsed_payload: dict[str, Any],
) -> None:
    store.persist_incoming_event(
        source=source,
        idempotency_key=idempotency_key,
        raw_body=raw_body,
        parsed_payload=parsed_payload,
    )


def _enqueue_job(
    store: Any,
    *,
    queue: str,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    scope_key: str,
    supersession_key: str | None = None,
) -> None:
    enqueue_job = store.enqueue_job
    parameters = inspect.signature(enqueue_job).parameters
    kwargs: dict[str, Any] = {
        "queue": queue,
        "job_type": job_type,
        "payload": payload,
        "idempotency_key": idempotency_key,
    }
    if "scope_key" in parameters:
        kwargs["scope_key"] = scope_key
    if "supersession_key" in parameters:
        kwargs["supersession_key"] = supersession_key
    enqueue_job(**kwargs)
