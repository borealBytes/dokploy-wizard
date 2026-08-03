from __future__ import annotations

from tests.integration.coder_remote_contract_config import loopback_access_url


def test_access_url_matches_the_remote_loopback_publish_port() -> None:
    # Given
    port = 23117

    # When
    access_url = loopback_access_url(port)

    # Then
    assert access_url == "http://127.0.0.1:23117"
