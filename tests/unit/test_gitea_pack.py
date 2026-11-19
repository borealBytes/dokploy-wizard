# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib import error as urlerror

import pytest

import dokploy_wizard.dokploy.gitea as gitea_module
from dokploy_wizard.dokploy.gitea import (
    DokployGiteaBackend,
    _CliResult,
    _render_compose_file,
)
from dokploy_wizard.packs.gitea import (
    GITEA_BUCKET,
    GITEA_DATA_RESOURCE_TYPE,
    GITEA_SERVICE_RESOURCE_TYPE,
    GiteaBootstrapState,
    GiteaError,
    GiteaResourceRecord,
    build_gitea_ledger,
    reconcile_gitea,
)
from dokploy_wizard.state import (
    GiteaProvisionerState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    load_gitea_provisioner,
    resolve_desired_state,
    write_gitea_provisioner,
)


def _raw(values: dict[str, str]) -> RawEnvInput:
    return RawEnvInput(format_version=1, values=values)


def _gitea_desired_state() -> Any:
    return resolve_desired_state(
        _raw(
            {
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "PACKS": "seaweedfs,gitea",
                "SEAWEEDFS_ACCESS_KEY": "ak",
                "SEAWEEDFS_SECRET_KEY": "sk",
            }
        )
    )


@dataclass
class FakeGiteaBackend:
    existing_service: GiteaResourceRecord | None = None
    existing_data: GiteaResourceRecord | None = None
    health_ok: bool = True
    health_results: list[bool] | None = None
    ensure_calls: int = 0
    service_kwargs: dict[str, object] | None = None

    def get_service(self, resource_id: str) -> GiteaResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> GiteaResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(self, **kwargs: object) -> GiteaResourceRecord:
        self.service_kwargs = dict(kwargs)
        self.existing_service = GiteaResourceRecord(
            resource_id="gitea-service-1",
            resource_name=str(kwargs["resource_name"]),
        )
        return self.existing_service

    def update_service(self, **kwargs: object) -> GiteaResourceRecord:
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> GiteaResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> GiteaResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> GiteaResourceRecord:
        self.existing_data = GiteaResourceRecord(
            resource_id="gitea-data-1", resource_name=resource_name
        )
        return self.existing_data

    def check_health(self, *, service: GiteaResourceRecord, url: str) -> bool:
        del service, url
        if self.health_results is not None:
            if self.health_results:
                return self.health_results.pop(0)
            return self.health_ok
        return self.health_ok

    def ensure_application_ready(
        self, *, secret_key_secret_ref: str
    ) -> tuple[GiteaBootstrapState, tuple[str, ...]]:
        self.ensure_calls += 1
        return (
            GiteaBootstrapState(
                initialized=True,
                secret_key_secret_ref=secret_key_secret_ref,
                bucket_ready=True,
                provisioner_ready=True,
            ),
            ("Gitea bootstrap placeholder completed.",),
        )


# -- reconciler ----------------------------------------------------------------


def test_reconcile_gitea_skips_when_pack_disabled() -> None:
    desired = resolve_desired_state(
        _raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com", "PACKS": "seaweedfs"})
    )
    phase = reconcile_gitea(
        dry_run=False,
        desired_state=desired,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeGiteaBackend(),
    )
    assert phase.result.outcome == "skipped"
    assert phase.result.enabled is False


def test_reconcile_gitea_requires_seaweedfs() -> None:
    # The resolver auto-selects the dependency; the reconciler guard covers a
    # desired state that was persisted without it.
    desired = replace(
        resolve_desired_state(
            _raw(
                {
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "ENABLE_GITEA": "true",
                }
            )
        ),
        enabled_packs=("gitea",),
    )
    with pytest.raises(GiteaError, match="requires the SeaweedFS pack"):
        reconcile_gitea(
            dry_run=True,
            desired_state=desired,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeGiteaBackend(),
        )


