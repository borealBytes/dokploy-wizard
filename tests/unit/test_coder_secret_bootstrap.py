from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy import coder as coder_module
from dokploy_wizard.dokploy.coder import DokployCoderBackend
from dokploy_wizard.dokploy.coder_secret_reconciliation import (
    CoderSecretClient,
    CoderSecretError,
    CoderSecretMetadata,
    CoderSecretSpec,
)
from dokploy_wizard.dokploy.coder_secret_reconciliation_types import (
    CoderSecretFailureKind,
)
from dokploy_wizard.dokploy.coder_template_migration_runtime import (
    ProductionMigrationInputs,
)
from dokploy_wizard.packs.coder import CoderError


class RecordingSecretClient:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._metadata: dict[str, CoderSecretMetadata] = {}
        self._values: dict[str, str] = {}

    def list_secrets(self) -> tuple[CoderSecretMetadata, ...]:
        return tuple(self._metadata.values())

    def write_secret(self, operation: str, spec: CoderSecretSpec) -> str:
        self._events.append(f"write:{operation}:{spec.name}")
        self._metadata[spec.name] = CoderSecretMetadata(
            secret_id=f"00000000-0000-4000-8000-{len(self._metadata) + 1:012d}",
            name=spec.name,
            env_name=spec.env_name,
            description=spec.description,
        )
        self._values[spec.name] = spec.value
        return sha256(operation.encode()).hexdigest()

    def verify_workspace_value_hash(self, spec: CoderSecretSpec, owner_id: str) -> str:
        del owner_id
        self._events.append(f"verify:{spec.name}")
        return sha256(self._values[spec.name].encode()).hexdigest()


class FailingSecretClient:
    def __init__(self, events: list[str], failure: CoderSecretError) -> None:
        self._events = events
        self._failure = failure

    def list_secrets(self) -> tuple[CoderSecretMetadata, ...]:
        return ()

    def write_secret(self, operation: str, spec: CoderSecretSpec) -> str:
        del operation, spec
        self._events.append("reconciliation-failed")
        raise self._failure

    def verify_workspace_value_hash(self, spec: CoderSecretSpec, owner_id: str) -> str:
        del spec, owner_id
        raise AssertionError("verification must not follow a failed write")


class FixedSecretClientFactory:
    def __init__(self, client: CoderSecretClient) -> None:
        self._client = client

    def __call__(
        self, *, container_name: str, session_token: str, state_dir: Path
    ) -> CoderSecretClient:
        del container_name, session_token, state_dir
        return self._client


def _backend(*, client_factory: FixedSecretClientFactory, state_dir: Path) -> DokployCoderBackend:
    return DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="dokploy-api-key",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        coder_hermes_key="SECRET-CODER-HERMES",
        coder_kdense_key="SECRET-CODER-KDENSE",
        visible_litellm_aliases=("opencode-go/deepseek-v4-flash", "openrouter/example"),
        coder_secret_client_factory=client_factory,
        state_dir=state_dir,
    )


def _patch_bootstrap(monkeypatch: pytest.MonkeyPatch, backend: DokployCoderBackend) -> None:
    backend._created_in_process = True
    monkeypatch.setattr(coder_module, "_wait_for_coder_bootstrap_api_ready", lambda hostname: None)
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: True)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-token")
    monkeypatch.setattr(
        coder_module, "_coder_container_name", lambda service_name: "coder-container"
    )
    monkeypatch.setattr(backend, "_workspace_runtime_image_replacements", lambda: {})
    monkeypatch.setattr(coder_module, "_ensure_default_workspace", lambda **kwargs: False)


def test_secret_workspace_hashes_precede_first_template_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    client = RecordingSecretClient(events)
    backend = _backend(client_factory=FixedSecretClientFactory(client), state_dir=tmp_path)
    _patch_bootstrap(monkeypatch, backend)

    def record_template_migration(inputs: ProductionMigrationInputs) -> None:
        events.extend(f"push:{source.name}" for source in inputs.sources)

    monkeypatch.setattr(
        coder_module,
        "execute_template_migration",
        record_template_migration,
    )

    backend.ensure_application_ready()

    verification_events = [event for event in events if event.startswith("verify:")]
    first_push = next(index for index, event in enumerate(events) if event.startswith("push:"))
    assert verification_events == [
        "verify:hermes-inference-provider",
        "verify:hermes-model",
        "verify:hermes-openai-api-base",
        "verify:hermes-openai-api-key",
        "verify:kdense-litellm-api-key",
    ]
    assert all(events.index(event) < first_push for event in verification_events)


def test_secret_reconciliation_failure_blocks_template_push_and_redacts_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    secret_value = "SECRET-CODER-HERMES"
    backend = _backend(
        client_factory=FixedSecretClientFactory(
            FailingSecretClient(events, CoderSecretError(f"remote failure {secret_value}"))
        ),
        state_dir=tmp_path,
    )
    _patch_bootstrap(monkeypatch, backend)

    def record_template_migration(inputs: ProductionMigrationInputs) -> None:
        events.extend(f"push:{source.name}" for source in inputs.sources)

    monkeypatch.setattr(
        coder_module,
        "execute_template_migration",
        record_template_migration,
    )

    with pytest.raises(CoderError) as raised:
        backend.ensure_application_ready()

    assert str(raised.value) == "Coder workspace secret reconciliation failed. unknown"
    assert secret_value not in str(raised.value)
    assert events == ["reconciliation-failed"]


def test_secret_reconciliation_failure_classifies_terminal_receipt(
    tmp_path: Path,
) -> None:
    # Given
    events: list[str] = []
    backend = _backend(
        client_factory=FixedSecretClientFactory(
            FailingSecretClient(
                events,
                CoderSecretError("provider detail is discarded", kind="receipt"),
            )
        ),
        state_dir=tmp_path,
    )

    # When / Then
    with pytest.raises(CoderError) as raised:
        backend._reconcile_coder_workspace_secrets(
            container_name="coder-container",
            session_token="session-token",
        )

    assert str(raised.value) == "Coder workspace secret reconciliation failed. receipt"
    assert events == ["reconciliation-failed"]


@pytest.mark.parametrize(
    "kind",
    (
        "blocked",
        "client",
        "metadata",
        "receipt_invalid",
        "receipt_owner",
        "receipt_read",
        "receipt_read_directory",
        "receipt_read_file",
        "receipt_read_json",
        "receipt_read_schema",
        "receipt_schema",
        "receipt_write",
        "reconciliation",
    ),
)
def test_secret_reconciliation_failure_keeps_only_typed_kind(
    tmp_path: Path,
    kind: CoderSecretFailureKind,
) -> None:
    events: list[str] = []
    backend = _backend(
        client_factory=FixedSecretClientFactory(
            FailingSecretClient(
                events,
                CoderSecretError("provider detail is discarded", kind=kind),
            )
        ),
        state_dir=tmp_path,
    )

    with pytest.raises(CoderError) as raised:
        backend._reconcile_coder_workspace_secrets(
            container_name="coder-container",
            session_token="session-token",
        )

    assert str(raised.value) == f"Coder workspace secret reconciliation failed. {kind}"
    assert events == ["reconciliation-failed"]
