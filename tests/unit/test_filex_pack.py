# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import dokploy_wizard.dokploy.filex as filex_module
from dokploy_wizard.dokploy.filex import DokployFilexBackend, _render_compose_file
from dokploy_wizard.packs.filex import (
    FILEX_BUCKET,
    FILEX_DATA_RESOURCE_TYPE,
    FILEX_OFFICE_VOLUME_RESOURCE_TYPE,
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
    existing_office: FilexResourceRecord | None = None
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

    def get_office_volume(self, resource_id: str) -> FilexResourceRecord | None:
        if self.existing_office is not None and self.existing_office.resource_id == resource_id:
            return self.existing_office
        return None

    def find_office_volume_by_name(self, resource_name: str) -> FilexResourceRecord | None:
        if (
            self.existing_office is not None
            and self.existing_office.resource_name == resource_name
        ):
            return self.existing_office
        return None

    def create_office_volume(self, resource_name: str) -> FilexResourceRecord:
        self.existing_office = FilexResourceRecord(
            resource_id="filex-office-1", resource_name=resource_name
        )
        return self.existing_office

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
    assert result.office_volume is not None and result.office_volume.action == "create"
    assert (
        result.office_volume is not None
        and result.office_volume.resource_name == "wizard-stack-filex-office-data"
    )
    assert phase.service_resource_id is None and phase.data_resource_id is None
    assert phase.office_volume_resource_id is None
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
        office_volume_resource_id=phase.office_volume_resource_id,
    )
    scopes = {resource.scope: resource.resource_type for resource in ledger.resources}
    assert scopes == {
        "stack:wizard-stack:filex:service": FILEX_SERVICE_RESOURCE_TYPE,
        "stack:wizard-stack:filex:data": FILEX_DATA_RESOURCE_TYPE,
        "stack:wizard-stack:filex:office-volume": FILEX_OFFICE_VOLUME_RESOURCE_TYPE,
    }
    assert phase.result.office_volume is not None
    assert phase.result.office_volume.resource_id == "filex-office-1"


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
        existing_office=FilexResourceRecord(
            resource_id="dokploy-compose:cmp-1:office-data",
            resource_name="wizard-stack-filex-office-data",
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
            OwnedResource(
                resource_type=FILEX_OFFICE_VOLUME_RESOURCE_TYPE,
                resource_id="dokploy-compose:cmp-1:office-data",
                scope="stack:wizard-stack:filex:office-volume",
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
    assert phase.result.office_volume is not None
    assert phase.result.office_volume.action == "reuse_owned"
    assert phase.office_volume_resource_id == "dokploy-compose:cmp-1:office-data"
    assert any(
        "ONLYOFFICE editing state persists in 'wizard-stack-filex-office-data'" in note
        for note in phase.result.notes
    )


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
    # The service rides the shared overlay so workspaces can reach the app and
    # its S3 door at http://wizard-stack-filex:5212 (the ~/shared mount door).
    assert "      - shared\n" in compose
    assert "  shared:\n    external: true\n    name: wizard-stack-shared\n" in compose


def test_render_filex_env_specs_carry_real_secrets_and_hash_anchor() -> None:
    rendered = _render()
    names = [spec.variable.name for spec in rendered.env_specs]
    assert names == [
        "FILEX_ADMIN_PASSWORD",
        "FILEX_SECRET_KEY",
        "FILEX_DEFAULT_STORAGE_S3_ACCESS_KEY",
        "FILEX_DEFAULT_STORAGE_S3_SECRET_KEY",
        "FILEX_ONLYOFFICE_JWT",
    ]
    values = {spec.variable.name: spec.variable.value for spec in rendered.env_specs}
    assert values["FILEX_ADMIN_PASSWORD"] == "hunter2 pass"
    assert values["FILEX_DEFAULT_STORAGE_S3_ACCESS_KEY"] == "ak"
    assert values["FILEX_DEFAULT_STORAGE_S3_SECRET_KEY"] == "sk"
    # ONE variable, BOTH containers: compose interpolation resolves the same
    # secret on the FileX side (editor config + callback signatures) and on
    # the DocumentServer side (its entrypoint writes it into local.json).
    office_spec = next(s for s in rendered.env_specs if s.variable.name == "FILEX_ONLYOFFICE_JWT")
    assert office_spec.target_services == (
        "wizard-stack-filex",
        "wizard-stack-filex-office",
    )
    office_jwt = values["FILEX_ONLYOFFICE_JWT"]
    assert office_jwt == filex_module._office_jwt_value("wizard-stack")
    assert len(office_jwt) == 64
    int(office_jwt, 16)
    # The rendered compose carries only the placeholder; the secret itself
    # never rides the compose text (it is Dokploy env, redacted at rest).
    assert office_jwt not in rendered.compose_file
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
    monkeypatch.setattr(filex_module, "_wait_for_container_up", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_office_container_id", lambda *_a, **_k: "office-c")
    monkeypatch.setattr(filex_module, "_pin_office_relative_url", lambda *_a, **_k: False)

    class _Resp:
        status = 200

        def read(self) -> bytes:
            # The login probe now consumes the session token (the provisioner
            # step reuses it instead of logging in twice), and every later call
            # in this test degrades to a note off the same empty-ish payload.
            return b'{"token": "sess-1"}'

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
    monkeypatch.setattr(filex_module, "_wait_for_container_up", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_office_container_id", lambda *_a, **_k: "office-c")
    monkeypatch.setattr(filex_module, "_pin_office_relative_url", lambda *_a, **_k: False)

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


class _ScriptedFxHttp:
    """Fake urlrequest.urlopen answering the loopback FileX API with a script."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        # (method, path) -> list of (status, json_bytes) responses, consumed in order.
        self.script: dict[tuple[str, str], list[tuple[int, bytes]]] = {}
        self.default: tuple[int, bytes] = (404, b'{"error":"unplanned"}')

    def queue(self, method: str, path: str, status: int, payload: object) -> None:
        self.script.setdefault((method, path), []).append((status, json.dumps(payload).encode()))

    def __call__(self, req: Any, **kwargs: Any) -> Any:
        self.requests.append(req)
        method = req.get_method()
        path = req.full_url.replace("https://127.0.0.1", "")
        entries = self.script.get((method, path))
        status, body = entries.pop(0) if entries else self.default

        class _Resp:
            def __init__(self, status: int, body: bytes) -> None:
                self.status = status
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "_ScriptedFxHttp._Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        if 200 <= status < 300:
            return _Resp(status, body)
        from urllib import error as urlerror

        raise urlerror.HTTPError(req.full_url, status, "scripted", {}, _io_bytes(body))


def _io_bytes(body: bytes) -> Any:
    import io

    return io.BytesIO(body)


def _provisioner_backend(tmp_path: Path) -> DokployFilexBackend:
    s3 = ScriptedS3(head_script=[200], versioning_script=[(200, "<Status>Enabled</Status>")])
    return _filex_backend(tmp_path, s3)


def test_provisioner_bootstraps_lane_token_and_retires_legacy_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh generation: the lane token rides the MAIN user, the bot dies with it.

    The pack used to own a service account; this pass replaces it with an
    admin-scope token on the deployer's own row (measured: an admin-scoped
    token owned by the main user answers every /api/admin route), and the
    bot's deletion happens in the same pass that proves the replacement.
    """
    from dokploy_wizard.state.store import load_filex_provisioner

    backend = _provisioner_backend(tmp_path)
    monkeypatch.setattr(filex_module, "_wait_for_local_https_health", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_wait_for_container_up", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_office_container_id", lambda *_a, **_k: "office-c")
    monkeypatch.setattr(filex_module, "_pin_office_relative_url", lambda *_a, **_k: False)
    http = _ScriptedFxHttp()
    http.queue("POST", "/api/auth/login", 200, {"token": "admin-jwt"})
    http.queue(
        "GET",
        "/api/admin/users",
        200,
        [
            {"id": 1, "username": "admin", "email": "Admin@Example.com"},
            {"id": 7, "username": "wizard-provisioner", "email": "wp@filex.example.com"},
        ],
    )
    http.queue(
        "GET",
        "/api/admin/ai-tokens",
        200,
        {"tokens": [{"id": 9, "label": "wizard-provisioner", "user_id": 7}]},
    )
    http.queue("DELETE", "/api/admin/ai-tokens/9", 200, {"ok": True})
    http.queue("POST", "/api/admin/ai-tokens", 201, {"token": "lane-token-1", "row": {"id": 11}})
    http.queue("DELETE", "/api/admin/users/7", 200, {"ok": True})
    monkeypatch.setattr(filex_module.urlrequest, "urlopen", http)

    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-filex-secret-key"
    )
    assert state.login_verified is True
    assert any("lane token 'workspace-provisioner'" in note for note in notes)
    assert any(
        "Deleted the legacy FileX service account 'wizard-provisioner'" in note
        for note in notes
    )

    stored = load_filex_provisioner(tmp_path)
    assert stored is not None
    # The document now describes the deployer's own row (adopted by email,
    # case-insensitively — the row above proves it).
    assert stored.username == "admin"
    assert stored.user_id == 1
    assert stored.token_id == 11
    assert stored.token == "lane-token-1"
    # The token is asked for with the admin scope: /api/admin delegation is
    # session-or-admin-scope only, and workspace scripts ride it to provision.
    mint = next(
        req for req in http.requests
        if req.full_url.endswith("/api/admin/ai-tokens") and req.get_method() == "POST"
    )
    body = json.loads(mint.data.decode())
    assert body["label"] == "workspace-provisioner"
    assert body["scopes"] == "read,write,delete,admin"
    assert body["kind"] == "user"
    assert body["user_id"] == 1
    assert (tmp_path / "filex-provisioner.json").stat().st_mode & 0o777 == 0o600


def test_provisioner_keeps_alive_lane_token_and_migrates_legacy_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dokploy_wizard.state.models import STATE_FORMAT_VERSION, FilexProvisionerState
    from dokploy_wizard.state.store import load_filex_provisioner, write_filex_provisioner

    # Pass 1: a doc from the previous generation (username = the bot) forces
    # the migration mint even though its token still authenticates.
    write_filex_provisioner(
        tmp_path,
        FilexProvisionerState(
            format_version=STATE_FORMAT_VERSION,
            username="wizard-provisioner",
            email="wizard-provisioner@filex.example.com",
            password="pw",
            user_id=7,
            token="old-token",
            token_id=9,
        ),
    )
    backend = _provisioner_backend(tmp_path)
    monkeypatch.setattr(filex_module, "_wait_for_local_https_health", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_wait_for_container_up", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_office_container_id", lambda *_a, **_k: "office-c")
    monkeypatch.setattr(filex_module, "_pin_office_relative_url", lambda *_a, **_k: False)
    http = _ScriptedFxHttp()
    http.queue("POST", "/api/auth/login", 200, {"token": "admin-jwt"})
    http.queue(
        "GET",
        "/api/admin/users",
        200,
        [
            {"id": 1, "username": "admin", "email": "admin@example.com"},
            {"id": 7, "username": "wizard-provisioner", "email": "wp@filex.example.com"},
        ],
    )
    http.queue(
        "GET",
        "/api/admin/ai-tokens",
        200,
        {"tokens": [{"id": 9, "label": "wizard-provisioner", "user_id": 7}]},
    )
    http.queue("DELETE", "/api/admin/ai-tokens/9", 200, {"ok": True})
    http.queue("POST", "/api/admin/ai-tokens", 201, {"token": "lane-token-1", "row": {"id": 11}})
    http.queue("DELETE", "/api/admin/users/7", 200, {"ok": True})
    monkeypatch.setattr(filex_module.urlrequest, "urlopen", http)
    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-filex-secret-key"
    )
    assert state.login_verified is True
    migrated = load_filex_provisioner(tmp_path)
    assert migrated.token == "lane-token-1"
    assert migrated.username == "admin"
    assert any(
        "Deleted the legacy FileX service account" in note for note in notes
    )

    # Pass 2: the new doc's token still authenticates and the bot is gone →
    # short-circuit, no re-mint.
    http2 = _ScriptedFxHttp()
    http2.queue("POST", "/api/auth/login", 200, {"token": "admin-jwt"})
    http2.queue(
        "GET", "/api/admin/users", 200,
        [{"id": 1, "username": "admin", "email": "admin@example.com"}],
    )
    http2.queue(
        "GET", "/api/admin/ai-tokens", 200,
        {"tokens": [{"id": 11, "label": "workspace-provisioner", "user_id": 1}]},
    )
    http2.queue("GET", "/api/auth/s3-keys", 200, {"keys": []})
    monkeypatch.setattr(filex_module.urlrequest, "urlopen", http2)
    backend2 = _provisioner_backend(tmp_path)
    state2, notes2 = backend2.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-filex-secret-key"
    )
    assert state2.login_verified is True
    assert load_filex_provisioner(tmp_path).token == "lane-token-1"
    assert not any(
        req.get_method() == "POST" and req.full_url.endswith("/api/admin/ai-tokens")
        for req in http2.requests
    )
    assert not any("lane token" in note for note in notes2)

    # Pass 3: the row was deleted server-side → label-match rotation.
    http3 = _ScriptedFxHttp()
    http3.queue("POST", "/api/auth/login", 200, {"token": "admin-jwt"})
    http3.queue(
        "GET", "/api/admin/users", 200,
        [{"id": 1, "username": "admin", "email": "admin@example.com"}],
    )
    http3.queue("GET", "/api/admin/ai-tokens", 200, {"tokens": []})
    http3.queue("POST", "/api/admin/ai-tokens", 200, {"token": "lane-token-2", "row": {"id": 14}})
    monkeypatch.setattr(filex_module.urlrequest, "urlopen", http3)
    backend3 = _provisioner_backend(tmp_path)
    state3, notes3 = backend3.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-filex-secret-key"
    )
    assert state3.login_verified is True
    rotated = load_filex_provisioner(tmp_path)
    assert rotated.token == "lane-token-2"
    assert rotated.token_id == 14
    assert any("lane token 'workspace-provisioner'" in note for note in notes3)


