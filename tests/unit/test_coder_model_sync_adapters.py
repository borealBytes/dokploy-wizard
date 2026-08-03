from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.workspace_catalog_sync import (
    CatalogModel,
    KdenseCatalogMetadata,
    ModelCatalog,
    TransactionBlockedError,
    WorkspaceCatalogSyncError,
    WorkspaceCatalogTransaction,
    adapter_plan,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_yaml import yaml_value_at


def _catalog(*, credential_environment: str = "OPENAI_API_KEY") -> ModelCatalog:
    kdense_metadata = (
        KdenseCatalogMetadata(
            prompt=3.0,
            completion=15.0,
            input_cache_read=0.3,
            input_cache_write=3.75,
            context_length=200000,
            max_completion_tokens=16000,
            source_id="deepseek",
            merged_decision_sha256="a" * 64,
            pricing_selection_sha256="b" * 64,
        )
        if credential_environment == "KDENSE_LITELLM_API_KEY"
        else None
    )
    return ModelCatalog(
        base_url="http://stack-shared-litellm:4000/v1",
        credential_environment=credential_environment,
        credential_value_sha256="a" * 64,
        models=(
            CatalogModel(
                alias="opencode-go/deepseek",
                display_name="DeepSeek",
                kdense_metadata=kdense_metadata,
            ),
        ),
    )


def _targets_by_name(root: Path) -> dict[str, bytes]:
    return {
        str(target.path.relative_to(root)): target.content
        for target in adapter_plan("primary", root, _catalog()).targets
    }


def test_primary_adapter_preserves_unrelated_json_bytes_and_owned_boundaries(
    tmp_path: Path,
) -> None:
    # Given
    config = tmp_path / ".config/opencode/opencode.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(
        b'{\n  "model": "keep",\n  "provider": {"other": {"x": 1}},\n  "tail": true\n}\n'
    )
    pi = tmp_path / ".pi/agent/models.json"
    pi.parent.mkdir(parents=True)
    pi.write_bytes(b'{"default":"keep", "providers":{"other":{}}, "tail":2}\n')
    settings = tmp_path / ".local/share/code-server/User/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"other.setting": true, "tail": "keep"}\n')

    # When
    rendered = _targets_by_name(tmp_path)

    # Then
    opencode = rendered[".config/opencode/opencode.json"]
    assert opencode.startswith(b'{\n  "model": "keep",\n  "provider": {"other": {"x": 1},')
    assert opencode.endswith(b'},\n  "tail": true\n}\n')
    assert json.loads(opencode)["model"] == "keep"
    pi_document = json.loads(rendered[".pi/agent/models.json"])
    assert pi_document["default"] == "keep"
    assert pi_document["providers"]["other"] == {}
    assert rendered[".pi/agent/models.json"].endswith(b', "tail":2}\n')
    settings_bytes = rendered[".local/share/code-server/User/settings.json"]
    assert settings_bytes.startswith(b'{"other.setting": true, "tail": "keep",')
    assert "github.copilot.chat.customOAIModels" in json.loads(settings_bytes)


def test_pointer_prehash_ignores_unrelated_edits_but_detects_owned_edits(
    tmp_path: Path,
) -> None:
    # Given
    path = tmp_path / ".config/opencode/opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"provider":{"litellm":{"models":{"old":{}}}},"unrelated":"one"}\n')
    first = adapter_plan("primary", tmp_path, _catalog()).targets[0]
    path.write_text('{"provider":{"litellm":{"models":{"old":{}}}},"unrelated":"two"}\n')

    # When
    unrelated = adapter_plan("primary", tmp_path, _catalog()).targets[0]
    path.write_text(
        '{"provider":{"litellm":{"models":{"user-owned-edit":{}}}},"unrelated":"two"}\n'
    )
    owned = adapter_plan("primary", tmp_path, _catalog()).targets[0]

    # Then
    assert first.owned_pointers[0].pre_sha256 == unrelated.owned_pointers[0].pre_sha256
    assert unrelated.content != first.content
    assert owned.owned_pointers[0].pre_sha256 != unrelated.owned_pointers[0].pre_sha256


def test_opencode_web_owns_no_pi_pointer_and_uses_openai_environment(tmp_path: Path) -> None:
    # Given / When
    plan = adapter_plan("opencode-web", tmp_path, _catalog())

    # Then
    assert all(".pi/agent/models.json" not in str(target.path) for target in plan.targets)
    assert len(plan.targets) == 3
    assert plan.credential_environment == "OPENAI_API_KEY"
    assert plan.base_environment == "OPENAI_API_BASE"


def test_hermes_updates_only_owned_mappings_and_never_refreshes_model(tmp_path: Path) -> None:
    # Given
    config = tmp_path / ".hermes/config.yaml"
    config.parent.mkdir(parents=True)
    model = "model:\n  provider: custom\n  default: user-choice\n"
    config.write_text(f"unrelated: keep\nproviders:\n  other: true\n{model}tail: keep\n")

    # When
    target = adapter_plan("hermes", tmp_path, _catalog()).targets[0]

    # Then
    text = target.content.decode()
    assert "unrelated: keep\n" in text
    assert "  other: true\n" in text
    assert model in text
    assert "tail: keep\n" in text
    assert yaml_value_at(target.content, ("providers", "openai", "key_env")) == ("OPENAI_API_KEY")
    assert (
        yaml_value_at(
            target.content,
            ("platforms", "api_server", "extra", "model_routes", "opencode-go/deepseek"),
        )
        == "openai"
    )


