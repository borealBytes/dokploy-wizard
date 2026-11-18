"""Stack upgrade drift reporting and gated LiteLLM image-pin application.

The `upgrade` command answers two questions and takes one gated action:

- ``--check`` compares every wizard-managed service's *running* image digest
  (from ``docker service``/``docker image`` inspection on the host) against the
  registry's current digest for the same tag, and reports the newest LiteLLM
  stable tag. Floating ``:latest`` labels lie, so digests are the only truth.
- Apply writes an operator-approved tag bump into the state-backed
  ``litellm-image-pin.json`` document, clears the shared-core compose artifact
  hash so the next pass is forced to redeploy, and reruns the modify flow.
- Rollback is the inverse operator action: write the previous tag back (or
  re-run ``upgrade --litellm <previous>``) and let modify redeploy. The
  pre-apply snapshot (state copy + shared-core ``pg_dumpall``) bounds the blast
  radius of either direction.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib import error, parse, request

from dokploy_wizard.dokploy.shared_core import (
    DEFAULT_LITELLM_IMAGE,
    DEFAULT_LITELLM_IMAGE_TAG,
    _shared_core_generated_secret,
)
from dokploy_wizard.state.models import (
    STATE_FORMAT_VERSION,
    AppliedStateCheckpoint,
    DesiredState,
    LiteLLMImagePin,
)
from dokploy_wizard.state.store import (
    load_litellm_image_pin,
    load_state_dir,
    write_applied_checkpoint,
    write_litellm_image_pin,
)

SNAPSHOTS_DIR_NAME = "snapshots"
_LITELLM_STABLE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:[.-](.+))?-stable$")
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
    )
)
_REGISTRY_TAG_PAGE_LIMIT = 20
_POSTGRES_DUMP_TIMEOUT_SECONDS = 300
_EXEC_TIMEOUT_SECONDS = 60


class UpgradeError(RuntimeError):
    """Raised when an upgrade apply cannot proceed (gates, registry, confirmation)."""


@dataclass(frozen=True)
class ImageRef:
    """Parsed ``repository:tag[@digest]`` reference."""

    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @property
    def tag_ref(self) -> str:
        if self.tag is None:
            return f"{self.registry}/{self.repository}"
        return f"{self.registry}/{self.repository}:{self.tag}"


def parse_image_ref(raw: str) -> ImageRef | None:
    """Parse a Docker image reference into registry/repository/tag/digest parts."""

    text = raw.strip()
    if text == "":
        return None
    digest: str | None = None
    if "@" in text:
        text, _, digest_part = text.rpartition("@")
        digest = digest_part if digest_part.startswith("sha256:") else None
    tag: str | None = None
    # A colon separates a tag only in the last path segment; a colon in an
    # earlier segment is a registry port (localhost:5000/foo/bar).
    if ":" in text.rsplit("/", 1)[-1]:
        text, _, tag_part = text.rpartition(":")
        tag = tag_part or None
    registry = "registry-1.docker.io"
    repository = text
    if "/" in text:
        first_segment, _, remainder = text.partition("/")
        if "." in first_segment or ":" in first_segment:
            registry = first_segment
            repository = remainder
    if registry == "docker.io":
        registry = "registry-1.docker.io"
    # Docker Hub single-segment names are library images.
    if registry == "registry-1.docker.io" and "/" not in repository:
        repository = f"library/{repository}"
    return ImageRef(registry=registry, repository=repository, tag=tag, digest=digest)


def stable_tag_sort_key(tag: str) -> tuple[int, int, int, tuple[str, ...]] | None:
    match = _LITELLM_STABLE_TAG_RE.match(tag)
    if match is None:
        return None
    major, minor, patch, suffix = match.groups()
    suffix_parts = tuple(re.split(r"[.\-]", suffix or ""))
    return (int(major), int(minor), int(patch), suffix_parts)


class ImageRegistry(Protocol):
    def manifest_digest(self, ref: ImageRef) -> str | None: ...

    def list_tags(self, ref: ImageRef) -> tuple[str, ...] | None: ...


class HttpImageRegistry:
    """Anonymous OCI/docker registry v2 client with bearer-challenge support."""

    def __init__(self, *, opener: Callable[..., Any] | None = None) -> None:
        self._opener = opener or _default_urlopen
        self._tokens: dict[tuple[str, str], str] = {}

    def manifest_digest(self, ref: ImageRef) -> str | None:
        url = f"https://{ref.registry}/v2/{ref.repository}/manifests/{ref.tag}"
        status, headers, _ = self._call(ref, url, headers={"Accept": _MANIFEST_ACCEPT})
        if status is None or status != 200:
            return None
        digest = _header_value(headers, "Docker-Content-Digest")
        return digest if isinstance(digest, str) and digest.startswith("sha256:") else None

    def list_tags(self, ref: ImageRef) -> tuple[str, ...] | None:
        tags: list[str] = []
        url: str | None = f"https://{ref.registry}/v2/{ref.repository}/tags/list"
        for _ in range(_REGISTRY_TAG_PAGE_LIMIT):
            if url is None:
                break
            status, headers, body = self._call(ref, url, headers={"Accept": "application/json"})
            if status is None or status != 200 or body is None:
                return None
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            page_tags = payload.get("tags") if isinstance(payload, dict) else None
            if isinstance(page_tags, list):
                tags.extend(str(item) for item in page_tags if isinstance(item, (str, int)))
            link = _parse_link_next(_header_value(headers, "Link"))
            # ghcr.io answers Link pagination with path-relative targets while
            # Docker Hub sends absolute URLs; resolve against the current page
            # so urllib always receives a fully qualified URL.
            url = parse.urljoin(url, link) if link is not None else None
        return tuple(tags)

    def _call(
        self, ref: ImageRef, url: str, *, headers: Mapping[str, str]
    ) -> tuple[int | None, Mapping[str, str], bytes | None]:
        request_headers = dict(headers)
        token = self._tokens.get((ref.registry, ref.repository))
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        status, response_headers, body = self._opener(url, request_headers)
        challenge = _header_value(response_headers, "WWW-Authenticate")
        if status == 401 and token is None and isinstance(challenge, str):
            token = self._exchange_token(ref, challenge)
            if token is not None:
                self._tokens[(ref.registry, ref.repository)] = token
                request_headers["Authorization"] = f"Bearer {token}"
                status, response_headers, body = self._opener(url, request_headers)
        return status, response_headers, body

    def _exchange_token(self, ref: ImageRef, challenge: str) -> str | None:
        parameters = _parse_bearer_challenge(challenge)
        realm = parameters.get("realm")
        if not realm:
            return None
        query: dict[str, str] = {}
        service = parameters.get("service")
        if service:
            query["service"] = service
        query["scope"] = parameters.get("scope") or f"repository:{ref.repository}:pull"
        url = f"{realm}?{parse.urlencode(query)}"
        status, _, body = self._opener(url, {"Accept": "application/json"})
        if status is None or status != 200 or body is None:
            return None
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        token_value = None
        if isinstance(payload, dict):
            raw_token = payload.get("token") or payload.get("access_token")
            if isinstance(raw_token, str) and raw_token != "":
                token_value = raw_token
        return token_value


def _default_urlopen(
    url: str, headers: Mapping[str, str]
) -> tuple[int | None, Mapping[str, str], bytes | None]:
    req = request.Request(url, headers=dict(headers), method="GET")
    try:
        with request.urlopen(req, timeout=30) as response:  # noqa: S310
            return response.status, dict(response.headers.items()), response.read()
    except error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, None
    except (error.URLError, TimeoutError, OSError):
        return None, {}, None


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name.lower():
            return value
    return None


def _parse_bearer_challenge(challenge: str) -> dict[str, str]:
    if not challenge.lower().startswith("bearer"):
        return {}
    parameters: dict[str, str] = {}
    for match in re.finditer(r'([A-Za-z]+)="([^"]*)"', challenge):
        parameters[match.group(1).lower()] = match.group(2)
    return parameters


def _parse_link_next(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if match is not None:
            return match.group(1)
    return None


class RuntimeProbe(Protocol):
    """Host-runtime facts the upgrade report needs; fake-friendly."""

    def service_image(self, service_name: str) -> str | None: ...

    def container_id_for_service(self, service_name: str) -> str | None: ...

    def exec(
        self, container_id: str, argv: Sequence[str], env: Mapping[str, str] = {}
    ) -> tuple[int, str, str] | None: ...

    def list_container_names(self) -> tuple[str, ...]: ...


class DockerCliProbe:
    """RuntimeProbe backed by the docker CLI on the host."""

    def service_image(self, service_name: str) -> str | None:
        result = _run_docker(
            [
                "docker",
                "service",
                "inspect",
                "--format",
                "{{.Spec.TaskTemplate.ContainerSpec.Image}}",
                service_name,
            ]
        )
        if result is None or result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()

    def container_id_for_service(self, service_name: str) -> str | None:
        for label in (
            f"com.docker.swarm.service.name={service_name}",
            f"com.docker.compose.service={service_name}",
        ):
            result = _run_docker(["docker", "ps", "-q", "--filter", f"label={label}"])
            if result is None or result.returncode != 0:
                continue
            ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if ids:
                return ids[0]
        return None

    def exec(
        self, container_id: str, argv: Sequence[str], env: Mapping[str, str] = {}
    ) -> tuple[int, str, str] | None:
        command = ["docker", "exec"]
        for key, value in env.items():
            command.extend(["-e", f"{key}={value}"])
        command.extend(["--workdir", "/app", container_id, *argv])
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=_EXEC_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.returncode, result.stdout, result.stderr

    def list_container_names(self) -> tuple[str, ...]:
        result = _run_docker(["docker", "ps", "--format", "{{.Names}}"])
        if result is None or result.returncode != 0:
            return ()
        return tuple(sorted(line.strip() for line in result.stdout.splitlines() if line.strip()))


def _run_docker(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError:
        return None


def apply_litellm_image_pin_to_env(
    *, litellm_env: dict[str, str], state_dir: Path
) -> dict[str, str]:
    """The single chokepoint that makes the state-backed pin win over the env.

    Every render site reads the image pair through ``LITELLM_IMAGE``/
    ``LITELLM_IMAGE_TAG``; rewriting the effective env map here keeps install,
    modify, and verification passes on the same pin without teaching each call
    site about the document.
    """

    pin = load_litellm_image_pin(state_dir)
    if pin is None:
        return litellm_env
    litellm_env["LITELLM_IMAGE"] = pin.image
    litellm_env["LITELLM_IMAGE_TAG"] = pin.tag
    return litellm_env


def litellm_effective_pin(*, litellm_env: Mapping[str, str], state_dir: Path) -> dict[str, str]:
    """Resolution order used by every render site: state pin > env > built-in default."""

    pin = load_litellm_image_pin(state_dir)
    if pin is not None:
        return {
            "image": pin.image,
            "tag": pin.tag,
            "source": "state-pin",
            "updated_at": pin.updated_at,
        }
    image = (
        litellm_env.get("LITELLM_IMAGE") or DEFAULT_LITELLM_IMAGE
    ).strip() or DEFAULT_LITELLM_IMAGE
    tag = (
        litellm_env.get("LITELLM_IMAGE_TAG") or DEFAULT_LITELLM_IMAGE_TAG
    ).strip() or DEFAULT_LITELLM_IMAGE_TAG
    has_env_override = "LITELLM_IMAGE_TAG" in litellm_env or "LITELLM_IMAGE" in litellm_env
    source = "env" if has_env_override else "default"
    return {"image": image, "tag": tag, "source": source}


def managed_service_candidates(desired_state: DesiredState) -> tuple[dict[str, str], ...]:
    """Service names the wizard owns, with the pack label each belongs to."""

    stack = desired_state.stack_name
    candidates: list[dict[str, str]] = []
    core = desired_state.shared_core
    if core.postgres is not None:
        candidates.append(
            {
                "service_name": core.postgres.service_name,
                "pack": "shared-core",
                "component": "postgres",
            }
        )
    if core.redis is not None:
        candidates.append(
            {
                "service_name": core.redis.service_name,
                "pack": "shared-core",
                "component": "redis",
            }
        )
    if core.mail_relay is not None:
        candidates.append(
            {
                "service_name": core.mail_relay.service_name,
                "pack": "shared-core",
                "component": "mail-relay",
            }
        )
    if core.litellm is not None:
        candidates.append(
            {
                "service_name": core.litellm.service_name,
                "pack": "shared-core",
                "component": "litellm",
            }
        )
    if core.model_sync is not None:
        candidates.append(
            {
                "service_name": core.model_sync.service_name,
                "pack": "shared-core",
                "component": "model-sync",
            }
        )
    candidates.append(
        {"service_name": f"{stack}-cloudflared", "pack": "networking", "component": "tunnel"}
    )
    pack_services = {
        "nextcloud": (f"{stack}-nextcloud", "nextcloud"),
        "onlyoffice": (f"{stack}-onlyoffice", "nextcloud"),
        "moodle": (f"{stack}-moodle", "moodle"),
        "docuseal": (f"{stack}-docuseal", "docuseal"),
        "coder": (f"{stack}-coder", "coder"),
        "seaweedfs": (f"{stack}-seaweedfs", "seaweedfs"),
        "matrix": (f"{stack}-matrix", "matrix"),
        "headscale": (f"{stack}-headscale", "headscale"),
    }
    enabled = set(desired_state.enabled_packs)
    for pack, (service_name, pack_label) in pack_services.items():
        if pack in enabled or (pack == "onlyoffice" and "nextcloud" in enabled):
            candidates.append({"service_name": service_name, "pack": pack_label, "component": pack})
    return tuple(candidates)


def build_upgrade_drift_report(
    *,
    desired_state: DesiredState,
    litellm_env: Mapping[str, str],
    state_dir: Path,
    registry: ImageRegistry,
    probe: RuntimeProbe,
) -> dict[str, Any]:
    """Read-only drift report: running digests vs registry, plus LiteLLM bump view."""

    images: list[dict[str, Any]] = []
    available = False
    for candidate in managed_service_candidates(desired_state):
        raw_ref = probe.service_image(candidate["service_name"])
        if raw_ref is None:
            images.append(
                {
                    **candidate,
                    "status": "missing",
                    "detail": "No running service image could be inspected.",
                }
            )
            continue
        available = True
        parsed = parse_image_ref(raw_ref)
        entry: dict[str, Any] = {
            **candidate,
            "status": "ok",
            "running_ref": raw_ref,
            "running_digest": parsed.digest if parsed is not None else None,
            "tag": parsed.tag if parsed is not None else None,
            "repository": (
                f"{parsed.registry}/{parsed.repository}" if parsed is not None else raw_ref
            ),
        }
        if parsed is not None and parsed.tag is not None:
            registry_digest = registry.manifest_digest(parsed)
            entry["registry_digest"] = registry_digest
            if entry["running_digest"] is not None and registry_digest is not None:
                stale = entry["running_digest"] != registry_digest
                entry["drift"] = "digest_stale" if stale else "current"
            elif parsed.digest is None:
                entry["drift"] = "tag_only_ref"
            else:
                entry["drift"] = "unknown"
        images.append(entry)

    return {
        "available": available,
        "detail": (
            "Compared running service image digests against registry digests."
            if available
            else "No wizard-managed Docker services were inspectable on this host."
        ),
        "images": images,
        "litellm": _build_litellm_section(
            desired_state=desired_state,
            litellm_env=litellm_env,
            state_dir=state_dir,
            registry=registry,
            probe=probe,
        ),
        "workspaces": _build_workspace_section(probe=probe, desired_state=desired_state),
    }


def _build_litellm_section(
    *,
    desired_state: DesiredState,
    litellm_env: Mapping[str, str],
    state_dir: Path,
    registry: ImageRegistry,
    probe: RuntimeProbe,
) -> dict[str, Any] | None:
    if desired_state.shared_core.litellm is None:
        return None
    effective = litellm_effective_pin(litellm_env=litellm_env, state_dir=state_dir)
    ref = parse_image_ref(f"{effective['image']}:{effective['tag']}")
    newest: str | None = None
    if ref is not None:
        tags = registry.list_tags(ref)
        if tags is not None:
            versions = [
                (stable_tag_sort_key(tag), tag)
                for tag in tags
                if stable_tag_sort_key(tag) is not None
            ]
            if versions:
                newest = max(versions)[1]
    current_key = stable_tag_sort_key(effective["tag"])
    newest_key = stable_tag_sort_key(newest) if newest is not None else None
    bump_available = (
        newest is not None
        and current_key is not None
        and newest_key is not None
        and newest_key > current_key
    )
    major_bump = bool(
        bump_available
        and current_key is not None
        and newest_key is not None
        and newest_key[0] > current_key[0]
    )
    return {
        "bump_available": bump_available,
        "current_digest": registry.manifest_digest(ref) if ref is not None else None,
        "current_image": effective["image"],
        "current_tag": effective["tag"],
        "major_bump": major_bump,
        "newest_stable_tag": newest,
        "pin_source": effective["source"],
        "running_digest": _running_digest(
            probe=probe,
            service_name=desired_state.shared_core.litellm.service_name,
        ),
    }


def _running_digest(*, probe: RuntimeProbe, service_name: str) -> str | None:
    raw_ref = probe.service_image(service_name)
    if raw_ref is None:
        return None
    parsed = parse_image_ref(raw_ref)
    return parsed.digest if parsed is not None else None


def _build_workspace_section(*, probe: RuntimeProbe, desired_state: DesiredState) -> dict[str, Any]:
    """Report-only Coder workspace inventory; never restarts anything here."""

    if "coder" not in desired_state.enabled_packs:
        return {"available": False, "detail": "Coder pack is not enabled.", "workspaces": []}
    service_name = f"{desired_state.stack_name}-coder"
    container_id = probe.container_id_for_service(service_name)
    if container_id is None:
        return {
            "available": False,
            "detail": f"Coder container for '{service_name}' was not found.",
            "workspaces": [],
        }
    result = probe.exec(container_id, ("/opt/coder", "list", "--output", "json"))
    if result is None or result[0] != 0:
        return {
            "available": False,
            "detail": (
                "coder list could not run inside the Coder container "
                "(the CLI needs a session token; this section is report-only)."
            ),
            "workspaces": [],
        }
    try:
        payload = json.loads(result[1] or "[]")
    except json.JSONDecodeError:
        return {
            "available": False,
            "detail": "coder workspaces ls returned unparseable output.",
            "workspaces": [],
        }
    workspaces = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            latest_build = item.get("latest_build") or {}
            status = item.get("status")
            if status is None and isinstance(latest_build, dict):
                status = latest_build.get("status")
            workspaces.append(
                {
                    "name": str(item.get("name", "")),
                    "status": str(status or ""),
                    "template": str(
                        item.get("template_name")
                        or (
                            latest_build.get("template_id", "")
                            if isinstance(latest_build, dict)
                            else ""
                        )
                    ),
                }
            )
    return {
        "available": True,
        "detail": (
            "Workspaces are report-only in this version; restart or recreate them explicitly."
        ),
        "workspaces": workspaces,
    }


def capture_upgrade_snapshot(
    *,
    state_dir: Path,
    desired_state: DesiredState,
    probe: RuntimeProbe,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Copy wizard state and dump the shared-core database into a pre-upgrade snapshot."""

    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    snapshot_dir = (
        state_dir / SNAPSHOTS_DIR_NAME / f"pre-upgrade-{timestamp.strftime('%Y%m%dT%H%M%SZ')}"
    )
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    snapshot: dict[str, Any] = {
        "captured_at": timestamp.isoformat(),
        "hermes": _capture_hermes_backup_note(probe=probe),
        "path": str(snapshot_dir),
        "postgres": {
            "status": "skipped",
            "detail": "Shared-core Postgres is not part of this stack.",
        },
        "state_copied": False,
    }
    shutil.copytree(
        state_dir,
        snapshot_dir / "state",
        ignore=shutil.ignore_patterns(SNAPSHOTS_DIR_NAME),
    )
    snapshot["state_copied"] = True
    core = desired_state.shared_core
    if core.postgres is not None:
        snapshot["postgres"] = _capture_postgres_dump(
            probe=probe, postgres_service=core.postgres.service_name, snapshot_dir=snapshot_dir
        )
    return snapshot