def test_provisioner_degrades_to_note_when_admin_session_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _provisioner_backend(tmp_path)
    monkeypatch.setattr(filex_module, "_wait_for_local_https_health", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_wait_for_container_up", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_office_container_id", lambda *_a, **_k: "office-c")
    monkeypatch.setattr(filex_module, "_pin_office_relative_url", lambda *_a, **_k: False)
    from urllib import error as urlerror

    def fake_urlopen(req: Any, **kwargs: Any) -> Any:
        raise urlerror.HTTPError(req.full_url, 429, "Too Many Requests", {}, _io_bytes(b"{}"))

    monkeypatch.setattr(filex_module.urlrequest, "urlopen", fake_urlopen)
    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-filex-secret-key"
    )
    assert state.login_verified is False
    assert any("provisioner step skipped" in note for note in notes)


def test_wait_for_container_up_gates_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cold-install race (true-zero proof, 2026-11): the first image pull can
    # outlive the health budget entirely, so the container loop is the long
    # wait and must tolerate ups that arrive late, not early.
    states = iter([False, False, True])
    monkeypatch.setattr(filex_module, "_docker_container_is_up", lambda *a, **k: next(states))
    monkeypatch.setattr(filex_module.time, "sleep", lambda *_a: None)
    assert filex_module._wait_for_container_up("stack-filex", attempts=5, delay_seconds=0)


