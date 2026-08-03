from __future__ import annotations

from dataclasses import replace

import pytest

from dokploy_wizard.dokploy.coder_runtime_asset_kdense_skills import verify_kdense_skills
from dokploy_wizard.dokploy.coder_runtime_asset_manifest import (
    load_runtime_manifest,
    runtime_manifest_path,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError


def test_valid_provenance_scientific_skills_contract() -> None:
    # Given
    skills = load_runtime_manifest(runtime_manifest_path()).kdense.skills

    # When / Then
    verify_kdense_skills(skills)


def test_lock_drift_scientific_skills_repository_rejects_before_download() -> None:
    # Given
    skills = load_runtime_manifest(runtime_manifest_path()).kdense.skills
    drifted = replace(skills, repository="invalid/repository")

    # When / Then
    with pytest.raises(RuntimeAssetError, match="repository"):
        verify_kdense_skills(drifted)