def _capture_postgres_dump(
    *, probe: RuntimeProbe, postgres_service: str, snapshot_dir: Path
) -> dict[str, Any]:
    container_id = probe.container_id_for_service(postgres_service)
    if container_id is None:
        return {
            "status": "skipped",
            "detail": f"No running container found for '{postgres_service}'.",
        }
    password = _shared_core_generated_secret(f"{postgres_service}-root-password")
    result = probe.exec(
        container_id, ("pg_dumpall", "-U", "postgres"), env={"PGPASSWORD": password}
    )
    if result is None:
        return {"status": "failed", "detail": "pg_dumpall could not execute."}
    returncode, stdout, stderr = result
    if returncode != 0:
        return {
            "status": "failed",
            "detail": f"pg_dumpall exited {returncode}: {stderr[-200:]}",
        }
    dump_path = snapshot_dir / "shared-core-postgres.sql.gz"
    dump_path.write_bytes(gzip.compress(stdout.encode("utf-8")))
    return {"dump_path": str(dump_path), "status": "captured"}


def _capture_hermes_backup_note(*, probe: RuntimeProbe) -> dict[str, Any]:
    hermes_containers = [name for name in probe.list_container_names() if "hermes" in name.lower()]
    if not hermes_containers:
        return {"status": "skipped", "detail": "No hermes gateway container found on this host."}
    notes: list[str] = []
    for container_name in hermes_containers:
        result = probe.exec(container_name, ("hermes", "backup"))
        if result is None:
            notes.append(f"{container_name}: hermes backup could not execute (best-effort).")
        elif result[0] != 0:
            notes.append(f"{container_name}: hermes backup exited {result[0]} (best-effort).")
        else:
            notes.append(f"{container_name}: hermes backup captured.")
    return {"detail": " ".join(notes), "status": "attempted"}


