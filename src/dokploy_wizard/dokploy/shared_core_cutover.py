"""Shared Core compose deployment adapter for LiteLLM database cutover."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dokploy_wizard.dokploy.env_spec import RenderedCompose
from dokploy_wizard.litellm.opencode_go_cutover_types import OpenCodeGoCutoverDeployment


@dataclass(frozen=True, slots=True)
class SharedCoreCutoverDeployment(OpenCodeGoCutoverDeployment):
    """Apply static or dynamic compose artifacts through one verified deployment seam."""

    transitional: RenderedCompose
    dynamic: RenderedCompose
    apply: Callable[[RenderedCompose], None]
    verify: Callable[[], None]

    def deploy_transitional(self) -> None:
        self.apply(self.transitional)

    def verify_transitional(self) -> None:
        self.verify()

    def deploy_dynamic(self) -> None:
        self.apply(self.dynamic)

    def verify_dynamic(self, aliases: tuple[str, ...]) -> None:
        del aliases
        self.verify()
