from __future__ import annotations

import json
import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Literal, assert_never

import pytest

from dokploy_wizard.dokploy.workspace_catalog_sync import (
    WorkspaceCatalogTransaction,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    JsonValue,
    LegacyAdoptionBinding,
    LegacyAdoptionRequest,
    LegacyPointerEvidence,
    TransactionBlockedError,
)

EvidenceMutation = Literal[
    "workspace_id",
    "template_id",
    "template_version_id",
    "target",
    "pointer",
    "mode",
    "shape",
    "scope",
    "base_url",
    "credential_value_sha256",
    "pointer_sha256",
    "legacy_renderer_sha256",
    "legacy_exact",
]
RequestPrehash = Literal["current_pre_sha256", "current_target_sha256"]
KdenseEvidenceMutation = Literal[
    "target_sha256",
    "symlink_state",
    "symlink_target",
    "symlink_sha256",
    "renderer_source_path",
    "renderer_source_revision",
]


def _json_sha(value: JsonValue) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(encoded).hexdigest()


def _change_evidence(
    evidence: LegacyPointerEvidence, field: EvidenceMutation
) -> LegacyPointerEvidence:
    match field:
        case "workspace_id":
            return replace(evidence, workspace_id="workspace-2")
        case "template_id":
            return replace(evidence, template_id="template-2")
        case "template_version_id":
            return replace(evidence, template_version_id="version-2")
        case "target":
            return replace(evidence, target="/different/target.json")
        case "pointer":
            return replace(evidence, pointer="/provider/other")
        case "mode":
            return replace(evidence, mode="0600")
        case "shape":
            return replace(evidence, shape="yaml-pointer")
        case "scope":
            return replace(evidence, scope="target-and-symlink")
        case "base_url":
            return replace(evidence, base_url="http://public-provider.example/v1")
        case "credential_value_sha256":
            return replace(evidence, credential_value_sha256="b" * 64)
        case "pointer_sha256":
            return replace(evidence, pointer_sha256="b" * 64)
        case "legacy_renderer_sha256":
            return replace(evidence, legacy_renderer_sha256="b" * 64)
        case "legacy_exact":
            return replace(evidence, legacy_exact=False)
        case unreachable:
            assert_never(unreachable)


def _stale_request(request: LegacyAdoptionRequest, field: RequestPrehash) -> LegacyAdoptionRequest:
    match field:
        case "current_pre_sha256":
            return replace(request, current_pre_sha256="b" * 64)
        case "current_target_sha256":
            return replace(request, current_target_sha256="b" * 64)
        case unreachable:
            assert_never(unreachable)


def _break_kdense_evidence(
    evidence: LegacyPointerEvidence, field: KdenseEvidenceMutation
) -> LegacyPointerEvidence:
    match field:
        case "target_sha256":
            return replace(evidence, target_sha256="e" * 64)
        case "symlink_state":
            return replace(evidence, symlink_state=None)
        case "symlink_target":
            return replace(evidence, symlink_target="other.json")
        case "symlink_sha256":
            return replace(evidence, symlink_sha256="e" * 64)
        case "renderer_source_path":
            return replace(evidence, renderer_source_path="other/models.json")
        case "renderer_source_revision":
            return replace(evidence, renderer_source_revision="e" * 39)
        case unreachable:
            assert_never(unreachable)


def _pointer_request(path: Path) -> LegacyAdoptionRequest:
    document = json.loads(path.read_bytes())
    pointer_sha = _json_sha(document["provider"]["litellm"])
    target_sha = sha256(path.read_bytes()).hexdigest()
    credential_sha = sha256(b"${OPENAI_API_KEY}").hexdigest()
    binding = LegacyAdoptionBinding(
        workspace_id="workspace-1",
        template_id="template-1",
        template_version_id="version-1",
        target=str(path),
        pointer="/provider/litellm",
        mode="0644",
        shape="json-pointer",
        scope="pointer",
        base_url="http://stack-shared-litellm:4000/v1",
        credential_value_sha256=credential_sha,
    )
    evidence = LegacyPointerEvidence(
        workspace_id=binding.workspace_id,
        template_id=binding.template_id,
        template_version_id=binding.template_version_id,
        target=binding.target,
        pointer=binding.pointer,
        mode=binding.mode,
        shape=binding.shape,
        scope=binding.scope,
        base_url=binding.base_url,
        credential_value_sha256=binding.credential_value_sha256,
        pointer_sha256=pointer_sha,
        legacy_renderer_sha256=pointer_sha,
        legacy_exact=True,
    )
    return LegacyAdoptionRequest(
        evidence=evidence,
        binding=binding,
        current_pre_sha256=pointer_sha,
        current_target_sha256=target_sha,
    )


