# mypy: ignore-errors
# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


_OPENCLAW_SECRET_KEY = "OPENCLAW_PROVIDER_API_KEY"
_OPENCLAW_SECRET_VALUE = "SECRET_TEST_OPENROUTER_VALUE"
_FARM_SECRET_KEY = "MY_FARM_ADVISOR_PROVIDER_API_KEY"
_FARM_SECRET_VALUE = "SECRET_TEST_FARM_PROVIDER_VALUE"
_NEXTCLOUD_SECRET_KEY = "NEXTCLOUD_ADMIN_PASSWORD"
_NEXTCLOUD_SECRET_VALUE = "SECRET_TEST_NEXTCLOUD_ADMIN_VALUE"
_SENTINEL_SECRET_VALUES = (
    _OPENCLAW_SECRET_VALUE,
    _FARM_SECRET_VALUE,
    _NEXTCLOUD_SECRET_VALUE,
)


def _future_env_contract() -> tuple[type[Any], type[Any], type[Any], type[Any], type[Exception]]:
    from dokploy_wizard.dokploy.env_spec import (  # type: ignore[import-not-found]
        DokployEnvReconciler,
        DokployEnvSpec,
        DokployEnvValidationError,
        DokployEnvVar,
        RenderedCompose,
    )

    return (
        DokployEnvReconciler,
        DokployEnvSpec,
        DokployEnvVar,
        RenderedCompose,
        DokployEnvValidationError,
    )


@dataclass
class _RecordedCompose:
    compose_id: str
    name: str


@dataclass
class _RecordingDokployEnvClient:
    events: list[str] = field(default_factory=list)
    compose_env_by_id: dict[str, str] = field(default_factory=dict)
    compose_file_by_id: dict[str, str] = field(default_factory=dict)
    compose_names_by_id: dict[str, str] = field(default_factory=dict)
    next_compose_number: int = 1

    def create_compose(
        self,
        *,
        name: str,
        environment_id: str,
        compose_file: str,
        app_name: str,
        env: str | None = None,
    ) -> _RecordedCompose:
        del environment_id, app_name
        compose_id = f"cmp-{self.next_compose_number}"
        self.next_compose_number += 1
        self.events.append(f"create:{name}")
        self.compose_names_by_id[compose_id] = name
        self.compose_file_by_id[compose_id] = compose_file
        if env is not None:
            self.events.append(f"env:{name}")
            self.compose_env_by_id[compose_id] = env
        return _RecordedCompose(compose_id=compose_id, name=name)

    def update_compose(
        self,
        *,
        compose_id: str,
        compose_file: str | None = None,
        env: str | None = None,
    ) -> _RecordedCompose:
        name = self.compose_names_by_id[compose_id]
        if env is not None:
            self.events.append(f"env:{name}")
            self.compose_env_by_id[compose_id] = env
        if compose_file is not None:
            self.events.append(f"compose:{name}")
            self.compose_file_by_id[compose_id] = compose_file
        return _RecordedCompose(compose_id=compose_id, name=name)

    def deploy_compose(
        self,
        *,
        compose_id: str,
        title: str | None,
        description: str | None,
    ) -> None:
        del title, description
        self.events.append(f"deploy:{self.compose_names_by_id[compose_id]}")