def test_reconcile_gitea_requires_postgres_allocation() -> None:
    desired = _gitea_desired_state()
    pruned = replace(
        desired,
        shared_core=replace(
            desired.shared_core,
            allocations=tuple(
                item for item in desired.shared_core.allocations if item.pack_name != "gitea"
            ),
        ),
    )
    with pytest.raises(GiteaError, match="postgres allocation is missing"):
        reconcile_gitea(
            dry_run=True,
            desired_state=pruned,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeGiteaBackend(),
        )


def test_reconcile_gitea_dry_run_plans_create_with_bindings() -> None:
    desired = _gitea_desired_state()
    phase = reconcile_gitea(
        dry_run=True,
        desired_state=desired,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeGiteaBackend(),
    )
    result = phase.result
    assert result.outcome == "plan_only"
    assert result.hostname == "gitea.example.com"
    assert result.service is not None and result.service.action == "create"
    assert result.persistent_data is not None and result.persistent_data.action == "create"
    assert phase.service_resource_id is None and phase.data_resource_id is None
    storage = result.storage
    assert storage is not None
    assert storage.bucket == GITEA_BUCKET
    assert storage.region == "us-east-1"
    # Gitea talks to SeaweedFS over the cluster network by service name; the
    # endpoint is the de-exposed internal host, never a public route.
    assert storage.endpoint_url == "http://wizard-stack-seaweedfs:8333"
    assert storage.access_key_secret_ref == "wizard-stack-seaweedfs-access-key"
    postgres = result.postgres
    assert postgres is not None
    # The planner normalizes hyphens: roles and databases are underscored.
    assert postgres.database_name == "wizard_stack_gitea"
    assert postgres.user_name == "wizard_stack_gitea"
    assert postgres.password_secret_ref == "wizard-stack-gitea-postgres-password"
    assert result.health_state is not None
    assert result.health_state.url == "https://gitea.example.com/api/healthz"
    assert any("shared-core postgres" in note for note in result.notes)


def test_reconcile_gitea_applies_create_and_owns_resources() -> None:
    desired = _gitea_desired_state()
    backend = FakeGiteaBackend()
    phase = reconcile_gitea(
        dry_run=False,
        desired_state=desired,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )
    assert phase.result.outcome == "applied"
    assert backend.ensure_calls == 1
    assert backend.service_kwargs is not None
    assert backend.service_kwargs["hostname"] == "gitea.example.com"
    assert backend.service_kwargs["postgres_service_name"] == "wizard-stack-shared-postgres"
    ledger = build_gitea_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id=phase.service_resource_id,
        data_resource_id=phase.data_resource_id,
    )
    scopes = {resource.scope: resource.resource_type for resource in ledger.resources}
    assert scopes == {
        "stack:wizard-stack:gitea:service": GITEA_SERVICE_RESOURCE_TYPE,
        "stack:wizard-stack:gitea:data": GITEA_DATA_RESOURCE_TYPE,
    }


def test_reconcile_gitea_owned_reuse_is_already_present() -> None:
    desired = _gitea_desired_state()
    backend = FakeGiteaBackend(
        existing_service=GiteaResourceRecord(
            resource_id="dokploy-compose:cmp-1:service",
            resource_name="wizard-stack-gitea",
        ),
        existing_data=GiteaResourceRecord(
            resource_id="dokploy-compose:cmp-1:data",
            resource_name="wizard-stack-gitea-data",
        ),
    )
    ledger = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(
                resource_type=GITEA_SERVICE_RESOURCE_TYPE,
                resource_id="dokploy-compose:cmp-1:service",
                scope="stack:wizard-stack:gitea:service",
            ),
            OwnedResource(
                resource_type=GITEA_DATA_RESOURCE_TYPE,
                resource_id="dokploy-compose:cmp-1:data",
                scope="stack:wizard-stack:gitea:data",
            ),
        ),
    )
    phase = reconcile_gitea(
        dry_run=False,
        desired_state=desired,
        ownership_ledger=ledger,
        backend=backend,
    )
    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None and phase.result.service.action == "update_owned"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.action == "reuse_owned"


