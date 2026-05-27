from __future__ import annotations

import json
from pathlib import Path

import pytest

from dokploy_wizard.artifact_secret_scan import (
    collect_secret_candidates,
    main,
    scan_artifacts,
)


def test_scan_fails_on_raw_install_env_secret_without_printing_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".install.env"
    secret = "SECRET_TEST_INSTALL_API_KEY_VALUE"
    env_file.write_text(
        f"ROOT_DOMAIN=example.com\nDOKPLOY_API_KEY={secret}\n",
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "proof.log").write_text(
        f"deployment accidentally logged DOKPLOY_API_KEY={secret}\n",
        encoding="utf-8",
    )

    result = scan_artifacts(
        artifact_roots=(artifact_dir,),
        candidates=collect_secret_candidates(env_file=env_file),
        secret_source_paths=(env_file,),
    )

    assert not result.passed
    rendered = json.dumps(result.to_dict(), sort_keys=True)
    assert "env:DOKPLOY_API_KEY" in rendered
    assert secret not in rendered


def test_scan_allows_key_names_and_redacted_fingerprints(tmp_path: Path) -> None:
    env_file = tmp_path / ".install.env"
    secret = "SECRET_TEST_ALLOWED_REDACTED_VALUE"
    env_file.write_text(f"OPENROUTER_API_KEY={secret}\n", encoding="utf-8")
    candidate = collect_secret_candidates(env_file=env_file)[0]
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "inspect-state.json").write_text(
        "OPENROUTER_API_KEY=<REDACTED>\n"
        f"fingerprint={candidate.fingerprint}\n"
        "key=OPENROUTER_API_KEY\n",
        encoding="utf-8",
    )

    result = scan_artifacts(
        artifact_roots=(artifact_dir,),
        candidates=(candidate,),
        secret_source_paths=(env_file,),
    )

    assert result.passed
    assert result.findings == ()


def test_scan_uses_generated_env_spec_secret_source_and_skips_source_file(tmp_path: Path) -> None:
    generated_secret = "SECRET_TEST_GENERATED_LITELLM_VIRTUAL_KEY"
    secret_source = tmp_path / "litellm-generated-keys.json"
    secret_source.write_text(
        json.dumps(
            {
                "format_version": 1,
                "master_key": "SECRET_TEST_GENERATED_MASTER_KEY",
                "virtual_keys": {"openclaw": generated_secret},
            }
        ),
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "collected-remote"
    artifact_dir.mkdir()
    (artifact_dir / "applied-state.json").write_text(
        f'{{"env":"{generated_secret}"}}\n',
        encoding="utf-8",
    )

    result = scan_artifacts(
        artifact_roots=(artifact_dir, secret_source),
        candidates=collect_secret_candidates(env_file=None, secret_sources=(secret_source,)),
        secret_source_paths=(secret_source,),
    )

    assert not result.passed
    rendered = json.dumps(result.to_dict(), sort_keys=True)
    assert "litellm-generated-keys.json:virtual_keys:openclaw" in rendered
    assert generated_secret not in rendered


def test_scan_uses_surfsense_generated_secret_source_and_skips_source_file(tmp_path: Path) -> None:
    generated_secret = "SECRET_TEST_SURFSENSE_GENERATED_SECRET_KEY"
    secret_source = tmp_path / "surfsense-generated-secrets.json"
    secret_source.write_text(
        json.dumps(
            {
                "format_version": 1,
                "secrets": {
                    "db_password": "SECRET_TEST_SURFSENSE_GENERATED_DB_PASSWORD",
                    "jwt_secret": "SECRET_TEST_SURFSENSE_GENERATED_JWT_SECRET",
                    "searxng_secret": "SECRET_TEST_SURFSENSE_GENERATED_SEARXNG_SECRET",
                    "secret_key": generated_secret,
                    "zero_admin_password": "SECRET_TEST_SURFSENSE_GENERATED_ZERO_ADMIN_PASSWORD",
                },
            }
        ),
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "collected-remote"
    artifact_dir.mkdir()
    (artifact_dir / "inspect-state.json").write_text(
        f'{{"SURFSENSE_SECRET_KEY":"{generated_secret}"}}\n',
        encoding="utf-8",
    )

    result = scan_artifacts(
        artifact_roots=(artifact_dir, secret_source),
        candidates=collect_secret_candidates(env_file=None, secret_sources=(secret_source,)),
        secret_source_paths=(secret_source,),
    )

    assert not result.passed
    rendered = json.dumps(result.to_dict(), sort_keys=True)
    assert "surfsense-generated-secrets.json:secrets:secret_key" in rendered
    assert generated_secret not in rendered


def test_cli_writes_json_summary_and_returns_nonzero_on_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".install.env"
    secret = "SECRET_TEST_CLI_PASSWORD_VALUE"
    env_file.write_text(f"DOKPLOY_ADMIN_PASSWORD={secret}\n", encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "compose.yml").write_text(secret, encoding="utf-8")
    output_path = tmp_path / "scan.json"

    exit_code = main(
        [
            "--artifact-root",
            str(artifact_dir),
            "--env-file",
            str(env_file),
            "--json-output",
            str(output_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert output_path.exists()
    assert secret not in captured.out
    assert secret not in output_path.read_text(encoding="utf-8")
