# pyright: reportMissingImports=false

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from dokploy_wizard.packs.openclaw.nexa_ingress import handle_onlyoffice_callback, handle_talk_webhook
from dokploy_wizard.packs.openclaw.nexa_runtime import (
    NexaOnlyofficeRuntimeResult,
    NexaTalkRuntimeResult,
    run_queued_nexa_job,
)
from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _runtime_dependencies_from_env
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
from tests.nexa_nextcloud_test_server import nextcloud_base_url, run_recording_nextcloud_server


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 20, hour, minute, tzinfo=UTC)


def _sidecar_env(*, base_url: str, state_dir: Path) -> dict[str, str]:
    return {
        "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL": base_url,
        "OPENCLAW_NEXA_TALK_SHARED_SECRET": TALK_SHARED_SECRET,
        "OPENCLAW_NEXA_TALK_SIGNING_SECRET": TALK_SIGNING_SECRET,
        "OPENCLAW_NEXA_ONLYOFFICE_CALLBACK_SECRET": ONLYOFFICE_CALLBACK_SECRET,
        "OPENCLAW_NEXA_WEBDAV_AUTH_USER": "nexa-agent",
        "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD": "webdav-app-password",
        "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
        "OPENCLAW_NEXA_AGENT_DISPLAY_NAME": "Nexa",
        "DOKPLOY_WIZARD_NEXA_STATE_DIR": str(state_dir),
    }


def _minimal_docx_bytes(text: str) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
                "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
                "</w:document>"
            ),
        )
    return buffer.getvalue()


def test_runtime_sidecar_live_talk_sender_completes_healthy_queue_job(tmp_path: Path) -> None:
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
    leased = store.lease_next_job(lease_owner="sidecar-talk", now=_ts(12, 46))
    assert leased is not None

    with run_recording_nextcloud_server(
        talk_shared_secret=TALK_SHARED_SECRET,
        talk_signing_secret=TALK_SIGNING_SECRET,
        webdav_user="nexa-agent",
        webdav_password="webdav-app-password",
        webdav_file_id="file-991",
        webdav_etag='"etag-171"',
        webdav_content=b"unused",
        webdav_content_type="text/plain; charset=utf-8",
    ) as server:
        env = _sidecar_env(base_url=nextcloud_base_url(server), state_dir=tmp_path / ".nexa-state")
        result = run_queued_nexa_job(
            leased,
            store=store,
            env=env,
            dependencies=_runtime_dependencies_from_env(env),
            now=_ts(12, 47),
        )

    assert ack == {"status_code": 202, "body": {"accepted": True}}
    assert result.status == "completed"
    talk_result = result.result
    assert isinstance(talk_result, NexaTalkRuntimeResult)
    assert talk_result.reply_dispatch.outcome == "sent"
    assert talk_result.reply_dispatch.visible_send is True
    talk_requests = [req for req in server.requests if req.path.startswith("/ocs/v2.php/apps/spreed/api/v1/bot/")]
    assert len(talk_requests) == 1
    talk_body = json.loads(talk_requests[0].body.decode("utf-8"))
    assert talk_requests[0].path == "/ocs/v2.php/apps/spreed/api/v1/bot/x9m3abp4/message"
    assert talk_body["replyTo"] == "msg-845"
    assert "referenceId" in talk_body
    assert "live sidecar path is healthy" in talk_body["message"]


def test_runtime_sidecar_live_webdav_loader_and_onlyoffice_executor_complete_authoritative_job(
    tmp_path: Path,
) -> None:
    store = DurableQueueStore(tmp_path)
    payload = load_json_fixture("nexa-onlyoffice-callback-status-2.json")
    ack = handle_onlyoffice_callback(
        body=json_bytes(payload),
        headers=build_onlyoffice_headers(),
        callback_secret=ONLYOFFICE_CALLBACK_SECRET,
        store=store,
    )
    leased = store.lease_next_job(lease_owner="sidecar-doc", now=_ts(13, 0))
    assert leased is not None

    with run_recording_nextcloud_server(
        talk_shared_secret=TALK_SHARED_SECRET,
        talk_signing_secret=TALK_SIGNING_SECRET,
        webdav_user="nexa-agent",
        webdav_password="webdav-app-password",
        webdav_file_id="file-991",
        webdav_etag='"etag-171"',
        webdav_content=_minimal_docx_bytes("Action items from the canonical Q2 plan"),
        webdav_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ) as server:
        state_dir = tmp_path / ".nexa-state"
        env = _sidecar_env(base_url=nextcloud_base_url(server), state_dir=state_dir)
        result = run_queued_nexa_job(
            leased,
            store=store,
            env=env,
            dependencies=_runtime_dependencies_from_env(env),
            now=_ts(13, 1),
        )

    assert ack == {"status_code": 200, "body": {"error": 0}}
    assert result.status == "completed"
    onlyoffice_result = result.result
    assert isinstance(onlyoffice_result, NexaOnlyofficeRuntimeResult)
    assert onlyoffice_result.retrieval_gate.action == "proceed"
    assert onlyoffice_result.action_result.outcome == "applied"
    assert onlyoffice_result.action_result.authoritative_write is False
    assert [req.method for req in server.requests if req.path.startswith("/remote.php/dav/files/")] == [
        "PROPFIND",
        "GET",
    ]
    recorded_actions = (tmp_path / ".nexa-state" / "onlyoffice-reconcile-actions.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(recorded_actions) == 1
    action_payload = json.loads(recorded_actions[0])
    assert action_payload["result"] == "structured_noop"
    assert action_payload["document_mutation_performed"] is False
    assert action_payload["actor"] == {"display_name": "Nexa", "user_id": "nexa-agent"}
