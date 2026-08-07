from __future__ import annotations

import dokploy_wizard.cli as cli
from dokploy_wizard.state import RawEnvInput


def test_task18_rehydration_preserves_nonsecret_drift() -> None:
    # Given
    existing = RawEnvInput(
        format_version=1,
        values={"DOKPLOY_ADMIN_PASSWORD": "<redacted>", "ROOT_DOMAIN": "old.test"},
    )
    requested = RawEnvInput(
        format_version=1,
        values={
            "DOKPLOY_ADMIN_EMAIL": "operator@example.test",
            "DOKPLOY_ADMIN_PASSWORD": "current-password",
            "ROOT_DOMAIN": "new.test",
        },
    )

    # When
    rehydrated = cli._rehydrate_inspection_redactions(existing, requested)

    # Then
    assert rehydrated.values == {
        "DOKPLOY_ADMIN_PASSWORD": "current-password",
        "ROOT_DOMAIN": "old.test",
    }
    assert rehydrated != requested


def test_task18_runtime_comparison_preserves_missing_admin_credentials() -> None:
    # Given
    raw = RawEnvInput(format_version=1, values={"ROOT_DOMAIN": "example.test"})

    # When
    comparison = cli._task18_runtime_comparison_raw(raw)

    # Then
    assert comparison == raw


def test_task18_runtime_comparison_omits_admin_credentials() -> None:
    # Given
    raw = RawEnvInput(
        format_version=1,
        values={
            "DOKPLOY_ADMIN_EMAIL": "operator@example.test",
            "DOKPLOY_ADMIN_PASSWORD": "fixture-password",
            "ROOT_DOMAIN": "example.test",
        },
    )

    # When
    comparison = cli._task18_runtime_comparison_raw(raw)

    # Then
    assert comparison.values == {"ROOT_DOMAIN": "example.test"}