def test_wait_for_container_up_gives_up_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filex_module, "_docker_container_is_up", lambda *a, **k: False)
    monkeypatch.setattr(filex_module.time, "sleep", lambda *_a: None)
    assert not filex_module._wait_for_container_up("stack-filex", attempts=3, delay_seconds=0)


# -- ONLYOFFICE editing sidecar -----------------------------------------------


def test_render_filex_compose_includes_the_office_sidecar() -> None:
    rendered = _render()
    compose = rendered.compose_file
    assert (
        "image: onlyoffice/documentserver:9.4.0.1"
        "@sha256:3ab6ebc7c605e5a32b7ae3ff19daed4925090245acc8100ce2230bd766c88212"
    ) in compose
    assert 'JWT_ENABLED: "true"' in compose
    assert (
        'JWT_SECRET: "${FILEX_ONLYOFFICE_JWT:?FILEX_ONLYOFFICE_JWT is required}"' in compose
    )
    # Callbacks ride the compose-internal overlay (private IPs) — the DS must
    # be told to trust them or every save round-trips fail with a SSRF guard.
    assert 'ALLOW_PRIVATE_IP_ADDRESS: "true"' in compose
    assert 'WELCOME_PAGE_ENABLED: "false"' in compose
    assert 'GENERATE_FONTS: "false"' in compose
    # One hostname, one certificate: the editor frame rides a PathPrefix
    # router on the FileX host, stripped before the container and stamped
    # into X-Forwarded-Prefix so the app can build absolute editor URLs.
    assert "wizard-stack-filex-office:" in compose
    assert (
        'traefik.http.routers.wizard-stack-filex-office.rule: '
        '"Host(`filex.example.com`) && PathPrefix(`/filex-frame`)"' in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-filex-office-strip.stripprefix.prefixes: '
        '"/filex-frame"' in compose
    )
    assert 'X-Forwarded-Prefix: "/filex-frame"' in compose
    assert 'traefik.http.services.wizard-stack-filex-office.loadbalancer.server.port: "80"' in compose
    # The editing contract: public frame origin, internal callback door,
    # frame path — three addresses and the secret, all placeholder-ized.
    assert 'FILEX_ONLYOFFICE_URL: "https://filex.example.com/filex-frame"' in compose
    assert 'FILEX_ONLYOFFICE_FRAME_ORIGIN: "https://filex.example.com"' in compose
    assert 'FILEX_ONLYOFFICE_CALLBACK_URL: "http://wizard-stack-filex:5212"' in compose
    assert "FILEX_ONLYOFFICE_JWT" in compose
    # State mounts: exactly the community-edition trio; the data volume is
    # ledger-tracked, log and lib are plain caches riding compose teardown.
    assert "- wizard-stack-filex-office-data:/var/www/onlyoffice/Data" in compose
    assert "- wizard-stack-filex-office-log:/var/log/onlyoffice" in compose
    assert "- wizard-stack-filex-office-lib:/var/lib/onlyoffice" in compose
    assert "  wizard-stack-filex-office-data:\n" in compose
    assert "  wizard-stack-filex-office-log:\n" in compose
    assert "  wizard-stack-filex-office-lib:\n" in compose


