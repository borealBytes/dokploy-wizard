# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, replace
from email.message import Message
from urllib import parse
from typing import Any, cast
from urllib import error as urlerror

import pytest

import dokploy_wizard.dokploy.docuseal as docuseal_module
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployDocuSealBackend,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.docuseal import (
    DokployDocuSealApi,
    _DocuSealSetupProbe,
    _local_https_health_check,
    _render_compose_file,
    _secret_key_base_value,
)
from dokploy_wizard.packs.docuseal import (
    DOCUSEAL_DATA_RESOURCE_TYPE,
    DOCUSEAL_SERVICE_RESOURCE_TYPE,
    DocuSealBootstrapState,
    DocuSealError,
    DocuSealResourceRecord,
    build_docuseal_ledger,
    reconcile_docuseal,
)
from dokploy_wizard.state import OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state


@dataclass
class FakeDocuSealBackend:
    existing_service: DocuSealResourceRecord | None = None
    existing_data: DocuSealResourceRecord | None = None
    health_ok: bool = True
    health_results: list[bool] | None = None
    ensure_calls: int = 0

    def get_service(self, resource_id: str) -> DocuSealResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> DocuSealResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(self, **kwargs: object) -> DocuSealResourceRecord:
        resource_name = str(kwargs["resource_name"])
        self.existing_service = DocuSealResourceRecord(
            resource_id="docuseal-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def update_service(self, **kwargs: object) -> DocuSealResourceRecord:
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> DocuSealResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> DocuSealResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> DocuSealResourceRecord:
        self.existing_data = DocuSealResourceRecord(
            resource_id="docuseal-data-1",
            resource_name=resource_name,
        )
        return self.existing_data

    def check_health(self, *, service: DocuSealResourceRecord, url: str) -> bool:
        del service, url
        if self.health_results is not None:
            if self.health_results:
                return self.health_results.pop(0)
            return self.health_ok
        return self.health_ok

    def ensure_application_ready(
        self, *, secret_key_base_secret_ref: str
    ) -> tuple[DocuSealBootstrapState, tuple[str, ...]]:
        self.ensure_calls += 1
        return (
            DocuSealBootstrapState(
                initialized=True,
                secret_key_base_secret_ref=secret_key_base_secret_ref,
            ),
            ("DocuSeal bootstrap placeholder completed.",),
        )


@dataclass
class FakeDokployDocuSealApiClient:
    projects: list[DokployProjectSummary] = None  # type: ignore[assignment]
    create_project_calls: int = 0
    create_compose_calls: int = 0
    update_compose_calls: int = 0
    deploy_calls: int = 0
    last_create_compose_file: str | None = None
    last_update_compose_file: str | None = None

    def __post_init__(self) -> None:
        if self.projects is None:
            self.projects = []

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        return tuple(self.projects)

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject:
        del description, env
        self.create_project_calls += 1
        self.projects.append(
            DokployProjectSummary(
                project_id="proj-1",
                name=name,
                environments=(
                    DokployEnvironmentSummary(
                        environment_id="env-1",
                        name="production",
                        is_default=True,
                        composes=(),
                    ),
                ),
            )
        )
        return DokployCreatedProject(project_id="proj-1", environment_id="env-1")

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord:
        del app_name
        self.create_compose_calls += 1
        self.last_create_compose_file = compose_file
        record = DokployComposeRecord(compose_id="cmp-1", name=name)
        self.projects[0] = DokployProjectSummary(
            project_id="proj-1",
            name=self.projects[0].name,
            environments=(
                DokployEnvironmentSummary(
                    environment_id=environment_id,
                    name="production",
                    is_default=True,
                    composes=(
                        DokployComposeSummary(
                            compose_id=record.compose_id,
                            name=record.name,
                            status=None,
                        ),
                    ),
                ),
            ),
        )
        return record

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord:
        self.update_compose_calls += 1
        self.last_update_compose_file = compose_file
        return DokployComposeRecord(compose_id=compose_id, name="wizard-stack-docuseal")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")


def _docuseal_desired_state():
    return resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_DOCUSEAL": "true",
            },
        )
    )


