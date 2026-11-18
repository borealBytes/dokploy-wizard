# ruff: noqa: E501
"""Unit tests for the box-local Traefik HTTPS edge (static trust + drop-in)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dokploy_wizard.networking.edge_https import (
    EDGE_HTTPS_FILENAME,
    TRUSTED_FORWARDED_IPS,
    apply_edge_https_config,
    apply_static_trust,
    build_edge_https_config,
    ensure_trusted_forwarded_headers,
)


def _recorder() -> tuple[list[str], Callable[[str], bool]]:
    calls: list[str] = []

    def restart(service: str) -> bool:
        calls.append(service)
        return True

    return calls, restart


_STATIC_CONFIG = (
    "entryPoints:\n"
    "  web:\n"
    "    address: :80\n"
    "  websecure:\n"
    "    address: :443\n"
    "    http3:\n"
    "      advertisedPort: 443\n"
    "providers:\n"
    "  file:\n"
    "    directory: /etc/dokploy/traefik/dynamic\n"
    "    watch: true\n"
)


def _traefik_dir(tmp_path: Path, *, with_static: bool = True, with_dynamic: bool = True) -> Path:
    if with_static:
        (tmp_path / "traefik.yml").write_text(_STATIC_CONFIG, encoding="utf-8")
    if with_dynamic:
        (tmp_path / "dynamic").mkdir()
    return tmp_path


def test_renderer_emits_v3_header_rules_sorted_and_deduplicated() -> None:
    config = build_edge_https_config(
        ["coder.example.com", "FileX.example.com", "coder.example.com", "dokploy.example.com"]
    )

    assert config.index("wizard-edge-https-coder-example-com:") < config.index(
        "wizard-edge-https-dokploy-example-com:"
    )
    assert config.count("wizard-edge-https-coder-example-com:") == 1
    # Hostnames are lower-cased before rendering.
    assert "wizard-edge-https-filex-example-com:" in config
    # Traefik v3 rule name: v2's HeadersRegexp was removed and fails open.
    assert (
        "rule: \"Host(`filex.example.com`) && Header(`X-Forwarded-Proto`, `http`)\"" in config
    )
    assert "priority: 1000" in config
    assert "redirectScheme:" in config
    assert "scheme: https" in config
    assert "permanent: true" in config
    assert "wizard-edge-discard:" in config


def test_renderer_is_byte_stable() -> None:
    first = build_edge_https_config(["b.example.com", "a.example.com"])
    second = build_edge_https_config(["a.example.com", "b.example.com"])
    assert first == second


def test_renderer_rejects_wildcards_and_junk() -> None:
    with pytest.raises(ValueError, match="Wildcard hostnames"):
        build_edge_https_config(["*.example.com"])
    with pytest.raises(ValueError, match="Invalid hostname"):
        build_edge_https_config(["not a host!"])


def test_static_patch_inserts_trust_under_websecure() -> None:
    patched, changed = ensure_trusted_forwarded_headers(_STATIC_CONFIG)

    assert changed
    assert "    forwardedHeaders:\n      trustedIPs:\n" in patched
    for cidr in TRUSTED_FORWARDED_IPS:
        assert f"- {cidr}" in patched
    # Inserted inside the websecure block, not into http3 or providers.
    lines = patched.splitlines()
    ws = lines.index("  websecure:")
    fh = lines.index("    forwardedHeaders:")
    assert ws < fh < lines.index("    http3:")
    assert lines.count("    forwardedHeaders:") == 1


def test_static_patch_is_idempotent_and_replaces_foreign_blocks() -> None:
    patched, _ = ensure_trusted_forwarded_headers(_STATIC_CONFIG)
    again, changed = ensure_trusted_forwarded_headers(patched)

    assert not changed
    assert again == patched

    # A pre-existing (e.g. experimental) block is rewritten to the desired one.
    foreign = patched.replace("192.168.0.0/16", "10.99.0.0/16")
    rewritten, changed = ensure_trusted_forwarded_headers(foreign)
    assert changed
    assert "10.99.0.0/16" not in rewritten
    assert "192.168.0.0/16" in rewritten


def test_static_patch_requires_websecure_block() -> None:
    with pytest.raises(ValueError, match="websecure"):
        ensure_trusted_forwarded_headers("entryPoints:\n  web:\n    address: :80\n")


def test_apply_static_trust_patches_file_and_restarts(tmp_path: Path) -> None:
    static = tmp_path / "traefik.yml"
    static.write_text(_STATIC_CONFIG, encoding="utf-8")
    restarts, restart = _recorder()

    notes = apply_static_trust(
        static_file=static,
        dry_run=False,
        restart=restart,
    )

    assert restarts == ["dokploy-traefik"]
    assert "forwardedHeaders:" in static.read_text(encoding="utf-8")
    assert any("trust tunnel-forwarded headers" in note for note in notes)

    # Second pass: already trusted, no restart churn.
    restarts.clear()
    notes = apply_static_trust(
        static_file=static,
        dry_run=False,
        restart=restart,
    )
    assert restarts == []
    assert any("already trusts forwarded headers" in note for note in notes)


def test_apply_static_trust_warns_on_restart_failure(tmp_path: Path) -> None:
    static = tmp_path / "traefik.yml"
    static.write_text(_STATIC_CONFIG, encoding="utf-8")

    notes = apply_static_trust(static_file=static, dry_run=False, restart=lambda _svc: False)

    assert "forwardedHeaders:" in static.read_text(encoding="utf-8")
    assert any(note.startswith("WARNING") for note in notes)


def test_apply_static_trust_dry_run_and_missing_file(tmp_path: Path) -> None:
    static = tmp_path / "traefik.yml"
    static.write_text(_STATIC_CONFIG, encoding="utf-8")

    notes = apply_static_trust(static_file=static, dry_run=True, restart=lambda _svc: True)
    assert _STATIC_CONFIG == static.read_text(encoding="utf-8")
    assert any("Would add forwardedHeaders.trustedIPs" in note for note in notes)

    notes = apply_static_trust(static_file=tmp_path / "gone.yml", dry_run=False)
    assert any(note.startswith("WARNING") for note in notes)


def test_apply_writes_both_halves(tmp_path: Path) -> None:
    directory = _traefik_dir(tmp_path)
    restarts, restart = _recorder()

    notes = apply_edge_https_config(
        ["dokploy.example.com"],
        traefik_dir=directory,
        dry_run=False,
        restart=restart,
    )

    assert restarts == ["dokploy-traefik"]
    assert "forwardedHeaders:" in (directory / "traefik.yml").read_text(encoding="utf-8")
    route_file = directory / "dynamic" / EDGE_HTTPS_FILENAME
    assert route_file.is_file()
    assert "wizard-edge-https-dokploy-example-com:" in route_file.read_text(encoding="utf-8")
    assert any("Patched" in note for note in notes)
    assert any("Wrote the Traefik HTTPS-edge redirect drop-in" in note for note in notes)


def test_apply_second_pass_is_noop(tmp_path: Path) -> None:
    directory = _traefik_dir(tmp_path)
    apply_edge_https_config(
        ["dokploy.example.com"],
        traefik_dir=directory,
        dry_run=False,
        restart=lambda _svc: True,
    )
    static_first = (directory / "traefik.yml").read_bytes()
    dropin_first = (directory / "dynamic" / EDGE_HTTPS_FILENAME).read_bytes()

    notes = apply_edge_https_config(
        ["dokploy.example.com"],
        traefik_dir=directory,
        dry_run=False,
        restart=lambda _svc: False,  # must not be called: nothing changes
    )

    assert (directory / "traefik.yml").read_bytes() == static_first
    assert (directory / "dynamic" / EDGE_HTTPS_FILENAME).read_bytes() == dropin_first
    assert any("already trusts forwarded headers" in note for note in notes)
    assert any("is up to date" in note for note in notes)

    # Adding a hostname rewrites the drop-in but leaves the static config alone.
    notes = apply_edge_https_config(
        ["dokploy.example.com", "filex.example.com"],
        traefik_dir=directory,
        dry_run=False,
        restart=lambda _svc: False,
    )
    assert any("Updated the Traefik HTTPS-edge redirect drop-in" in note for note in notes)
    assert "wizard-edge-https-filex-example-com:" in (
        directory / "dynamic" / EDGE_HTTPS_FILENAME
    ).read_text(encoding="utf-8")


def test_apply_dry_run_writes_nothing(tmp_path: Path) -> None:
    directory = _traefik_dir(tmp_path)

    notes = apply_edge_https_config(
        ["dokploy.example.com"],
        traefik_dir=directory,
        dry_run=True,
        restart=lambda _svc: True,
    )

    assert not (directory / "dynamic" / EDGE_HTTPS_FILENAME).exists()
    assert _STATIC_CONFIG == (directory / "traefik.yml").read_text(encoding="utf-8")
    assert any("Would add forwardedHeaders.trustedIPs" in note for note in notes)
    assert any(
        "Would write the Traefik HTTPS-edge redirect drop-in" in note for note in notes
    )


def test_apply_warns_once_without_a_traefik_install(tmp_path: Path) -> None:
    missing = tmp_path / "not-installed"

    notes = apply_edge_https_config(
        ["dokploy.example.com"],
        traefik_dir=missing,
        dry_run=False,
        restart=lambda _svc: True,
    )

    assert not missing.exists()
    assert len(notes) == 1
    assert notes[0].startswith("WARNING")
    assert "no Traefik install" in notes[0]