def test_reconcile_gitea_fails_when_health_never_passes() -> None:
    desired = _gitea_desired_state()
    backend = FakeGiteaBackend(health_results=[False, False])
    with pytest.raises(GiteaError, match="health check failed"):
        reconcile_gitea(
            dry_run=False,
            desired_state=desired,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
        )


# -- Dokploy backend: compose rendering ---------------------------------------


def _render(**overrides: object) -> Any:
    kwargs: dict[str, object] = {
        "stack_name": "wizard-stack",
        "hostname": "gitea.example.com",
        "postgres_service_name": "wizard-stack-shared-postgres",
        "postgres": _gitea_desired_state().shared_core.allocations[0].postgres,
        "s3_endpoint_url": "http://wizard-stack-seaweedfs:8333",
        "bucket": GITEA_BUCKET,
        "s3_access_key": "ak",
        "s3_secret_key": "sk",
        "secret_key_secret_ref": "wizard-stack-gitea-secret-key",
    }
    kwargs.update(overrides)
    return _render_compose_file(**kwargs)


def test_render_gitea_compose_locks_install_routes_storage_and_keeps_ssh_internal() -> None:
    rendered = _render()
    compose = rendered.compose_file
    assert (
        "image: gitea/gitea:28.4.0-rootless"
        "@sha256:15ab7fadb44981a2b781f40c712cad07066197f395100ea1c6326e14e5bb073a"
    ) in compose
    # Rootless image: explicit unprivileged run user, both roots on one volume.
    assert "user: 1000:1000" in compose
    assert "- wizard-stack-gitea-data:/var/lib/gitea" in compose
    assert "- wizard-stack-gitea-data:/etc/gitea" in compose
    assert 'GITEA__database__DB_TYPE: postgres' in compose
    assert 'GITEA__database__HOST: "wizard-stack-shared-postgres:5432"' in compose
    assert 'GITEA__database__NAME: "wizard_stack_gitea"' in compose
    assert 'GITEA__database__USER: "wizard_stack_gitea"' in compose
    assert (
        'GITEA__database__PASSWD: "${GITEA_DB_PASSWORD:?GITEA_DB_PASSWORD is required}"' in compose
    )
    assert 'GITEA__server__ROOT_URL: "https://gitea.example.com/"' in compose
    assert 'GITEA__security__INSTALL_LOCK: \'true\'' in compose
    assert 'GITEA__service__DISABLE_REGISTRATION: \'true\'' in compose
    # Private by default (opt-in public stays possible): user visibility
    # defaults to private, new repos start private, and the explore page is
    # closed to anonymous visitors.
    assert 'GITEA__options__DEFAULT_USER_VISIBILITY: private' in compose
    assert 'GITEA__repository__DEFAULT_PRIVATE: \'true\'' in compose
    assert 'GITEA__explore__DISABLE_ANONYMOUS_ACCESS: \'true\'' in compose
    assert 'GITEA__server__START_SSH_SERVER: \'true\'' in compose
    assert 'GITEA__server__SSH_PORT: \'2222\'' in compose
    assert 'GITEA__server__LFS_START_SERVER: \'true\'' in compose
    assert 'GITEA__server__ENABLE_HEALTHCHECK: \'true\'' in compose
    # LFS + attachments ride the stack's SeaweedFS gateway via the modern
    # (1.20+) storage schema — the only one Gitea 28 reads; the pre-1.20
    # spellings silently keep local storage. The driver takes a bare
    # host:port, so the scheme is stripped from the internal endpoint.
    assert 'GITEA__lfs__STORAGE_TYPE: minio' in compose
    assert 'GITEA__attachment__STORAGE_TYPE: minio' in compose
    assert 'GITEA__lfs__MINIO_BUCKET: "gitea"' in compose
    assert 'GITEA__attachment__MINIO_BUCKET: "gitea"' in compose
    assert 'GITEA__storage.minio__STORAGE_TYPE: minio' in compose
    assert 'GITEA__storage.minio__MINIO_ENDPOINT: "wizard-stack-seaweedfs:8333"' in compose
    assert 'GITEA__storage.minio__MINIO_PATH_STYLE: \'true\'' in compose
    assert (
        'GITEA__storage.minio__MINIO_ACCESS_KEY_ID: '
        '"${GITEA_S3_ACCESS_KEY:?GITEA_S3_ACCESS_KEY is required}"' in compose
    )
    # Traefik rides the tunnel edge on the websecure entrypoint and stamps the
    # forwarded headers Gitea needs to build clone URLs against ROOT_URL.
    assert 'traefik.http.routers.wizard-stack-gitea.rule: "Host(`gitea.example.com`)"' in compose
    assert "wizard-stack-gitea-forwarded-https" in compose
    assert 'X-Forwarded-Proto: "https"' in compose
    assert 'traefik.http.services.wizard-stack-gitea.loadbalancer.server.port: "3000"' in compose
    assert "- '2222'" in compose
    assert "dokploy-network:" in compose
    # The service rides the shared overlay so Coder workspaces can clone over
    # the private network without a public git remote.
    assert "      - shared\n" in compose
    assert "  shared:\n    external: true\n    name: wizard-stack-shared\n" in compose
    assert "GITEA__mailer__ENABLED: 'false'" in compose


