"""Identity derivations shared across the packs.

The Coder first-user lane, the Gitea account spring, and the LiteLLM
workspace-key policy all normalize the same operator email into the same
login. The derivation lives in this stdlib-only module so every lane sees
one definition and no pack has to import another pack's private helper.
"""

from __future__ import annotations

import re


def username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", local).strip("-")
    return normalized or "admin"


def display_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return local.title() or "Dokploy Admin"