def confirm_upgrade_apply(
    *,
    non_interactive: bool,
    confirm_file: Path | None,
    prompt: Callable[[str], str] = input,
) -> tuple[str, ...]:
    """Gate the apply step; mirrors the uninstall typed-confirmation discipline."""

    weak_phrases = {"y", "yes", "ok", "okay", "confirm"}
    if non_interactive:
        if confirm_file is None:
            raise UpgradeError(
                "Non-interactive upgrade apply requires --confirm-file with a typed confirmation."
            )
        lines = tuple(
            line.strip()
            for line in confirm_file.read_text(encoding="utf-8").splitlines()
            if line.strip() != "" and not line.lstrip().startswith("#")
        )
    else:
        lines = (prompt("Type 'upgrade' to snapshot and apply this upgrade: "),)
    if not lines:
        raise UpgradeError("Upgrade confirmation did not contain any usable confirmation text.")
    for line in lines:
        if line.strip().lower() in weak_phrases:
            raise UpgradeError(
                f"Upgrade confirmation '{line}' is too weak; type the word 'upgrade' explicitly."
            )
    if not any("upgrade" in line.lower() for line in lines):
        raise UpgradeError("Upgrade confirmation must include the word 'upgrade'.")
    return lines


def resolve_litellm_bump_plan(
    *,
    desired_state: DesiredState,
    litellm_env: Mapping[str, str],
    state_dir: Path,
    registry: ImageRegistry,
    target_tag: str,
    allow_major: bool,
) -> dict[str, Any]:
    """Validate an operator-approved LiteLLM tag bump and describe the transition."""

    core = desired_state.shared_core
    if core.litellm is None:
        raise UpgradeError("This stack has no LiteLLM shared-core service to upgrade.")
    effective = litellm_effective_pin(litellm_env=litellm_env, state_dir=state_dir)
    target = parse_image_ref(f"{effective['image']}:{target_tag}")
    if target is None:
        raise UpgradeError(f"Target tag '{target_tag}' is not a valid image reference.")
    current_key = stable_tag_sort_key(effective["tag"])
    target_key = stable_tag_sort_key(target_tag)
    if target_key is None:
        raise UpgradeError(
            f"Target tag '{target_tag}' is not a stable release tag (vMAJOR.MINOR.PATCH-stable)."
        )
    digest = registry.manifest_digest(target)
    if digest is None:
        raise UpgradeError(
            f"Registry has no manifest for {effective['image']}:{target_tag}; "
            "the LiteLLM image supports no build for this tag."
        )
    if current_key is not None and target_key[0] != current_key[0] and not allow_major:
        direction = "bump" if target_key > current_key else "downgrade"
        raise UpgradeError(
            f"LiteLLM major {direction} {effective['tag']} -> {target_tag} requires --allow-major. "
            "Majors can change schema and config semantics; review the upstream release "
            "notes and the shared-core migration surface first."
        )
    return {
        "image": effective["image"],
        "previous_pin_source": effective["source"],
        "previous_tag": effective["tag"],
        "registry_digest": digest,
        "target_tag": target_tag,
    }


