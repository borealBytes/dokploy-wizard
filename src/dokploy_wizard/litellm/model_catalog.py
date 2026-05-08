from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

DEFAULT_LOCAL_CANONICAL_ALIAS = "tuxdesktop.tailb12aa5.ts.net/unsloth-active"
DEFAULT_LOCAL_UPSTREAM_TARGET = "openai/unsloth-active"

_KNOWN_UPSTREAM_PROVIDERS = frozenset(
    {
        "openai",
        "azure",
        "anthropic",
        "bedrock",
        "vertex_ai",
        "gemini",
        "nvidia",
        "huggingface",
        "ollama",
        "openrouter",
    }
)
_LEGACY_UPSTREAM_TARGETS = {
    "nvidia/moonshot/kimi-k2.5": "nvidia/moonshotai/kimi-k2.5",
}


@dataclass(frozen=True)
class ModelCostMetadata:
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None


@dataclass(frozen=True)
class ModelCatalogEntry:
    alias: str
    upstream_target: str
    provider_slug: str
    model_id: str
    visible_to_consumers: tuple[str, ...]
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None


@dataclass(frozen=True)
class ModelCatalog:
    entries: tuple[ModelCatalogEntry, ...]
    default_alias_order: tuple[str, ...]
    _entries_by_alias: Mapping[str, ModelCatalogEntry] = field(repr=False)
    _visible_aliases_by_consumer: Mapping[str, tuple[str, ...]] = field(repr=False)
    _fallback_alias_order_by_consumer: Mapping[str, tuple[str, ...]] = field(repr=False)

    def entry_for(self, alias: str) -> ModelCatalogEntry:
        return self._entries_by_alias[alias]

    def alias_to_upstream_target(self) -> dict[str, str]:
        return {entry.alias: entry.upstream_target for entry in self.entries}

    def visible_aliases_for(self, consumer: str) -> tuple[str, ...]:
        return self._visible_aliases_by_consumer.get(consumer, ())

    def fallback_alias_order_for(self, consumer: str) -> tuple[str, ...]:
        return self._fallback_alias_order_by_consumer.get(consumer, ())


def build_model_catalog(
    *,
    local_alias: str = DEFAULT_LOCAL_CANONICAL_ALIAS,
    local_upstream_target: str = DEFAULT_LOCAL_UPSTREAM_TARGET,
    openrouter_model_ids: Iterable[str] = (),
    opencode_go_model_ids: Iterable[str] = (),
    nvidia_alias_targets: Mapping[str, str] | None = None,
    visible_aliases_by_consumer: Mapping[str, Iterable[str]] | None = None,
    default_alias_order: Iterable[str] | None = None,
    cost_metadata_by_alias: Mapping[str, ModelCostMetadata] | None = None,
) -> ModelCatalog:
    visible_aliases = _normalize_visible_aliases(visible_aliases_by_consumer)
    cost_metadata = dict(cost_metadata_by_alias or {})
    consumers_by_alias = _consumers_by_alias(visible_aliases)

    entries: list[ModelCatalogEntry] = []
    entries_by_alias: dict[str, ModelCatalogEntry] = {}

    def add_entry(alias: str, upstream_target: str, provider_slug: str, model_id: str) -> None:
        if alias in entries_by_alias:
            raise ValueError(f"Duplicate model alias: {alias}")
        costs = cost_metadata.get(alias, ModelCostMetadata())
        entry = ModelCatalogEntry(
            alias=alias,
            upstream_target=upstream_target,
            provider_slug=provider_slug,
            model_id=model_id,
            visible_to_consumers=tuple(sorted(consumers_by_alias.get(alias, ()))),
            input_cost_per_token=costs.input_cost_per_token,
            output_cost_per_token=costs.output_cost_per_token,
        )
        entries.append(entry)
        entries_by_alias[alias] = entry

    add_entry(
        alias=local_alias,
        upstream_target=_normalize_model_ref(local_upstream_target),
        provider_slug=_provider_slug_from_alias(local_alias),
        model_id=_model_id_from_alias(local_alias),
    )

    for model_id in opencode_go_model_ids:
        normalized_model_id = model_id.strip()
        add_entry(
            alias=f"opencode-go/{normalized_model_id}",
            upstream_target=_normalize_openai_target(normalized_model_id),
            provider_slug="opencode-go",
            model_id=normalized_model_id,
        )

    for model_id in openrouter_model_ids:
        alias = f"openrouter/{model_id.strip()}"
        add_entry(
            alias=alias,
            upstream_target=_normalize_model_ref(alias),
            provider_slug="openrouter",
            model_id=model_id.strip(),
        )

    for alias, target in (nvidia_alias_targets or {}).items():
        normalized_alias = alias.strip()
        add_entry(
            alias=normalized_alias,
            upstream_target=_normalize_model_ref(target.strip()),
            provider_slug=_provider_slug_from_alias(normalized_alias),
            model_id=_model_id_from_alias(normalized_alias),
        )

    known_aliases = set(entries_by_alias)
    _validate_alias_references(visible_aliases, known_aliases, context="visible alias")
    _validate_alias_references(
        {"default": _dedupe(default_alias_order or tuple(entries_by_alias))},
        known_aliases,
        context="default alias",
    )
    _validate_alias_references(
        {"costs": cost_metadata},
        known_aliases,
        context="cost metadata alias",
    )

    resolved_default_alias_order = _dedupe(default_alias_order or tuple(entries_by_alias))
    fallback_alias_order_by_consumer = {
        consumer: _consumer_fallback_order(
            visible_aliases_for_consumer=aliases,
            default_alias_order=resolved_default_alias_order,
        )
        for consumer, aliases in visible_aliases.items()
    }

    return ModelCatalog(
        entries=tuple(entries),
        default_alias_order=resolved_default_alias_order,
        _entries_by_alias=entries_by_alias,
        _visible_aliases_by_consumer=visible_aliases,
        _fallback_alias_order_by_consumer=fallback_alias_order_by_consumer,
    )


