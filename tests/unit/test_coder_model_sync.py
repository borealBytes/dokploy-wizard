from __future__ import annotations

import json
import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Literal, assert_never

import pytest

from dokploy_wizard.dokploy.workspace_catalog_sync import (
    CatalogModel,
    CatalogTarget,
    KdenseCatalogMetadata,
    LegacyAdoptionBinding,
    LegacyAdoptionRequest,
    LegacyPointerEvidence,
    ModelCatalog,
    ProcessIdentity,
    RuntimeIdentityError,
    TransactionBlockedError,
    TransactionCasError,
    WorkspaceCatalogSyncError,
    WorkspaceCatalogTransaction,
    adapter_plan,
    observed_process_identity,
    signal_exact_process,
)


def _target(path: Path, content: bytes) -> CatalogTarget:
    return CatalogTarget.file(path=path, content=content, mode=0o640)


def _write_legacy_target(path: Path) -> str:
    path.write_bytes(
        b'{"provider":{"litellm":{"models":{"legacy":{}},"options":'
        b'{"baseURL":"http://stack-shared-litellm:4000/v1",'
        b'"apiKey":"${OPENAI_API_KEY}"}}}}\n'
    )
    path.chmod(0o640)
    return sha256(
        b'{"models":{"legacy":{}},"options":{"apiKey":"${OPENAI_API_KEY}",'
        b'"baseURL":"http://stack-shared-litellm:4000/v1"}}'
    ).hexdigest()


def _legacy(path: Path, baseline: str) -> LegacyPointerEvidence:
    return LegacyPointerEvidence(
        workspace_id="workspace-1",
        template_id="template-1",
        template_version_id="version-1",
        target=str(path),
        pointer="/provider/litellm",
        mode="0640",
        shape="json-pointer",
        scope="pointer",
        base_url="http://stack-shared-litellm:4000/v1",
        credential_value_sha256=sha256(b"${OPENAI_API_KEY}").hexdigest(),
        pointer_sha256=baseline,
        legacy_renderer_sha256=baseline,
        legacy_exact=True,
    )


def _legacy_request(path: Path, baseline: str) -> LegacyAdoptionRequest:
    evidence = _legacy(path, baseline)
    return LegacyAdoptionRequest(
        evidence=evidence,
        binding=LegacyAdoptionBinding(
            workspace_id=evidence.workspace_id,
            template_id=evidence.template_id,
            template_version_id=evidence.template_version_id,
            target=evidence.target,
            pointer=evidence.pointer,
            mode=evidence.mode,
            shape=evidence.shape,
            scope=evidence.scope,
            base_url=evidence.base_url,
            credential_value_sha256=evidence.credential_value_sha256,
        ),
        current_pre_sha256=baseline,
        current_target_sha256=sha256(path.read_bytes()).hexdigest(),
    )


def test_generation_transaction_commit_cleans_preimages_and_preserves_mode(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "config.json"
    target.write_bytes(b'{"before":true}\n')
    target.chmod(0o640)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=1)

    # When
    prepared = transaction.prepare(
        targets=(_target(target, b'{"after":true}\n'),), explicit_operator_update=True
    )
    committed = transaction.commit(cas_token=prepared.cas_token)

    # Then
    assert committed.phase == "committed"
    assert target.read_bytes() == b'{"after":true}\n'
    assert os.stat(target).st_mode & 0o777 == 0o640
    assert not (transaction.transaction_dir / "preimages" / "0.bin").exists()
    stored = json.loads((transaction.transaction_dir / "transaction.json").read_text())
    assert stored["targets"][0]["pre_state"] == "file"


@pytest.mark.parametrize("kind", ["absent", "file", "symlink"])
@pytest.mark.parametrize(
    "crash_after", ["prepared", "files_written", "pre_switch_verified", "switched"]
)
def test_generation_crash_matrix_restores_all_preimage_kinds(
    tmp_path: Path, kind: str, crash_after: str
) -> None:
    # Given
    target = tmp_path / "target"
    original = b"original"
    if kind == "file":
        target.write_bytes(original)
        target.chmod(0o640)
    elif kind == "symlink":
        source = tmp_path / "source"
        source.write_bytes(original)
        target.symlink_to(source.name)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=2)
    prepared = transaction.prepare(
        targets=(_target(target, b"replacement"),), explicit_operator_update=kind != "absent"
    )

    # When
    with pytest.raises(RuntimeError, match="injected crash"):
        transaction.commit(cas_token=prepared.cas_token, crash_after=crash_after)
    recovered = transaction.recover(cas_token=transaction.current().cas_token)

    # Then
    assert recovered.phase == "rolled_back"
    if kind == "absent":
        assert not target.exists()
    elif kind == "file":
        assert target.read_bytes() == original
        assert os.stat(target).st_mode & 0o777 == 0o640
    else:
        assert target.is_symlink()
        assert os.readlink(target) == "source"