def test_render_gitea_compose_wires_shared_mail_relay_when_present() -> None:
    rendered = _render(
        smtp_host="wizard-stack-shared-postfix",
        smtp_port=25,
        smtp_from_address="notifications@example.com",
    )
    compose = rendered.compose_file
    assert "GITEA__mailer__ENABLED: 'true'" in compose
    # SMTP_ADDR is looked up as a bare hostname; the port rides SMTP_PORT.
    assert 'GITEA__mailer__SMTP_ADDR: "wizard-stack-shared-postfix"' in compose
    assert "GITEA__mailer__SMTP_PORT: '25'" in compose
    assert 'GITEA__mailer__FROM: "notifications@example.com"' in compose


def test_render_gitea_env_specs_carry_real_secrets_and_hash_anchor() -> None:
    rendered = _render()
    names = [spec.variable.name for spec in rendered.env_specs]
    assert names == [
        "GITEA_DB_PASSWORD",
        "GITEA_SECRET_KEY",
        "GITEA_S3_ACCESS_KEY",
        "GITEA_S3_SECRET_KEY",
    ]
    values = {spec.variable.name: spec.variable.value for spec in rendered.env_specs}
    assert values["GITEA_DB_PASSWORD"] == "change-me"
    assert values["GITEA_S3_ACCESS_KEY"] == "ak"
    assert values["GITEA_S3_SECRET_KEY"] == "sk"
    secret_key = values["GITEA_SECRET_KEY"]
    # Deterministic per stack+ref, 32+ bytes, hex.
    assert len(secret_key) == 64
    int(secret_key, 16)
    rotated = _render(s3_access_key="rotated")
    assert rotated.compose_file == rendered.compose_file
    assert {spec.variable.name: spec.variable.value for spec in rotated.env_specs}[
        "GITEA_S3_ACCESS_KEY"
    ] == "rotated"


# -- Dokploy backend: bucket provisioning ---------------------------------------


