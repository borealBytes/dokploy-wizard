# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import dokploy_wizard.dokploy.filex as filex_module
from dokploy_wizard.dokploy.filex import DokployFilexBackend, _render_compose_file
from dokploy_wizard.packs.filex import (
    FILEX_BUCKET,
    FILEX_DATA_RESOURCE_TYPE,
    FILEX_SERVICE_RESOURCE_TYPE,
    FilexBootstrapState,
    FilexError,
    FilexResourceRecord,
    build_filex_ledger,
    reconcile_filex,
)
from dokploy_wizard.packs.seaweedfs import internal_s3_hostname
from dokploy_wizard.state import OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state


def _raw(values: dict[str, str]) -> RawEnvInput:
    return RawEnvInput(format_version=1, values=values)


def _filex_desired_state() -> Any:
    return resolve_desired_state(
        _raw(
            {
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "PACKS": "seaweedfs,filex",
                "SEAWEEDFS_ACCESS_KEY": "ak",
                "SEAWEEDFS_SECRET_KEY": "sk",
            }
        )
    )


@dataclass
class FakeFilexBackend:
    existing_service: FilexResourceRecord | None = None
    existing_data: FilexResourceRecord | None = None
    health_ok: bool = True
    health_results: list[bool] | None = None
    ensure_calls: int = 0
    service_kwargs: dict[str, object] | None = None

    def get_service(self, resource_id: str) -> FilexResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> FilexResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(self, **kwargs: object) -> FilexResourceRecord:
        self.service_kwargs = dict(kwargs)
        self.existing_service = FilexResourceRecord(
            resource_id="filex-service-1",
            resource_name=str(kwargs["resource_name"]),
        )
        return self.existing_service

    def update_service(self, **kwargs: object) -> FilexResourceRecord:
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> FilexResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> FilexResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> FilexResourceRecord:
        self.existing_data = FilexResourceRecord(
            resource_id="filex-data-1", resource_name=resource_name
        )
        return self.existing_data

    def check_health(self, *, service: FilexResourceRecord, url: str) -> bool:
        del service, url
        if self.health_results is not None:
            if self.health_results:
                return self.health_results.pop(0)
            return self.health_ok
        return self.health_ok

    def ensure_application_ready(
        self, *, secret_key_secret_ref: str
    ) -> tuple[FilexBootstrapState, tuple[str, ...]]:
        self.ensure_calls += 1
        return (
            FilexBootstrapState(
                initialized=True,
                secret_key_secret_ref=secret_key_secret_ref,
                bucket_ready=True,
                versioning_enabled=True,
                login_verified=None,
            ),
            ("FileX bootstrap placeholder completed.",),
        )


def test_reconcile_filex_skips_when_pack_disabled() -> None:
    desired = resolve_desired_state(
        _raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com", "PACKS": "seaweedfs"})
    )
    phase = reconcile_filex(
        dry_run=False,
        desired_state=desired,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeFilexBackend(),
    )
    assert phase.result.outcome == "skipped"
    assert phase.result.enabled is False


def test_reconcile_filex_requires_seaweedfs() -> None:
    from dataclasses import replace

    # The resolver auto-selects the dependency; the reconciler guard covers a
    # desired state that was persisted without it.
    desired = replace(
        resolve_desired_state(
            _raw(
                {
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "ENABLE_FILEX": "true",
                }
            )
        ),
        enabled_packs=("filex",),
    )
    with pytest.raises(FilexError, match="requires the SeaweedFS pack"):
        reconcile_filex(
            dry_run=True,
            desired_state=desired,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeFilexBackend(),
        )


def test_reconcile_filex_dry_run_plans_create_with_storage_binding() -> None:
    desired = _filex_desired_state()
    phase = reconcile_filex(
        dry_run=True,
        desired_state=desired,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeFilexBackend(),
    )
    result = phase.result
    assert result.outcome == "plan_only"
    assert result.hostname == "filex.example.com"
    assert result.service is not None and result.service.action == "create"
    assert result.persistent_data is not None and result.persistent_data.action == "create"
    assert phase.service_resource_id is None and phase.data_resource_id is None
    storage = result.storage
    assert storage is not None
    assert storage.bucket == FILEX_BUCKET
    assert storage.prefix == "files"
    assert storage.region == "us-east-1"
    # FileX talks to SeaweedFS over the cluster network by service name; the
    # endpoint is not the de-exposed public hostname.
    assert storage.endpoint_url == "http://wizard-stack-seaweedfs:8333"
    assert storage.access_key_secret_ref == "wizard-stack-seaweedfs-access-key"
    assert result.health_state is not None
    assert result.health_state.url == "https://filex.example.com/healthz"
    assert any("versioning" in note for note in result.notes)


