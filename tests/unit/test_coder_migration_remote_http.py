from __future__ import annotations

from tests.integration.coder_migration_remote_http import ReadinessTiming, wait_for_coder_readiness
from tests.integration.coder_migration_remote_protocol import (
    FirstUserMissing,
    RemoteCoderResponse,
)


def test_readiness_retries_health_before_first_user_probe() -> None:
    # Given
    responses = iter(
        (
            RemoteCoderResponse(status=503, body=b"", headers=()),
            RemoteCoderResponse(status=200, body=b"OK", headers=()),
            RemoteCoderResponse(status=200, body=b'{"version":"v2"}', headers=()),
            RemoteCoderResponse(
                status=404,
                body=b'{"message":"not found"}',
                headers=(("x-coder-build-version", "v2"),),
            ),
        )
    )
    paths: list[str] = []

    def read(path: str) -> RemoteCoderResponse:
        paths.append(path)
        return next(responses)

    def sleep(_: float) -> None:
        return None

    # When
    result = wait_for_coder_readiness(read, ReadinessTiming(120, lambda: 0.0, sleep))

    # Then
    assert result == FirstUserMissing(build_version="v2")
    assert paths == ["/healthz", "/healthz", "/api/v2/buildinfo", "/api/v2/users/first"]