def _gitea_backend(
    tmp_path: Path, s3: object, client: object | None = None
) -> DokployGiteaBackend:
    return DokployGiteaBackend(
        api_url="https://127.0.0.1:3000",
        api_key="key",
        stack_name="wizard-stack",
        hostname="gitea.example.com",
        # The provisioner is the deployer: the account login derives from
        # this email's local part and its password is the stack password.
        admin_email="clayton@example.com",
        admin_password="StackPw!23",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=_gitea_desired_state().shared_core.allocations[0].postgres,
        s3_endpoint_url="http://wizard-stack-seaweedfs:8333",
        s3_internal_host="wizard-stack-s3.internal",
        s3_access_key="ak",
        s3_secret_key="sk",
        state_dir=tmp_path,
        client=client if client is not None else _ProjectsClient(),
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
    """Answers HEAD/PUT on /gitea/ with a scripted sequence (no versioning plane)."""

    head_script: list[int] = field(default_factory=list)
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def request(self, method: str, path: str, query: str = "", body: bytes = b"") -> tuple[int, str]:
        self.calls.append((method, path, query))
        assert path == "/gitea/"
        if method == "HEAD":
            return (self.head_script.pop(0), "")
        if method == "PUT" and query == "":
            return (200, "")
        raise AssertionError((method, path, query))


def test_bucket_provisioning_creates_bucket_without_versioning(tmp_path: Path) -> None:
    s3 = ScriptedS3(head_script=[404])
    backend = _gitea_backend(tmp_path, s3)
    ready, notes = backend._ensure_bucket(attempts=2, delay_seconds=0)
    assert ready
    assert ("PUT", "/gitea/", "") in s3.calls
    assert all(query == "" for _method, _path, query in s3.calls)
    assert any("Created Gitea storage bucket" in note for note in notes)


def test_bucket_provisioning_is_idempotent_when_already_present(tmp_path: Path) -> None:
    s3 = ScriptedS3(head_script=[200])
    backend = _gitea_backend(tmp_path, s3)
    ready, notes = backend._ensure_bucket(attempts=2, delay_seconds=0)
    assert ready
    assert notes == ()


def test_bucket_provisioning_retries_until_route_becomes_ready(tmp_path: Path) -> None:
    s3 = ScriptedS3(head_script=[503, 404])
    backend = _gitea_backend(tmp_path, s3)
    ready, _notes = backend._ensure_bucket(attempts=3, delay_seconds=0)
    assert ready
    assert [call[0] for call in s3.calls[:2]] == ["HEAD", "HEAD"]


def test_bucket_provisioning_gives_up_as_unready_not_crash(tmp_path: Path) -> None:
    s3 = ScriptedS3(head_script=[503, 503])
    backend = _gitea_backend(tmp_path, s3)
    ready, notes = backend._ensure_bucket(attempts=2, delay_seconds=0)
    assert ready is False
    assert any("could not be verified" in note for note in notes)


# -- Dokploy backend: provisioner account ---------------------------------------


class _ScriptedGiteaHttp:
    """Fake urlrequest.urlopen answering the loopback Gitea API with a script."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.script: dict[tuple[str, str], list[tuple[int, bytes]]] = {}
        self.default: tuple[int, bytes] = (404, b'{"errors":[]}')

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

            def __enter__(self) -> "_ScriptedGiteaHttp._Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        if 200 <= status < 300:
            return _Resp(status, body)
        raise urlerror.HTTPError(req.full_url, status, "scripted", {}, io.BytesIO(body))


@dataclass
class ScriptedCli:
    """Stands in for `docker exec … gitea` and records every invocation."""

    results: list[_CliResult]
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, service_name: str, args: list[str]) -> _CliResult:
        assert service_name == "wizard-stack-gitea"
        self.calls.append(list(args))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def _provisioner_args(cli: ScriptedCli) -> list[str]:
    return cli.calls[0]


def test_provisioner_seed_is_gated_on_the_health_wait_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Box-measured cold-start race: on a first install the in-container CLI
    ran while the just-deployed Gitea task was still starting, saw 'no
    running container', and the seed degraded to a WARNING (the coder phase's
    sync then 401ed against the stale doc the wiped database left behind).
    The health wait now runs FIRST: while it reports not-ready, the CLI must
    never run at all, and seeding only happens once it passes."""
    backend = _gitea_backend(tmp_path, ScriptedS3(head_script=[200]))
    order: list[str] = []

    def fake_health(*args: object, **kwargs: object) -> bool:
        order.append("health")
        return False

    def no_cli(service_name: str, args: list[str]) -> _CliResult:
        raise AssertionError("CLI must not run while Gitea is not serving yet")

    monkeypatch.setattr(gitea_module, "_wait_for_local_https_health", fake_health)
    monkeypatch.setattr(gitea_module, "_gitea_cli_exec", no_cli)
    http = _ScriptedGiteaHttp()
    monkeypatch.setattr(gitea_module.urlrequest, "urlopen", http)

    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-gitea-secret-key"
    )
    assert order == ["health"]
    assert state.initialized is False
    assert state.provisioner_ready is False
    # A not-ready Gitea surfaces through the health gate, not as a CLI WARNING.
    assert not any(note.startswith("WARNING") for note in notes)