def test_render_docuseal_compose_includes_database_secret_persistent_storage_and_forwarded_https() -> (
    None
):
    secret_key_base_secret_ref = "wizard-stack-docuseal-secret-key-base"
    compose = _render_compose_file(
        stack_name="wizard-stack",
        hostname="docuseal.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_docuseal",
            user_name="wizard_stack_docuseal",
            password_secret_ref="wizard-stack-docuseal-postgres-password",
        ),
        secret_key_base_secret_ref=secret_key_base_secret_ref,
    )

    assert "image: docuseal/docuseal:latest" in compose
    assert (
        'DATABASE_URL: "postgres://wizard_stack_docuseal:change-me@wizard-stack-shared-postgres:5432/wizard_stack_docuseal?sslmode=disable"'
        in compose
    )
    assert (
        f"SECRET_KEY_BASE: {_secret_key_base_value('wizard-stack', secret_key_base_secret_ref)}"
        in compose
    )
    assert 'DOKPLOY_WIZARD_DOCUSEAL_BASE_URL: "https://docuseal.example.com"' in compose
    assert "working_dir: /data/docuseal" in compose
    assert "wizard-stack-docuseal-data:/data/docuseal" in compose
    assert (
        'traefik.http.routers.wizard-stack-docuseal.middlewares: "wizard-stack-docuseal-forwarded-https"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-docuseal-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-docuseal-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "docuseal.example.com"'
        in compose
    )
    assert (
        'ruby -rnet/http -e "uri = URI(%q{http://127.0.0.1:3000/up}); '
        'response = Net::HTTP.get_response(uri); '
        'exit(response.is_a?(Net::HTTPSuccess) ? 0 : 1)"'
        in compose
    )


def test_render_docuseal_compose_includes_local_postfix_smtp_when_configured() -> None:
    compose = _render_compose_file(
        stack_name="wizard-stack",
        hostname="docuseal.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_docuseal",
            user_name="wizard_stack_docuseal",
            password_secret_ref="wizard-stack-docuseal-postgres-password",
        ),
        secret_key_base_secret_ref="wizard-stack-docuseal-secret-key-base",
        smtp_host="wizard-stack-shared-postfix",
        smtp_port=587,
        smtp_domain="example.com",
        smtp_from_address="DoNotReply@example.com",
    )

    assert "SMTP_ADDRESS: wizard-stack-shared-postfix" in compose
    assert "SMTP_PORT: '587'" in compose
    assert "SMTP_DOMAIN: example.com" in compose
    assert 'SMTP_FROM: "DoNotReply@example.com"' in compose
    assert "SMTP_ENABLE_STARTTLS: 'false'" in compose


def test_dokploy_docuseal_backend_create_and_update_paths_keep_single_compose_stable() -> None:
    create_client = FakeDokployDocuSealApiClient()
    create_backend = DokployDocuSealBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="docuseal.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_docuseal",
            user_name="wizard_stack_docuseal",
            password_secret_ref="wizard-stack-docuseal-postgres-password",
        ),
        client=cast(DokployDocuSealApi, create_client),
    )

    created = create_backend.create_service(
        resource_name="wizard-stack-docuseal",
        hostname="docuseal.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_docuseal",
            user_name="wizard_stack_docuseal",
            password_secret_ref="wizard-stack-docuseal-postgres-password",
        ),
        data_resource_name="wizard-stack-docuseal-data",
    )

    assert created.resource_id == "dokploy-compose:cmp-1:service"
    assert create_client.create_project_calls == 1
    assert create_client.create_compose_calls == 1
    assert create_client.update_compose_calls == 0
    assert create_client.deploy_calls == 1
    assert create_client.last_create_compose_file is not None

    existing_project = DokployProjectSummary(
        project_id="proj-1",
        name="wizard-stack",
        environments=(
            DokployEnvironmentSummary(
                environment_id="env-1",
                name="production",
                is_default=True,
                composes=(
                    DokployComposeSummary(
                        compose_id="cmp-existing",
                        name="wizard-stack-docuseal",
                        status="done",
                    ),
                ),
            ),
        ),
    )
    update_client = FakeDokployDocuSealApiClient(projects=[existing_project])
    update_backend = DokployDocuSealBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="docuseal.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_docuseal",
            user_name="wizard_stack_docuseal",
            password_secret_ref="wizard-stack-docuseal-postgres-password",
        ),
        client=cast(DokployDocuSealApi, update_client),
    )

    updated = update_backend.create_service(
        resource_name="wizard-stack-docuseal",
        hostname="docuseal.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_docuseal",
            user_name="wizard_stack_docuseal",
            password_secret_ref="wizard-stack-docuseal-postgres-password",
        ),
        data_resource_name="wizard-stack-docuseal-data",
    )

    assert updated.resource_id == "dokploy-compose:cmp-existing:service"
    assert update_client.create_compose_calls == 0
    assert update_client.update_compose_calls == 1
    assert update_client.deploy_calls == 1
    assert update_client.last_update_compose_file is not None


