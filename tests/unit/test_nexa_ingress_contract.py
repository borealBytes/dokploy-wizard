# mypy: ignore-errors
# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
INGRESS_MODULE = "dokploy_wizard.packs.openclaw.nexa_ingress"
TALK_SHARED_SECRET = "talk-shared-secret-test"
TALK_SIGNING_SECRET = "talk-signing-secret-test"
ONLYOFFICE_CALLBACK_SECRET = "onlyoffice-callback-secret-test"


@dataclass
class FakeIngressStore:
    persisted_events: list[dict[str, Any]] = field(default_factory=list)
    enqueued_jobs: list[dict[str, Any]] = field(default_factory=list)

    def persist_incoming_event(
        self,
        *,
        source: str,
        idempotency_key: str,
        raw_body: bytes,
        parsed_payload: dict[str, Any],
    ) -> None:
        self.persisted_events.append(
            {
                "idempotency_key": idempotency_key,
                "parsed_payload": parsed_payload,
                "raw_body": raw_body,
                "source": source,
            }
        )

    def enqueue_job(
        self,
        *,
        queue: str,
        job_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        self.enqueued_jobs.append(
            {
                "idempotency_key": idempotency_key,
                "job_type": job_type,
                "payload": payload,
                "queue": queue,
            }
        )


def _load_json_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _build_talk_headers(body: bytes, *, shared_secret: str, signing_secret: str) -> dict[str, str]:
    signature = hmac.new(signing_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Nextcloud-Talk-Secret": shared_secret,
        "X-Nextcloud-Talk-Signature": f"sha256={signature}",
    }


def _build_onlyoffice_headers(secret: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Onlyoffice-Callback-Secret": secret,
    }


def _expected_talk_idempotency_key(payload: dict[str, Any]) -> str:
    return f"talk:{payload['webhookEventId']}"


def _expected_onlyoffice_idempotency_key(payload: dict[str, Any]) -> str:
    version = payload["history"]["serverVersion"]
    return f"onlyoffice:{payload['key']}:status:{payload['status']}:version:{version}"


def _load_ingress_module() -> Any:
    try:
        return importlib.import_module(INGRESS_MODULE)
    except ModuleNotFoundError:
        pytest.fail(
            "Missing Nexa ingress implementation module "
            f"{INGRESS_MODULE}. Add the queue-first Talk/ONLYOFFICE receiver contract "
            "without changing this test.",
            pytrace=False,
        )


def _require_handler(module: Any, name: str) -> Any:
    handler = getattr(module, name, None)
    if handler is None:
        pytest.fail(
            f"Missing {INGRESS_MODULE}.{name} required by the Nexa ingress contract tests.",
            pytrace=False,
        )
    return handler


def _ack_status_code(ack: Any) -> Any:
    if isinstance(ack, dict):
        return ack.get("status_code")
    return getattr(ack, "status_code", None)


def _ack_body(ack: Any) -> Any:
    if isinstance(ack, dict):
        return ack.get("body")
    return getattr(ack, "body", None)


def test_talk_fixture_models_signed_room_message_with_thread_capability() -> None:
    payload = _load_json_fixture("nexa-talk-webhook-room-message.json")

    assert payload["event"] == "message"
    assert payload["webhookEventId"] == "evt-talk-room-42-msg-845-v2"
    assert payload["conversation"]["id"] == "room-42"
    assert payload["capabilities"]["bots-v1"] is True
    assert payload["capabilities"]["threads"] is True
    assert payload["message"]["id"] == "msg-845"
    assert payload["message"]["parent"]["id"] == "msg-840"
    assert payload["initiator"]["id"] == "clay"


def test_onlyoffice_status_2_fixture_models_saved_document_callback_without_comment_events() -> (
    None
):
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-2.json")

    assert payload["status"] == 2
    assert payload["key"] == "document-key-abc123"
    assert payload["url"].endswith("output.docx")
    assert payload["history"]["serverVersion"] == "171"
    assert payload["users"] == ["clay", "nexa"]
    assert "comments" not in payload


def test_onlyoffice_status_6_fixture_models_force_save_signal_without_comment_events() -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-6.json")

    assert payload["status"] == 6
    assert payload["forcesavetype"] == 1
    assert payload["history"]["serverVersion"] == "172"
    assert payload["users"] == ["clay"]
    assert "comments" not in payload


def test_handle_talk_webhook_rejects_invalid_shared_secret_before_persist_or_enqueue() -> None:
    payload = _load_json_fixture("nexa-talk-webhook-room-message.json")
    body = _json_bytes(payload)
    headers = _build_talk_headers(
        body,
        shared_secret="wrong-shared-secret",
        signing_secret=TALK_SIGNING_SECRET,
    )
    module = _load_ingress_module()
    handle_talk_webhook = _require_handler(module, "handle_talk_webhook")
    store = FakeIngressStore()

    ack = handle_talk_webhook(
        body=body,
        headers=headers,
        talk_shared_secret=TALK_SHARED_SECRET,
        talk_signing_secret=TALK_SIGNING_SECRET,
        store=store,
    )

    assert _ack_status_code(ack) == 401
    assert _ack_body(ack) == {"error": "unauthorized"}
    assert store.persisted_events == []
    assert store.enqueued_jobs == []


def test_handle_talk_webhook_rejects_invalid_signature_before_persist_or_enqueue() -> None:
    payload = _load_json_fixture("nexa-talk-webhook-room-message.json")
    body = _json_bytes(payload)
    headers = _build_talk_headers(
        body,
        shared_secret=TALK_SHARED_SECRET,
        signing_secret="wrong-signing-secret",
    )
    module = _load_ingress_module()
    handle_talk_webhook = _require_handler(module, "handle_talk_webhook")
    store = FakeIngressStore()

    ack = handle_talk_webhook(
        body=body,
        headers=headers,
        talk_shared_secret=TALK_SHARED_SECRET,
        talk_signing_secret=TALK_SIGNING_SECRET,
        store=store,
    )

    assert _ack_status_code(ack) == 401
    assert _ack_body(ack) == {"error": "invalid_signature"}
    assert store.persisted_events == []
    assert store.enqueued_jobs == []


def test_handle_talk_webhook_persists_event_enqueues_intent_and_returns_immediate_ack() -> None:
    payload = _load_json_fixture("nexa-talk-webhook-room-message.json")
    body = _json_bytes(payload)
    headers = _build_talk_headers(
        body,
        shared_secret=TALK_SHARED_SECRET,
        signing_secret=TALK_SIGNING_SECRET,
    )
    module = _load_ingress_module()
    handle_talk_webhook = _require_handler(module, "handle_talk_webhook")
    store = FakeIngressStore()

    ack = handle_talk_webhook(
        body=body,
        headers=headers,
        talk_shared_secret=TALK_SHARED_SECRET,
        talk_signing_secret=TALK_SIGNING_SECRET,
        store=store,
    )

    assert _ack_status_code(ack) == 202
    assert _ack_body(ack) == {"accepted": True}
    assert store.persisted_events == [
        {
            "idempotency_key": _expected_talk_idempotency_key(payload),
            "parsed_payload": payload,
            "raw_body": body,
            "source": "nextcloud-talk",
        }
    ]
    assert store.enqueued_jobs == [
        {
            "idempotency_key": _expected_talk_idempotency_key(payload),
            "job_type": "nexa.talk.process_message",
            "payload": {
                "conversation_id": "room-42",
                "message_id": "msg-845",
                "thread_id": "thread-room-42-msg-840",
                "user_id": "clay",
            },
            "queue": "foreground",
        }
    ]


def test_handle_talk_webhook_drops_thread_context_when_threads_capability_is_absent() -> None:
    payload = _load_json_fixture("nexa-talk-webhook-room-message.json")
    payload["capabilities"]["threads"] = False
    body = _json_bytes(payload)
    headers = _build_talk_headers(
        body,
        shared_secret=TALK_SHARED_SECRET,
        signing_secret=TALK_SIGNING_SECRET,
    )
    module = _load_ingress_module()
    handle_talk_webhook = _require_handler(module, "handle_talk_webhook")
    store = FakeIngressStore()

    ack = handle_talk_webhook(
        body=body,
        headers=headers,
        talk_shared_secret=TALK_SHARED_SECRET,
        talk_signing_secret=TALK_SIGNING_SECRET,
        store=store,
    )

    assert _ack_status_code(ack) == 202
    assert store.enqueued_jobs == [
        {
            "idempotency_key": _expected_talk_idempotency_key(payload),
            "job_type": "nexa.talk.process_message",
            "payload": {
                "conversation_id": "room-42",
                "message_id": "msg-845",
                "thread_id": None,
                "user_id": "clay",
            },
            "queue": "foreground",
        }
    ]


def test_handle_onlyoffice_callback_acknowledges_status_2_and_enqueues_reconcile_job() -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-2.json")
    body = _json_bytes(payload)
    headers = _build_onlyoffice_headers(ONLYOFFICE_CALLBACK_SECRET)
    module = _load_ingress_module()
    handle_onlyoffice_callback = _require_handler(module, "handle_onlyoffice_callback")
    store = FakeIngressStore()

    ack = handle_onlyoffice_callback(
        body=body,
        headers=headers,
        callback_secret=ONLYOFFICE_CALLBACK_SECRET,
        store=store,
    )

    assert _ack_status_code(ack) == 200
    assert _ack_body(ack) == {"error": 0}
    assert store.persisted_events == [
        {
            "idempotency_key": _expected_onlyoffice_idempotency_key(payload),
            "parsed_payload": payload,
            "raw_body": body,
            "source": "onlyoffice-document-server",
        }
    ]
    assert store.enqueued_jobs == [
        {
            "idempotency_key": _expected_onlyoffice_idempotency_key(payload),
            "job_type": "nexa.onlyoffice.reconcile_saved_document",
            "payload": {
                "authoritative": True,
                "document_key": "document-key-abc123",
                "download_url": payload["url"],
                "status": 2,
            },
            "queue": "foreground",
        }
    ]


def test_handle_onlyoffice_callback_acknowledges_status_6_but_marks_force_save_non_authoritative(
) -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-6.json")
    body = _json_bytes(payload)
    headers = _build_onlyoffice_headers(ONLYOFFICE_CALLBACK_SECRET)
    module = _load_ingress_module()
    handle_onlyoffice_callback = _require_handler(module, "handle_onlyoffice_callback")
    store = FakeIngressStore()

    ack = handle_onlyoffice_callback(
        body=body,
        headers=headers,
        callback_secret=ONLYOFFICE_CALLBACK_SECRET,
        store=store,
    )

    assert _ack_status_code(ack) == 200
    assert _ack_body(ack) == {"error": 0}
    assert store.enqueued_jobs == [
        {
            "idempotency_key": _expected_onlyoffice_idempotency_key(payload),
            "job_type": "nexa.onlyoffice.reconcile_saved_document",
            "payload": {
                "authoritative": False,
                "document_key": "document-key-abc123",
                "download_url": payload["url"],
                "force_save_type": 1,
                "status": 6,
            },
            "queue": "background",
        }
    ]


def test_handle_onlyoffice_callback_does_not_treat_comment_like_fields_as_comment_events_in_v1(
) -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-2.json")
    payload["comments"] = [{"comment": "Please tighten this paragraph.", "id": "comment-1"}]
    body = _json_bytes(payload)
    headers = _build_onlyoffice_headers(ONLYOFFICE_CALLBACK_SECRET)
    module = _load_ingress_module()
    handle_onlyoffice_callback = _require_handler(module, "handle_onlyoffice_callback")
    store = FakeIngressStore()

    ack = handle_onlyoffice_callback(
        body=body,
        headers=headers,
        callback_secret=ONLYOFFICE_CALLBACK_SECRET,
        store=store,
    )

    assert _ack_status_code(ack) == 200
    assert _ack_body(ack) == {"error": 0}
    assert store.enqueued_jobs == [
        {
            "idempotency_key": _expected_onlyoffice_idempotency_key(payload),
            "job_type": "nexa.onlyoffice.reconcile_saved_document",
            "payload": {
                "authoritative": True,
                "document_key": "document-key-abc123",
                "download_url": payload["url"],
                "status": 2,
            },
            "queue": "foreground",
        }
    ]