def test_provisioner_bootstraps_account_token_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _gitea_backend(tmp_path, ScriptedS3(head_script=[200]))
    monkeypatch.setattr(gitea_module, "_wait_for_local_https_health", lambda *a, **k: True)
    cli = ScriptedCli([_CliResult(ok=True, output="admin user created")])
    monkeypatch.setattr(gitea_module, "_gitea_cli_exec", cli)
    http = _ScriptedGiteaHttp()
    http.queue(
        "POST",
        "/api/v1/users/clayton/tokens",
        201,
        {"name": "wizard-provisioner", "sha1": "prov-token-1"},
    )
    monkeypatch.setattr(gitea_module.urlrequest, "urlopen", http)

    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-gitea-secret-key"
    )
    assert state.bucket_ready is True
    assert state.provisioner_ready is True
    assert any("Seeded Gitea admin account 'clayton'" in note for note in notes)

    args = _provisioner_args(cli)
    assert args[:3] == ["admin", "user", "create"]
    assert "--admin" in args and "--must-change-password=false" in args
    username_index = args.index("--username") + 1
    assert args[username_index] == "clayton"
    email_index = args.index("--email") + 1
    assert args[email_index] == "clayton@example.com"
    # The account password is the stack password, and the CLI pins it on
    # every pass (one credential across surfaces survives a rotation).
    pin = next(call for call in cli.calls if call[:3] == ["admin", "user", "change-password"])
    assert pin[pin.index("--password") + 1] == "StackPw!23"
    # Box-measured regression: this Gitea build defaults change-password's
    # --must-change-password to TRUE, so the pin must carry the explicit
    # false — a flagless pin re-stamps the flag the create just cleared and
    # the mint one step later dies at 403 (zero-state loop, live box).
    assert "--must-change-password=false" in pin

    stored = load_gitea_provisioner(tmp_path)
    assert stored is not None
    assert stored.username == "clayton"
    assert stored.email == "clayton@example.com"
    assert stored.password == "StackPw!23"
    assert stored.token == "prov-token-1"
    mint = next(req for req in http.requests if req.get_method() == "POST")
    body = json.loads(mint.data.decode())
    assert body["name"] == "wizard-provisioner"
    # Gitea 28's API accepts only the bare "all" grant; category:access forms
    # are rejected with 400 (measured live).
    assert body["scopes"] == ["all"]
    import base64

    auth = mint.get_header("Authorization")
    assert auth.startswith("Basic ")
    assert base64.b64decode(auth.split(" ", 1)[1]).decode().startswith("clayton:")
    assert (tmp_path / "gitea-provisioner.json").stat().st_mode & 0o777 == 0o600


def test_provisioner_alive_token_short_circuits_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_gitea_provisioner(
        tmp_path,
        GiteaProvisionerState(
            format_version=1,
            username="clayton",
            email="clayton@example.com",
            password="StackPw!23",
            token="alive-token",
        ),
    )
    backend = _gitea_backend(tmp_path, ScriptedS3(head_script=[200]))
    monkeypatch.setattr(gitea_module, "_wait_for_local_https_health", lambda *a, **k: True)

    cli = ScriptedCli([_CliResult(ok=True, output="password updated")])
    monkeypatch.setattr(gitea_module, "_gitea_cli_exec", cli)
    http = _ScriptedGiteaHttp()
    http.queue("GET", "/api/v1/user", 200, {"username": "clayton"})
    # The deployer's own row is visible-public (seeded before the privacy
    # default): the visibility migration flips it and says so once.
    http.queue("GET", "/api/v1/admin/users/clayton", 200, {"username": "clayton", "visibility": "public"})
    http.queue("PATCH", "/api/v1/admin/users/clayton", 200, {"username": "clayton", "visibility": "private"})
    monkeypatch.setattr(gitea_module.urlrequest, "urlopen", http)
    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-gitea-secret-key"
    )
    assert state.provisioner_ready is True
    assert load_gitea_provisioner(tmp_path).token == "alive-token"
    # No re-mint, no CLI create — but the password pin runs and the legacy
    # account (absent from the script: 404) is not mourned.
    assert not any(call[:3] == ["admin", "user", "create"] for call in cli.calls)
    assert any(call[:3] == ["admin", "user", "change-password"] for call in cli.calls)
    assert "Set Gitea account 'clayton' visibility to private (privacy default)." in notes