def _consumer_fallback_order(
    *,
    visible_aliases_for_consumer: tuple[str, ...],
    default_alias_order: tuple[str, ...],
) -> tuple[str, ...]:
    ordered_visible = [
        alias for alias in default_alias_order if alias in visible_aliases_for_consumer
    ]
    for alias in visible_aliases_for_consumer:
        if alias not in ordered_visible:
            ordered_visible.append(alias)
    return tuple(ordered_visible)


def _consumers_by_alias(
    visible_aliases_by_consumer: Mapping[str, tuple[str, ...]],
) -> dict[str, list[str]]:
    consumers_by_alias: dict[str, list[str]] = defaultdict(list)
    for consumer, aliases in visible_aliases_by_consumer.items():
        for alias in aliases:
            consumers_by_alias[alias].append(consumer)
    return consumers_by_alias


def _normalize_visible_aliases(
    visible_aliases_by_consumer: Mapping[str, Iterable[str]] | None,
) -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}
    for consumer, aliases in (visible_aliases_by_consumer or {}).items():
        normalized[consumer] = _dedupe(alias.strip() for alias in aliases if alias.strip())
    return normalized


def _validate_alias_references(
    references_by_group: Mapping[str, object],
    known_aliases: set[str],
    *,
    context: str,
) -> None:
    for group_name, references in references_by_group.items():
        aliases: Iterable[object]
        if isinstance(references, Mapping):
            aliases = references.keys()
        elif isinstance(references, tuple | list | set | frozenset):
            aliases = references
        else:
            raise TypeError(f"Unsupported alias reference group for {context}: {group_name}")
        for alias in aliases:
            if alias not in known_aliases:
                raise ValueError(f"Unknown {context} '{alias}' in group '{group_name}'")


def _provider_slug_from_alias(alias: str) -> str:
    provider_slug, _, _ = alias.partition("/")
    return provider_slug


def _model_id_from_alias(alias: str) -> str:
    _, separator, model_id = alias.partition("/")
    return model_id if separator else alias


def _normalize_openai_target(model_id: str) -> str:
    provider, separator, _ = model_id.partition("/")
    if separator and provider in _KNOWN_UPSTREAM_PROVIDERS:
        return _normalize_model_ref(model_id)
    return _normalize_model_ref(f"openai/{model_id}")


def _normalize_model_ref(model_ref: str) -> str:
    return _LEGACY_UPSTREAM_TARGETS.get(model_ref, model_ref)


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
