from __future__ import annotations

from collections.abc import Callable

import pytest

from dokploy_wizard.proof import model_sync_task1_post_install_probe as post_install_probe
from dokploy_wizard.proof.model_sync_identity import RemoteProbe, RemoteProofError


def _probe() -> RemoteProbe:
    return RemoteProbe(
        machine_sha256="a" * 64,
        ssh_sha256="b" * 64,
        boot_sha256="c" * 64,
        architecture="amd64",
        namespace_clean=True,
        inventory={"cloudflare": ()},
        plane_states={"cloudflare": "present"},
    )


def test_capture_post_install_probe_retries_transient_collector_failure() -> None:
    # Given
    expected = _probe()
    attempts = 0
    delays: list[float] = []

    def capture() -> RemoteProbe:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RemoteProofError("remote preflight transport failed")
        return expected

    # When
    actual = post_install_probe.capture_post_install_probe(capture, delays.append)

    # Then
    assert actual is expected
    assert attempts == 3
    assert delays == [10.0, 10.0]


def test_capture_post_install_probe_preserves_terminal_collector_failure() -> None:
    # Given
    attempts = 0

    def capture() -> RemoteProbe:
        nonlocal attempts
        attempts += 1
        raise RemoteProofError("remote preflight transport failed")

    def ignore_sleep(_delay: float) -> None:
        return None

    sleep: Callable[[float], None] = ignore_sleep

    # When / Then
    with pytest.raises(RemoteProofError, match="remote preflight transport failed"):
        post_install_probe.capture_post_install_probe(capture, sleep)
    assert attempts == 24
