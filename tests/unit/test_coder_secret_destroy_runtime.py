from __future__ import annotations

from pathlib import Path

from dokploy_wizard.dokploy.coder_secret_destroy import CoderSecretDestroyer

from .coder_secret_destroy_support import (
    OWNER_ID,
    SECRET_IDENTITIES,
    SecretDestroyClient,
    metadata,
    write_completed_secret_receipt,
)


def test_destroy_deletes_only_exact_receipted_secret_ids_after_teardown(tmp_path: Path) -> None:
    write_completed_secret_receipt(tmp_path)
    extra = metadata(
        (
            "00000000-0000-4000-8000-000000000099",
            "unowned-secret",
            "UNOWNED_SECRET",
            "Unowned",
        )
    )
    client = SecretDestroyClient([*(metadata(identity) for identity in SECRET_IDENTITIES), extra])

    CoderSecretDestroyer(state_dir=tmp_path, client=client, owner_id=OWNER_ID).destroy()

    assert client.secrets == [extra]
    assert [event for event in client.events if event.startswith("delete:")] == [
        f"delete:{identity[0]}" for identity in SECRET_IDENTITIES
    ]
    assert client.events.count("list") == 15
    assert (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert (tmp_path / "coder-secret-destroy-receipts-v1.json").exists()
