from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from dokploy_wizard.dokploy.coder_runtime_asset_kdense_types import KdenseRuntimeContract
from dokploy_wizard.remote_transport import (
    ParamikoRemoteTransport,
    RemoteCapturedCommandFailure,
    RemoteCommandCaptureLimits,
)
from dokploy_wizard.state.env import parse_env_file

_LIMITS = RemoteCommandCaptureLimits(1800, 1024, 1024)


@dataclass(frozen=True, slots=True)
class _EnvFingerprint:
    mode: int
    sha256: str


def run_remote_kdense_production_build(root: Path, contract: KdenseRuntimeContract) -> None:
    """Build patched pinned K-Dense only in an attempt-owned VPS directory."""

    env_path = root / ".install-min.env"
    before = _fingerprint(env_path)
    values = parse_env_file(env_path).values
    remote_root = f"/tmp/dokploy-wizard-task7-{uuid4().hex}"
    transport: ParamikoRemoteTransport | None = None
    try:
        transport = ParamikoRemoteTransport.connect(
            hostname=values["VPS_HOST"],
            username="root",
            password=values["VPS_ROOT_PASSWORD"],
            remote_root="/tmp",
        )
        with TemporaryDirectory(prefix="dokploy-wizard-task7-") as directory:
            local = Path(directory)
            script = local / "build.sh"
            script.write_text(_script(contract, remote_root), encoding="utf-8")
            script.chmod(0o700)
            transport.ensure_dir(remote_root)
            transport.upload(script, f"{remote_root}/build.sh")
            for patch in contract.patches:
                transport.upload(root / patch.path, f"{remote_root}/{Path(patch.path).name}")
            try:
                result = transport.capture(
                    "task7-build", f"sh {shlex.quote(f'{remote_root}/build.sh')}", _LIMITS
                )
            except RemoteCapturedCommandFailure as error:
                raise RuntimeError(_failure_stage(error.stderr)) from None
        if result.stdout != b"TASK7_STATUS=0\n":
            raise RuntimeError("remote K-Dense build marker is invalid")
    finally:
        if transport is not None:
            try:
                transport.capture("task7-cleanup", f"rm -rf -- {shlex.quote(remote_root)}", _LIMITS)
            finally:
                transport.close()
        if _fingerprint(env_path) != before:
            raise RuntimeError("install-min environment changed during remote K-Dense build")


def _fingerprint(path: Path) -> _EnvFingerprint:
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return _EnvFingerprint(path.stat().st_mode & 0o777, digest)


def _failure_stage(stderr: bytes) -> str:
    prefix = b"TASK7_FAILURE="
    for line in stderr.splitlines():
        if line.startswith(prefix):
            return line.decode("ascii", "strict")
    return "TASK7_FAILURE=unclassified"


def _script(contract: KdenseRuntimeContract, remote_root: str) -> str:
    node = contract.node
    cpython = contract.cpython
    uv = contract.uv
    records = "\n".join(
        f"verify {shlex.quote(path.sha256)} {shlex.quote(path.path)}"
        for path in contract.source_paths
    )
    amd64 = (
        f"  x86_64) node_url={shlex.quote(node.amd64.url)}; node_sha={node.amd64.sha256}; "
        f"node_root=node-{node.version}-linux-x64 ;;"
    )
    arm64 = (
        f"  aarch64) node_url={shlex.quote(node.arm64.url)}; node_sha={node.arm64.sha256}; "
        f"node_root=node-{node.version}-linux-arm64 ;;"
    )
    cpython_amd64 = (
        f"cpython_url={shlex.quote(cpython.amd64.url)}; cpython_sha={cpython.amd64.sha256};"
    )
    cpython_arm64 = (
        f"cpython_url={shlex.quote(cpython.arm64.url)}; cpython_sha={cpython.arm64.sha256};"
    )
    uv_amd64 = f"uv_url={shlex.quote(uv.amd64.url)}; uv_sha={uv.amd64.sha256};"
    uv_arm64 = f"uv_url={shlex.quote(uv.arm64.url)}; uv_sha={uv.arm64.sha256};"
    web_build = (
        '(cd web && "$node" "$npm" ci --no-audit --no-fund && "$node" "$npm" run test '
        '&& NEXT_PUBLIC_ADK_API_URL= "$node" "$npm" run build) > "$root/web.log" 2>&1'
    )
    server_test = (
        '(cd server && "$node" "$npm" ci --no-audit --no-fund && "$node" "$npm" run test -- '
        'test/credentials-central.test.ts test/latex-assist.test.ts test/methods-draft.test.ts '
        'test/models.test.ts test/speech.test.ts test/steer-abort.test.ts) '
        '> "$root/server.log" 2>&1'
    )
    return f'''set -eu
root={shlex.quote(remote_root)}
stage=source
trap 'status=$?; if [ "$status" -ne 0 ]; then printf "TASK7_FAILURE=%s\\n" "$stage" >&2; fi' EXIT
archive="$root/source.tar.gz"
curl -fsSL {shlex.quote(contract.codeload_url)} -o "$archive" >/dev/null 2>&1
tar -xzf "$archive" -C "$root" >/dev/null 2>&1
source="$root/{contract.archive_root}"
test ! -e "$source/pyproject.toml"
test ! -e "$source/prep_sandbox.py"
test -z "$(find "$source" -name uv.lock -print -quit)"
test -f "$source/server/src/prep.ts"
cd "$source"
verify() {{ printf '%s  %s/%s\\n' "$1" "$source" "$2" | sha256sum -c - >/dev/null; }}
{records}
stage=patch
patch --batch --forward --dry-run -p1 --input "$root/kdense-pricing-cache.patch" >/dev/null 2>&1
patch --batch --forward -p1 --input "$root/kdense-pricing-cache.patch" >/dev/null 2>&1
patch --batch --forward --dry-run -p1 --input "$root/kdense-central-only.patch" >/dev/null 2>&1
patch --batch --forward -p1 --input "$root/kdense-central-only.patch" >/dev/null 2>&1
case "$(uname -m)" in
{amd64}
{arm64}
  *) exit 1 ;;
esac
stage=node
curl -fsSL "$node_url" -o "$root/node.tar.xz" >/dev/null 2>&1
printf '%s  %s\\n' "$node_sha" "$root/node.tar.xz" | sha256sum -c - >/dev/null
tar -xJf "$root/node.tar.xz" -C "$root" >/dev/null 2>&1
node="$root/$node_root/bin/node"
npm="$root/$node_root/lib/node_modules/npm/bin/npm-cli.js"
stage=tool-checks
case "$(uname -m)" in
  x86_64) {cpython_amd64} {uv_amd64} ;;
  aarch64) {cpython_arm64} {uv_arm64} ;;
  *) exit 1 ;;
esac
curl -fsSL "$cpython_url" -o "$root/cpython.tar.gz" >/dev/null 2>&1
printf '%s  %s\n' "$cpython_sha" "$root/cpython.tar.gz" | sha256sum -c - >/dev/null
curl -fsSL "$uv_url" -o "$root/uv.tar.gz" >/dev/null 2>&1
printf '%s  %s\n' "$uv_sha" "$root/uv.tar.gz" | sha256sum -c - >/dev/null
PATH="$root/$node_root/bin:$PATH"
stage=server-test
{server_test}
stage=web-build
{web_build}
printf 'TASK7_STATUS=0\\n'
'''