def test_provisioner_rotates_dead_token_and_tolerates_existing_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_gitea_provisioner(
        tmp_path,
        GiteaProvisionerState(
            format_version=1,
            username="clayton",
            email="clayton@example.com",
            password="StackPw!23",
            token="dead-token",
        ),
    )
    backend = _gitea_backend(tmp_path, ScriptedS3(head_script=[200]))
    monkeypatch.setattr(gitea_module, "_wait_for_local_https_health", lambda *a, **k: True)
    cli = ScriptedCli(
        [_CliResult(ok=False, output="Error: user 'clayton' name is already taken")]
    )
    monkeypatch.setattr(gitea_module, "_gitea_cli_exec", cli)
    http = _ScriptedGiteaHttp()
    http.queue("GET", "/api/v1/user", 401, {"message": "invalid token"})
    http.queue("POST", "/api/v1/users/clayton/tokens", 409, {})
    http.queue("DELETE", "/api/v1/users/clayton/tokens/wizard-provisioner", 204, {})
    http.queue(
        "POST",
        "/api/v1/users/clayton/tokens",
        201,
        {"sha1": "fresh-token"},
    )
    monkeypatch.setattr(gitea_module.urlrequest, "urlopen", http)
    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-gitea-secret-key"
    )
    assert state.provisioner_ready is True
    assert any("Re-minted" in note for note in notes)
    assert load_gitea_provisioner(tmp_path).token == "fresh-token"
    # The stored password no longer matches the CLI-seeded one after a state-doc
    # loss scenario; the CLI create tolerating 'already taken' is what makes the
    # rotation converge instead of failing the pass.
    assert cli.calls[0][:3] == ["admin", "user", "create"]