def test_reconcile_filex_applies_create_and_owns_resources() -> None:
    desired = _filex_desired_state()
    backend = FakeFilexBackend()
    phase = reconcile_filex(
        dry_run=False,
        desired_state=desired,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )
    assert phase.result.outcome == "applied"
    assert backend.ensure_calls == 1
    assert backend.service_kwargs is not None
    assert backend.service_kwargs["hostname"] == "filex.example.com"
    assert backend.service_kwargs["bucket"] == FILEX_BUCKET
    ledger = build_filex_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id=phase.service_resource_id,
        data_resource_id=phase.data_resource_id,
    )
    scopes = {resource.scope: resource.resource_type for resource in ledger.resources}
    assert scopes == {
        "stack:wizard-stack:filex:service": FILEX_SERVICE_RESOURCE_TYPE,
        "stack:wizard-stack:filex:data": FILEX_DATA_RESOURCE_TYPE,
    }


def test_reconcile_filex_owned_reuse_is_already_present() -> None:
    desired = _filex_desired_state()
    backend = FakeFilexBackend(
        existing_service=FilexResourceRecord(
            resource_id="dokploy-compose:cmp-1:service",
            resource_name="wizard-stack-filex",
        ),
        existing_data=FilexResourceRecord(
            resource_id="dokploy-compose:cmp-1:data",
            resource_name="wizard-stack-filex-data",
        ),
    )
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                resource_type=FILEX_SERVICE_RESOURCE_TYPE,
                resource_id="dokploy-compose:cmp-1:service",
                scope="stack:wizard-stack:filex:service",
            ),
            OwnedResource(
                resource_type=FILEX_DATA_RESOURCE_TYPE,
                resource_id="dokploy-compose:cmp-1:data",
                scope="stack:wizard-stack:filex:data",
            ),
        ),
    )
    phase = reconcile_filex(
        dry_run=False,
        desired_state=desired,
        ownership_ledger=ledger,
        backend=backend,
    )
    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None and phase.result.service.action == "update_owned"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.action == "reuse_owned"


def test_reconcile_filex_fails_when_health_never_passes() -> None:
    desired = _filex_desired_state()
    backend = FakeFilexBackend(health_results=[False, False])
    with pytest.raises(FilexError, match="health check failed"):
        reconcile_filex(
            dry_run=False,
            desired_state=desired,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
        )


def test_internal_s3_hostname_shape() -> None:
    assert internal_s3_hostname("wizard-stack") == "wizard-stack-s3.internal"


# -- Dokploy backend: compose rendering ---------------------------------------


def _render(**overrides: object) -> Any:
    kwargs: dict[str, object] = {
        "stack_name": "wizard-stack",
        "hostname": "filex.example.com",
        "admin_email": "admin@example.com",
        "admin_password": "hunter2 pass",
        "s3_endpoint_url": "http://wizard-stack-seaweedfs:8333",
        "bucket": FILEX_BUCKET,
        "s3_access_key": "ak",
        "s3_secret_key": "sk",
        "secret_key_secret_ref": "wizard-stack-filex-secret-key",
    }
    kwargs.update(overrides)
    return _render_compose_file(**kwargs)


def test_render_filex_compose_seeds_storage_versioning_and_route() -> None:
    rendered = _render()
    compose = rendered.compose_file
    assert (
        "image: ghcr.io/brf-tech/filex:v0.61.2"
        "@sha256:e8756486300542bcfb7f8cf1a323a583c2444815da09c8947389197d43b04c8d"
    ) in compose
    assert 'FILEX_PUBLIC_URL: "https://filex.example.com"' in compose
    assert 'FILEX_ADMIN_EMAIL: "admin@example.com"' in compose
    assert 'FILEX_ADMIN_PASSWORD: "${FILEX_ADMIN_PASSWORD:?FILEX_ADMIN_PASSWORD is required}"' in compose
    assert 'FILEX_SECRET_KEY: "${FILEX_SECRET_KEY:?FILEX_SECRET_KEY is required}"' in compose
    assert "FILEX_TRUSTED_PROXIES: auto" in compose
    assert "FILEX_DEFAULT_STORAGE_DRIVER: s3" in compose
    assert 'FILEX_DEFAULT_STORAGE_S3_BUCKET: "filex"' in compose
    assert 'FILEX_DEFAULT_STORAGE_S3_PREFIX: "files"' in compose
    assert 'FILEX_DEFAULT_STORAGE_S3_ENDPOINT: "http://wizard-stack-seaweedfs:8333"' in compose
    assert 'FILEX_DEFAULT_STORAGE_S3_REGION: "us-east-1"' in compose
    assert 'FILEX_DEFAULT_STORAGE_S3_PATH_STYLE: "true"' in compose
    assert "FILEX_VERSIONS_ON_OVERWRITE: '1'" in compose
    assert "FILEX_TRASH_RETENTION_DAYS: '30'" in compose
    # Traefik rides the tunnel edge on the websecure entrypoint and stamps the
    # forwarded headers FileX needs to build share links against the public URL.
    assert 'traefik.http.routers.wizard-stack-filex.rule: "Host(`filex.example.com`)"' in compose
    assert "wizard-stack-filex-forwarded-https" in compose
    assert 'X-Forwarded-Proto: "https"' in compose
    assert 'traefik.http.services.wizard-stack-filex.loadbalancer.server.port: "5212"' in compose
    assert "- wizard-stack-filex-data:/data" in compose
    assert "dokploy-network:" in compose