def test_atomic_update_rejects_cas_mismatch_before_write(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "config.json"
    target.write_bytes(b"before")
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=3)
    transaction.prepare(targets=(_target(target, b"after"),), explicit_operator_update=True)

    # When / Then
    with pytest.raises(TransactionCasError):
        transaction.commit(cas_token="0" * 64)
    assert target.read_bytes() == b"before"


def test_concurrent_edit_blocks_without_overwrite_and_retains_preimage(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "config.json"
    target.write_bytes(b"before")
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=4)
    prepared = transaction.prepare(
        targets=(_target(target, b"managed"),), explicit_operator_update=True
    )
    target.write_bytes(b"user-edit")

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.commit(cas_token=prepared.cas_token)
    assert target.read_bytes() == b"user-edit"
    assert (transaction.transaction_dir / "preimages" / "0.bin").read_bytes() == b"before"
    assert transaction.current().phase == "blocked"


def test_legacy_exact_adoption_writes_receipt_before_mutation(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "opencode.json"
    baseline = _write_legacy_target(target)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=5)
    request = _legacy_request(target, baseline)

    # When
    transaction.adopt_legacy(request)

    # Then
    persisted = transaction.transaction_dir / "legacy-adoption-v1.json"
    stored = json.loads(persisted.read_text())
    assert stored["schema_version"] == 1
    assert stored["legacy_renderer_sha256"] == baseline
    assert "mode" not in stored
    assert os.stat(persisted).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("collision", ["file", "symlink", "dangling-symlink"])
def test_legacy_adoption_receipt_rejects_existing_leaf(
    tmp_path: Path, collision: str
) -> None:
    # Given
    target = tmp_path / "opencode.json"
    baseline = _write_legacy_target(target)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=12)
    receipt = transaction.transaction_dir / "legacy-adoption-v1.json"
    if collision == "file":
        receipt.write_bytes(b"collision")
    else:
        receipt.symlink_to(target if collision == "symlink" else tmp_path / "missing")
    before = os.lstat(receipt)

    # When / Then
    with pytest.raises(TransactionBlockedError, match="receipt already exists"):
        transaction.adopt_legacy(_legacy_request(target, baseline))
    assert os.lstat(receipt).st_ino == before.st_ino


def test_legacy_nonexact_conflict_blocks_without_adoption_receipt(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "opencode.json"
    target.write_bytes(
        b'{"provider":{"litellm":{"models":{},"options":'
        b'{"baseURL":"http://stack-shared-litellm:4000/v1",'
        b'"apiKey":"${OPENAI_API_KEY}"}}}}\n'
    )
    target.chmod(0o640)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=6)
    request = _legacy_request(target, "b" * 64)
    request = replace(request, evidence=replace(request.evidence, legacy_exact=False))

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.adopt_legacy(request)
    assert not (transaction.transaction_dir / "legacy-adoption-v1.json").exists()


def test_process_identity_mismatch_blocks_before_signal(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "config.json"
    target.write_bytes(b"before")
    identity = ProcessIdentity(
        name="hermes",
        pid=42,
        start_time_ticks=7,
        argv_sha256="a" * 64,
        executable_sha256="b" * 64,
        generation=7,
    )
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=7)
    prepared = transaction.prepare(
        targets=(_target(target, b"after"),), processes=(identity,), explicit_operator_update=True
    )
    switched = transaction.commit(cas_token=prepared.cas_token)
    signalled: list[int] = []

    class ProcessControl:
        def stop(self, pid: int) -> None:
            signalled.append(pid)

        def start(self, previous: ProcessIdentity) -> int:
            return previous.pid

        def replacement_pid(self, previous: ProcessIdentity) -> int | None:
            del previous
            return None

        def verify_health(self) -> None:
            return None

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.stop_processes(cas_token=switched.cas_token, control=ProcessControl())
    assert transaction.current().phase == "blocked"
    assert signalled == []


def test_process_identity_mismatch_never_signals_reused_pid() -> None:
    # Given
    observed = observed_process_identity(name="hermes", pid=os.getpid(), generation=8)
    changed = replace(observed, start_time_ticks=observed.start_time_ticks + 1)
    signalled: list[int] = []

    # When / Then
    with pytest.raises(RuntimeIdentityError, match="identity changed"):
        signal_exact_process(changed, signalled.append)
    assert signalled == []