def _parse_env_payload(payload: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in payload.splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def _assert_payload_value(payload: str, *, key: str, expected_value: str) -> None:
    parsed = _parse_env_payload(payload)
    if key not in parsed:
        pytest.fail(f"expected env payload to contain key {key}")
    if parsed[key] != expected_value:
        pytest.fail(f"unexpected env payload value for key {key}")


def _assert_message_redacts_sentinels(message: str) -> None:
    leaked = [value for value in _SENTINEL_SECRET_VALUES if value in message]
    if leaked:
        pytest.fail("failure message leaked a raw sentinel secret value")


def test_reconciler_applies_env_before_first_safe_compose_deploy() -> None:
    (
        DokployEnvReconciler,
        DokployEnvSpec,
        DokployEnvVar,
        RenderedCompose,
        _DokployEnvValidationError,
    ) = _future_env_contract()
    client = _RecordingDokployEnvClient()
    compose_file = f"""services:
  wizard-stack-openclaw:
    image: ghcr.io/openclaw/openclaw:latest
    environment:
      {_OPENCLAW_SECRET_KEY}: \"${{{_OPENCLAW_SECRET_KEY}:?{_OPENCLAW_SECRET_KEY} is required}}\"
"""
    rendered = RenderedCompose(
        compose_file=compose_file,
        env_specs=(
            DokployEnvSpec(
                variable=DokployEnvVar(
                    name=_OPENCLAW_SECRET_KEY,
                    value=_OPENCLAW_SECRET_VALUE,
                    sensitive=True,
                    source="operator-input",
                ),
                owner="openclaw",
                target_services=("wizard-stack-openclaw",),
                placeholder=f"${{{_OPENCLAW_SECRET_KEY}:?{_OPENCLAW_SECRET_KEY} is required}}",
                required=True,
            ),
        ),
    )

    record = DokployEnvReconciler(client=client).reconcile_compose(
        name="wizard-stack-openclaw",
        environment_id="env-1",
        app_name="wizard-stack-openclaw",
        rendered=rendered,
        existing_compose_id=None,
    )

    assert record.compose_id == "cmp-1"
    assert client.events == [
        "create:wizard-stack-openclaw",
        "env:wizard-stack-openclaw",
        "compose:wizard-stack-openclaw",
        "deploy:wizard-stack-openclaw",
    ]
    assert _OPENCLAW_SECRET_VALUE not in client.compose_file_by_id[record.compose_id]
    _assert_payload_value(
        client.compose_env_by_id[record.compose_id],
        key=_OPENCLAW_SECRET_KEY,
        expected_value=_OPENCLAW_SECRET_VALUE,
    )


def test_reconciler_rejects_unmatched_placeholder_without_echoing_secret_values() -> None:
    (
        DokployEnvReconciler,
        _DokployEnvSpec,
        _DokployEnvVar,
        RenderedCompose,
        DokployEnvValidationError,
    ) = _future_env_contract()
    rendered = RenderedCompose(
        compose_file="""services:
  wizard-stack-openclaw:
    image: ghcr.io/openclaw/openclaw:latest
    environment:
      MISSING_SECRET: "${MISSING_SECRET:?MISSING_SECRET is required}"
""",
        env_specs=(),
    )

    with pytest.raises(DokployEnvValidationError) as exc_info:
        DokployEnvReconciler(client=_RecordingDokployEnvClient()).validate_rendered_compose(rendered)

    message = str(exc_info.value)
    assert "MISSING_SECRET" in message
    _assert_message_redacts_sentinels(message)


def test_reconciler_rejects_raw_secret_scalar_without_echoing_secret_value() -> None:
    (
        DokployEnvReconciler,
        DokployEnvSpec,
        DokployEnvVar,
        RenderedCompose,
        DokployEnvValidationError,
    ) = _future_env_contract()
    rendered = RenderedCompose(
        compose_file=f"""services:
  wizard-stack-openclaw:
    image: ghcr.io/openclaw/openclaw:latest
    environment:
      {_OPENCLAW_SECRET_KEY}: "{_OPENCLAW_SECRET_VALUE}"
""",
        env_specs=(
            DokployEnvSpec(
                variable=DokployEnvVar(
                    name=_OPENCLAW_SECRET_KEY,
                    value=_OPENCLAW_SECRET_VALUE,
                    sensitive=True,
                    source="operator-input",
                ),
                owner="openclaw",
                target_services=("wizard-stack-openclaw",),
                placeholder=f"${{{_OPENCLAW_SECRET_KEY}:?{_OPENCLAW_SECRET_KEY} is required}}",
                required=True,
            ),
        ),
    )

    with pytest.raises(DokployEnvValidationError) as exc_info:
        DokployEnvReconciler(client=_RecordingDokployEnvClient()).validate_rendered_compose(rendered)

    message = str(exc_info.value)
    assert _OPENCLAW_SECRET_KEY in message
    assert "wizard-stack-openclaw" in message
    _assert_message_redacts_sentinels(message)


def test_reconciler_rejects_unrelated_service_receiving_unrelated_secret() -> None:
    (
        DokployEnvReconciler,
        DokployEnvSpec,
        DokployEnvVar,
        RenderedCompose,
        DokployEnvValidationError,
    ) = _future_env_contract()
    rendered = RenderedCompose(
        compose_file=f"""services:
  wizard-stack-nextcloud:
    image: nextcloud:29-apache
    environment:
      {_FARM_SECRET_KEY}: \"${{{_FARM_SECRET_KEY}:?{_FARM_SECRET_KEY} is required}}\"
  wizard-stack-my-farm-advisor:
    image: ghcr.io/borealbytes/my-farm-advisor:latest
    environment:
      {_FARM_SECRET_KEY}: \"${{{_FARM_SECRET_KEY}:?{_FARM_SECRET_KEY} is required}}\"
""",
        env_specs=(
            DokployEnvSpec(
                variable=DokployEnvVar(
                    name=_FARM_SECRET_KEY,
                    value=_FARM_SECRET_VALUE,
                    sensitive=True,
                    source="operator-input",
                ),
                owner="my-farm-advisor",
                target_services=("wizard-stack-my-farm-advisor",),
                placeholder=f"${{{_FARM_SECRET_KEY}:?{_FARM_SECRET_KEY} is required}}",
                required=True,
            ),
        ),
    )

    with pytest.raises(DokployEnvValidationError) as exc_info:
        DokployEnvReconciler(client=_RecordingDokployEnvClient()).validate_rendered_compose(rendered)

    message = str(exc_info.value)
    assert _FARM_SECRET_KEY in message
    assert "wizard-stack-nextcloud" in message
    assert "my-farm-advisor" in message
    _assert_message_redacts_sentinels(message)