def _write_pointer_target(path: Path) -> None:
    path.write_bytes(
        b'{"model":"untouched","provider":{"litellm":{"models":{"legacy":{}},'
        b'"options":{"baseURL":"http://stack-shared-litellm:4000/v1",'
        b'"apiKey":"${OPENAI_API_KEY}"}}},'
        b'"user":{"bytes":"stay"}}\n'
    )
    path.chmod(0o644)


def test_exact_pointer_adoption_persists_private_receipt_before_any_target_write(
    tmp_path: Path,
) -> None:
    # Given
    target = tmp_path / "opencode.json"
    _write_pointer_target(target)
    original = target.read_bytes()
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=31)

    # When
    receipt = transaction.adopt_legacy(_pointer_request(target))

    # Then
    persisted = transaction.transaction_dir / "legacy-adoption-v1.json"
    stored = json.loads(persisted.read_text())
    assert target.read_bytes() == original
    assert receipt.current_pre_sha256 == stored["current_pre_sha256"]
    assert stored["baseline_sha256"] == stored["legacy_renderer_sha256"]
    assert os.stat(persisted).st_mode & 0o777 == 0o600
    assert "${OPENAI_API_KEY}" not in persisted.read_text()
    assert "models" not in persisted.read_text()


@pytest.mark.parametrize(
    "field",
    [
        "workspace_id",
        "template_id",
        "template_version_id",
        "target",
        "pointer",
        "mode",
        "shape",
        "scope",
        "base_url",
        "credential_value_sha256",
        "pointer_sha256",
        "legacy_renderer_sha256",
        "legacy_exact",
    ],
)
def test_adoption_rejects_each_changed_evidence_field(
    tmp_path: Path, field: EvidenceMutation
) -> None:
    # Given
    target = tmp_path / "opencode.json"
    _write_pointer_target(target)
    original = target.read_bytes()
    request = _pointer_request(target)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=32)
    changed = replace(request, evidence=_change_evidence(request.evidence, field))

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.adopt_legacy(changed)
    assert not (transaction.transaction_dir / "legacy-adoption-v1.json").exists()
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    "request_field",
    ["current_pre_sha256", "current_target_sha256"],
)
def test_adoption_rejects_each_stale_current_prehash(
    tmp_path: Path, request_field: RequestPrehash
) -> None:
    # Given
    target = tmp_path / "opencode.json"
    _write_pointer_target(target)
    request = _pointer_request(target)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=33)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.adopt_legacy(_stale_request(request, request_field))
    assert not (transaction.transaction_dir / "legacy-adoption-v1.json").exists()


def test_adoption_rejects_missing_required_evidence(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "opencode.json"
    _write_pointer_target(target)
    request = _pointer_request(target)
    missing = replace(request, evidence=replace(request.evidence, workspace_id=""))
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=36)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.adopt_legacy(missing)
    assert not (transaction.transaction_dir / "legacy-adoption-v1.json").exists()


def test_adoption_rejects_structural_pointer_drift_even_with_fresh_whole_file_hash(
    tmp_path: Path,
) -> None:
    # Given
    target = tmp_path / "opencode.json"
    _write_pointer_target(target)
    request = _pointer_request(target)
    target.write_bytes(
        b'{"model":"untouched","provider":{"litellm":{"models":{"edited":{}},'
        b'"options":{"baseURL":"http://stack-shared-litellm:4000/v1",'
        b'"apiKey":"${OPENAI_API_KEY}"}}},'
        b'"user":{"bytes":"stay"}}\n'
    )
    changed = replace(
        request,
        current_target_sha256=sha256(target.read_bytes()).hexdigest(),
    )
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=34)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.adopt_legacy(changed)
    assert not (transaction.transaction_dir / "legacy-adoption-v1.json").exists()


def test_adoption_rejects_pointer_credential_drift_with_self_consistent_hashes(
    tmp_path: Path,
) -> None:
    # Given
    target = tmp_path / "opencode.json"
    _write_pointer_target(target)
    request = _pointer_request(target)
    target.write_bytes(
        b'{"model":"untouched","provider":{"litellm":{"models":{"legacy":{}},'
        b'"options":{"baseURL":"http://stack-shared-litellm:4000/v1",'
        b'"apiKey":"${DIFFERENT_KEY}"}}},"user":{"bytes":"stay"}}\n'
    )
    pointer = json.loads(target.read_bytes())["provider"]["litellm"]
    pointer_sha = _json_sha(pointer)
    changed = replace(
        request,
        evidence=replace(
            request.evidence,
            pointer_sha256=pointer_sha,
            legacy_renderer_sha256=pointer_sha,
        ),
        current_pre_sha256=pointer_sha,
        current_target_sha256=sha256(target.read_bytes()).hexdigest(),
    )
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=39)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.adopt_legacy(changed)
    assert not (transaction.transaction_dir / "legacy-adoption-v1.json").exists()


