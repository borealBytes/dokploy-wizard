"""Confirmation validation for uninstall flows."""

from __future__ import annotations

from pathlib import Path


class UninstallConfirmationError(RuntimeError):
    """Raised when uninstall confirmation is missing or too weak."""


_WEAK_PHRASES = {"y", "yes", "ok", "okay", "confirm"}


def collect_confirmation_lines(
    *,
    non_interactive: bool,
    confirm_file: Path | None,
    mode: str,
    environment: str,
) -> tuple[str, ...]:
    if non_interactive:
        if confirm_file is None:
            raise UninstallConfirmationError(
                "Non-interactive uninstall requires --confirm-file with typed confirmations."
            )
        lines = _load_confirmation_file(confirm_file)
    else:
        lines = _prompt_confirmation_lines(mode=mode, environment=environment)

    _validate_confirmation_lines(lines=lines, mode=mode, environment=environment)
    return lines


def _load_confirmation_file(confirm_file: Path) -> tuple[str, ...]:
    content = confirm_file.read_text(encoding="utf-8")
    lines = tuple(
        line.strip()
        for line in content.splitlines()
        if line.strip() != "" and not line.lstrip().startswith("#")
    )
    if not lines:
        raise UninstallConfirmationError(
            f"Confirmation file '{confirm_file}' did not contain any usable confirmation text."
        )
    return lines


def _prompt_confirmation_lines(*, mode: str, environment: str) -> tuple[str, ...]:
    prompts: tuple[str, ...]
    if mode == "destroy":
        prompts = (
            "Type a first acknowledgement that you understand this is destructive: ",
            "Type a second acknowledgement that you want to destroy data: ",
            f"Type the final confirmation including destroy intent and '{environment}': ",
        )
    else:
        prompts = (
            f"Type a confirmation including uninstall intent, retain data, and '{environment}': ",
        )
    return tuple(input(prompt).strip() for prompt in prompts)


def _validate_confirmation_lines(*, lines: tuple[str, ...], mode: str, environment: str) -> None:
    normalized = tuple(line.casefold() for line in lines)
    for line in normalized:
        if line in _WEAK_PHRASES:
            raise UninstallConfirmationError(
                "Weak confirmation phrases like bare 'yes' are rejected for uninstall."
            )

    environment_token = environment.casefold()
    if mode == "retain":
        final_line = normalized[-1]
        if (
            "uninstall" not in final_line
            or "retain" not in final_line
            or "data" not in final_line
            or environment_token not in final_line
        ):
            raise UninstallConfirmationError(
                "Retain-data uninstall confirmation must mention uninstall intent, retain data, "
                f"and environment '{environment}'."
            )
        return

    if len(normalized) != 3:
        raise UninstallConfirmationError(
            "Destroy mode requires exactly three ordered confirmation lines."
        )
    if "understand" not in normalized[0]:
        raise UninstallConfirmationError(
            "Destroy confirmation line 1 must explicitly acknowledge understanding the risk."
        )
    if "destroy" not in normalized[1] or "data" not in normalized[1]:
        raise UninstallConfirmationError(
            "Destroy confirmation line 2 must explicitly state destroy-data intent."
        )
    if "destroy" not in normalized[2] or environment_token not in normalized[2]:
        raise UninstallConfirmationError(
            "Destroy confirmation line 3 must include destroy intent plus the environment "
            f"identifier '{environment}'."
        )