def test_hermes_seeds_model_structurally_only_when_absent(tmp_path: Path) -> None:
    # Given
    config = tmp_path / ".hermes/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text('description: "the word model: is unrelated"\n')

    # When
    target = adapter_plan("hermes", tmp_path, _catalog()).targets[0]

    # Then
    assert yaml_value_at(target.content, ("model", "provider")) == "openai"
    assert yaml_value_at(target.content, ("model", "default")) == "opencode-go/deepseek"
    assert target.content.startswith(b'description: "the word model: is unrelated"\n')


def test_hermes_quotes_model_route_keys_that_contain_yaml_delimiters(tmp_path: Path) -> None:
    # Given
    catalog = replace(
        _catalog(),
        models=(
            CatalogModel(
                alias="openrouter/provider/model:free",
                display_name="Provider Model",
            ),
        ),
    )

    # When
    target = adapter_plan("hermes", tmp_path, catalog).targets[0]

    # Then
    assert (
        yaml_value_at(
            target.content,
            ("platforms", "api_server", "extra", "model_routes", "openrouter/provider/model:free"),
        )
        == "openai"
    )


def test_hermes_rejects_malformed_or_duplicate_yaml_without_replacement(tmp_path: Path) -> None:
    # Given
    config = tmp_path / ".hermes/config.yaml"
    config.parent.mkdir(parents=True)
    malformed = b"providers:\n  openai: true\nproviders:\n  other: true\n"
    config.write_bytes(malformed)

    # When / Then
    with pytest.raises(WorkspaceCatalogSyncError, match="YAML"):
        adapter_plan("hermes", tmp_path, _catalog())
    assert config.read_bytes() == malformed


def test_kdense_owns_immutable_generation_and_valid_current_symlink(tmp_path: Path) -> None:
    # Given / When
    plan = adapter_plan(
        "kdense",
        tmp_path,
        _catalog(credential_environment="KDENSE_LITELLM_API_KEY"),
    )

    # Then
    generated = next(target for target in plan.targets if target.kind == "file")
    current = next(target for target in plan.targets if target.kind == "symlink")
    assert generated.path.parent == current.path.parent / current.content.decode()
    assert generated.path.name == "models.json"
    assert plan.credential_environment == "KDENSE_LITELLM_API_KEY"
    assert plan.base_environment == "KDENSE_LITELLM_BASE_URL"
    assert b"OPENAI_API_KEY" not in generated.content
    assert b"KDENSE_LITELLM_API_KEY" not in generated.content


def test_kdense_generation_commit_materializes_a_resolving_current_symlink(
    tmp_path: Path,
) -> None:
    # Given
    plan = adapter_plan(
        "kdense",
        tmp_path,
        _catalog(credential_environment="KDENSE_LITELLM_API_KEY"),
    )
    generated = next(target for target in plan.targets if target.kind == "file")
    current = next(target for target in plan.targets if target.kind == "symlink")
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=41)
    generated.path.parent.mkdir(parents=True)
    prepared = transaction.prepare(targets=plan.targets)

    # When
    transaction.commit(cas_token=prepared.cas_token)

    # Then
    assert current.path.is_symlink()
    assert current.path.resolve(strict=True) == generated.path.parent
    assert generated.path.read_bytes() == generated.content


def test_owned_pointer_edit_after_prepare_blocks_without_overwrite(tmp_path: Path) -> None:
    # Given
    config = tmp_path / ".config/opencode/opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"provider":{"litellm":{"models":{"legacy":{}}}},"unrelated":"keep"}\n')
    plan = adapter_plan("primary", tmp_path, _catalog())
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=42)
    prepared = transaction.prepare(targets=plan.targets, explicit_operator_update=True)
    conflict = b'{"provider":{"litellm":{"models":{"user-edit":{}}}},"unrelated":"keep"}\n'
    config.write_bytes(conflict)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.commit(cas_token=prepared.cas_token)
    assert config.read_bytes() == conflict


@pytest.mark.parametrize(
    ("adapter", "credential_environment"),
    [
        ("primary", "KDENSE_LITELLM_API_KEY"),
        ("opencode-web", "KDENSE_LITELLM_API_KEY"),
        ("hermes", "KDENSE_LITELLM_API_KEY"),
        ("kdense", "OPENAI_API_KEY"),
    ],
)
def test_adapter_rejects_wrong_credential_environment(
    tmp_path: Path, adapter: str, credential_environment: str
) -> None:
    # Given / When / Then
    with pytest.raises(WorkspaceCatalogSyncError, match="credential environment"):
        adapter_plan(adapter, tmp_path, _catalog(credential_environment=credential_environment))


@pytest.mark.parametrize(
    "base_url",
    [
        "https://stack-shared-litellm:4000/v1",
        "http://api.openai.com:4000/v1",
        "http://stack-shared-litellm:4000",
        "http://stack-shared-litellm:4000/v1?public=true",
        "http://user@stack-shared-litellm:4000/v1",
    ],
)
def test_adapter_rejects_non_internal_litellm_base(tmp_path: Path, base_url: str) -> None:
    # Given
    catalog = replace(_catalog(), base_url=base_url)

    # When / Then
    with pytest.raises(WorkspaceCatalogSyncError, match="base URL"):
        adapter_plan("primary", tmp_path, catalog)