def apply_litellm_pin(
    *,
    state_dir: Path,
    plan: Mapping[str, Any],
    desired_state: DesiredState,
    now: datetime | None = None,
) -> LiteLLMImagePin:
    """Persist the new pin and force the next shared-core compose pass to redeploy."""

    pin = LiteLLMImagePin(
        format_version=STATE_FORMAT_VERSION,
        image=str(plan["image"]),
        tag=str(plan["target_tag"]),
        updated_at=(now or datetime.now(UTC)).astimezone(UTC).isoformat(),
    )
    write_litellm_image_pin(state_dir, pin)
    clear_compose_artifact_hash(state_dir, desired_state.shared_core.network_name)
    return pin


def clear_compose_artifact_hash(state_dir: Path, service_key: str) -> bool:
    """Drop a persisted compose artifact hash so the next pass is forced to deploy."""

    applied_state = load_state_dir(state_dir).applied_state
    if applied_state is None or service_key not in applied_state.compose_artifact_hashes:
        return False
    updated_hashes = {
        key: value
        for key, value in applied_state.compose_artifact_hashes.items()
        if key != service_key
    }
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=applied_state.format_version,
            desired_state_fingerprint=applied_state.desired_state_fingerprint,
            completed_steps=applied_state.completed_steps,
            compose_artifact_hashes=updated_hashes,
            lifecycle_checkpoint_contract_version=(
                applied_state.lifecycle_checkpoint_contract_version
            ),
        ),
    )
    return True
