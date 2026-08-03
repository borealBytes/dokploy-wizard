from __future__ import annotations

import json
import os
import time
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

import dokploy_wizard.dokploy.workspace_catalog_sync_hermes as hermes_runtime
import dokploy_wizard.dokploy.workspace_catalog_sync_runtime as runtime
from dokploy_wizard.dokploy.workspace_catalog_sync import (
    CatalogModel,
    CatalogTarget,
    KdenseCatalogMetadata,
    ModelCatalog,
    ProcessIdentity,
    TransactionBlockedError,
    TransactionRecord,
    WorkspaceCatalogSyncError,
    WorkspaceCatalogTransaction,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_catalog_fetch import CatalogUnavailableError
from dokploy_wizard.dokploy.workspace_catalog_sync_hermes import HermesProcessReload
from dokploy_wizard.dokploy.workspace_catalog_sync_kdense import KdenseProcessReload
from dokploy_wizard.dokploy.workspace_catalog_sync_record_lock import RecordLock
from dokploy_wizard.dokploy.workspace_catalog_sync_runtime_inputs import ModelSyncSettings
from dokploy_wizard.proof.strict_lease import strict_proof_lease
from dokploy_wizard.proof.workspace_lifecycle import (
    WorkspaceProofPhase,
    WorkspaceProofRef,
    WorkspaceProofStore,
    run_workspace_proof,
)
from tests.integration.coder_template_runtime_support import (
    catalog as _catalog,
)
from tests.integration.coder_template_runtime_support import (
    opencode_models as _opencode_models,
)
from tests.integration.coder_template_runtime_support import (
    settings as _settings,
)
from tests.unit.test_coder_model_sync_rejections import _ProcessController


def test_primary_refresh_happy_replaces_catalog_without_process_transition(tmp_path: Path) -> None:
    # Given
    opencode_config = tmp_path / ".config/opencode/opencode.json"
    opencode_config.parent.mkdir(parents=True)
    unmanaged_fragment = b'"unmanaged": { "spacing": [1, 2] }'
    opencode_config.write_bytes(b"{\n  " + unmanaged_fragment + b"\n}\n")
    runtime.refresh_catalog(
        workspace_root=tmp_path,
        adapter="primary",
        catalog=_catalog("opencode-go/a"),
    )

    # When
    outcome = runtime.refresh_catalog(
        workspace_root=tmp_path,
        adapter="primary",
        catalog=_catalog("opencode-go/b"),
    )

    # Then
    assert outcome.file_freshness == "updated"
    assert outcome.process_freshness == "new-process-required"
    assert set(_opencode_models(tmp_path)) == {"opencode-go/b"}
    assert unmanaged_fragment in opencode_config.read_bytes()
    assert set(
        json.loads((tmp_path / ".pi/agent/models.json").read_text(encoding="utf-8"))["providers"][
            "litellm"
        ]["models"][0]
    ) == {"id", "name"}
    transactions = tmp_path / ".local/state/dokploy-wizard/model-sync/transactions"
    records = tuple(transactions.glob("*/transaction.json"))
    assert all(
        json.loads(record.read_text(encoding="utf-8"))["processes"] == [] for record in records
    )


def test_opencode_web_refresh_happy_updates_only_its_managed_adapters(tmp_path: Path) -> None:
    # Given
    pi_config = tmp_path / ".pi/agent/models.json"
    pi_config.parent.mkdir(parents=True)
    pi_config.write_bytes(b'{"unmanaged":true}\n')

    # When
    runtime.refresh_catalog(
        workspace_root=tmp_path,
        adapter="opencode-web",
        catalog=_catalog("opencode-go/b"),
    )

    # Then
    assert set(_opencode_models(tmp_path)) == {"opencode-go/b"}
    assert pi_config.read_bytes() == b'{"unmanaged":true}\n'
    settings = json.loads(
        (tmp_path / ".local/share/code-server/User/settings.json").read_text(encoding="utf-8")
    )
    assert set(settings["github.copilot.chat.customOAIModels"]) == {"opencode-go/b"}


def test_periodic_refresh_preserves_unchanged_catalog_bytes(tmp_path: Path) -> None:
    # Given
    catalog = _catalog("opencode-go/b")
    runtime.refresh_catalog(workspace_root=tmp_path, adapter="primary", catalog=catalog)
    target = tmp_path / ".config/opencode/opencode.json"
    before = os.stat(target)

    # When
    outcome = runtime.refresh_catalog(workspace_root=tmp_path, adapter="primary", catalog=catalog)

    # Then
    after = os.stat(target)
    assert outcome.file_freshness == "current"
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_periodic_refresh_retains_safe_fallback_after_first_fetch_failure(tmp_path: Path) -> None:
    # Given
    def unavailable(_: ModelSyncSettings) -> tuple[str, ...]:
        raise CatalogUnavailableError("LiteLLM catalog is unavailable")

    # When
    outcome = runtime.refresh_from_settings(
        workspace_root=tmp_path,
        adapter="primary",
        settings=_settings(),
        fetcher=unavailable,
    )

    # Then
    assert outcome.file_freshness == "fallback"
    assert set(_opencode_models(tmp_path)) == {"opencode-go/default", "openrouter/fallback"}


def test_refresh_lock_contention_uses_shared_utility_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    observed: list[Path] = []
    original = runtime._refresh_lock

    def observed_lock(root: Path) -> RecordLock:
        observed.append(root / ".local/state/dokploy-wizard/model-sync/refresh")
        return original(root)

    monkeypatch.setattr(runtime, "_refresh_lock", observed_lock)

    # When
    runtime.refresh_catalog(
        workspace_root=tmp_path,
        adapter="primary",
        catalog=_catalog("opencode-go/b"),
    )

    # Then
    assert observed == [tmp_path / ".local/state/dokploy-wizard/model-sync/refresh"]


def test_refresh_bad_catalog_preserves_existing_files(tmp_path: Path) -> None:
    # Given
    target = tmp_path / ".config/opencode/opencode.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'{"unmanaged":true}\n')

    def malformed(_: ModelSyncSettings) -> tuple[str, ...]:
        return ("invalid",)

    # When / Then
    with pytest.raises(WorkspaceCatalogSyncError, match="model alias"):
        runtime.refresh_from_settings(
            workspace_root=tmp_path,
            adapter="primary",
            settings=_settings(),
            fetcher=malformed,
        )
    assert target.read_bytes() == b'{"unmanaged":true}\n'


def test_refresh_user_config_conflict_preserves_exact_user_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    runtime.refresh_catalog(
        workspace_root=tmp_path,
        adapter="primary",
        catalog=_catalog("opencode-go/a"),
    )
    target = tmp_path / ".config/opencode/opencode.json"
    user_bytes = b'{"user":"edit"}\n'
    original = WorkspaceCatalogTransaction.prepare

    def prepare_with_user_edit(
        transaction: WorkspaceCatalogTransaction,
        *,
        targets: tuple[CatalogTarget, ...],
        processes: tuple[ProcessIdentity, ...] = (),
        explicit_operator_update: bool = False,
    ) -> TransactionRecord:
        prepared = original(
            transaction,
            targets=targets,
            processes=processes,
            explicit_operator_update=explicit_operator_update,
        )
        target.write_bytes(user_bytes)
        return prepared

    monkeypatch.setattr(WorkspaceCatalogTransaction, "prepare", prepare_with_user_edit)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        runtime.refresh_catalog(
            workspace_root=tmp_path,
            adapter="primary",
            catalog=_catalog("opencode-go/b"),
        )
    assert target.read_bytes() == user_bytes
    assert sha256(target.read_bytes()).hexdigest() == sha256(user_bytes).hexdigest()


def test_hermes_catalog_reload_preserves_unmanaged_yaml_and_is_noop_when_current(
    tmp_path: Path,
) -> None:
    # Given
    first = _catalog("opencode-go/a")
    runtime.refresh_catalog(workspace_root=tmp_path, adapter="hermes", catalog=first)
    config = tmp_path / ".hermes/config.yaml"
    config.write_bytes(config.read_bytes() + b"unmanaged: preserve\n")
    controller = _ProcessController()
    previous = controller.observe(name="hermes", generation=2)
    reload = HermesProcessReload(process=previous, control=controller)

    try:
        # When
        updated = runtime.refresh_catalog(
            workspace_root=tmp_path,
            adapter="hermes",
            catalog=_catalog("opencode-go/b"),
            hermes_process_reload=reload,
        )
        after_reload = os.stat(config)
        transactions = tmp_path / ".local/state/dokploy-wizard/model-sync/transactions"
        records_after_reload = tuple(sorted(transactions.glob("*/transaction.json")))
        current = runtime.refresh_catalog(
            workspace_root=tmp_path,
            adapter="hermes",
            catalog=_catalog("opencode-go/b"),
            hermes_process_reload=reload,
        )

        # Then
        assert updated.file_freshness == "updated"
        assert updated.process_freshness == "reloaded"
        assert current.file_freshness == "current"
        assert current.process_freshness == "not-required"
        assert b"unmanaged: preserve\n" in config.read_bytes()
        assert (os.stat(config).st_ino, os.stat(config).st_mtime_ns) == (
            after_reload.st_ino,
            after_reload.st_mtime_ns,
        )
        assert tuple(sorted(transactions.glob("*/transaction.json"))) == records_after_reload
        reloaded = json.loads(records_after_reload[-1].read_text(encoding="utf-8"))
        assert reloaded["phase"] == "committed"
        assert [(process["name"], process["status"]) for process in reloaded["processes"]] == [
            ("hermes", "verified")
        ]
        assert len(controller._processes) == 1
    finally:
        controller.close()


def test_hermes_catalog_reload_waits_for_all_surfaces_to_be_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    health_urls = (
        "http://127.0.0.1:8642/health",
        "http://127.0.0.1:9119/api/status",
        "http://127.0.0.1:8787/health",
    )
    attempts = dict.fromkeys(health_urls, 0)
    now = 0.0

    def delayed_urlopen(url: str, timeout: float) -> nullcontext[SimpleNamespace]:
        del timeout
        attempts[url] += 1
        if attempts[url] == 1:
            raise URLError("surface is still starting")
        return nullcontext(SimpleNamespace(status=200))

    def monotonic() -> float:
        return now

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(hermes_runtime, "urlopen", delayed_urlopen)
    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(time, "sleep", advance)
    control = hermes_runtime.HermesSupervisorControl(
        pid_file=tmp_path / "supervisor.pid",
        supervisor=tmp_path / "supervisor",
        health_urls=health_urls,
    )

    # When
    control.verify_health()

    # Then
    assert attempts == dict.fromkeys(health_urls, 2)


def test_hermes_health_rollback_restores_the_prior_catalog(tmp_path: Path) -> None:
    # Given
    runtime.refresh_catalog(
        workspace_root=tmp_path, adapter="hermes", catalog=_catalog("opencode-go/a")
    )
    config = tmp_path / ".hermes/config.yaml"
    before = config.read_bytes()

    class FailOnceProcessController(_ProcessController):
        def __init__(self) -> None:
            super().__init__()
            self._fail_health = True

        def verify_health(self) -> None:
            if self._fail_health:
                self._fail_health = False
                raise WorkspaceCatalogSyncError("Hermes health check failed")
            super().verify_health()

    controller = FailOnceProcessController()
    previous = controller.observe(name="hermes", generation=2)
    reload = HermesProcessReload(process=previous, control=controller)

    try:
        # When / Then
        with pytest.raises(TransactionBlockedError, match="health"):
            runtime.refresh_catalog(
                workspace_root=tmp_path,
                adapter="hermes",
                catalog=_catalog("opencode-go/b"),
                hermes_process_reload=reload,
            )
        assert config.read_bytes() == before
        transactions = tmp_path / ".local/state/dokploy-wizard/model-sync/transactions"
        rolled_back = json.loads(
            sorted(transactions.glob("*/transaction.json"))[-1].read_text(encoding="utf-8")
        )
        assert rolled_back["phase"] == "rolled_back"
        assert [process["name"] for process in rolled_back["processes"]] == ["hermes"]
        assert len(controller._processes) == 1
    finally:
        controller.close()


def test_hermes_invalid_yaml_preserves_the_user_file(tmp_path: Path) -> None:
    # Given
    config = tmp_path / ".hermes/config.yaml"
    config.parent.mkdir(parents=True)
    invalid = b"providers:\n  openai: {}\nproviders:\n  custom: {}\n"
    config.write_bytes(invalid)

    # When / Then
    with pytest.raises(WorkspaceCatalogSyncError, match="YAML"):
        runtime.refresh_catalog(
            workspace_root=tmp_path, adapter="hermes", catalog=_catalog("opencode-go/a")
        )
    assert config.read_bytes() == invalid


def test_kdense_build_rollback_restores_the_prior_generation(tmp_path: Path) -> None:
    # Given
    first = _kdense_catalog("opencode-go/first")
    runtime.refresh_catalog(workspace_root=tmp_path, adapter="kdense", catalog=first)
    current = tmp_path / ".local/state/dokploy-wizard/model-sync/current/models.json"
    before = current.read_bytes()

    class FailOnceProcessController(_ProcessController):
        def __init__(self) -> None:
            super().__init__()
            self._fail_health = True

        def verify_health(self) -> None:
            if self._fail_health:
                self._fail_health = False
                raise WorkspaceCatalogSyncError("K-Dense health check failed")
            super().verify_health()

    controller = FailOnceProcessController()
    process = controller.observe(name="kdense-supervisor", generation=2)
    reload = KdenseProcessReload(process=process, control=controller)

    try:
        # When / Then
        with pytest.raises(TransactionBlockedError, match="health"):
            runtime.refresh_catalog(
                workspace_root=tmp_path,
                adapter="kdense",
                catalog=_kdense_catalog("opencode-go/second"),
                kdense_process_reload=reload,
            )
        assert current.read_bytes() == before
        transactions = tmp_path / ".local/state/dokploy-wizard/model-sync/transactions"
        rolled_back = json.loads(
            sorted(transactions.glob("*/transaction.json"))[-1].read_text(encoding="utf-8")
        )
        assert rolled_back["phase"] == "rolled_back"
        assert rolled_back["processes"][0]["name"] == "kdense-supervisor"
    finally:
        controller.close()


def _kdense_catalog(alias: str) -> ModelCatalog:
    source_id = alias.removeprefix("opencode-go/")
    return ModelCatalog(
        base_url="http://wizard-shared-litellm:4000/v1",
        credential_environment="KDENSE_LITELLM_API_KEY",
        credential_value_sha256="a" * 64,
        models=(
            CatalogModel(
                alias=alias,
                display_name=source_id,
                kdense_metadata=KdenseCatalogMetadata(
                    prompt=3.0,
                    completion=15.0,
                    input_cache_read=0.3,
                    input_cache_write=3.75,
                    context_length=200000,
                    max_completion_tokens=16000,
                    source_id=source_id,
                    merged_decision_sha256="a" * 64,
                    pricing_selection_sha256="b" * 64,
                ),
            ),
        ),
    )


class _WorkspaceProofClient:
    def __init__(self, *, fail_after_delete: bool = False) -> None:
        self.fail_after_delete = fail_after_delete
        self.workspaces: dict[str, WorkspaceProofRef] = {}

    def find(self, workspace_name: str) -> tuple[WorkspaceProofRef, ...]:
        workspace = self.workspaces.get(workspace_name)
        return () if workspace is None else (workspace,)

    def create(self, *, template_name: str, workspace_name: str) -> WorkspaceProofRef:
        workspace = WorkspaceProofRef(
            workspace_id=f"workspace-{workspace_name}",
            workspace_name=workspace_name,
            template_name=template_name,
        )
        self.workspaces[workspace_name] = workspace
        return workspace

    def test(self, workspace: WorkspaceProofRef) -> None:
        assert workspace.workspace_name in self.workspaces

    def stop(self, workspace: WorkspaceProofRef) -> None:
        assert workspace.workspace_name in self.workspaces

    def delete(self, workspace: WorkspaceProofRef) -> None:
        del self.workspaces[workspace.workspace_name]
        if self.fail_after_delete:
            self.fail_after_delete = False
            raise RuntimeError("simulated deletion crash")


def test_proof_workspace_all_templates_create_test_stop_and_delete(tmp_path: Path) -> None:
    client = _WorkspaceProofClient()
    templates = (
        "ubuntu-vscode-hermes",
        "ubuntu-vscode-kdense-byok",
        "ubuntu-vscode-opencode-pi",
        "ubuntu-vscode-opencode-web",
    )

    with strict_proof_lease(tmp_path / "strict.lock") as recorder:
        receipts = tuple(
            run_workspace_proof(
                client=client,
                store=WorkspaceProofStore(tmp_path / template),
                template_name=template,
                mutation_recorder=recorder,
            )
            for template in templates
        )
        recorder.assert_strict_zero()

    assert all(receipt.phase is WorkspaceProofPhase.DELETED for receipt in receipts)
    assert client.workspaces == {}


def test_proof_workspace_crash_recovery_publishes_cleanup_receipt(tmp_path: Path) -> None:
    client = _WorkspaceProofClient(fail_after_delete=True)
    store = WorkspaceProofStore(tmp_path / "proof")

    with strict_proof_lease(tmp_path / "strict.lock") as recorder:
        with pytest.raises(RuntimeError, match="simulated deletion crash"):
            run_workspace_proof(
                client=client,
                store=store,
                template_name="ubuntu-vscode-opencode-pi",
                mutation_recorder=recorder,
            )
        recovered = run_workspace_proof(
            client=client,
            store=store,
            template_name="ubuntu-vscode-opencode-pi",
            mutation_recorder=recorder,
        )

    assert recovered.phase is WorkspaceProofPhase.DELETED
    assert client.workspaces == {}