def test_render_filex_env_specs_carry_real_secrets_and_hash_anchor() -> None:
    rendered = _render()
    names = [spec.variable.name for spec in rendered.env_specs]
    assert names == [
        "FILEX_ADMIN_PASSWORD",
        "FILEX_SECRET_KEY",
        "FILEX_DEFAULT_STORAGE_S3_ACCESS_KEY",
        "FILEX_DEFAULT_STORAGE_S3_SECRET_KEY",
    ]
    values = {spec.variable.name: spec.variable.value for spec in rendered.env_specs}
    assert values["FILEX_ADMIN_PASSWORD"] == "hunter2 pass"
    assert values["FILEX_DEFAULT_STORAGE_S3_ACCESS_KEY"] == "ak"
    assert values["FILEX_DEFAULT_STORAGE_S3_SECRET_KEY"] == "sk"
    secret_key = values["FILEX_SECRET_KEY"]
    # Deterministic per stack+ref, 32+ bytes, hex.
    assert secret_key != "hunter2 pass"
    assert len(secret_key) == 64
    int(secret_key, 16)
    # Changing the operator password must move the artifact anchor, or the
    # compose-noop guard would skip a redeploy that rotates stored secrets.
    rotated = _render(admin_password="rotated")
    assert rotated.compose_file == rendered.compose_file
    assert {spec.variable.name: spec.variable.value for spec in rotated.env_specs}[
        "FILEX_ADMIN_PASSWORD"
    ] == "rotated"


def _filex_backend(tmp_path: Path, s3: object) -> DokployFilexBackend:
    return DokployFilexBackend(
        api_url="https://127.0.0.1:3000",
        api_key="key",
        stack_name="wizard-stack",
        hostname="filex.example.com",
        admin_email="admin@example.com",
        admin_password="hunter2",
        s3_endpoint_url="http://wizard-stack-seaweedfs:8333",
        s3_internal_host="wizard-stack-s3.internal",
        s3_access_key="ak",
        s3_secret_key="sk",
        state_dir=tmp_path,
        client=_ProjectsClient(),
        s3_client=s3,  # type: ignore[arg-type]
    )


class _ProjectsClient:
    def list_projects(self) -> tuple[Any, ...]:
        return ()

    def create_project(self, **kwargs: object) -> Any:
        raise AssertionError("unexpected create_project")

    def create_compose(self, **kwargs: object) -> Any:
        raise AssertionError("unexpected create_compose")

    def update_compose(self, **kwargs: object) -> Any:
        raise AssertionError("unexpected update_compose")

    def deploy_compose(self, **kwargs: object) -> Any:
        raise AssertionError("unexpected deploy_compose")


@dataclass
class ScriptedS3:
    """Answers HEAD/GET/PUT on /filex/ with a scripted sequence."""

    head_script: list[int] = field(default_factory=list)
    versioning_script: list[tuple[int, str]] = field(default_factory=list)
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def request(self, method: str, path: str, query: str = "", body: bytes = b"") -> tuple[int, str]:
        self.calls.append((method, path, query))
        assert path == "/filex/"
        if method == "HEAD":
            return (self.head_script.pop(0), "")
        if method == "GET" and query == "versioning=":
            return self.versioning_script.pop(0)
        if method == "PUT" and query == "":
            return (200, "")
        if method == "PUT" and query == "versioning=":
            assert b"<Status>Enabled</Status>" in body
            return (200, "")
        raise AssertionError((method, path, query))