def _kdense_request(target: Path, current: Path) -> LegacyAdoptionRequest:
    target_sha = _json_sha(json.loads(target.read_bytes()))
    symlink_target = os.readlink(current)
    symlink_sha = sha256(symlink_target.encode()).hexdigest()
    base_url = "http://stack-shared-litellm:4000/v1"
    credential_sha = "c" * 64
    pointer_sha = _json_sha(
        {
            "base_url": base_url,
            "credential_value_sha256": credential_sha,
            "symlink_sha256": symlink_sha,
            "target_sha256": target_sha,
        }
    )
    binding = LegacyAdoptionBinding(
        workspace_id="workspace-kdense",
        template_id="template-kdense",
        template_version_id="version-kdense",
        target=str(target),
        pointer=str(current),
        mode="0644",
        shape="json-target-and-symlink",
        scope="target-and-symlink",
        base_url=base_url,
        credential_value_sha256=credential_sha,
    )
    evidence = LegacyPointerEvidence(
        workspace_id=binding.workspace_id,
        template_id=binding.template_id,
        template_version_id=binding.template_version_id,
        target=binding.target,
        pointer=binding.pointer,
        mode=binding.mode,
        shape=binding.shape,
        scope=binding.scope,
        base_url=base_url,
        credential_value_sha256=credential_sha,
        pointer_sha256=pointer_sha,
        legacy_renderer_sha256=pointer_sha,
        legacy_exact=True,
        target_sha256=target_sha,
        symlink_state="present",
        symlink_target=symlink_target,
        symlink_sha256=symlink_sha,
        renderer_source_path="web/src/data/models.json",
        renderer_source_revision="d" * 40,
    )
    return LegacyAdoptionRequest(
        evidence=evidence,
        binding=binding,
        current_pre_sha256=pointer_sha,
        current_target_sha256=sha256(target.read_bytes()).hexdigest(),
    )


@pytest.mark.parametrize("drift", ["target", "symlink"])
def test_kdense_adoption_validates_target_and_current_symlink_hashes(
    tmp_path: Path, drift: str
) -> None:
    # Given
    target = tmp_path / "models.json"
    target.write_text('[{"id":"legacy"}]\n')
    target.chmod(0o644)
    current = tmp_path / "current"
    current.symlink_to("models.json")
    request = _kdense_request(target, current)
    if drift == "target":
        target.write_text('[{"id":"edited"}]\n')
        request = replace(
            request,
            current_target_sha256=sha256(target.read_bytes()).hexdigest(),
        )
    else:
        current.unlink()
        current.symlink_to("other.json")
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=35)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.adopt_legacy(request)
    assert not (transaction.transaction_dir / "legacy-adoption-v1.json").exists()


def test_kdense_adoption_rejects_missing_current_symlink_as_conflict(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "models.json"
    target.write_text('[{"id":"legacy"}]\n')
    target.chmod(0o644)
    current = tmp_path / "current"
    current.symlink_to("models.json")
    request = _kdense_request(target, current)
    current.unlink()
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=38)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.adopt_legacy(request)
    assert not (transaction.transaction_dir / "legacy-adoption-v1.json").exists()


@pytest.mark.parametrize(
    "field",
    [
        "target_sha256",
        "symlink_state",
        "symlink_target",
        "symlink_sha256",
        "renderer_source_path",
        "renderer_source_revision",
    ],
)
def test_kdense_adoption_rejects_each_changed_dual_target_evidence_field(
    tmp_path: Path, field: KdenseEvidenceMutation
) -> None:
    # Given
    target = tmp_path / "models.json"
    target.write_text('[{"id":"legacy"}]\n')
    target.chmod(0o644)
    current = tmp_path / "current"
    current.symlink_to("models.json")
    request = _kdense_request(target, current)
    request = replace(
        request,
        evidence=_break_kdense_evidence(request.evidence, field),
    )
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=37)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.adopt_legacy(request)
    assert not (transaction.transaction_dir / "legacy-adoption-v1.json").exists()
