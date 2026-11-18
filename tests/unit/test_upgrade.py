# mypy: ignore-errors
"""Group 3: upgrade drift reporting, snapshotting, and gated LiteLLM pin apply."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from dokploy_wizard.state import (
    APPLIED_STATE_FILE,
    STATE_FORMAT_VERSION,
    AppliedStateCheckpoint,
    ComposeArtifactHashState,
    LiteLLMImagePin,
    RawEnvInput,
    StateValidationError,
    load_litellm_image_pin,
    load_state_dir,
    persist_install_scaffold,
    resolve_desired_state,
    write_applied_checkpoint,
    write_litellm_image_pin,
)
from dokploy_wizard.upgrade import (
    UpgradeError,
    apply_litellm_image_pin_to_env,
    apply_litellm_pin,
    build_upgrade_drift_report,
    capture_upgrade_snapshot,
    clear_compose_artifact_hash,
    confirm_upgrade_apply,
    litellm_effective_pin,
    parse_image_ref,
    resolve_litellm_bump_plan,
    stable_tag_sort_key,
)

FAKE_SHA_A = "sha256:" + "a" * 64
FAKE_SHA_B = "sha256:" + "b" * 64


def _upgrade_raw_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=STATE_FORMAT_VERSION,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "nextcloud,coder",
        },
    )


def _upgrade_desired_state():
    return resolve_desired_state(_upgrade_raw_env())


class FakeRegistry:
    def __init__(
        self,
        *,
        digests: dict[str, str | None] | None = None,
        tags: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.digests = digests or {}
        self.tags = tags or {}
        self.manifest_requests: list[str] = []
        self.tag_requests: list[str] = []

    def _key(self, ref) -> str:
        return f"{ref.registry}/{ref.repository}"

    def manifest_digest(self, ref) -> str | None:
        self.manifest_requests.append(f"{self._key(ref)}:{ref.tag}")
        return self.digests.get(f"{self._key(ref)}:{ref.tag}")

    def list_tags(self, ref) -> tuple[str, ...] | None:
        self.tag_requests.append(self._key(ref))
        return self.tags.get(self._key(ref))


class FakeProbe:
    def __init__(
        self,
        *,
        images: dict[str, str] | None = None,
        containers: dict[str, str] | None = None,
        exec_results: dict[tuple[str, tuple[str, ...]], tuple[int, str, str] | None] | None = None,
        container_names: tuple[str, ...] = (),
    ) -> None:
        self.images = images or {}
        self.containers = containers or {}
        self.exec_results = exec_results or {}
        self.container_names = container_names
        self.exec_calls: list[tuple[str, tuple[str, ...], dict[str, str]]] = []

    def service_image(self, service_name: str) -> str | None:
        return self.images.get(service_name)

    def container_id_for_service(self, service_name: str) -> str | None:
        return self.containers.get(service_name)

    def exec(self, container_id, argv, env={}):  # noqa: B006
        key = (container_id, tuple(argv))
        self.exec_calls.append((container_id, tuple(argv), dict(env)))
        if key in self.exec_results:
            return self.exec_results[key]
        return None

    def list_container_names(self) -> tuple[str, ...]:
        return self.container_names


# --- state document ----------------------------------------------------------


def test_litellm_image_pin_round_trips_through_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    assert load_litellm_image_pin(state_dir) is None

    pin = LiteLLMImagePin(
        format_version=STATE_FORMAT_VERSION,
        image="ghcr.io/berriai/litellm",
        tag="v1.84.0-stable",
        updated_at="2026-11-17T00:00:00+00:00",
    )
    write_litellm_image_pin(state_dir, pin)

    path = state_dir / "litellm-image-pin.json"
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert load_litellm_image_pin(state_dir) == pin


def test_litellm_image_pin_rejects_unknown_format_version() -> None:
    with pytest.raises(StateValidationError):
        LiteLLMImagePin.from_dict(
            {
                "format_version": 99,
                "image": "ghcr.io/berriai/litellm",
                "tag": "v1.84.0-stable",
                "updated_at": "2026-11-17T00:00:00+00:00",
            }
        )


def test_apply_pin_to_env_overrides_env_and_default(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    env = {"LITELLM_IMAGE_TAG": "v1.80.0-stable"}

    # Without a pin document the env (and its absence) passes through untouched.
    assert apply_litellm_image_pin_to_env(litellm_env=dict(env), state_dir=state_dir) == env
    assert litellm_effective_pin(litellm_env={}, state_dir=state_dir)["tag"] == "v1.83.14-stable"
    assert litellm_effective_pin(litellm_env={}, state_dir=state_dir)["source"] == "default"

    write_litellm_image_pin(
        state_dir,
        LiteLLMImagePin(
            format_version=STATE_FORMAT_VERSION,
            image="ghcr.io/berriai/litellm",
            tag="v1.84.0-stable",
            updated_at="2026-11-17T00:00:00+00:00",
        ),
    )

    effective = apply_litellm_image_pin_to_env(litellm_env=dict(env), state_dir=state_dir)
    assert effective["LITELLM_IMAGE_TAG"] == "v1.84.0-stable"
    assert litellm_effective_pin(litellm_env=env, state_dir=state_dir)["source"] == "state-pin"


# --- image reference parsing -------------------------------------------------


def test_parse_image_ref_handles_real_reference_shapes() -> None:
    parsed = parse_image_ref(f"ghcr.io/berriai/litellm:v1.83.14-stable@{FAKE_SHA_A}")
    assert parsed is not None
    assert (parsed.registry, parsed.repository, parsed.tag, parsed.digest) == (
        "ghcr.io",
        "berriai/litellm",
        "v1.83.14-stable",
        FAKE_SHA_A,
    )

    parsed = parse_image_ref("redis:7-alpine")
    assert parsed is not None
    assert (parsed.registry, parsed.repository, parsed.tag) == (
        "registry-1.docker.io",
        "library/redis",
        "7-alpine",
    )

    parsed = parse_image_ref("pgvector/pgvector:pg16")
    assert parsed is not None
    assert (parsed.registry, parsed.repository, parsed.tag) == (
        "registry-1.docker.io",
        "pgvector/pgvector",
        "pg16",
    )

    parsed = parse_image_ref("localhost:5000/team/app:1.2.3")
    assert parsed is not None
    assert (parsed.registry, parsed.repository, parsed.tag) == (
        "localhost:5000",
        "team/app",
        "1.2.3",
    )

    assert parse_image_ref("") is None
    bare = parse_image_ref("alpine")
    assert bare is not None
    assert (bare.registry, bare.repository, bare.tag) == (
        "registry-1.docker.io",
        "library/alpine",
        None,
    )


def test_stable_tag_sort_key_orders_and_filters() -> None:
    tags = ["v1.83.14-stable", "v1.84.0-stable", "v1.9.0-stable", "v2.0.0-stable"]
    ordered = sorted(tags, key=lambda tag: stable_tag_sort_key(tag))
    assert ordered == ["v1.9.0-stable", "v1.83.14-stable", "v1.84.0-stable", "v2.0.0-stable"]
    assert stable_tag_sort_key("latest") is None
    assert stable_tag_sort_key("main") is None
    assert stable_tag_sort_key("v1.84.0") is None


# --- drift report ------------------------------------------------------------


def test_build_upgrade_drift_report_classifies_digest_drift(tmp_path: Path) -> None:
    desired_state = _upgrade_desired_state()
    registry = FakeRegistry(
        digests={
            "ghcr.io/berriai/litellm:v1.83.14-stable": FAKE_SHA_B,
            "registry-1.docker.io/library/redis:7-alpine": FAKE_SHA_A,
            "registry-1.docker.io/library/nextcloud:apache": FAKE_SHA_B,
        },
        tags={"ghcr.io/berriai/litellm": ("v1.83.14-stable", "v1.84.0-stable", "latest")},
    )
    probe = FakeProbe(
        images={
            "wizard-stack-shared-litellm": f"ghcr.io/berriai/litellm:v1.83.14-stable@{FAKE_SHA_A}",
            "wizard-stack-shared-redis": f"redis:7-alpine@{FAKE_SHA_A}",
            "wizard-stack-nextcloud": f"nextcloud:apache@{FAKE_SHA_A}",
            "wizard-stack-coder": "ghcr.io/coder/coder:latest",
        },
    )

    report = build_upgrade_drift_report(
        desired_state=desired_state,
        litellm_env={},
        state_dir=tmp_path / "state",
        registry=registry,
        probe=probe,
    )

    by_service = {entry["service_name"]: entry for entry in report["images"]}
    assert by_service["wizard-stack-shared-redis"]["drift"] == "current"
    assert by_service["wizard-stack-nextcloud"]["drift"] == "digest_stale"
    # A floating tag running from a bare ref cannot be digest-compared.
    assert by_service["wizard-stack-coder"]["drift"] == "tag_only_ref"
    # The OnlyOffice service is a candidate (nextcloud pack implies it) but the
    # fake host reports nothing running.
    assert by_service["wizard-stack-onlyoffice"]["status"] == "missing"
    # Packs that are not enabled never appear as candidates.
    assert "wizard-stack-matrix" not in by_service

    litellm = report["litellm"]
    assert litellm["pin_source"] == "default"
    assert litellm["current_tag"] == "v1.83.14-stable"
    assert litellm["running_digest"] == FAKE_SHA_A
    assert litellm["current_digest"] == FAKE_SHA_B
    assert litellm["newest_stable_tag"] == "v1.84.0-stable"
    assert litellm["bump_available"] is True
    assert litellm["major_bump"] is False
    assert report["workspaces"]["available"] is False


def test_build_upgrade_drift_report_without_registry_tag_list(tmp_path: Path) -> None:
    desired_state = _upgrade_desired_state()
    registry = FakeRegistry(digests={}, tags={})
    probe = FakeProbe(
        images={
            "wizard-stack-shared-litellm": f"ghcr.io/berriai/litellm:v1.83.14-stable@{FAKE_SHA_A}"
        }
    )

    report = build_upgrade_drift_report(
        desired_state=desired_state,
        litellm_env={"LITELLM_IMAGE_TAG": "v1.83.14-stable"},
        state_dir=tmp_path / "state",
        registry=registry,
        probe=probe,
    )

    litellm = report["litellm"]
    assert litellm["pin_source"] == "env"
    assert litellm["newest_stable_tag"] is None
    assert litellm["bump_available"] is False


# --- snapshot ----------------------------------------------------------------


def test_capture_upgrade_snapshot_copies_state_and_dumps_postgres(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    desired_state = _upgrade_desired_state()
    persist_install_scaffold(state_dir, _upgrade_raw_env(), desired_state)
    (state_dir / "litellm-generated-keys.json").write_text("{}\n", encoding="utf-8")
    # A previous snapshot must not be copied into the new one.
    (state_dir / "snapshots" / "pre-upgrade-old").mkdir(parents=True)
    (state_dir / "snapshots" / "pre-upgrade-old" / "marker.txt").write_text("old", encoding="utf-8")

    probe = FakeProbe(
        containers={desired_state.shared_core.postgres.service_name: "pg-container"},
        exec_results={
            ("pg-container", ("pg_dumpall", "-U", "postgres")): (0, "COPY DONE\n", ""),
        },
    )

    snapshot = capture_upgrade_snapshot(
        state_dir=state_dir, desired_state=desired_state, probe=probe
    )

    assert snapshot["postgres"]["status"] == "captured"
    assert snapshot["state_copied"] is True
    snapshot_dir = Path(snapshot["path"])
    assert snapshot_dir.parent == state_dir / "snapshots"
    copied = snapshot_dir / "state" / APPLIED_STATE_FILE
    assert copied.exists()
    assert not (snapshot_dir / "state" / "snapshots").exists()
    dump_path = Path(snapshot["postgres"]["dump_path"])
    assert gzip.decompress(dump_path.read_bytes()) == b"COPY DONE\n"
    container_id, argv, env = probe.exec_calls[0]
    assert container_id == "pg-container"
    assert argv == ("pg_dumpall", "-U", "postgres")
    # Matches the deterministic shared-core root password derivation.
    from dokploy_wizard.dokploy.shared_core import _shared_core_generated_secret

    assert env == {
        "PGPASSWORD": _shared_core_generated_secret(
            f"{desired_state.shared_core.postgres.service_name}-root-password"
        )
    }
    assert snapshot["hermes"]["status"] == "skipped"


def test_capture_upgrade_snapshot_records_pg_dump_failure(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    desired_state = _upgrade_desired_state()
    persist_install_scaffold(state_dir, _upgrade_raw_env(), desired_state)
    probe = FakeProbe(
        containers={desired_state.shared_core.postgres.service_name: "pg-container"},
        exec_results={
            ("pg-container", ("pg_dumpall", "-U", "postgres")): (2, "", "pg_dumpall: error"),
        },
        container_names=("wizard-stack-hermes-gateway-1",),
    )
    probe.exec_results[
        ("wizard-stack-hermes-gateway-1", ("hermes", "backup"))
    ] = (127, "", "hermes: not found")

    snapshot = capture_upgrade_snapshot(
        state_dir=state_dir, desired_state=desired_state, probe=probe
    )

    assert snapshot["postgres"]["status"] == "failed"
    assert "pg_dumpall: error" in snapshot["postgres"]["detail"]
    # hermes backup is best-effort: attempted, failure noted, snapshot survives.
    assert snapshot["hermes"]["status"] == "attempted"
    assert "best-effort" in snapshot["hermes"]["detail"]


# --- confirmation and bump plan -----------------------------------------------


def test_confirm_upgrade_apply_gates_paths(tmp_path: Path) -> None:
    with pytest.raises(UpgradeError, match="--confirm-file"):
        confirm_upgrade_apply(non_interactive=True, confirm_file=None)

    weak = tmp_path / "weak.confirm"
    weak.write_text("yes\n", encoding="utf-8")
    with pytest.raises(UpgradeError, match="too weak"):
        confirm_upgrade_apply(non_interactive=True, confirm_file=weak)

    missing = tmp_path / "missing.confirm"
    missing.write_text("ship it\n", encoding="utf-8")
    with pytest.raises(UpgradeError, match="the word 'upgrade'"):
        confirm_upgrade_apply(non_interactive=True, confirm_file=missing)

    good = tmp_path / "good.confirm"
    good.write_text("# comment\nupgrade now\n", encoding="utf-8")
    assert confirm_upgrade_apply(non_interactive=True, confirm_file=good) == ("upgrade now",)

    prompted: list[str] = []

    def fake_prompt(text: str) -> str:
        prompted.append(text)
        return "upgrade"

    assert confirm_upgrade_apply(
        non_interactive=False, confirm_file=None, prompt=fake_prompt
    ) == ("upgrade",)
    assert prompted


def _bump_plan_state_dir(tmp_path: Path, pin_tag: str | None) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    if pin_tag is not None:
        write_litellm_image_pin(
            state_dir,
            LiteLLMImagePin(
                format_version=STATE_FORMAT_VERSION,
                image="ghcr.io/berriai/litellm",
                tag=pin_tag,
                updated_at="2026-11-17T00:00:00+00:00",
            ),
        )
    return state_dir


def test_resolve_litellm_bump_plan_validates_minor_bump(tmp_path: Path) -> None:
    desired_state = _upgrade_desired_state()
    registry = FakeRegistry(digests={"ghcr.io/berriai/litellm:v1.84.0-stable": FAKE_SHA_B})

    plan = resolve_litellm_bump_plan(
        desired_state=desired_state,
        litellm_env={},
        state_dir=_bump_plan_state_dir(tmp_path, None),
        registry=registry,
        target_tag="v1.84.0-stable",
        allow_major=False,
    )

    assert plan["previous_tag"] == "v1.83.14-stable"
    assert plan["previous_pin_source"] == "default"
    assert plan["target_tag"] == "v1.84.0-stable"
    assert plan["registry_digest"] == FAKE_SHA_B


def test_resolve_litellm_bump_plan_refuses_major_without_flag_and_unknown_tags(
    tmp_path: Path,
) -> None:
    desired_state = _upgrade_desired_state()
    registry = FakeRegistry(
        digests={
            "ghcr.io/berriai/litellm:v2.0.0-stable": FAKE_SHA_B,
            "ghcr.io/berriai/litellm:v1.84.0-stable": FAKE_SHA_A,
        }
    )
    state_dir = _bump_plan_state_dir(tmp_path, "v1.84.0-stable")

    with pytest.raises(UpgradeError, match="--allow-major"):
        resolve_litellm_bump_plan(
            desired_state=desired_state,
            litellm_env={},
            state_dir=state_dir,
            registry=registry,
            target_tag="v2.0.0-stable",
            allow_major=False,
        )

    with pytest.raises(UpgradeError, match="no manifest"):
        resolve_litellm_bump_plan(
            desired_state=desired_state,
            litellm_env={},
            state_dir=state_dir,
            registry=registry,
            target_tag="v1.99.9-stable",
            allow_major=False,
        )

    with pytest.raises(UpgradeError, match="not a stable release tag"):
        resolve_litellm_bump_plan(
            desired_state=desired_state,
            litellm_env={},
            state_dir=state_dir,
            registry=registry,
            target_tag="nightly",
            allow_major=True,
        )

    plan = resolve_litellm_bump_plan(
        desired_state=desired_state,
        litellm_env={},
        state_dir=state_dir,
        registry=registry,
        target_tag="v2.0.0-stable",
        allow_major=True,
    )
    assert plan["previous_tag"] == "v1.84.0-stable"
    # Rollback direction (major downgrade) is also gated.
    with pytest.raises(UpgradeError, match="major downgrade"):
        resolve_litellm_bump_plan(
            desired_state=desired_state,
            litellm_env={},
            state_dir=_bump_plan_state_dir(tmp_path / "down", "v2.0.0-stable"),
            registry=registry,
            target_tag="v1.84.0-stable",
            allow_major=False,
        )


# --- apply --------------------------------------------------------------------


def test_apply_litellm_pin_writes_document_and_clears_shared_core_hash(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    desired_state = _upgrade_desired_state()
    persist_install_scaffold(state_dir, _upgrade_raw_env(), desired_state)
    applied = load_state_dir(state_dir).applied_state
    assert applied is not None
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=applied.format_version,
            desired_state_fingerprint=applied.desired_state_fingerprint,
            completed_steps=applied.completed_steps,
            compose_artifact_hashes={
                desired_state.shared_core.network_name: ComposeArtifactHashState(
                    service_id=desired_state.shared_core.network_name,
                    rendered_compose_sha256="c" * 64,
                ),
                "wizard-stack-nextcloud": ComposeArtifactHashState(
                    service_id="wizard-stack-nextcloud",
                    rendered_compose_sha256="d" * 64,
                ),
            },
            lifecycle_checkpoint_contract_version=applied.lifecycle_checkpoint_contract_version,
        ),
    )

    pin = apply_litellm_pin(
        state_dir=state_dir,
        plan={
            "image": "ghcr.io/berriai/litellm",
            "target_tag": "v1.84.0-stable",
            "registry_digest": FAKE_SHA_B,
        },
        desired_state=desired_state,
    )

    assert pin.tag == "v1.84.0-stable"
    assert load_litellm_image_pin(state_dir) == pin
    updated = load_state_dir(state_dir).applied_state
    assert updated is not None
    # Only the shared-core compose hash is cleared; other packs stay noop-skippable.
    assert desired_state.shared_core.network_name not in updated.compose_artifact_hashes
    assert "wizard-stack-nextcloud" in updated.compose_artifact_hashes


def test_clear_compose_artifact_hash_is_idempotent_without_checkpoint(tmp_path: Path) -> None:
    assert clear_compose_artifact_hash(tmp_path / "missing", "anything") is False


# --- flow ----------------------------------------------------------------------


def _flow_state_dir(tmp_path: Path):
    state_dir = tmp_path / "state"
    desired_state = _upgrade_desired_state()
    persist_install_scaffold(state_dir, _upgrade_raw_env(), desired_state)
    return state_dir, desired_state


def test_run_upgrade_flow_check_mode_is_read_only(tmp_path: Path) -> None:
    from dokploy_wizard import cli

    state_dir, _ = _flow_state_dir(tmp_path)
    registry = FakeRegistry(
        digests={"ghcr.io/berriai/litellm:v1.83.14-stable": FAKE_SHA_A},
        tags={"ghcr.io/berriai/litellm": ("v1.83.14-stable",)},
    )
    modify_calls: list[dict[str, Any]] = []

    summary = cli.run_upgrade_flow(
        env_file=tmp_path / "stack.env",
        state_dir=state_dir,
        check=True,
        raw_env=_upgrade_raw_env(),
        registry=registry,
        probe=FakeProbe(),
        modify_runner=lambda **kwargs: modify_calls.append(kwargs) or {"outcome": "noop"},
    )

    assert summary["mode"] == "check"
    assert summary["drift"]["litellm"]["bump_available"] is False
    assert modify_calls == []
    assert not (state_dir / "snapshots").exists()
    assert load_litellm_image_pin(state_dir) is None


def test_run_upgrade_flow_apply_snapshots_pins_and_reuses_modify(tmp_path: Path) -> None:
    from dokploy_wizard import cli

    state_dir, desired_state = _flow_state_dir(tmp_path)
    confirm = tmp_path / "upgrade.confirm"
    confirm.write_text("upgrade the stack\n", encoding="utf-8")
    registry = FakeRegistry(
        digests={
            "ghcr.io/berriai/litellm:v1.84.0-stable": FAKE_SHA_B,
            "registry-1.docker.io/library/redis:7-alpine": FAKE_SHA_A,
        },
        tags={"ghcr.io/berriai/litellm": ("v1.84.0-stable",)},
    )
    probe = FakeProbe(
        images={
            "wizard-stack-shared-litellm": f"ghcr.io/berriai/litellm:v1.83.14-stable@{FAKE_SHA_A}"
        },
        containers={desired_state.shared_core.postgres.service_name: "pg-container"},
        exec_results={("pg-container", ("pg_dumpall", "-U", "postgres")): (0, "SQL\n", "")},
    )
    modify_calls: list[dict[str, Any]] = []

    summary = cli.run_upgrade_flow(
        env_file=tmp_path / "stack.env",
        state_dir=state_dir,
        litellm_tag="v1.84.0-stable",
        non_interactive=True,
        confirm_file=confirm,
        raw_env=_upgrade_raw_env(),
        registry=registry,
        probe=probe,
        modify_runner=lambda **kwargs: modify_calls.append(kwargs) or {"outcome": "applied"},
    )

    assert summary["mode"] == "applied"
    assert summary["plan"]["previous_tag"] == "v1.83.14-stable"
    assert summary["snapshot"]["postgres"]["status"] == "captured"
    assert summary["rollback"]["command"].endswith("--litellm v1.83.14-stable")
    assert summary["modify"] == {"outcome": "applied"}
    assert len(modify_calls) == 1
    assert modify_calls[0]["dry_run"] is False
    assert modify_calls[0]["raw_env"] == _upgrade_raw_env()

    # The pin is now the effective source; env could disagree and lose anyway.
    pin = load_litellm_image_pin(state_dir)
    assert pin is not None and pin.tag == "v1.84.0-stable"
    # And the next backend build must render it (verified through the same loader
    # the shared-core backend chokepoint uses).
    effective = apply_litellm_image_pin_to_env(
        litellm_env={"LITELLM_IMAGE_TAG": "v1.83.14-stable"}, state_dir=state_dir
    )
    assert effective["LITELLM_IMAGE_TAG"] == "v1.84.0-stable"


def test_run_upgrade_flow_dry_run_writes_nothing(tmp_path: Path) -> None:
    from dokploy_wizard import cli

    state_dir, _ = _flow_state_dir(tmp_path)
    registry = FakeRegistry(
        digests={"ghcr.io/berriai/litellm:v1.84.0-stable": FAKE_SHA_B},
        tags={"ghcr.io/berriai/litellm": ("v1.84.0-stable",)},
    )

    summary = cli.run_upgrade_flow(
        env_file=tmp_path / "stack.env",
        state_dir=state_dir,
        litellm_tag="v1.84.0-stable",
        dry_run=True,
        raw_env=_upgrade_raw_env(),
        registry=registry,
        probe=FakeProbe(),
    )

    assert summary["mode"] == "dry-run"
    assert summary["plan"]["target_tag"] == "v1.84.0-stable"
    assert load_litellm_image_pin(state_dir) is None
    assert not (state_dir / "snapshots").exists()


def test_run_upgrade_flow_requires_installed_state(tmp_path: Path) -> None:
    from dokploy_wizard import cli

    with pytest.raises(StateValidationError, match="install first"):
        cli.run_upgrade_flow(
            env_file=tmp_path / "stack.env",
            state_dir=tmp_path / "fresh-state",
            raw_env=_upgrade_raw_env(),
            registry=FakeRegistry(),
            probe=FakeProbe(),
        )


def test_run_upgrade_flow_non_interactive_requires_confirmation(tmp_path: Path) -> None:
    from dokploy_wizard import cli

    state_dir, _ = _flow_state_dir(tmp_path)
    registry = FakeRegistry(
        digests={"ghcr.io/berriai/litellm:v1.84.0-stable": FAKE_SHA_B},
    )

    with pytest.raises(UpgradeError, match="--confirm-file"):
        cli.run_upgrade_flow(
            env_file=tmp_path / "stack.env",
            state_dir=state_dir,
            litellm_tag="v1.84.0-stable",
            non_interactive=True,
            raw_env=_upgrade_raw_env(),
            registry=registry,
            probe=FakeProbe(),
        )

    # Confirmation failure must not have written a pin or snapshot.
    assert load_litellm_image_pin(state_dir) is None
    assert not (state_dir / "snapshots").exists()


# --- HttpImageRegistry (fake opener) ------------------------------------------


def test_http_image_registry_performs_bearer_challenge_and_digest(tmp_path: Path) -> None:
    from dokploy_wizard.upgrade import HttpImageRegistry
    from dokploy_wizard.upgrade import parse_image_ref as parse

    calls: list[tuple[str, dict[str, str]]] = []

    def opener(url: str, headers):
        calls.append((url, dict(headers)))
        if url.startswith("https://ghcr.io/token"):
            assert "scope=repository%3Aberriai%2Flitellm%3Apull" in url
            return 200, {}, json.dumps({"token": "anon-token"}).encode("utf-8")
        if "/manifests/" in url:
            if "Authorization" not in headers:
                # Anonymous challenge first, exactly like ghcr does.
                return (
                    401,
                    {"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'},
                    b"",
                )
            assert headers["Authorization"] == "Bearer anon-token"
            return 200, {"Docker-Content-Digest": FAKE_SHA_B}, b"{}"
        return 404, {}, b"{}"

    registry = HttpImageRegistry(opener=opener)
    ref = parse("ghcr.io/berriai/litellm:v1.84.0-stable")

    assert registry.manifest_digest(ref) == FAKE_SHA_B
    assert [call[0] for call in calls][0].endswith("/manifests/v1.84.0-stable")
    # The token is fetched once and reused for the retry.
    assert sum(1 for call in calls if call[0].startswith("https://ghcr.io/token")) == 1


def test_http_image_registry_lists_tags_with_link_pagination() -> None:
    from dokploy_wizard.upgrade import HttpImageRegistry
    from dokploy_wizard.upgrade import parse_image_ref as parse

    def opener(url: str, headers):
        del headers
        if "?" not in url:
            return 404, {}, b"{}"
        if "last=" not in url:
            return (
                200,
                {"Link": f'<{url}&last=v1.83.14-stable>; rel="next"'},
                json.dumps({"tags": ["v1.83.14-stable", "latest"]}).encode("utf-8"),
            )
        if "last=v1.83.14" in url:
            return 200, {}, json.dumps({"tags": ["v1.84.0-stable"]}).encode("utf-8")
        return 404, {}, b"{}"

    registry = HttpImageRegistry(opener=opener)
    ref = parse("ghcr.io/berriai/litellm")

    assert registry.list_tags(ref) == ("v1.83.14-stable", "latest", "v1.84.0-stable")


def test_http_image_registry_resolves_relative_link_pagination() -> None:
    """ghcr.io returns path-relative Link targets with its own n=0 page size;
    the client must fully qualify the URL and re-assert our page size."""
    from dokploy_wizard.upgrade import HttpImageRegistry
    from dokploy_wizard.upgrade import parse_image_ref as parse

    seen: list[str] = []

    def opener(url: str, headers):
        del headers
        seen.append(url)
        assert url.startswith("https://"), f"relative URL leaked to opener: {url}"
        assert url.endswith("n=1000"), f"page size not enforced: {url}"
        if url.endswith("tags/list?n=1000"):
            return (
                200,
                {"Link": '</v2/berriai/litellm/tags/list?last=v1.83.14-stable&n=0>; rel="next"'},
                json.dumps({"tags": ["v1.83.14-stable", "latest"]}).encode("utf-8"),
            )
        if "last=v1.83.14" in url:
            return 200, {}, json.dumps({"tags": ["v1.84.0-stable"]}).encode("utf-8")
        return 404, {}, b"{}"

    registry = HttpImageRegistry(opener=opener)
    ref = parse("ghcr.io/berriai/litellm")

    assert registry.list_tags(ref) == ("v1.83.14-stable", "latest", "v1.84.0-stable")
    assert seen == [
        "https://ghcr.io/v2/berriai/litellm/tags/list?n=1000",
        "https://ghcr.io/v2/berriai/litellm/tags/list?last=v1.83.14-stable&n=1000",
    ]


def test_docker_cli_probe_service_image_falls_back_to_compose_container(
    monkeypatch,
) -> None:
    """Compose runtimes have no swarm service; the probe must resolve the
    image reference (with a content digest) from the running container."""
    from dokploy_wizard import upgrade

    def fake_run_docker(command: list[str]) -> Any:
        class Result:
            returncode = 0
            stdout = ""

        result = Result()
        if command[:3] == ["docker", "service", "inspect"]:
            result.stdout = ""
        elif command[:2] == ["docker", "ps"]:
            result.stdout = "abc123\n"
        elif command[:2] == ["docker", "inspect"]:
            result.stdout = "ghcr.io/berriai/litellm:v1.83.14-stable img999\n"
        elif command[:3] == ["docker", "image", "inspect"]:
            result.stdout = (
                '["ghcr.io/berriai/litellm@sha256:aaa", "quay.io/other@sha256:bbb"]\n'
            )
        else:  # pragma: no cover - guards test intent
            result.returncode = 1
        return result

    monkeypatch.setattr(upgrade, "_run_docker", fake_run_docker)
    probe = upgrade.DockerCliProbe()

    assert (
        probe.service_image("showmeseewhy-com-shared-litellm")
        == "ghcr.io/berriai/litellm@sha256:aaa"
    )


def test_docker_cli_probe_exec_does_not_assume_a_workdir(monkeypatch) -> None:
    """--workdir /app made every exec fail with exit 127 on images without
    that directory (e.g. the shared postgres container)."""
    from dokploy_wizard import upgrade

    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    probe = upgrade.DockerCliProbe()

    assert probe.exec("abc123", ("pg_dumpall", "-U", "postgres"), env={"PGPASSWORD": "pw"}) == (
        0,
        "ok",
        "",
    )
    command = captured["command"]
    assert isinstance(command, list)
    assert "--workdir" not in command
    assert command[:2] == ["docker", "exec"]
    assert "-e" in command and "PGPASSWORD=pw" in command
    assert command[4:] == ["abc123", "pg_dumpall", "-U", "postgres"]


# --- CLI surface ---------------------------------------------------------------


def test_upgrade_parser_flags_round_trip() -> None:
    from dokploy_wizard.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "upgrade",
            "--env-file",
            "./.install.env",
            "--state-dir",
            "./state",
            "--check",
        ]
    )
    assert args.command == "upgrade"
    assert args.check is True
    assert args.litellm is None
    assert args.allow_major is False

    args = parser.parse_args(
        ["upgrade", "--env-file", "./.install.env", "--litellm", "v1.84.0-stable", "--allow-major"]
    )
    assert args.litellm == "v1.84.0-stable"
    assert args.allow_major is True
    assert args.non_interactive is False


def test_handle_upgrade_prints_json_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dokploy_wizard import cli

    state_dir, _ = _flow_state_dir(tmp_path)
    env_file = tmp_path / "stack.env"
    env_file.write_text(
        "STACK_NAME=wizard-stack\nROOT_DOMAIN=example.com\nPACKS=nextcloud,coder\n",
        encoding="utf-8",
    )
    monkey_registry = FakeRegistry(
        digests={"ghcr.io/berriai/litellm:v1.83.14-stable": FAKE_SHA_A},
        tags={"ghcr.io/berriai/litellm": ("v1.83.14-stable",)},
    )
    monkeypatched = {
        "HttpImageRegistry": lambda **kwargs: monkey_registry,
        "DockerCliProbe": FakeProbe,
    }
    original_http = cli.HttpImageRegistry
    original_probe = cli.DockerCliProbe
    cli.HttpImageRegistry = monkeypatched["HttpImageRegistry"]  # type: ignore[misc,assignment]
    cli.DockerCliProbe = monkeypatched["DockerCliProbe"]  # type: ignore[misc,assignment]
    try:
        exit_code = cli.main(
            [
                "upgrade",
                "--env-file",
                str(tmp_path / "stack.env"),
                "--state-dir",
                str(state_dir),
                "--check",
            ]
        )
    finally:
        cli.HttpImageRegistry = original_http  # type: ignore[misc,assignment]
        cli.DockerCliProbe = original_probe  # type: ignore[misc,assignment]

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "upgrade"
    assert payload["mode"] == "check"