def test_provisioner_bridge_repairs_the_upgrade_box_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Box-measured upgrade path: Gitea 403s basic auth until the bridge PATCH.

    The previous wizard generation left the deployer's account non-admin and
    flagged must-change; the CLI pin rewrites the password but not the flag
    (measured twice), and every basic-auth mint then dies at 403. The legacy
    service account's token is the only credential that can fix the row at
    boot time — the whole 200/200/201 sequence was measured live.
    """
    write_gitea_provisioner(
        tmp_path,
        GiteaProvisionerState(
            format_version=1,
            username="dokploy-wizard",
            email="dokploy-wizard@gitea.example.com",
            password="old-pw",
            token="legacy-token",
        ),
    )
    backend = _gitea_backend(tmp_path, ScriptedS3(head_script=[200]))
    cli = ScriptedCli(
        [
            _CliResult(ok=False, output="Error: user 'clayton' name is already taken"),
            _CliResult(ok=True, output="password updated"),
            _CliResult(ok=True, output="user deleted"),
        ]
    )
    monkeypatch.setattr(gitea_module, "_gitea_cli_exec", cli)
    http = _ScriptedGiteaHttp()
    http.queue("GET", "/api/v1/user", 200, {"username": "dokploy-wizard"})
    http.queue(
        "GET",
        "/api/v1/admin/users/clayton",
        200,
        {"username": "clayton", "must_change_password": True, "is_admin": False},
    )
    http.queue("PATCH", "/api/v1/admin/users/clayton", 200, {"username": "clayton"})
    http.queue("POST", "/api/v1/users/clayton/tokens", 201, {"sha1": "bridged-token"})
    http.queue(
        "GET", "/api/v1/admin/users/dokploy-wizard", 200, {"username": "dokploy-wizard"}
    )
    monkeypatch.setattr(gitea_module.urlrequest, "urlopen", http)

    ready, note = backend._ensure_provisioner()

    assert ready is True
    assert note is not None
    assert "bridge" in note
    assert "Deleted the legacy Gitea service account" in note
    stored = load_gitea_provisioner(tmp_path)
    assert stored is not None
    assert stored.username == "clayton"
    assert stored.token == "bridged-token"
    # The legacy account dies through the CLI, gated on the admin-API 200.
    assert cli.calls[2][:3] == ["admin", "user", "delete"]


def test_provisioner_bridge_skips_an_account_that_already_reads_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drift-gated: the PATCH runs only when the row actually needs it."""
    write_gitea_provisioner(
        tmp_path,
        GiteaProvisionerState(
            format_version=1,
            username="dokploy-wizard",
            email="dokploy-wizard@gitea.example.com",
            password="old-pw",
            token="legacy-token",
        ),
    )
    backend = _gitea_backend(tmp_path, ScriptedS3(head_script=[200]))
    cli = ScriptedCli(
        [
            _CliResult(ok=False, output="Error: user 'clayton' name is already taken"),
            _CliResult(ok=True, output="password updated"),
            _CliResult(ok=True, output="user deleted"),
        ]
    )
    monkeypatch.setattr(gitea_module, "_gitea_cli_exec", cli)
    http = _ScriptedGiteaHttp()
    http.queue("GET", "/api/v1/user", 200, {"username": "dokploy-wizard"})
    http.queue(
        "GET",
        "/api/v1/admin/users/clayton",
        200,
        {"username": "clayton", "must_change_password": False, "is_admin": True},
    )
    http.queue("POST", "/api/v1/users/clayton/tokens", 201, {"sha1": "fresh-token"})
    http.queue("GET", "/api/v1/admin/users/dokploy-wizard", 404, {})
    monkeypatch.setattr(gitea_module.urlrequest, "urlopen", http)

    ready, note = backend._ensure_provisioner()

    assert ready is True
    assert "bridge" not in (note or "")
    assert ("PATCH", "https://127.0.0.1/api/v1/admin/users/clayton") not in [
        (request.get_method(), request.full_url) for request in http.requests
    ]


def test_provisioner_degrades_to_warning_when_cli_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _gitea_backend(tmp_path, ScriptedS3(head_script=[200]))
    monkeypatch.setattr(gitea_module, "_wait_for_local_https_health", lambda *a, **k: True)
    cli = ScriptedCli([_CliResult(ok=False, output="Error: Custom setup wasn't valid")])
    monkeypatch.setattr(gitea_module, "_gitea_cli_exec", cli)
    http = _ScriptedGiteaHttp()
    monkeypatch.setattr(gitea_module.urlrequest, "urlopen", http)
    state, notes = backend.ensure_application_ready(
        secret_key_secret_ref="wizard-stack-gitea-secret-key"
    )
    assert state.provisioner_ready is False
    assert state.bucket_ready is True
    assert any(note.startswith("WARNING") for note in notes)


def test_gitea_backend_rejects_off_plan_values(tmp_path: Path) -> None:
    backend = _gitea_backend(tmp_path, ScriptedS3())
    with pytest.raises(GiteaError, match="hostname no longer matches"):
        backend.create_service(
            resource_name="wizard-stack-gitea",
            hostname="other.example.com",
            postgres_service_name="wizard-stack-shared-postgres",
            postgres=_gitea_desired_state().shared_core.allocations[0].postgres,
            data_resource_name="wizard-stack-gitea-data",
        )
    with pytest.raises(GiteaError, match="postgres inputs no longer match"):
        backend.create_service(
            resource_name="wizard-stack-gitea",
            hostname="gitea.example.com",
            postgres_service_name="someone-elses-postgres",
            postgres=_gitea_desired_state().shared_core.allocations[0].postgres,
            data_resource_name="wizard-stack-gitea-data",
        )
    with pytest.raises(GiteaError, match="SECRET_KEY secret ref"):
        backend.ensure_application_ready(secret_key_secret_ref="wizard-stack-other-secret-key")
