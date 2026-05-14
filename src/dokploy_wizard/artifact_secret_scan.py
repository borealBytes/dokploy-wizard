"""Scan collected proof artifacts for raw secret values."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dokploy_wizard.state import StateValidationError, parse_env_file
from dokploy_wizard.verification import key_is_sensitive

_MIN_SECRET_LENGTH = 4
_EXTRA_SECRET_KEY_RE = re.compile(
    r"(?:auth[_-]?key|authkey|credential|credentials|cookie|session)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class SecretCandidate:
    """One raw value that must not appear in shareable proof artifacts."""

    label: str
    value: str

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.value.encode("utf-8")).hexdigest()[:12]
        return f"sha256:{digest}"


@dataclass(frozen=True)
class LeakFinding:
    """Location of a raw secret value without containing the raw value itself."""

    path: str
    label: str
    fingerprint: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "label": self.label,
            "line": self.line,
            "path": self.path,
        }


@dataclass(frozen=True)
class ArtifactScanResult:
    """Summary of a proof artifact secret scan."""

    scanned_files: int
    skipped_secret_sources: tuple[str, ...]
    findings: tuple[LeakFinding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [finding.to_dict() for finding in self.findings],
            "passed": self.passed,
            "scanned_files": self.scanned_files,
            "skipped_secret_sources": list(self.skipped_secret_sources),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        candidates = collect_secret_candidates(
            env_file=args.env_file,
            secret_sources=tuple(args.secret_source),
        )
        result = scan_artifacts(
            artifact_roots=tuple(args.artifact_root),
            candidates=candidates,
            secret_source_paths=_source_paths(args.env_file, args.secret_source),
        )
        payload = result.to_dict()
        rendered = json.dumps(payload, indent=2, sort_keys=True)
        if args.json_output is not None:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0 if result.passed else 1
    except (OSError, StateValidationError, ValueError) as error:
        print(f"artifact-secret-scan: {error}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="artifact-secret-scan",
        description=(
            "Scan collected remote state/log/compose proof artifacts for raw secret values. "
            "Findings include only key labels and redacted fingerprints."
        ),
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        action="append",
        required=True,
        help="artifact directory or file to scan; may be provided more than once",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="operator .install.env source for sensitive KEY=VALUE denylist entries",
    )
    parser.add_argument(
        "--secret-source",
        type=Path,
        action="append",
        default=[],
        help=(
            "additional JSON/text/env file containing env-spec or generated secret values; "
            "the source file itself is skipped during scanning"
        ),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="optional path to save the JSON scan summary",
    )
    return parser


def collect_secret_candidates(
    *, env_file: Path | None, secret_sources: Sequence[Path] = ()
) -> tuple[SecretCandidate, ...]:
    candidates: list[SecretCandidate] = []
    if env_file is not None:
        candidates.extend(_candidates_from_env_file(env_file))
    for source in secret_sources:
        candidates.extend(_candidates_from_secret_source(source))
    return _dedupe_candidates(candidates)


def scan_artifacts(
    *,
    artifact_roots: Sequence[Path],
    candidates: Sequence[SecretCandidate],
    secret_source_paths: Sequence[Path] = (),
) -> ArtifactScanResult:
    if not artifact_roots:
        raise ValueError("at least one artifact root is required")
    source_paths = {path.resolve() for path in secret_source_paths}
    findings: list[LeakFinding] = []
    scanned_files = 0
    for artifact_path in _iter_artifact_files(artifact_roots):
        resolved = artifact_path.resolve()
        if resolved in source_paths:
            continue
        text = _read_text_artifact(artifact_path)
        if text is None:
            continue
        scanned_files += 1
        findings.extend(_find_leaks(path=artifact_path, text=text, candidates=candidates))
    return ArtifactScanResult(
        scanned_files=scanned_files,
        skipped_secret_sources=tuple(str(path) for path in sorted(source_paths)),
        findings=tuple(findings),
    )


def _candidates_from_env_file(env_file: Path) -> tuple[SecretCandidate, ...]:
    raw_env = parse_env_file(env_file)
    return tuple(
        SecretCandidate(label=f"env:{key}", value=value)
        for key, value in raw_env.values.items()
        if _is_secret_key(key) and _is_scanworthy_secret_value(value)
    )


def _candidates_from_secret_source(source: Path) -> tuple[SecretCandidate, ...]:
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() == ".json":
        payload = json.loads(text)
        return tuple(_walk_json_secret_values(payload, label=source.name, parent_sensitive=False))
    return tuple(_walk_text_secret_values(text, label=source.name))


def _walk_json_secret_values(
    value: Any, *, label: str, parent_sensitive: bool
) -> Iterable[SecretCandidate]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            sensitive = parent_sensitive or _is_secret_key(key_text)
            child_label = f"{label}:{key_text}"
            yield from _walk_json_secret_values(
                item,
                label=child_label,
                parent_sensitive=sensitive,
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_json_secret_values(
                item,
                label=f"{label}[{index}]",
                parent_sensitive=parent_sensitive,
            )
        return
    if isinstance(value, str) and parent_sensitive and _is_scanworthy_secret_value(value):
        yield SecretCandidate(label=label, value=value)


def _walk_text_secret_values(text: str, *, label: str) -> Iterable[SecretCandidate]:
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped == "" or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if _is_secret_key(key) and _is_scanworthy_secret_value(value):
            yield SecretCandidate(label=f"{label}:{key}:line{line_number}", value=value)


def _find_leaks(
    *, path: Path, text: str, candidates: Sequence[SecretCandidate]
) -> tuple[LeakFinding, ...]:
    findings: list[LeakFinding] = []
    for candidate in candidates:
        start = 0
        while True:
            index = text.find(candidate.value, start)
            if index == -1:
                break
            line = text.count("\n", 0, index) + 1
            findings.append(
                LeakFinding(
                    path=str(path),
                    label=candidate.label,
                    fingerprint=candidate.fingerprint,
                    line=line,
                )
            )
            start = index + len(candidate.value)
    return tuple(findings)


def _iter_artifact_files(artifact_roots: Sequence[Path]) -> Iterable[Path]:
    for root in artifact_roots:
        if root.is_file():
            yield root
            continue
        if not root.exists():
            raise ValueError(f"artifact root does not exist: {root}")
        if not root.is_dir():
            raise ValueError(f"artifact root is not a file or directory: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_file():
                yield path


def _read_text_artifact(path: Path) -> str | None:
    payload = path.read_bytes()
    if b"\x00" in payload:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("utf-8", errors="replace")


def _dedupe_candidates(candidates: Sequence[SecretCandidate]) -> tuple[SecretCandidate, ...]:
    by_value: dict[str, SecretCandidate] = {}
    for candidate in candidates:
        by_value.setdefault(candidate.value, candidate)
    return tuple(sorted(by_value.values(), key=lambda item: item.label))


def _source_paths(env_file: Path | None, secret_sources: Sequence[Path]) -> tuple[Path, ...]:
    paths = [*secret_sources]
    if env_file is not None:
        paths.append(env_file)
    return tuple(paths)


def _is_secret_key(key: str) -> bool:
    return key_is_sensitive(key) or _EXTRA_SECRET_KEY_RE.search(key) is not None


def _is_scanworthy_secret_value(value: str) -> bool:
    normalized = value.lower()
    return (
        value != ""
        and len(value) >= _MIN_SECRET_LENGTH
        and normalized not in {"true", "false", "none", "null", "<redacted>"}
        and not normalized.startswith("sha256:")
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
