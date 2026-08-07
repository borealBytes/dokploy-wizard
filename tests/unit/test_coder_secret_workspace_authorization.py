from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import threading
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_authorization import (
    WorkspaceSupersessionContext,
    capture_workspace_supersession_authorization,
    require_workspace_supersession_context,
)
from dokploy_wizard.dokploy.coder_secret_workspace_contract import (
    WorkspaceVerificationIntent,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationReceiptStore,
)
from dokploy_wizard.dokploy.coder_secret_workspace_supersession import (
    supersede_exhausted_receipt,
)
from tests.unit.test_coder_secret_workspace_supersession import (
    _authorization,
    _exhausted_store,
)


class BlockingRunner:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release

    def __call__(self, command: tuple[str, ...]) -> str:
        del command
        self.entered.set()
        self.release.wait(timeout=5)
        return "[]"


class EmptyInventoryRunner:
    def __call__(self, command: tuple[str, ...]) -> str:
        del command
        return "[]"


def _context() -> WorkspaceSupersessionContext:
    return WorkspaceSupersessionContext(
        machine_sha256="1" * 64,
        ssh_sha256="2" * 64,
        lifecycle_sha256="3" * 64,
        stack_sha256="4" * 64,
        final_commit="5" * 40,
        attempt_context_sha256="6" * 64,
    )


def _intent() -> WorkspaceVerificationIntent:
    return WorkspaceVerificationIntent("b" * 64, "OPENAI_API_KEY", "a" * 64)


def test_capture_writes_private_exact_parent_authorization(tmp_path: Path) -> None:
    store, parent = _exhausted_store(tmp_path)
    output_dir = tmp_path / "evidence"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "authorization.json"

    capture_workspace_supersession_authorization(store.state_dir, output, _context())

    payload = json.loads(output.read_text())
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert base64.b64decode(payload["parent_receipt_base64"], validate=True) == parent
    assert payload["schema_version"] == 1


def test_capture_reuses_only_the_exact_authorization(tmp_path: Path) -> None:
    store, _parent = _exhausted_store(tmp_path)
    output_dir = tmp_path / "evidence"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "authorization.json"
    capture_workspace_supersession_authorization(store.state_dir, output, _context())
    expected = output.read_bytes()

    capture_workspace_supersession_authorization(store.state_dir, output, _context())

    assert output.read_bytes() == expected


def test_authorization_context_must_match_canonical_task(tmp_path: Path) -> None:
    store, _parent = _exhausted_store(tmp_path)
    output_dir = tmp_path / "evidence"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "authorization.json"
    capture_workspace_supersession_authorization(store.state_dir, output, _context())
    wrong_context = WorkspaceSupersessionContext(
        machine_sha256="9" * 64,
        ssh_sha256="2" * 64,
        lifecycle_sha256="3" * 64,
        stack_sha256="4" * 64,
        final_commit="5" * 40,
        attempt_context_sha256="6" * 64,
    )

    with pytest.raises(CoderSecretClientError, match="cannot be captured"):
        require_workspace_supersession_context(output, wrong_context)


def test_atomic_write_failure_preserves_exact_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, parent = _exhausted_store(tmp_path)
    authorization = _authorization(tmp_path, parent)

    def fail_write(_receipt: object) -> None:
        raise OSError("injected atomic write failure")

    monkeypatch.setattr(store, "write", fail_write)

    with pytest.raises(OSError, match="injected atomic write failure"):
        supersede_exhausted_receipt(
            lambda _command: "[]", store, _intent(), authorization
        )

    assert store.load_bytes() == parent


def test_external_authorization_environment_enables_store_supersession(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, parent = _exhausted_store(tmp_path)
    authorization = _authorization(tmp_path, parent)
    receipt = store.load()
    assert receipt is not None
    monkeypatch.setenv(
        "DOKPLOY_WIZARD_CODER_VERIFIER_SUPERSESSION_AUTHORIZATION",
        str(authorization),
    )

    successor = store.supersede_authorized(
        EmptyInventoryRunner(), receipt, _intent()
    )

    assert successor.protocol_revision == 2
    assert successor.predecessor_receipt_bytes == parent


def test_concurrent_supersession_has_one_successor_and_no_fork(tmp_path: Path) -> None:
    store, parent = _exhausted_store(tmp_path)
    authorization = _authorization(tmp_path, parent)
    entered = threading.Event()
    release = threading.Event()

    runner = BlockingRunner(entered, release)

    def attempt() -> int:
        try:
            return supersede_exhausted_receipt(
                runner,
                WorkspaceVerificationReceiptStore(store.state_dir),
                _intent(),
                authorization,
            ).protocol_revision
        except CoderSecretClientError:
            return 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(attempt)
        assert entered.wait(timeout=5)
        second = executor.submit(attempt)
        release.set()
        results = (first.result(timeout=5), second.result(timeout=5))

    assert sorted(results) == [0, 2]
    receipt = store.load()
    assert receipt is not None
    assert receipt.protocol_revision == 2
    assert receipt.predecessor_receipt_bytes == parent