def test_ensure_application_ready_initializes_docuseal_via_internal_setup_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployDocuSealBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="docuseal.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_docuseal",
            user_name="wizard_stack_docuseal",
            password_secret_ref="wizard-stack-docuseal-postgres-password",
        ),
        client=cast(DokployDocuSealApi, FakeDokployDocuSealApiClient()),
    )
    backend._created_in_process = True

    waited_urls: list[str] = []
    monkeypatch.setattr(
        docuseal_module,
        "_wait_for_local_https_health",
        lambda url: waited_urls.append(url) or True,
    )
    initialized = iter([False, True])
    monkeypatch.setattr(
        docuseal_module,
        "_docuseal_is_initialized",
        lambda hostname: next(initialized),
    )
    setup_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        docuseal_module,
        "_submit_docuseal_setup",
        lambda *, hostname, admin_email, admin_password: setup_calls.append(
            (hostname, admin_email, admin_password)
        ),
    )
    monkeypatch.setattr(docuseal_module, "_wait_for_docuseal_initialization", lambda hostname: True)

    state, notes = backend.ensure_application_ready(
        secret_key_base_secret_ref="wizard-stack-docuseal-secret-key-base"
    )

    assert waited_urls == ["https://docuseal.example.com/up"]
    assert setup_calls == [
        ("docuseal.example.com", "admin@example.com", "ChangeMeSoon")
    ]
    assert state == DocuSealBootstrapState(
        initialized=True,
        secret_key_base_secret_ref="wizard-stack-docuseal-secret-key-base",
    )
    assert notes == ("Initialized DocuSeal via internal /setup flow.",)


def test_ensure_application_ready_is_rerun_safe_when_docuseal_is_already_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployDocuSealBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="docuseal.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_docuseal",
            user_name="wizard_stack_docuseal",
            password_secret_ref="wizard-stack-docuseal-postgres-password",
        ),
        client=cast(DokployDocuSealApi, FakeDokployDocuSealApiClient()),
    )
    monkeypatch.setattr(docuseal_module, "_docuseal_is_initialized", lambda hostname: True)
    submit_calls: list[str] = []
    monkeypatch.setattr(
        docuseal_module,
        "_submit_docuseal_setup",
        lambda **kwargs: submit_calls.append("called"),
    )

    state, notes = backend.ensure_application_ready(
        secret_key_base_secret_ref="wizard-stack-docuseal-secret-key-base"
    )

    assert submit_calls == []
    assert state == DocuSealBootstrapState(
        initialized=True,
        secret_key_base_secret_ref="wizard-stack-docuseal-secret-key-base",
    )
    assert notes == ("DocuSeal already initialized; skipped internal setup flow.",)


