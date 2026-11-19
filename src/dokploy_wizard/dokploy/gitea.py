# mypy: ignore-errors
# ruff: noqa: E501
"""Dokploy-backed Gitea runtime backend.

Gitea runs as the official rootless image: repositories live on a named volume
(``/var/lib/gitea`` + ``/etc/gitea`` on one volume), the database is a per-pack
allocation on the shared-core Postgres, and LFS objects plus attachments are
redirected to the ``gitea`` bucket on the stack's SeaweedFS gateway. Three
responsibilities live here:

* render the compose app (install-locked config, public route, internal SSH);
* provision the bucket itself over SigV4 through the loopback edge, exactly
  like FileX does (UFW on the box blocks host->overlay TCP);
* seed the wizard's admin account through the in-container CLI (an
  install-locked Gitea has no signup path) and mint its API token once.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest

from dokploy_wizard.core.models import SharedPostgresAllocation
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
from dokploy_wizard.dokploy.filex import LoopbackSigV4Client
from dokploy_wizard.identity import username_from_email
from dokploy_wizard.packs.gitea import (
    GITEA_BUCKET,
    GITEA_S3_REGION,
    GiteaBootstrapState,
    GiteaError,
    GiteaResourceRecord,
)
from dokploy_wizard.state.models import GiteaProvisionerState
from dokploy_wizard.state.store import (
    load_gitea_provisioner,
    write_gitea_provisioner,
)
from dokploy_wizard.verification import ServiceVerificationResult

# Pinned by digest so a rerun can never silently swap the binary under the
# ledger. Same digest serves 28.4-rootless and latest-rootless.
_DEFAULT_GITEA_IMAGE = (
    "gitea/gitea:28.4.0-rootless"
    "@sha256:15ab7fadb44981a2b781f40c712cad07066197f395100ea1c6326e14e5bb073a"
)
_DEFAULT_GITEA_PORT = "3000"
_DEFAULT_GITEA_SSH_PORT = "2222"
_GITEA_DATA_ROOT = "/var/lib/gitea"
_GITEA_CONFIG_ROOT = "/etc/gitea"
_GITEA_RUN_USER = "1000:1000"
_GITEA_HEALTH_PATH = "/api/healthz"
_GITEA_SHARED_DB_PASSWORD = "change-me"
_PROVISIONER_TOKEN_NAME = "wizard-provisioner"
# The service account this pack used to run its lanes as is dead: the
# deployer's own admin account (login derived silently from the admin
# email's local part) now owns the token, so the account list shows humans
# only. The constant lives on for the migration path — a modify pass finds
# and deletes the old row once the replacement stands.
_LEGACY_PROVISIONER_USERNAME = "dokploy-wizard"
# Admin user list pagination. The single-row admin GET is dead on the
# deployed Gitea 28.4 (405, measured), so the paged list is the only alive
# read lane; these mirror the coder phase's identity-sync lane (30/page,
# 20 pages).
_GITEA_LIST_PAGE_SIZE = 30
_GITEA_LIST_MAX_PAGES = 20
# Admin account, full write access: the provisioner drives identity automation
# (users, orgs, keys, OIDC bindings) from the box side. Gitea 28's scope grammar
# takes the bare "all" grant; the "category:access" forms (all:all, write:all,
# admin:write) are rejected with 400 — measured against the live API.
_PROVISIONER_TOKEN_SCOPES = "all"


class DokployGiteaApi(Protocol):
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


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


class DokployGiteaBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        s3_endpoint_url: str,
        s3_internal_host: str,
        s3_access_key: str,
        s3_secret_key: str,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_from_address: str | None = None,
        admin_email: str = "",
        admin_password: str = "",
        bucket: str = GITEA_BUCKET,
        state_dir: Path = Path(".dokploy-wizard-state"),
        client: DokployGiteaApi | None = None,
        s3_client: object | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
        self._postgres_service_name = postgres_service_name
        self._postgres = postgres
        self._s3_endpoint_url = s3_endpoint_url
        self._s3_internal_host = s3_internal_host
        self._s3_access_key = s3_access_key
        self._s3_secret_key = s3_secret_key
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_from_address = smtp_from_address
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._bucket = bucket
        self._secret_key_secret_ref = _secret_key_secret_ref(stack_name)
        self._state_dir = state_dir
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._s3_client = s3_client
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False

    # -- DocuSeal-shaped backend surface -------------------------------------

    def get_service(self, resource_id: str) -> GiteaResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return GiteaResourceRecord(
            resource_id=resource_id,
            resource_name=_service_name(self._stack_name),
        )

    def find_service_by_name(self, resource_name: str) -> GiteaResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return GiteaResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def create_service(self, **kwargs: object) -> GiteaResourceRecord:
        resource_name = str(kwargs["resource_name"])
        hostname = str(kwargs["hostname"])
        postgres_service_name = str(kwargs["postgres_service_name"])
        postgres = kwargs["postgres"]
        data_resource_name = str(kwargs["data_resource_name"])
        if resource_name != _service_name(self._stack_name):
            raise GiteaError("Gitea service name does not match the active Dokploy plan.")
        if hostname != self._hostname:
            raise GiteaError("Gitea hostname no longer matches the active Dokploy plan.")
        if postgres_service_name != self._postgres_service_name or postgres != self._postgres:
            raise GiteaError("Gitea postgres inputs no longer match the active Dokploy plan.")
        if data_resource_name != _data_name(self._stack_name):
            raise GiteaError("Gitea data resource name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return GiteaResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"),
            resource_name=resource_name,
        )

    def update_service(self, **kwargs: object) -> GiteaResourceRecord:
        kwargs.pop("resource_id", None)
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> GiteaResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return GiteaResourceRecord(
            resource_id=resource_id,
            resource_name=_data_name(self._stack_name),
        )

    def find_persistent_data_by_name(self, resource_name: str) -> GiteaResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return GiteaResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def create_persistent_data(self, resource_name: str) -> GiteaResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise GiteaError("Gitea data resource name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return GiteaResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"),
            resource_name=resource_name,
        )

    def check_health(self, *, service: GiteaResourceRecord, url: str) -> bool:
        del service
        return _local_https_health_check(url)

    # -- application readiness --------------------------------------------------

    def ensure_application_ready(
        self, *, secret_key_secret_ref: str
    ) -> tuple[GiteaBootstrapState, tuple[str, ...]]:
        if secret_key_secret_ref != self._secret_key_secret_ref:
            raise GiteaError(
                "Gitea SECRET_KEY secret ref no longer matches the active Dokploy plan."
            )
        notes: list[str] = []
        # Gate on the container first: a first install seeds while Dokploy has
        # just deployed Gitea and the task may still be starting (measured on
        # the clean-slate proof: the in-container CLI saw 'no running
        # container', the seed degraded to a WARNING, and the coder phase's
        # sync then 401ed against the stale doc left by the wiped database).
        # The health wait proves the container is up AND serving, which both
        # the CLI seed and the basic-auth token mint require; while it has not
        # passed, seeding is skipped and the reconciler's health gate owns the
        # verdict (a not-ready service raises there — the next pass retries).
        initialized = _wait_for_local_https_health(_health_url(self._hostname))
        bucket_ready, s3_notes = self._ensure_bucket()
        notes.extend(s3_notes)
        provisioner_ready = False
        if initialized:
            provisioner_ready, provisioner_note = self._ensure_provisioner()
            if provisioner_note is not None:
                notes.append(provisioner_note)
            if provisioner_ready:
                notes.extend(self._migrate_identity_visibility())
        return (
            GiteaBootstrapState(
                initialized=initialized,
                secret_key_secret_ref=secret_key_secret_ref,
                bucket_ready=bucket_ready,
                provisioner_ready=provisioner_ready,
            ),
            tuple(notes),
        )

    # -- bucket provisioning ---------------------------------------------------

    def _s3(self) -> LoopbackSigV4Client:
        if self._s3_client is None:
            self._s3_client = LoopbackSigV4Client(
                host_header=self._s3_internal_host,
                access_key=self._s3_access_key,
                secret_key=self._s3_secret_key,
            )
        return self._s3_client  # type: ignore[no-any-return]

    def _ensure_bucket(
        self, *, attempts: int = 12, delay_seconds: float = 5.0
    ) -> tuple[bool, tuple[str, ...]]:
        s3 = self._s3()
        bucket_path = f"/{self._bucket}/"
        notes: list[str] = []
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                status, body = s3.request("HEAD", bucket_path)  # type: ignore[attr-defined]
                if status == 404:
                    create_status, create_body = s3.request("PUT", bucket_path)  # type: ignore[attr-defined]
                    if create_status not in {200, 409} and "BucketAlreadyOwnedByYou" not in create_body:
                        raise GiteaError(
                            f"Creating Gitea bucket '{self._bucket}' failed with HTTP {create_status}: {create_body[:200]}"
                        )
                    notes.append(f"Created Gitea storage bucket '{self._bucket}'.")
                elif status >= 500 or status == 403:
                    raise _TransientS3Error(f"HEAD bucket returned HTTP {status}")
                return True, tuple(notes)
            except (GiteaError, _TransientS3Error) as error:
                last_error = error
                if attempt < attempts - 1:
                    time.sleep(delay_seconds)
        del last_error
        # The gateway may be wedged; surface as an unready bucket instead of a
        # hard failure so the reconciler's health gate owns the verdict.
        notes.append(
            f"Gitea bucket '{self._bucket}' could not be verified yet; the S3 gateway may still be starting."
        )
        return False, tuple(notes)

    # -- provisioner account -----------------------------------------------------

    def _provisioner_login(self) -> str:
        # The deployer's Gitea login, derived silently from the admin email's
        # local part (no new env key) — the same derivation the Coder
        # first-user lane and the template's admin-login bake use. Their
        # Gitea password is the stack admin password: one credential, every
        # surface.
        return username_from_email(self._admin_email or "admin@example.com")

    def _ensure_provisioner(self) -> tuple[bool, str | None]:
        """Seed (or verify) the deployer's admin account and its API token.

        The wizard runs no bot account anymore: the person who installed the
        stack IS the provisioner. The account is created through the
        in-container CLI (an install-locked Gitea exposes no signup path),
        its password is pinned to the stack admin password on every pass,
        and the token is minted through the API over the loopback edge with
        basic auth, then re-verified on every later pass from the state
        document. A `dokploy-wizard` leftover from the previous generation is
        deleted once its replacement proves alive — and on upgrade boxes the
        deployer's own account is repaired through the legacy bridge, but
        only after the basic-auth mint is refused (see
        `_bridge_legacy_provisioner`): the refusal is the only must-change
        signal this Gitea build still gives — the list payload omits the
        flag and the single-row admin GET is gone (405, measured live).
        """
        login = self._provisioner_login()
        email = self._admin_email
        password = self._admin_password
        existing = load_gitea_provisioner(self._state_dir)
        if (
            existing is not None
            and existing.username == login
            and self._api_token_valid(existing.token, username=login)
        ):
            self._pin_provisioner_password(login=login, password=password)
            retire_note = self._retire_legacy_provisioner(login=login, token=existing.token)
            return True, retire_note

        create = _gitea_cli_exec(
            self._compose_name,
            [
                "admin",
                "user",
                "create",
                "--username",
                login,
                "--email",
                email,
                "--password",
                password,
                "--admin",
                "--must-change-password=false",
            ],
        )
        if not create.ok and not _cli_reports_existing(create.output):
            return False, f"WARNING: Gitea provisioner account could not be created: {create.output[:200]}"
        self._pin_provisioner_password(login=login, password=password)
        token = self._mint_provisioner_token(login=login, password=password)
        bridge_note = None
        if token is None:
            # The mint's refusal is the drift signal; the bridge repairs
            # through the legacy token and the mint retries once. A healthy
            # box never reaches the PATCH.
            bridge_note = self._bridge_legacy_provisioner(existing, login=login)
            if bridge_note is not None:
                token = self._mint_provisioner_token(login=login, password=password)
        if token is None:
            return False, "WARNING: Gitea provisioner API token could not be minted."
        retire_note = self._retire_legacy_provisioner(login=login, token=token)
        write_gitea_provisioner(
            self._state_dir,
            GiteaProvisionerState(
                format_version=1,
                username=login,
                email=email,
                password=password,
                token=token,
            ),
        )
        if existing is None or existing.username != login:
            note = f"Seeded Gitea admin account '{login}' with an API token."
        else:
            note = "Re-minted the Gitea provisioner API token after verifying the account."
        if bridge_note:
            note = f"{bridge_note} {note}"
        if retire_note:
            note = f"{note} {retire_note}"
        return True, note

    def _pin_provisioner_password(self, *, login: str, password: str) -> None:
        # Keep the account password equal to the stack password so the boot
        # lane's basic-auth (one credential across surfaces) survives a
        # rotation; the FileX lane PATCHes adopted accounts the same way on
        # every boot. The must-change flag rides explicitly as false: this
        # Gitea build defaults `change-password`'s --must-change-password to
        # TRUE (measured end to end on the live box — a flagless pin flipped
        # a freshly created flag-clear row back to must-change, and the
        # mint one step later died at 403 "You must change your password";
        # the same flag on the pin returned the row to false and basic auth
        # to 200). Without this flag the CLI create's
        # --must-change-password=false is undone by the pin on every pass.
        _gitea_cli_exec(
            self._compose_name,
            [
                "admin",
                "user",
                "change-password",
                "--username",
                login,
                "--password",
                password,
                "--must-change-password=false",
            ],
        )

    def _bridge_legacy_provisioner(
        self, existing: GiteaProvisionerState | None, *, login: str
    ) -> str | None:
        """Repair the deployer's account on upgrade boxes, through the legacy token.

        Box-measured: on a box the previous wizard generation ran, the
        deployer's Gitea account arrives non-admin AND carrying the
        must-change-password flag, and Gitea answers every basic-auth call
        with 403 "You must change your password" until the flag clears —
        the token mint is the victim (the CLI pin rewrites the password but
        does not touch the flag; measured twice). At mint time the only
        credential able to PATCH those columns is the legacy service
        account's token from the state document: it is still admin until
        the retire step below deletes it. One PATCH, both columns, the
        documented basic-auth lane then mints again — the whole sequence
        measured live (200/200/201). The gate is the mint refusal itself:
        this Gitea build answers the single-row admin GET with 405 (no
        pre-read possible), the list payload omits the flag, and the PATCH
        is idempotent — so the bridge fires only after a basic-auth mint
        was refused, and a healthy account never feels it. Boxes that never
        ran the previous generation have no legacy token and skip here —
        their create+pin lane carries --must-change-password=false on both
        commands (measured the necessity on the live box: this Gitea build
        defaults change-password's flag to TRUE, so a flagless pin re-stamps
        the row the create just cleared, and the mint dies at 403).
        """
        if existing is None or existing.username == login:
            return None
        if login == _LEGACY_PROVISIONER_USERNAME:
            return None  # the deployer IS the legacy account: nothing to bridge
        if not self._api_token_valid(existing.token, username=existing.username):
            return None
        status, _body = self._gitea_api(
            "PATCH",
            f"/api/v1/admin/users/{parse.quote(login, safe='')}",
            token=existing.token,
            body={"must_change_password": False, "admin": True},
        )
        if status != 200:
            return None
        return (
            f"Repaired the deployer's Gitea account '{login}' through the legacy "
            f"'{_LEGACY_PROVISIONER_USERNAME}' bridge (admin hat, must-change flag)."
        )

    def _gitea_admin_user_rows(self, *, token: str) -> tuple[list[dict], int | None]:
        """Paged admin user list through the loopback edge.

        The single-row admin GET does not exist on this build (405,
        measured — see `_migrate_identity_visibility`); the paged list is
        the alive read lane and carries per-row `visibility`. Returns
        (rows, failed_page_status). Pagination mirrors the coder phase's
        identity-sync lane (page size 30, 20 pages).
        """
        rows: list[dict] = []
        page = 1
        while page <= _GITEA_LIST_MAX_PAGES:
            status, payload = self._gitea_api(
                "GET", f"/api/v1/admin/users?page={page}", token=token
            )
            if status != 200 or not isinstance(payload, list):
                return rows, status
            rows.extend(row for row in payload if isinstance(row, dict))
            if len(payload) < _GITEA_LIST_PAGE_SIZE:
                return rows, None
            page += 1
        return rows, None

    @staticmethod
    def _gitea_row_login(row: dict) -> str:
        return str(row.get("username") or row.get("login") or "").lower()

    def _retire_legacy_provisioner(self, *, login: str, token: str) -> str | None:
        """Delete the previous generation's service account, once.

        The replacement token is the authority: the old row is looked up
        through the paged admin list (an unreadable list reads as absent —
        the next pass retries) and deleted through the CLI. An account
        that still owns repositories survives the pass with a visible
        warning rather than a surprise purge — the wizard never deletes
        what a human may have moved there.
        """
        if login == _LEGACY_PROVISIONER_USERNAME:
            return None
        rows, _failure = self._gitea_admin_user_rows(token=token)
        if not any(self._gitea_row_login(row) == _LEGACY_PROVISIONER_USERNAME for row in rows):
            return None
        delete = _gitea_cli_exec(
            self._compose_name,
            ["admin", "user", "delete", "--username", _LEGACY_PROVISIONER_USERNAME],
        )
        if delete.ok:
            return (
                f"Deleted the legacy Gitea service account "
                f"'{_LEGACY_PROVISIONER_USERNAME}' (its token now rides '{login}')."
            )
        return (
            f"WARNING: legacy Gitea account '{_LEGACY_PROVISIONER_USERNAME}' could not be "
            f"deleted ({delete.output[:120]}); it likely still owns repositories."
        )

    def _mint_provisioner_token(self, *, login: str, password: str) -> str | None:
        basic = f"{login}:{password}"
        body = {"name": _PROVISIONER_TOKEN_NAME, "scopes": [_PROVISIONER_TOKEN_SCOPES]}
        status, payload = self._gitea_api(
            "POST", f"/api/v1/users/{login}/tokens", basic=basic, body=body
        )
        if status == 201 and isinstance(payload, dict) and payload.get("sha1"):
            return str(payload["sha1"])
        if status == 409:
            # A stale token row from a lost state document: drop and re-mint.
            self._gitea_api(
                "DELETE", f"/api/v1/users/{login}/tokens/{_PROVISIONER_TOKEN_NAME}", basic=basic
            )
            status, payload = self._gitea_api(
                "POST", f"/api/v1/users/{login}/tokens", basic=basic, body=body
            )
            if status == 201 and isinstance(payload, dict) and payload.get("sha1"):
                return str(payload["sha1"])
        return None

    def _api_token_valid(self, token: str, *, username: str) -> bool:
        status, payload = self._gitea_api("GET", "/api/v1/user", token=token)
        return status == 200 and isinstance(payload, dict) and payload.get("username") == username

    def _migrate_identity_visibility(self) -> list[str]:
        """Flip wizard-owned accounts to private visibility (privacy default).

        Accounts sprung before the privacy default shipped stand public on
        the box — and so does the fresh CLI-seeded deployer row: the admin
        CLI does not consult `DEFAULT_USER_VISIBILITY` (measured, the
        from-zero install's first pass booted 'clayton' public despite the
        option). The same ledger that authorizes the enable/disable sync
        authorizes this flip, plus the deployer's own seeded account (the
        pack created it, so it needs no ledger claim). Rows are read
        through the paged admin list — the single-row admin GET answers 405
        on the deployed Gitea 28.4 (measured; the lane that rode it silently
        skipped for a full pass, which is why a failed list read says so
        rather than continuing) — and only rows that are not already
        private are PATCHed; the PATCH route and its 200-on-visibility
        behavior are measured against the deployed Gitea 28.4.
        """
        from dokploy_wizard.state.store import load_gitea_identities

        existing = load_gitea_provisioner(self._state_dir)
        if existing is None:
            return []
        stored = load_gitea_identities(self._state_dir)
        targets = {login.lower() for login in (stored.usernames if stored else ())}
        targets.add(existing.username.lower())
        rows, list_failure = self._gitea_admin_user_rows(token=existing.token)
        if list_failure is not None:
            return [
                "Skipped the visibility migration: Gitea admin user list failed "
                f"(HTTP {list_failure})."
            ]
        visibilities = {
            self._gitea_row_login(row): str(row.get("visibility") or "").lower()
            for row in rows
        }
        notes: list[str] = []
        for login in sorted(targets):
            if visibilities.get(login) is None:
                continue  # row gone (manual delete): nothing to flip
            if visibilities[login] == "private":
                continue
            status, _body = self._gitea_api(
                "PATCH",
                f"/api/v1/admin/users/{parse.quote(login, safe='')}",
                token=existing.token,
                body={"visibility": "private"},
            )
            if status == 200:
                notes.append(f"Set Gitea account '{login}' visibility to private (privacy default).")
        return notes

    def _gitea_api(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        basic: str | None = None,
        body: dict | None = None,
    ) -> tuple[int | None, object]:
        """One Gitea API call through the loopback edge (UFW blocks host->overlay)."""
        import base64

        headers = {"Host": self._hostname, "Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Token {token}"
        elif basic is not None:
            encoded = base64.b64encode(basic.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
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
        for project in self._client.list_projects():
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
            postgres_service_name=self._postgres_service_name,
            postgres=self._postgres,
            s3_endpoint_url=self._s3_endpoint_url,
            bucket=self._bucket,
            s3_access_key=self._s3_access_key,
            s3_secret_key=self._s3_secret_key,
            secret_key_secret_ref=self._secret_key_secret_ref,
            smtp_host=self._smtp_host,
            smtp_port=self._smtp_port,
            smtp_from_address=self._smtp_from_address,
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
                    title="dokploy-wizard gitea reconcile",
                    description="Update Gitea compose app",
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
                            title="dokploy-wizard gitea reconcile",
                            description="Update Gitea compose app",
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
                    title="dokploy-wizard gitea reconcile",
                    description="Create Gitea compose app",
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
                title="dokploy-wizard gitea reconcile",
                description="Create Gitea compose app",
            )
        except DokployApiError as error:
            raise GiteaError(str(error)) from error
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
        except GiteaError as error:
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="app",
                status="fail",
                detail=str(error),
            )
        if not bootstrap_state.bucket_ready or not bootstrap_state.provisioner_ready:
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="app",
                status="fail",
                detail="Gitea bucket or provisioner account is not ready.",
            )
        if not bootstrap_state.initialized:
            return ServiceVerificationResult(
                service_name=self._compose_name,
                tier="app",
                status="fail",
                detail="Gitea /api/healthz endpoint is not answering.",
            )
        return ServiceVerificationResult(
            service_name=self._compose_name,
            tier="app",
            status="pass",
            detail="Gitea runtime checks passed.",
        )

    def _verify_current_service(self) -> ServiceVerificationResult:
        is_up = _docker_container_is_up(self._compose_name)
        return ServiceVerificationResult(
            service_name=self._compose_name,
            tier="app",
            status="pass" if is_up else "fail",
            detail=f"Gitea container for '{self._compose_name}' is {'up' if is_up else 'not up'}.",
            evidence_command=["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
        )


class _TransientS3Error(RuntimeError):
    """SeaweedFS S3 route not ready yet; bucket provisioning retries."""


@dataclass(frozen=True)
class _CliResult:
    ok: bool
    output: str


def _gitea_cli_exec(service_name: str, args: list[str]) -> _CliResult:
    """Run `gitea <args>` as the rootless user inside the service container."""
    container_id = _find_running_container(service_name)
    if container_id is None:
        return _CliResult(ok=False, output=f"no running container for service '{service_name}'")
    command = [
        "docker",
        "exec",
        "--user",
        _GITEA_RUN_USER,
        container_id,
        "gitea",
        "-c",
        f"{_GITEA_CONFIG_ROOT}/app.ini",
        *args,
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return _CliResult(ok=False, output=str(error))
    output = (result.stdout + result.stderr).strip()
    return _CliResult(ok=result.returncode == 0, output=output)


def _cli_reports_existing(output: str) -> bool:
    lowered = output.lower()
    return "already exists" in lowered or "is already taken" in lowered or "name is taken" in lowered


def _find_running_container(service_name: str) -> str | None:
    # Dokploy deploys compose apps as their own projects: container names are
    # '{project}-{service}-{replica}' (project carries a random suffix), not the
    # swarm '<service>.<slot>.<task>' shape. The compose service label is the
    # stable identity; the same lookup the in-container CLI lanes have always
    # used (Gitea seeds through it, and FileX health gates on it).
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.service={service_name}",
                "--format",
                "{{.ID}}\t{{.Names}}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    candidates: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        container_id, _, name = line.partition("\t")
        if container_id.strip() and name.strip():
            candidates.append((container_id.strip(), name.strip()))
    if not candidates:
        return None
    for container_id, name in candidates:
        if name == service_name or name.startswith(f"{service_name}."):
            return container_id
    for container_id, name in candidates:
        if name.endswith(f"-{service_name}-1"):
            return container_id
    return candidates[0][0]


def _resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:{kind}"


def _parse_resource_id(resource_id: str, kind: str) -> str | None:
    prefix = "dokploy-compose:"
    if not resource_id.startswith(prefix):
        return None
    _, _, remainder = resource_id.partition(prefix)
    compose_id, separator, tail = remainder.partition(":")
    if not separator or tail != kind or not compose_id:
        return None
    return compose_id


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    return next((environment for environment in project.environments), None)


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-gitea"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-gitea-data"


def _secret_key_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-gitea-secret-key"


def _secret_key_value(stack_name: str, secret_key_secret_ref: str) -> str:
    material = f"dokploy-wizard:gitea:{stack_name}:{secret_key_secret_ref}"
    return sha256(material.encode("utf-8")).hexdigest()


def _health_url(hostname: str) -> str:
    return f"https://{hostname}{_GITEA_HEALTH_PATH}"


def _render_compose_file(
    *,
    stack_name: str,
    hostname: str,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    s3_endpoint_url: str,
    bucket: str,
    s3_access_key: str,
    s3_secret_key: str,
    secret_key_secret_ref: str,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_from_address: str | None = None,
) -> RenderedCompose:
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    health_url = _health_url(hostname)
    db_password_env = "GITEA_DB_PASSWORD"
    secret_key_env = "GITEA_SECRET_KEY"
    s3_access_env = "GITEA_S3_ACCESS_KEY"
    s3_secret_env = "GITEA_S3_SECRET_KEY"
    secret_key_value = _secret_key_value(stack_name, secret_key_secret_ref)
    # Gitea's S3 driver takes a bare host:port and a USE_SSL flag; strip the
    # scheme from the stack endpoint the wizard standardizes on internally.
    s3_host = s3_endpoint_url.split("://", 1)[-1]
    mailer_lines: list[str] = []
    if smtp_host is not None and smtp_port is not None and smtp_from_address is not None:
        # SMTP_ADDR is looked up as a bare hostname — a host:port value fails
        # resolution with "no such host" (measured live). The port belongs in
        # SMTP_PORT.
        mailer_lines = [
            "      GITEA__mailer__ENABLED: 'true'\n",
            f"      GITEA__mailer__SMTP_ADDR: {_yaml_quote(smtp_host)}\n",
            f"      GITEA__mailer__SMTP_PORT: '{smtp_port}'\n",
            f"      GITEA__mailer__FROM: {_yaml_quote(smtp_from_address)}\n",
            "      GITEA__mailer__PROTOCOL: 'smtp'\n",
        ]
    else:
        mailer_lines = ["      GITEA__mailer__ENABLED: 'false'\n"]
    compose_file = (
        "services:\n"
        f"  {service_name}:\n"
        f"    image: {_DEFAULT_GITEA_IMAGE}\n"
        "    restart: unless-stopped\n"
        f"    user: {_GITEA_RUN_USER}\n"
        "    environment:\n"
        f"      GITEA__database__DB_TYPE: postgres\n"
        f"      GITEA__database__HOST: {_yaml_quote(f'{postgres_service_name}:5432')}\n"
        f"      GITEA__database__NAME: {_yaml_quote(postgres.database_name)}\n"
        f"      GITEA__database__USER: {_yaml_quote(postgres.user_name)}\n"
        f"      {db_password_env}: \"{_required_placeholder(db_password_env)}\"\n"
        f"      GITEA__database__PASSWD: \"{_required_placeholder(db_password_env)}\"\n"
        "      GITEA__database__SSL_MODE: disable\n"
        f"      GITEA__server__ROOT_URL: {_yaml_quote(f'https://{hostname}/')}\n"
        f"      GITEA__server__SSH_DOMAIN: {_yaml_quote(hostname)}\n"
        f"      GITEA__server__SSH_PORT: '{_DEFAULT_GITEA_SSH_PORT}'\n"
        f"      GITEA__server__SSH_LISTEN_PORT: '{_DEFAULT_GITEA_SSH_PORT}'\n"
        "      GITEA__server__START_SSH_SERVER: 'true'\n"
        "      GITEA__server__LFS_START_SERVER: 'true'\n"
        "      GITEA__server__ENABLE_HEALTHCHECK: 'true'\n"
        "      GITEA__security__INSTALL_LOCK: 'true'\n"
        f"      {secret_key_env}: \"{_required_placeholder(secret_key_env)}\"\n"
        f"      GITEA__security__SECRET_KEY: \"{_required_placeholder(secret_key_env)}\"\n"
        "      GITEA__service__DISABLE_REGISTRATION: 'true'\n"
        # Private by default, opt-in public: new users see only their own
        # visibility options (nothing is force-published), freshly created
        # repos start private, and the explore page is closed to anonymous
        # web. A repo or profile the owner makes public stays public — the
        # point is that nothing leaks before the owner chooses, not that the
        # box is hermetic. (Identities arrive only through Coder's roster,
        # so registration has stayed off since the pack began.)
        "      GITEA__options__DEFAULT_USER_VISIBILITY: private\n"
        "      GITEA__repository__DEFAULT_PRIVATE: 'true'\n"
        "      GITEA__explore__DISABLE_ANONYMOUS_ACCESS: 'true'\n"
        "      GITEA__repository__DEFAULT_BRANCH: main\n"
        # Storage redirection, modern (1.20+) schema measured live on the box:
        # the consumer sections name the driver and their bucket; the shared
        # [storage.minio] section carries the connection with MINIO_-prefixed
        # keys. The pre-1.20 spellings (consumer STORAGE=, unprefixed keys,
        # [minio] section) are silently ignored — attachments fall back to
        # local storage without a word in the log.
        "      GITEA__attachment__STORAGE_TYPE: minio\n"
        f"      GITEA__attachment__MINIO_BUCKET: {_yaml_quote(bucket)}\n"
        "      GITEA__lfs__STORAGE_TYPE: minio\n"
        f"      GITEA__lfs__MINIO_BUCKET: {_yaml_quote(bucket)}\n"
        "      GITEA__storage.minio__STORAGE_TYPE: minio\n"
        f"      GITEA__storage.minio__MINIO_ENDPOINT: {_yaml_quote(s3_host)}\n"
        f"      GITEA__storage.minio__MINIO_LOCATION: {_yaml_quote(GITEA_S3_REGION)}\n"
        "      GITEA__storage.minio__MINIO_PATH_STYLE: 'true'\n"
        "      GITEA__storage.minio__MINIO_USE_SSL: 'false'\n"
        f"      {s3_access_env}: \"{_required_placeholder(s3_access_env)}\"\n"
        f"      GITEA__storage.minio__MINIO_ACCESS_KEY_ID: \"{_required_placeholder(s3_access_env)}\"\n"
        f"      {s3_secret_env}: \"{_required_placeholder(s3_secret_env)}\"\n"
        f"      GITEA__storage.minio__MINIO_SECRET_ACCESS_KEY: \"{_required_placeholder(s3_secret_env)}\"\n"
        + "".join(mailer_lines) +
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{service_name}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}.rule: "Host(`{hostname}`)"\n'
        f'      traefik.http.routers.{service_name}.middlewares: "{service_name}-forwarded-https"\n'
        f'      traefik.http.routers.{service_name}.tls: "true"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "{hostname}"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"\n'
        f'      traefik.http.services.{service_name}.loadbalancer.server.port: "{_DEFAULT_GITEA_PORT}"\n'
        "    volumes:\n"
        f"      - {data_name}:{_GITEA_DATA_ROOT}\n"
        f"      - {data_name}:{_GITEA_CONFIG_ROOT}\n"
        "    expose:\n"
        f"      - '{_DEFAULT_GITEA_PORT}'\n"
        f"      - '{_DEFAULT_GITEA_SSH_PORT}'\n"
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
        # here (external) so Coder workspaces can reach the git remote at
        # http://{stack}-gitea:3000 over the private network.
        "  shared:\n"
        "    external: true\n"
        f"    name: {stack_name}-shared\n"
        f"# Managed health endpoint: {health_url}\n"
    )
    return RenderedCompose(
        compose_file=compose_file,
        env_specs=(
            _gitea_env_spec(
                name=db_password_env,
                value=_gitea_db_password(postgres),
                target_services=(service_name,),
                source="gitea-postgres-password",
            ),
            _gitea_env_spec(
                name=secret_key_env,
                value=secret_key_value,
                target_services=(service_name,),
                source="gitea-secret-key",
            ),
            _gitea_env_spec(
                name=s3_access_env,
                value=s3_access_key,
                target_services=(service_name,),
                source="gitea-s3-access-key",
            ),
            _gitea_env_spec(
                name=s3_secret_env,
                value=s3_secret_key,
                target_services=(service_name,),
                source="gitea-s3-secret-key",
            ),
        ),
    )


def _gitea_env_spec(
    *, name: str, value: str, target_services: tuple[str, ...], source: str
) -> DokployEnvSpec:
    return DokployEnvSpec(
        variable=DokployEnvVar(name=name, value=value, sensitive=True, source=source),
        owner="gitea",
        target_services=target_services,
        placeholder=_required_placeholder(name),
        required=True,
    )


def _gitea_db_password(postgres: SharedPostgresAllocation) -> str:
    # The shared-core init script provisions every per-pack role with the one
    # shared service password (overlay-only network, no host publish). Gitea
    # matches that literal rather than inventing a second secret.
    del postgres
    return _GITEA_SHARED_DB_PASSWORD


def _required_placeholder(name: str) -> str:
    return f"${{{name}:?{name} is required}}"


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _json_or_none(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _https_context():
    import ssl

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


def _docker_container_is_up(service_name: str) -> bool:
    return _find_running_container(service_name) is not None


__all__ = ["DokployGiteaBackend"]
