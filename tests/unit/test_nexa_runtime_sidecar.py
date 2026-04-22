from __future__ import annotations

import logging

import pytest

from dokploy_wizard.packs.openclaw.nexa_runtime_sidecar import _update_presence


def test_update_presence_degrades_gracefully_on_nextcloud_429(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []

    def fake_raw_request(url: str, **kwargs: object) -> tuple[bytes, str | None]:
        del kwargs
        calls.append(url)
        raise RuntimeError(
            "HTTP 429 from https://nextcloud.example.com/ocs/v2.php/apps/user_status/api/v1/heartbeat: Reached maximum delay"
        )

    monkeypatch.setattr(
        "dokploy_wizard.packs.openclaw.nexa_runtime_sidecar._raw_request",
        fake_raw_request,
    )
    env = {
        "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL": "https://nextcloud.example.com",
        "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
        "OPENCLAW_NEXA_AGENT_PASSWORD": "app-password",
    }

    with caplog.at_level(logging.INFO):
        _update_presence(env, status="online", message="Ready")

    assert len(calls) == 1
    assert "rate-limited by Nextcloud" in caplog.text
    assert "WARNING" not in caplog.text