def test_absent_creation_commits_requested_mode_and_ownership_baseline(
    tmp_path: Path,
) -> None:
    # Given
    target = tmp_path / "new.json"
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=9)

    # When
    prepared = transaction.prepare(targets=(_target(target, b"created"),))
    committed = transaction.commit(cas_token=prepared.cas_token)

    # Then
    assert committed.targets[0].pre_state == "absent"
    assert committed.targets[0].post_mode == "0640"
    assert target.read_bytes() == b"created"
    assert os.stat(target).st_mode & 0o777 == 0o640
    stored = json.loads((transaction.transaction_dir / "transaction.json").read_text())
    assert stored["schema_version"] == 2


def test_automatic_transition_is_forbidden_for_existing_workspace(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "config.json"
    target.write_bytes(b"before")
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=10)

    # When / Then
    with pytest.raises(TransactionBlockedError, match="automatic transition"):
        transaction.prepare(targets=(_target(target, b"after"),))
    assert target.read_bytes() == b"before"


def test_staged_symlink_replacement_is_rejected_before_target_write(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "config.json"
    outside = tmp_path / "outside"
    outside.write_bytes(b"managed")
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=11)
    prepared = transaction.prepare(targets=(_target(target, b"managed"),))
    staged = transaction.transaction_dir / "staged" / "0.bin"
    staged.unlink()
    staged.symlink_to(outside)

    # When / Then
    with pytest.raises(TransactionBlockedError, match="staged"):
        transaction.commit(cas_token=prepared.cas_token)
    assert not target.exists()


def test_kdense_catalog_update_renders_priced_central_models(tmp_path: Path) -> None:
    # Given
    catalog = _kdense_catalog()

    # When
    plan = adapter_plan("kdense", tmp_path, catalog)

    # Then
    generated = next(target for target in plan.targets if target.kind == "file")
    models = json.loads(generated.content)
    assert models == [
        {
            "context_length": 200000,
            "id": "opencode-go/anthropic/claude-sonnet-4-5",
            "label": "Claude Sonnet 4.5",
            "max_completion_tokens": 16000,
            "pricing": {
                "completion": 15.0,
                "input_cache_read": 0.3,
                "input_cache_write": 3.75,
                "prompt": 3.0,
            },
            "provider": "LiteLLM",
            "provenance": {
                "dokploy_pricing_selection_sha256": "b" * 64,
                "managed_by": "dokploy-wizard",
                "managed_catalog": "opencode-go",
                "merged_decision_sha256": "a" * 64,
                "source_id": "anthropic/claude-sonnet-4-5",
            },
        }
    ]


@pytest.mark.parametrize("field", ("prompt", "completion"))
def test_kdense_unpriced_catalog_rejects_zero_prices(
    tmp_path: Path, field: Literal["prompt", "completion"]
) -> None:
    # Given
    match field:
        case "prompt":
            metadata = replace(_kdense_metadata(), prompt=0.0)
        case "completion":
            metadata = replace(_kdense_metadata(), completion=0.0)
        case unreachable:
            assert_never(unreachable)
    catalog = _kdense_catalog(metadata=metadata)

    # When / Then
    with pytest.raises(WorkspaceCatalogSyncError, match="pricing"):
        adapter_plan("kdense", tmp_path, catalog)


def test_kdense_unknown_zero_provenance_rejects_unowned_model(tmp_path: Path) -> None:
    # Given
    catalog = _kdense_catalog(metadata=replace(_kdense_metadata(), source_id="other/model"))

    # When / Then
    with pytest.raises(WorkspaceCatalogSyncError, match="provenance"):
        adapter_plan("kdense", tmp_path, catalog)


def _kdense_catalog(metadata: KdenseCatalogMetadata | None = None) -> ModelCatalog:
    return ModelCatalog(
        base_url="http://stack-shared-litellm:4000/v1",
        credential_environment="KDENSE_LITELLM_API_KEY",
        credential_value_sha256="d" * 64,
        models=(
            CatalogModel(
                alias="opencode-go/anthropic/claude-sonnet-4-5",
                display_name="Claude Sonnet 4.5",
                kdense_metadata=_kdense_metadata() if metadata is None else metadata,
            ),
        ),
    )


def _kdense_metadata() -> KdenseCatalogMetadata:
    return KdenseCatalogMetadata(
        prompt=3.0,
        completion=15.0,
        input_cache_read=0.3,
        input_cache_write=3.75,
        context_length=200000,
        max_completion_tokens=16000,
        source_id="anthropic/claude-sonnet-4-5",
        merged_decision_sha256="a" * 64,
        pricing_selection_sha256="b" * 64,
    )
