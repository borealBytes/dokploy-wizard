# mypy: ignore-errors
# ruff: noqa: E501
"""Dokploy-backed FileX runtime backend.

FileX is a single Go binary with SQLite in ``/data``; its files live in the
SeaweedFS S3 bucket the wizard owns. Three responsibilities live here:

* render the compose app (public URL, first-boot seed env, traefik route);
* provision the bucket itself — create it and turn native versioning on — over
  SigV4 through the loopback edge (UFW on the box blocks host→overlay TCP, so
  every host-side call rides ``https://127.0.0.1`` with an explicit Host
  header), exactly like the pack health checks already do;
* prove the seeded admin can sign in once, through the public JSON login
  endpoint, without tripping the sign-in attempt limiter.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import ssl
import subprocess
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest

from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.compose_noop import (
    apply_compose_noop_guard,
    apply_rendered_compose_to_existing,
    persist_compose_artifact_hash,
)
from dokploy_wizard.dokploy.env_spec import DokployEnvSpec, DokployEnvVar, RenderedCompose
from dokploy_wizard.packs.filex import (
    FILEX_BUCKET,
    FILEX_S3_REGION,
    FILEX_STORAGE_PREFIX,
    FilexBootstrapState,
    FilexError,
    FilexResourceRecord,
)
from dokploy_wizard.state.models import STATE_FORMAT_VERSION, FilexProvisionerState
from dokploy_wizard.state.store import load_filex_provisioner, write_filex_provisioner
from dokploy_wizard.verification import ServiceVerificationResult

# Pinned to filex v0.61.2 (BRF-Tech/filex release, 2026-11-18). The tag names the
# release for humans; the digest is what Docker resolves, so reruns and reinstalls
# land on the same bytes. Verify the tag/digest pair before every bump:
#   TOKEN=$(curl -s https://ghcr.io/token?scope=repository:brf-tech/filex:pull | ...)
#   curl -sI -H "Authorization: Bearer $TOKEN" \
#     -H "Accept: application/vnd.oci.image.index.v1+json" \
#     https://ghcr.io/v2/brf-tech/filex/manifests/v0.61.2 | grep -i docker-content-digest
# v0.61.x is also the floor for S3-key attribution in the audit log, which the
# ~/shared workspace mount depends on.
# Service account the wizard manages inside FileX. Workspace boot scripts use
# its admin-scoped API token to materialize the workspace owner's FileX account
# and that person's own credentials; the token is the only credential baked into
# the Coder template, and /api/admin delegation requires the admin scope (a
# plain user token is refused by FileX by design).
FILEX_PROVISIONER_USERNAME = "wizard-provisioner"
_DEFAULT_FILEX_IMAGE = (
    "ghcr.io/brf-tech/filex:v0.61.2"
    "@sha256:e8756486300542bcfb7f8cf1a323a583c2444815da09c8947389197d43b04c8d"
)
_DEFAULT_FILEX_PORT = "5212"
_DEFAULT_FILEX_DATA_ROOT = "/data"
_VERSIONING_XML = (
    '<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    "<Status>Enabled</Status></VersioningConfiguration>"
)


class DokployFilexApi(Protocol):
    def list_projects(self) -> tuple[DokployProjectSummary, ...]: ...

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject: ...

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord: ...

    def update_compose(
        self, *, compose_id: str, compose_file: str | None = None, env: str | None = None
    ) -> DokployComposeRecord: ...

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


class S3Requester(Protocol):
    """Minimal signed-S3 surface so reconciler tests can fake the bucket plane."""

    def request(
        self, method: str, path: str, query: str = "", body: bytes = b""
    ) -> tuple[int, str]: ...


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


class LoopbackSigV4Client:
    """Path-style SigV4 S3 client bound to one explicit Host header.

    Requests always hit ``https://127.0.0.1`` — the box's published edge — and
    carry the routing Host the wizard owns (the internal SeaweedFS hostname
    after the public s3 route went away). TLS verification is off: the edge
    answers unknown SNI with its default certificate.
    """

    def __init__(
        self,
        *,
        host_header: str,
        access_key: str,
        secret_key: str,
        region: str = FILEX_S3_REGION,
    ) -> None:
        self._host_header = host_header
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region

    def request(
        self, method: str, path: str, query: str = "", body: bytes = b""
    ) -> tuple[int, str]:
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_headers = (
            f"host:{self._host_header}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = (
            f"{method}\n{path}\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = (
            "AWS4-HMAC-SHA256\n"
            f"{amz_date}\n"
            f"{scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        def _sign(key: bytes, message: str) -> bytes:
            return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

        signing_key = _sign(
            _sign(
                _sign(_sign(f"AWS4{self._secret_key}".encode("utf-8"), date_stamp), self._region),
                "s3",
            ),
            "aws4_request",
        )
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"https://127.0.0.1{path}"
        if query:
            url = f"{url}?{query}"
        req = urlrequest.Request(
            url,
            data=body or None,
            method=method,
            headers={
                "Host": self._host_header,
                "x-amz-content-sha256": payload_hash,
                "x-amz-date": amz_date,
                "Authorization": (
                    f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
                    f"SignedHeaders={signed_headers}, Signature={signature}"
                ),
            },
        )
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with urlrequest.urlopen(req, timeout=20, context=context) as response:  # noqa: S310
                return response.status, response.read().decode("utf-8", "ignore")
        except urlerror.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "ignore")


class DokployFilexBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        hostname: str,
        admin_email: str,
        admin_password: str,
        s3_endpoint_url: str,
        s3_internal_host: str,
        s3_access_key: str,
        s3_secret_key: str,
        bucket: str = FILEX_BUCKET,
        state_dir: Path = Path(".dokploy-wizard-state"),
        client: DokployFilexApi | None = None,
        s3_client: S3Requester | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._s3_endpoint_url = s3_endpoint_url
        self._s3_internal_host = s3_internal_host
        self._s3_access_key = s3_access_key
        self._s3_secret_key = s3_secret_key
        self._bucket = bucket
        self._secret_key_secret_ref = _secret_key_secret_ref(stack_name)
        self._state_dir = state_dir
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._s3_client = s3_client
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False

    # -- DocuSeal-shaped backend surface -------------------------------------

    def get_service(self, resource_id: str) -> FilexResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return FilexResourceRecord(
            resource_id=resource_id,
            resource_name=_service_name(self._stack_name),
        )

    def find_service_by_name(self, resource_name: str) -> FilexResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return FilexResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def create_service(self, **kwargs: object) -> FilexResourceRecord:
        resource_name = str(kwargs["resource_name"])
        hostname = str(kwargs["hostname"])
        bucket = str(kwargs["bucket"])
        data_resource_name = str(kwargs["data_resource_name"])
        if resource_name != _service_name(self._stack_name):
            raise FilexError("FileX service name does not match the active Dokploy plan.")
        if hostname != self._hostname:
            raise FilexError("FileX hostname no longer matches the active Dokploy plan.")
        if bucket != self._bucket:
            raise FilexError("FileX bucket no longer matches the active Dokploy plan.")
        if data_resource_name != _data_name(self._stack_name):
            raise FilexError("FileX data resource name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return FilexResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def update_service(self, **kwargs: object) -> FilexResourceRecord:
        kwargs.pop("resource_id", None)
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> FilexResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return FilexResourceRecord(
            resource_id=resource_id,
            resource_name=_data_name(self._stack_name),
        )

    def find_persistent_data_by_name(self, resource_name: str) -> FilexResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return FilexResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def create_persistent_data(self, resource_name: str) -> FilexResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise FilexError("FileX data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return FilexResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def check_health(self, *, service: FilexResourceRecord, url: str) -> bool:
        del service
        if _local_https_health_check(url):
            return True
        return False

    def ensure_application_ready(
        self, *, secret_key_secret_ref: str
    ) -> tuple[FilexBootstrapState, tuple[str, ...]]:
        if secret_key_secret_ref != self._secret_key_secret_ref:
            raise FilexError(
                "FileX FILEX_SECRET_KEY secret ref no longer matches the active Dokploy plan."
            )
        health_url = _health_url(self._hostname)
        if not _wait_for_container_up(self._compose_name):
            raise FilexError(
                "FileX container did not come up before seeding (image pull not finished?).",
            )
        if not _wait_for_local_https_health(health_url):
            raise FilexError(
                "FileX /healthz endpoint did not become locally reachable before seeding."
            )
        notes: list[str] = []
        bucket_ready, versioning_enabled, s3_notes = self._ensure_bucket_and_versioning()
        notes.extend(s3_notes)
        login_verified, admin_session, login_note = self._probe_admin_login()
        if login_note is not None:
            notes.append(login_note)
        provisioner, provisioner_note = self._ensure_provisioner(admin_session)
        if provisioner_note is not None:
            notes.append(provisioner_note)
        del provisioner  # persisted to its own 0600 state document, not the phase result
        return (
            FilexBootstrapState(
                initialized=True,
                secret_key_secret_ref=secret_key_secret_ref,
                bucket_ready=bucket_ready,
                versioning_enabled=versioning_enabled,
                login_verified=login_verified,
            ),
            tuple(notes),
        )

    # -- bucket provisioning ---------------------------------------------------

    def _s3(self) -> S3Requester:
        if self._s3_client is None:
            self._s3_client = LoopbackSigV4Client(
                host_header=self._s3_internal_host,
                access_key=self._s3_access_key,
                secret_key=self._s3_secret_key,
            )
        return self._s3_client

    def _ensure_bucket_and_versioning(
        self, *, attempts: int = 12, delay_seconds: float = 5.0
    ) -> tuple[bool, bool, tuple[str, ...]]:
        s3 = self._s3()
        bucket_path = f"/{self._bucket}/"
        notes: list[str] = []
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                status, body = s3.request("HEAD", bucket_path)
                if status == 404:
                    create_status, create_body = s3.request("PUT", bucket_path)
                    if create_status not in {200, 409} and "BucketAlreadyOwnedByYou" not in create_body:
                        raise FilexError(
                            f"Creating FileX bucket '{self._bucket}' failed with HTTP {create_status}: {create_body[:200]}"
                        )
                    notes.append(f"Created FileX storage bucket '{self._bucket}'.")
                elif status >= 500 or status == 403:
                    raise _TransientS3Error(f"HEAD bucket returned HTTP {status}")
                version_status, version_body = s3.request("GET", bucket_path, query="versioning=")
                if "<Status>Enabled" in version_body:
                    return True, True, tuple(notes)
                put_status, put_body = s3.request(
                    "PUT", bucket_path, query="versioning=", body=_VERSIONING_XML.encode("utf-8")
                )
                if put_status != 200:
                    raise FilexError(
                        f"Enabling versioning on FileX bucket '{self._bucket}' failed with HTTP {put_status}: {put_body[:200]}"
                    )
                notes.append(
                    f"Enabled native object versioning on FileX bucket '{self._bucket}'."
                )
                return True, True, tuple(notes)
            except _TransientS3Error as error:
                last_error = error
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
        raise FilexError(
            f"SeaweedFS S3 endpoint (internal host '{self._s3_internal_host}') did not become reachable for bucket provisioning: {last_error}"
        )

    def _probe_admin_login(self) -> tuple[bool, str | None]:
        payload = json.dumps(
            {"email": self._admin_email, "password": self._admin_password}
        ).encode("utf-8")
        req = urlrequest.Request(
            "https://127.0.0.1/api/auth/login",
            data=payload,
            method="POST",
            headers={
                "Host": self._hostname,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlrequest.urlopen(req, timeout=20, context=_https_context()) as response:  # noqa: S310
                status = response.status
                body = _json_or_none(response.read())
        except urlerror.HTTPError as exc:
            status = exc.code
            body = None
        except (urlerror.URLError, TimeoutError):
            return False, None, "FileX admin login probe could not reach the service; skipped."
        if status == 200 and isinstance(body, dict) and isinstance(body.get("token"), str):
            return (
                True,
                body["token"],
                "FileX admin login verified with the seeded credentials.",
            )
        if status in {401, 403, 429}:
            return (
                False,
                None,
                "FileX admin credentials differ from the environment seed or the login is rate-limited; stored values own the account.",
            )
        return False, None, f"FileX admin login probe answered HTTP {status}; skipped."

    # -- provisioner service account ------------------------------------------------

    def _ensure_provisioner(
        self, admin_session: str | None
    ) -> tuple[FilexProvisionerState | None, str | None]:
        """Guarantee the wizard's FileX service account and its admin-scoped token.

        Workspace boot scripts use this token to materialize each owner's FileX
        account and per-(user, workspace) credentials at first boot, so every
        write carries both a person and a machine. /api/admin delegation is
        session-or-admin-scope only, hence the admin scope on this one account;
        everything below it (owner accounts, their tokens) is a plain user.
        """
        if admin_session is None:
            return (
                load_filex_provisioner(self._state_dir),
                "FileX provisioner step skipped: no admin session (login probe did not verify).",
            )
        email = f"{FILEX_PROVISIONER_USERNAME}@{self._hostname}"
        password = _provisioner_password(self._stack_name, self._secret_key_secret_ref)
        status, users = self._fx_api("GET", "/api/admin/users", token=admin_session)
        if status != 200:
            return (
                load_filex_provisioner(self._state_dir),
                f"FileX provisioner step skipped: user listing answered HTTP {status}.",
            )
        user = _find_user_by_username(users, FILEX_PROVISIONER_USERNAME)
        if user is None:
            status, created = self._fx_api(
                "POST",
                "/api/admin/users",
                token=admin_session,
                body={
                    "email": email,
                    "username": FILEX_PROVISIONER_USERNAME,
                    "role": "admin",
                    "password": password,
                },
            )
            if status not in {200, 201} or not isinstance(created, dict) or "id" not in created:
                detail = "" if isinstance(created, dict) else str(created)[:120]
                return (
                    None,
                    f"FileX provisioner account could not be created (HTTP {status}); {detail}",
                )
            user = created
        user_id = int(user["id"])

        existing = load_filex_provisioner(self._state_dir)
        status, tokens_payload = self._fx_api("GET", "/api/admin/ai-tokens", token=admin_session)
        rows: list[dict] = []
        if status == 200 and isinstance(tokens_payload, dict):
            rows = [r for r in tokens_payload.get("tokens", []) if isinstance(r, dict)]
        mine = [r for r in rows if r.get("label") == FILEX_PROVISIONER_USERNAME]

        if existing is not None and any(r.get("id") == existing.token_id for r in mine):
            alive_status, _ = self._fx_api("GET", "/api/auth/s3-keys", token=existing.token)
            if alive_status == 200:
                return existing, None

        # State lost, token rotated server-side, or the stored secret died:
        # label-match every provisioner row away and mint a replacement. The
        # token is shown exactly once by FileX, so the state document written
        # here is the durable copy.
        for row in mine:
            row_id = row.get("id")
            if row_id is not None:
                self._fx_api("DELETE", f"/api/admin/ai-tokens/{row_id}", token=admin_session)
        status, minted = self._fx_api(
            "POST",
            "/api/admin/ai-tokens",
            token=admin_session,
            body={
                "label": FILEX_PROVISIONER_USERNAME,
                "user_id": user_id,
                # read/write/delete for file attribution flows; admin for the
                # /api/admin delegation the workspace provisioner scripts ride.
                "scopes": "read,write,delete,admin",
                "kind": "user",
            },
        )
        token = minted.get("token") if isinstance(minted, dict) else None
        row = minted.get("row") if isinstance(minted, dict) else None
        # FileX answers 201 Created for the mint (measured live).
        if status not in (200, 201) or not isinstance(token, str) or not isinstance(row, dict):
            return None, f"FileX provisioner token mint failed (HTTP {status}); ~/shared mounts will warn."
        state = FilexProvisionerState(
            format_version=STATE_FORMAT_VERSION,
            username=FILEX_PROVISIONER_USERNAME,
            email=email,
            password=password,
            user_id=user_id,
            token=token,
            token_id=int(row.get("id", 0) or 0),
        )
        write_filex_provisioner(self._state_dir, state)
        note = (
            "Provisioned FileX service account 'wizard-provisioner' with an admin-scoped "
            "token (workspace ~/shared identity automation)."
        )
        if existing is not None:
            note = (
                "Rotated the FileX provisioner token (stored secret or server row was gone); "
                "workspace mounts adopt the new key on their next boot."
            )
        return state, note

    def _fx_api(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict | None = None,
    ) -> tuple[int | None, object]:
        """One FileX API call through the loopback edge (UFW blocks host->overlay)."""
        headers = {"Host": self._hostname, "Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urlrequest.Request(f"https://127.0.0.1{path}", data=data, method=method, headers=headers)
        try:
            with urlrequest.urlopen(req, timeout=20, context=_https_context()) as response:  # noqa: S310
                return response.status, _json_or_none(response.read())
        except urlerror.HTTPError as exc:
            return exc.code, _json_or_none(exc.read())
        except (urlerror.URLError, TimeoutError):
            return None, None

    # -- compose application ----------------------------------------------------

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise FilexError(str(error)) from error
        for project in projects:
            if project.name != self._stack_name:
                continue
            environment = _pick_environment(project)
            if environment is None:
                continue
            for compose in environment.composes:
                if compose.name == self._compose_name:
                    locator = _ComposeLocator(
                        project_id=project.project_id,
                        environment_id=environment.environment_id,
                        compose_id=compose.compose_id,
                    )
                    self._applied_locator = locator
                    return locator
        return None

    def _ensure_compose_applied(self) -> _ComposeLocator:
        if self._applied_locator is not None and self._created_in_process:
            return self._applied_locator
        compose_file = _render_compose_file(
            stack_name=self._stack_name,
            hostname=self._hostname,
            admin_email=self._admin_email,
            admin_password=self._admin_password,
            s3_endpoint_url=self._s3_endpoint_url,
            bucket=self._bucket,
            s3_access_key=self._s3_access_key,
            s3_secret_key=self._s3_secret_key,
            secret_key_secret_ref=self._secret_key_secret_ref,
        )
        try:
            if self._applied_locator is not None:
                current_locator = self._applied_locator
                result = apply_compose_noop_guard(
                    rendered_compose=compose_file,
                    service_key=self._compose_name,
                    state_dir=self._state_dir,
                    client=self._client,
                    locator=current_locator,
                    compose_id=current_locator.compose_id,
                    title="dokploy-wizard filex reconcile",
                    description="Update FileX compose app",
                    verify_current=self._verify_current_application,
                    locator_factory=lambda compose_id: _ComposeLocator(
                        project_id=current_locator.project_id,
                        environment_id=current_locator.environment_id,
                        compose_id=compose_id,
                    ),
                )
                self._created_in_process = result.status == "applied"
                self._applied_locator = result.locator
                return self._applied_locator
            projects = self._client.list_projects()
            for project in projects:
                if project.name != self._stack_name:
                    continue
                environment = _pick_environment(project)
                if environment is None:
                    break
                for compose in environment.composes:
                    if compose.name == self._compose_name:
                        locator = _ComposeLocator(
                            project_id=project.project_id,
                            environment_id=environment.environment_id,
                            compose_id=compose.compose_id,
                        )
                        result = apply_compose_noop_guard(
                            rendered_compose=compose_file,
                            service_key=self._compose_name,
                            state_dir=self._state_dir,
                            client=self._client,
                            locator=locator,
                            compose_id=compose.compose_id,
                            title="dokploy-wizard filex reconcile",
                            description="Update FileX compose app",
                            verify_current=self._verify_current_application,
                            locator_factory=lambda compose_id: _ComposeLocator(
                                project_id=project.project_id,
                                environment_id=environment.environment_id,
                                compose_id=compose_id,
                            ),
                        )
                        self._created_in_process = result.status == "applied"
                        self._applied_locator = result.locator
                        return result.locator
                created = self._client.create_compose(
                    name=self._compose_name,
                    environment_id=environment.environment_id,
                    compose_file="services: {}\n",
                    app_name=self._compose_name,
                )
                apply_rendered_compose_to_existing(
                    client=self._client,
                    compose_id=created.compose_id,
                    rendered_compose=compose_file,
                )
                self._client.deploy_compose(
                    compose_id=created.compose_id,
                    title="dokploy-wizard filex reconcile",
                    description="Create FileX compose app",
                )
                locator = _ComposeLocator(
                    project_id=project.project_id,
                    environment_id=environment.environment_id,
                    compose_id=created.compose_id,
                )
                persist_compose_artifact_hash(
                    state_dir=self._state_dir,
                    service_key=self._compose_name,
                    rendered_compose=compose_file,
                )
                self._applied_locator = locator
                self._created_in_process = True
                return locator
            created_project = self._client.create_project(
                name=self._stack_name,
                description="Managed by dokploy-wizard",
                env=None,
            )
            created_compose = self._client.create_compose(
                name=self._compose_name,
                environment_id=created_project.environment_id,
                compose_file="services: {}\n",
                app_name=self._compose_name,
            )
            apply_rendered_compose_to_existing(
                client=self._client,
                compose_id=created_compose.compose_id,
                rendered_compose=compose_file,
            )
            self._client.deploy_compose(
                compose_id=created_compose.compose_id,
                title="dokploy-wizard filex reconcile",
                description="Create FileX compose app",
            )
        except DokployApiError as error:
            raise FilexError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        persist_compose_artifact_hash(
            state_dir=self._state_dir,
            service_key=self._compose_name,
            rendered_compose=compose_file,
        )
        self._created_in_process = True
        self._applied_locator = locator
        return locator

    def _verify_current_application(self) -> ServiceVerificationResult:
        try:
            bootstrap_state, _notes = self.ensure_application_ready(
                secret_key_secret_ref=self._secret_key_secret_ref,
            )
        except FilexError as error:
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="app",
                status="fail",
                detail=str(error),
            )
        if not bootstrap_state.bucket_ready or not bootstrap_state.versioning_enabled:
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="app",
                status="fail",
                detail="FileX bucket or bucket versioning is not ready.",
            )
        return ServiceVerificationResult(
            service_name=self._compose_name,
            tier="app",
            status="pass",
            detail="FileX runtime checks passed.",
        )

    def _verify_current_service(self) -> ServiceVerificationResult:
        is_up = _docker_container_is_up(self._compose_name)
        return ServiceVerificationResult(
            service_name=self._compose_name,
            tier="app",
            status="pass" if is_up else "fail",
            detail=f"FileX container for '{self._compose_name}' is {'up' if is_up else 'not up'}.",
            evidence_command=["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
        )


class _TransientS3Error(RuntimeError):
    """SeaweedFS S3 route not ready yet; bucket provisioning retries."""


def _resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:{kind}"


def _parse_resource_id(resource_id: str, kind: str) -> str | None:
    prefix = "dokploy-compose:"
    suffix = f":{kind}"
    if not resource_id.startswith(prefix) or not resource_id.endswith(suffix):
        return None
    compose_id = resource_id.removeprefix(prefix).removesuffix(suffix)
    return compose_id or None


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    if not project.environments:
        return None
    for environment in project.environments:
        if environment.is_default:
            return environment
    return project.environments[0]


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-filex"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-filex-data"


def _secret_key_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-filex-secret-key"


def _secret_key_value(stack_name: str, secret_key_secret_ref: str) -> str:
    material = f"dokploy-wizard:filex:{stack_name}:{secret_key_secret_ref}"
    return sha256(material.encode("utf-8")).hexdigest()


def _health_url(hostname: str) -> str:
    return f"https://{hostname}/healthz"


def _render_compose_file(
    *,
    stack_name: str,
    hostname: str,
    admin_email: str,
    admin_password: str,
    s3_endpoint_url: str,
    bucket: str,
    s3_access_key: str,
    s3_secret_key: str,
    secret_key_secret_ref: str,
) -> RenderedCompose:
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    health_url = _health_url(hostname)
    password_env = "FILEX_ADMIN_PASSWORD"
    secret_key_env = "FILEX_SECRET_KEY"
    s3_access_env = "FILEX_DEFAULT_STORAGE_S3_ACCESS_KEY"
    s3_secret_env = "FILEX_DEFAULT_STORAGE_S3_SECRET_KEY"
    secret_key_value = _secret_key_value(stack_name, secret_key_secret_ref)
    compose_file = (
        "services:\n"
        f"  {service_name}:\n"
        f"    image: {_DEFAULT_FILEX_IMAGE}\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        f"      FILEX_PUBLIC_URL: {_yaml_quote(f'https://{hostname}')}\n"
        f"      FILEX_ADMIN_EMAIL: {_yaml_quote(admin_email)}\n"
        f"      {password_env}: \"{_required_placeholder(password_env)}\"\n"
        f"      {secret_key_env}: \"{_required_placeholder(secret_key_env)}\"\n"
        "      FILEX_TRUSTED_PROXIES: auto\n"
        "      FILEX_DEFAULT_STORAGE_DRIVER: s3\n"
        '      FILEX_DEFAULT_STORAGE_NAME: "Files"\n'
        f"      FILEX_DEFAULT_STORAGE_S3_BUCKET: {_yaml_quote(bucket)}\n"
        f"      FILEX_DEFAULT_STORAGE_S3_PREFIX: {_yaml_quote(FILEX_STORAGE_PREFIX)}\n"
        f"      FILEX_DEFAULT_STORAGE_S3_ENDPOINT: {_yaml_quote(s3_endpoint_url)}\n"
        f"      FILEX_DEFAULT_STORAGE_S3_REGION: {_yaml_quote(FILEX_S3_REGION)}\n"
        f"      {s3_access_env}: \"{_required_placeholder(s3_access_env)}\"\n"
        f"      {s3_secret_env}: \"{_required_placeholder(s3_secret_env)}\"\n"
        "      FILEX_DEFAULT_STORAGE_S3_PATH_STYLE: \"true\"\n"
        "      FILEX_VERSIONS_ON_OVERWRITE: '1'\n"
        "      FILEX_TRASH_RETENTION_DAYS: '30'\n"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{service_name}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}.rule: "Host(`{hostname}`)"\n'
        f'      traefik.http.routers.{service_name}.middlewares: "{service_name}-forwarded-https"\n'
        f'      traefik.http.routers.{service_name}.tls: "true"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "{hostname}"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"\n'
        f'      traefik.http.services.{service_name}.loadbalancer.server.port: "{_DEFAULT_FILEX_PORT}"\n'
        "    volumes:\n"
        f"      - {data_name}:{_DEFAULT_FILEX_DATA_ROOT}\n"
        "    expose:\n"
        f"      - '{_DEFAULT_FILEX_PORT}'\n"
        "    networks:\n"
        "      - default\n"
        "      - dokploy-network\n"
        "      - shared\n"
        "volumes:\n"
        f"  {data_name}:\n"
        "networks:\n"
        "  dokploy-network:\n"
        "    external: true\n"
        # The shared overlay is created by the shared-core compose; attached
        # here (external) so Coder workspaces reach the app and its S3 door at
        # http://{stack}-filex:{port} — the ~/shared mount rides that door.
        "  shared:\n"
        "    external: true\n"
        f"    name: {stack_name}-shared\n"
        f"# Managed health endpoint: {health_url}\n"
    )
    return RenderedCompose(
        compose_file=compose_file,
        env_specs=(
            _filex_env_spec(
                name=password_env,
                value=admin_password,
                target_services=(service_name,),
                source="filex-admin-password",
            ),
            _filex_env_spec(
                name=secret_key_env,
                value=secret_key_value,
                target_services=(service_name,),
                source="filex-secret-key",
            ),
            _filex_env_spec(
                name=s3_access_env,
                value=s3_access_key,
                target_services=(service_name,),
                source="filex-s3-access-key",
            ),
            _filex_env_spec(
                name=s3_secret_env,
                value=s3_secret_key,
                target_services=(service_name,),
                source="filex-s3-secret-key",
            ),
        ),
    )


def _filex_env_spec(
    *, name: str, value: str, target_services: tuple[str, ...], source: str
) -> DokployEnvSpec:
    return DokployEnvSpec(
        variable=DokployEnvVar(name=name, value=value, sensitive=True, source=source),
        owner="filex",
        target_services=target_services,
        placeholder=_required_placeholder(name),
        required=True,
    )


def _required_placeholder(name: str) -> str:
    return f"${{{name}:?{name} is required}}"


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _provisioner_password(stack_name: str, secret_key_secret_ref: str) -> str:
    """Deterministic provisioner password: stable across reruns, derivable, no new secret."""
    material = f"dokploy-wizard:filex-provisioner:{stack_name}:{secret_key_secret_ref}"
    return sha256(material.encode("utf-8")).hexdigest()


def _find_user_by_username(payload: object, username: str) -> dict | None:
    rows: list = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("users", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
    for row in rows:
        if isinstance(row, dict) and row.get("username") == username:
            return row
    return None


def _json_or_none(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _https_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _local_https_health_check(url: str) -> bool:
    parsed = parse.urlsplit(url)
    if not parsed.hostname:
        return False
    request_path = parsed.path or "/"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    req = urlrequest.Request(
        f"https://127.0.0.1{request_path}",
        headers={"Host": parsed.hostname},
        method="GET",
    )
    try:
        with urlrequest.urlopen(req, timeout=15, context=_https_context()):  # noqa: S310
            return True
    except urlerror.HTTPError:
        return False
    except (urlerror.URLError, TimeoutError):
        return False


def _wait_for_local_https_health(
    url: str, *, attempts: int = 12, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _local_https_health_check(url):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _wait_for_container_up(
    service_name: str, *, attempts: int = 72, delay_seconds: float = 5.0
) -> bool:
    """Cold-install gate: a fresh box pulls the pack image DURING this phase.

    Dokploy's deploy is async and the health edge only answers once the task
    is scheduled and traefik noticed — measured on a true-zero proof, the
    60-second health budget lost that race. The container-up loop is the
    honest long wait (six minutes of pulls); the health check keeps its
    short budget for what it actually covers: the process inside coming to
    life and the edge catching up.
    """
    for attempt in range(attempts):
        if _docker_container_is_up(service_name):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _docker_container_is_up(service_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        name, _, status = line.partition("\t")
        if service_name not in name:
            continue
        return status.startswith("Up ")
    return False


__all__ = ["DokployFilexBackend", "LoopbackSigV4Client"]