def test_office_jwt_state_doc_round_trips_at_0600(tmp_path: Path) -> None:
    from dokploy_wizard.state.store import load_filex_office_jwt, write_filex_office_jwt

    secret = filex_module._office_jwt_value("wizard-stack")
    # Deterministic per stack: losing the state dir never orphans running
    # editor sessions from their key (same contract as FILEX_SECRET_KEY).
    assert secret == filex_module._office_jwt_value("wizard-stack")
    assert secret != filex_module._office_jwt_value("other-stack")
    document = write_filex_office_jwt(tmp_path, jwt_secret=secret)
    assert document["jwt_secret"] == secret
    assert load_filex_office_jwt(tmp_path) == document
    assert (tmp_path / "filex-office-jwt.json").stat().st_mode & 0o777 == 0o600


def test_office_sidecar_gate_records_jwt_and_pins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s3 = ScriptedS3()
    backend = _filex_backend(tmp_path, s3)
    monkeypatch.setattr(filex_module, "_wait_for_container_up", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_wait_for_local_https_health", lambda *a, **k: True)
    monkeypatch.setattr(filex_module, "_office_container_id", lambda *_a: "office-c")
    pinned: list[str] = []
    monkeypatch.setattr(
        filex_module,
        "_pin_office_relative_url",
        lambda container: pinned.append(container) or True,
    )
    notes = backend._ensure_office_sidecar()
    assert pinned == ["office-c"]
    assert any("Pinned the ONLYOFFICE subpath" in note for note in notes)
    assert any("/filex-frame/healthcheck" in note for note in notes)
    from dokploy_wizard.state.store import load_filex_office_jwt

    stored = load_filex_office_jwt(tmp_path)
    assert stored is not None
    assert stored["jwt_secret"] == filex_module._office_jwt_value("wizard-stack")
    assert (tmp_path / "filex-office-jwt.json").stat().st_mode & 0o777 == 0o600


def test_office_sidecar_gate_honest_about_a_missing_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = _filex_backend(tmp_path, ScriptedS3())
    monkeypatch.setattr(filex_module, "_wait_for_container_up", lambda *a, **k: False)
    with pytest.raises(FilexError, match="did not come up"):
        backend._ensure_office_sidecar()


def test_office_relative_url_pin_never_touches_a_pinned_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> Any:
        calls.append(list(command))
        joined = " ".join(command)
        if "supervisorctl" in joined:
            return subprocess.CompletedProcess(command, 0, "", "")
        script = command[command.index("-c") + 1]
        if "sys.argv[2]" in script:  # merge variant
            return subprocess.CompletedProcess(command, 0, "unchanged\n", "")
        return subprocess.CompletedProcess(command, 0, "/filex-frame\n", "")  # read

    monkeypatch.setattr(filex_module.subprocess, "run", fake_run)
    assert filex_module._pin_office_relative_url("office-c") is False
    assert len(calls) == 1  # read only: no merge, no restart — editors stay up


def test_office_relative_url_pin_merges_and_restarts_only_on_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> Any:
        calls.append(list(command))
        joined = " ".join(command)
        if "supervisorctl" in joined:
            return subprocess.CompletedProcess(command, 0, "ds:docservice: started\n", "")
        script = command[command.index("-c") + 1]
        if "sys.argv[2]" in script:
            return subprocess.CompletedProcess(command, 0, "changed\n", "")
        return subprocess.CompletedProcess(command, 0, "\n", "")  # unset

    monkeypatch.setattr(filex_module.subprocess, "run", fake_run)
    assert filex_module._pin_office_relative_url("office-c") is True
    assert len(calls) == 3  # read, merge, restart
    assert calls[1][:4] == ["docker", "exec", "office-c", "python3"]
    assert "ds:docservice" in calls[2] and "ds:converter" in calls[2]


def test_office_relative_url_pin_snippets_are_valid_python() -> None:
    import ast

    # The two docker-exec programs must parse as standalone modules; they
    # broke once before (a 'for' chained after a semicolon), so pin the check.
    ast.parse(filex_module._OFFICE_RELATIVE_URL_READ)
    ast.parse(filex_module._OFFICE_RELATIVE_URL_MERGE)


def test_office_volume_ledger_name_matches_the_compose_mount() -> None:
    from dokploy_wizard.packs.filex import filex_office_volume_name

    assert filex_office_volume_name("wizard-stack") == "wizard-stack-filex-office-data"
    assert filex_office_volume_name("wizard-stack") == filex_module._office_volume_name(
        "wizard-stack", "data"
    )


def test_build_filex_ledger_replaces_the_office_row_and_omits_it_when_none() -> None:
    first = build_filex_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="s-1",
        data_resource_id="d-1",
        office_volume_resource_id="o-1",
    )
    second = build_filex_ledger(
        existing_ledger=first,
        stack_name="wizard-stack",
        service_resource_id="s-1",
        data_resource_id="d-1",
        office_volume_resource_id="o-2",
    )
    office_rows = [
        resource
        for resource in second.resources
        if resource.resource_type == FILEX_OFFICE_VOLUME_RESOURCE_TYPE
    ]
    assert [(r.resource_id, r.scope) for r in office_rows] == [
        ("o-2", "stack:wizard-stack:filex:office-volume")
    ]
    # A build that carries no office id drops the row (the pack's own scopes
    # are owned wholesale by the builder — no stale half-state survives).
    third = build_filex_ledger(
        existing_ledger=first,
        stack_name="wizard-stack",
        service_resource_id="s-1",
        data_resource_id="d-1",
    )
    assert {resource.resource_type for resource in third.resources} == {
        FILEX_SERVICE_RESOURCE_TYPE,
        FILEX_DATA_RESOURCE_TYPE,
    }