def test_dokploy_docuseal_health_waits_for_public_route_on_first_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployDocuSealBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="docuseal.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_docuseal",
            user_name="wizard_stack_docuseal",
            password_secret_ref="wizard-stack-docuseal-postgres-password",
        ),
        client=cast(DokployDocuSealApi, FakeDokployDocuSealApiClient()),
    )
    backend._created_in_process = True
    monkeypatch.setattr(docuseal_module, "_local_https_health_check", lambda url: False)
    monkeypatch.setattr(docuseal_module, "_public_https_health_check", lambda url: False)
    waited_urls: list[str] = []
    monkeypatch.setattr(
        docuseal_module,
        "_wait_for_public_https_health",
        lambda url: waited_urls.append(url) or True,
    )

    ok = backend.check_health(
        service=DocuSealResourceRecord("docuseal-service-1", "wizard-stack-docuseal"),
        url="https://docuseal.example.com/up",
    )

    assert ok is True
    assert waited_urls == ["https://docuseal.example.com/up"]


def test_docuseal_setup_probe_interprets_setup_page_redirect_and_404() -> None:
    assert (
        docuseal_module._docuseal_setup_probe_indicates_initialized(
            _DocuSealSetupProbe(
                status=200,
                location=None,
                body='<form action="/setup"><input name="user[email]" /></form>',
            )
        )
        is False
    )
    assert (
        docuseal_module._docuseal_setup_probe_indicates_initialized(
            _DocuSealSetupProbe(status=302, location="/sign_in", body="")
        )
        is True
    )
    assert (
        docuseal_module._docuseal_setup_probe_indicates_initialized(
            _DocuSealSetupProbe(status=404, location=None, body="")
        )
        is False
    )


def test_docuseal_setup_probe_treats_live_initial_setup_form_as_not_initialized() -> None:
    assert (
        docuseal_module._docuseal_setup_probe_indicates_initialized(
            _DocuSealSetupProbe(
                status=200,
                location=None,
                body=(
                    "<html><head><title>DocuSeal | Open Source Document Signing</title></head>"
                    "<body><h1>Initial Setup</h1><label>Email</label><label>Company name</label>"
                    "<label>Password</label><label>App URL</label>"
                    '<input name="account[name]" /><input name="encrypted_config[value]" />'
                    "</body></html>"
                ),
            )
        )
        is False
    )


def test_submit_docuseal_setup_rejects_response_that_still_shows_setup_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status = 200
        headers = {"Location": None}

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def read(self) -> bytes:
            return (
                b"<html><body><h1>Initial Setup</h1>"
                b"<input name=\"account[name]\" />"
                b"<input name=\"encrypted_config[value]\" />"
                b"</body></html>"
            )

    class FakeOpener:
        def open(self, req: Any, timeout: int) -> FakeResponse:
            del req, timeout
            return FakeResponse()

    monkeypatch.setattr(
        docuseal_module.urlrequest,
        "build_opener",
        lambda *handlers: FakeOpener(),
    )

    with pytest.raises(DocuSealError, match="did not converge"):
        docuseal_module._submit_docuseal_setup(
            hostname="docuseal.example.com",
            admin_email="admin@example.com",
            admin_password="ChangeMeSoon",
        )


def test_docuseal_setup_form_context_extracts_live_csrf_and_defaults() -> None:
    context = docuseal_module._docuseal_setup_form_context(
        """
        <html>
          <head>
            <meta name="csrf-param" content="authenticity_token" />
            <meta name="csrf-token" content="csrf-123" />
          </head>
          <body>
            <form action="/setup" method="post">
              <input type="hidden" name="account[timezone]" value="UTC" />
              <select name="account[locale]">
                <option value="en-US" selected>English</option>
              </select>
            </form>
          </body>
        </html>
        """
    )

    assert context.authenticity_token == "csrf-123"
    assert context.timezone == "UTC"
    assert context.locale == "en-US"