def test_bucket_provisioning_creates_and_enables_versioning(tmp_path: Path) -> None:
    s3 = ScriptedS3(
        head_script=[404],
        versioning_script=[(200, ""), (200, "")],
    )
    backend = _filex_backend(tmp_path, s3)
    ready, versioning, notes = backend._ensure_bucket_and_versioning(attempts=2, delay_seconds=0)
    assert ready and versioning
    assert ("PUT", "/filex/", "") in s3.calls
    assert ("PUT", "/filex/", "versioning=") in s3.calls
    assert any("Created FileX storage bucket" in note for note in notes)
    assert any("versioning" in note for note in notes)


def test_bucket_provisioning_is_idempotent_when_already_versioned(tmp_path: Path) -> None:
    s3 = ScriptedS3(
        head_script=[200],
        versioning_script=[(200, '<?xml version="1.0"?><VersioningConfiguration>'
                                 '<Status>Enabled</Status></VersioningConfiguration>')],
    )
    backend = _filex_backend(tmp_path, s3)
    ready, versioning, notes = backend._ensure_bucket_and_versioning(attempts=2, delay_seconds=0)
    assert ready and versioning
    assert ("PUT", "/filex/", "versioning=") not in s3.calls
    assert notes == ()


def test_bucket_provisioning_retries_until_route_becomes_ready(tmp_path: Path) -> None:
    s3 = ScriptedS3(
        head_script=[503, 404],
        versioning_script=[(200, ""), (200, "")],
    )
    backend = _filex_backend(tmp_path, s3)
    ready, versioning, _notes = backend._ensure_bucket_and_versioning(
        attempts=3, delay_seconds=0
    )
    assert ready and versioning
    assert [call[0] for call in s3.calls[:2]] == ["HEAD", "HEAD"]


def test_bucket_provisioning_gives_up_with_clear_error(tmp_path: Path) -> None:
    s3 = ScriptedS3(head_script=[503, 503], versioning_script=[])
    backend = _filex_backend(tmp_path, s3)
    with pytest.raises(FilexError, match="did not become reachable"):
        backend._ensure_bucket_and_versioning(attempts=2, delay_seconds=0)


def test_ensure_application_ready_reports_login_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s3 = ScriptedS3(head_script=[200], versioning_script=[(200, "<Status>Enabled</Status>")])
    backend = _filex_backend(tmp_path, s3)
    monkeypatch.setattr(filex_module, "_wait_for_local_https_health", lambda *a, **k: True)

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    login_requests: list[Any] = []

    def fake_urlopen(req: Any, **kwargs: Any) -> _Resp:
        login_requests.append(req)
        return _Resp()

    monkeypatch.setattr(filex_module.urlrequest, "urlopen", fake_urlopen)
    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-filex-secret-key"
    )
    assert state.initialized is True
    assert state.bucket_ready is True
    assert state.versioning_enabled is True
    assert state.login_verified is True
    assert any("login verified" in note for note in notes)
    request = login_requests[0]
    assert request.full_url == "https://127.0.0.1/api/auth/login"
    assert request.get_header("Host") == "filex.example.com"


def test_ensure_application_ready_login_rate_limit_is_note_not_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s3 = ScriptedS3(head_script=[200], versioning_script=[(200, "<Status>Enabled</Status>")])
    backend = _filex_backend(tmp_path, s3)
    monkeypatch.setattr(filex_module, "_wait_for_local_https_health", lambda *a, **k: True)

    from urllib import error as urlerror

    def fake_urlopen(req: Any, **kwargs: Any) -> Any:
        raise urlerror.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(filex_module.urlrequest, "urlopen", fake_urlopen)
    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-filex-secret-key"
    )
    assert state.login_verified is False
    assert any("rate-limited" in note for note in notes)


def test_filex_backend_rejects_off_plan_plan_values(tmp_path: Path) -> None:
    backend = _filex_backend(tmp_path, ScriptedS3())
    with pytest.raises(FilexError, match="hostname no longer matches"):
        backend.create_service(
            resource_name="wizard-stack-filex",
            hostname="other.example.com",
            bucket=FILEX_BUCKET,
            data_resource_name="wizard-stack-filex-data",
        )
    with pytest.raises(FilexError, match="bucket no longer matches"):
        backend.create_service(
            resource_name="wizard-stack-filex",
            hostname="filex.example.com",
            bucket="someone-elses-bucket",
            data_resource_name="wizard-stack-filex-data",
        )