def test_submit_docuseal_setup_posts_live_form_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class FakeResponse:
        def __init__(self, *, status: int, body: str, location: str | None = None) -> None:
            self.status = status
            self.headers = {"Location": location}
            self._body = body.encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def read(self) -> bytes:
            return self._body

    class FakeOpener:
        def open(self, req: Any, timeout: int) -> FakeResponse:
            requests.append(
                {
                    "url": req.full_url,
                    "method": req.get_method(),
                    "data": req.data,
                    "headers": dict(req.header_items()),
                    "timeout": timeout,
                }
            )
            if req.get_method() == "GET":
                return FakeResponse(
                    status=200,
                    body=(
                        '<meta name="csrf-param" content="authenticity_token" />'
                        '<meta name="csrf-token" content="csrf-123" />'
                        '<input type="hidden" name="account[timezone]" value="UTC" />'
                        '<select name="account[locale]">'
                        '<option value="en-US" selected>English</option>'
                        "</select>"
                    ),
                )
            return FakeResponse(status=302, body="", location="/sign_in")

    monkeypatch.setattr(
        docuseal_module.urlrequest,
        "build_opener",
        lambda *handlers: FakeOpener(),
    )

    docuseal_module._submit_docuseal_setup(
        hostname="docuseal.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
    )

    assert [request["method"] for request in requests] == ["GET", "POST"]
    post_body = parse.parse_qs(requests[1]["data"].decode("utf-8"), keep_blank_values=True)
    assert post_body["authenticity_token"] == ["csrf-123"]
    assert post_body["account[timezone]"] == ["UTC"]
    assert post_body["account[locale]"] == ["en-US"]
    assert post_body["account[name]"] == ["Dokploy Wizard"]
    assert post_body["encrypted_config[value]"] == ["https://docuseal.example.com"]
    assert post_body["user[email]"] == ["admin@example.com"]
    assert "app_url" not in post_body


def test_local_https_health_check_uses_host_header_against_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    monkeypatch.setattr(
        docuseal_module.ssl,
        "create_default_context",
        lambda: type("Ctx", (), {"check_hostname": True, "verify_mode": None})(),
    )

    def fake_urlopen(req: Any, timeout: int, context: object) -> FakeResponse:
        captured["full_url"] = req.full_url
        captured["host"] = req.headers.get("Host")
        captured["timeout"] = timeout
        captured["check_hostname"] = getattr(context, "check_hostname", None)
        return FakeResponse()

    monkeypatch.setattr(docuseal_module.urlrequest, "urlopen", fake_urlopen)

    ok = _local_https_health_check("https://docuseal.example.com/up?test=1")

    assert ok is True
    assert captured == {
        "full_url": "https://127.0.0.1/up?test=1",
        "host": "docuseal.example.com",
        "timeout": 15,
        "check_hostname": False,
    }


def test_local_https_health_check_treats_404_as_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docuseal_module.ssl,
        "create_default_context",
        lambda: type("Ctx", (), {"check_hostname": True, "verify_mode": None})(),
    )

    def fake_urlopen(req: Any, timeout: int, context: object) -> Any:
        del req, timeout, context
        raise urlerror.HTTPError(
            url="https://127.0.0.1/up",
            code=404,
            msg="Not Found",
            hdrs=Message(),
            fp=None,
        )

    monkeypatch.setattr(docuseal_module.urlrequest, "urlopen", fake_urlopen)

    assert _local_https_health_check("https://docuseal.example.com/up") is False


def test_reconcile_docuseal_creates_service_and_data() -> None:
    phase = reconcile_docuseal(
        dry_run=False,
        desired_state=_docuseal_desired_state(),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeDocuSealBackend(),
    )

    assert phase.result.outcome == "applied"
    assert phase.result.hostname == "docuseal.example.com"
    assert phase.service_resource_id == "docuseal-service-1"
    assert phase.data_resource_id == "docuseal-data-1"
    assert phase.result.service is not None
    assert phase.result.persistent_data is not None
    assert phase.result.bootstrap_state is not None
    assert phase.result.bootstrap_state.initialized is True
    assert phase.result.health_state is not None
    assert phase.result.health_state.passed is True
    assert phase.result.health_state.path == "/up"
    assert phase.result.config is not None
    assert phase.result.config.access_url == "https://docuseal.example.com"
    assert phase.result.config.postgres.database_name == "wizard_stack_docuseal"


def test_reconcile_docuseal_returns_plan_only_shape_for_dry_run() -> None:
    phase = reconcile_docuseal(
        dry_run=True,
        desired_state=_docuseal_desired_state(),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeDocuSealBackend(),
    )

    assert phase.result.outcome == "plan_only"
    assert phase.service_resource_id is None
    assert phase.data_resource_id is None
    assert phase.result.service is not None
    assert phase.result.service.action == "create"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.action == "create"
    assert phase.result.bootstrap_state is not None
    assert phase.result.bootstrap_state.initialized is None
    assert (
        phase.result.bootstrap_state.secret_key_base_secret_ref
        == "wizard-stack-docuseal-secret-key-base"
    )
    assert phase.result.health_state is not None
    assert phase.result.health_state.url == "https://docuseal.example.com/up"
    assert phase.result.health_state.passed is None
    assert phase.result.config is not None
    assert phase.result.config.postgres.user_name == "wizard_stack_docuseal"


def test_reconcile_docuseal_fails_when_hostname_missing() -> None:
    desired_state = replace(_docuseal_desired_state(), hostnames={})

    with pytest.raises(DocuSealError, match="canonical DocuSeal hostname"):
        reconcile_docuseal(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeDocuSealBackend(),
        )


def test_reconcile_docuseal_fails_when_shared_postgres_allocation_missing() -> None:
    desired_state = _docuseal_desired_state()
    desired_state = replace(
        desired_state,
        shared_core=replace(desired_state.shared_core, allocations=()),
    )

    with pytest.raises(DocuSealError, match="shared-core postgres allocation is missing"):
        reconcile_docuseal(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeDocuSealBackend(),
        )


def test_reconcile_docuseal_reuses_owned_resources_and_reports_already_present() -> None:
    backend = FakeDocuSealBackend(
        existing_service=DocuSealResourceRecord(
            resource_id="docuseal-service-1",
            resource_name="wizard-stack-docuseal",
        ),
        existing_data=DocuSealResourceRecord(
            resource_id="docuseal-data-1",
            resource_name="wizard-stack-docuseal-data",
        ),
        health_results=[False, True],
    )

    phase = reconcile_docuseal(
        dry_run=False,
        desired_state=_docuseal_desired_state(),
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=DOCUSEAL_SERVICE_RESOURCE_TYPE,
                    resource_id="docuseal-service-1",
                    scope="stack:wizard-stack:docuseal:service",
                ),
                OwnedResource(
                    resource_type=DOCUSEAL_DATA_RESOURCE_TYPE,
                    resource_id="docuseal-data-1",
                    scope="stack:wizard-stack:docuseal:data",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None
    assert phase.result.service.action == "update_owned"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.action == "reuse_owned"
    assert phase.result.bootstrap_state is not None
    assert phase.result.bootstrap_state.initialized is True
    assert phase.result.health_state is not None
    assert phase.result.health_state.passed is True
    assert backend.ensure_calls == 1


def test_build_docuseal_ledger_replaces_prior_owned_resources_cleanly() -> None:
    ledger = build_docuseal_ledger(
        existing_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=DOCUSEAL_SERVICE_RESOURCE_TYPE,
                    resource_id="old-service",
                    scope="stack:wizard-stack:docuseal:service",
                ),
                OwnedResource(
                    resource_type=DOCUSEAL_DATA_RESOURCE_TYPE,
                    resource_id="old-data",
                    scope="stack:wizard-stack:docuseal:data",
                ),
                OwnedResource(
                    resource_type="cloudflare_tunnel",
                    resource_id="tunnel-1",
                    scope="account:account-123",
                ),
            ),
        ),
        stack_name="wizard-stack",
        service_resource_id="new-service",
        data_resource_id="new-data",
    )

    assert {(item.resource_type, item.resource_id, item.scope) for item in ledger.resources} == {
        (DOCUSEAL_SERVICE_RESOURCE_TYPE, "new-service", "stack:wizard-stack:docuseal:service"),
        (DOCUSEAL_DATA_RESOURCE_TYPE, "new-data", "stack:wizard-stack:docuseal:data"),
        ("cloudflare_tunnel", "tunnel-1", "account:account-123"),
    }
