"""Component FMV lookup table (spec 5.1).

The curated table lives in ``data/prices.json``; values refreshed by the
harvester are stored in the database and merged in here (pinned rows always
win).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..textutils import normalize_key

log = get_logger("snipeklein.valuation.table")


@dataclass
class Component:
    id: str
    kind: str
    label: str
    fmv_eur: float
    aliases: list[str] = field(default_factory=list)
    tdp_w: int | None = None
    source: str = "curated"
    confidence: float = 1.0
    sample_n: int = 0
    pinned: bool = False
    patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)

    def matches(self, haystack: str) -> bool:
        return self.match_length(haystack) > 0

    def match_length(self, haystack: str) -> int:
        """Length of the longest matching alias (0 = no match)."""
        key = normalize_key(haystack)
        if not key:
            return 0
        best = 0
        for alias in self.aliases:
            alias_key = normalize_key(alias)
            if alias_key and alias_key in key and len(alias_key) > best:
                best = len(alias_key)
        return best

    def search(self, haystack: str) -> bool:
        """Regex based match (used by the cheap triage stage)."""
        return self.match_regex_length(haystack) > 0

    def exact_match(self, haystack: str) -> bool:
        """Match the model *without* a different variant.

        'rtx 4070' must not match an 'rtx 4070 ti' card and 'ryzen 7 7800x'
        must not match a 'ryzen 7 7800x3d' card - the variants are worth
        different amounts.
        """
        for alias in self.aliases:
            pattern = _exact_pattern(alias)
            if pattern.search(haystack):
                return True
        return False

    def match_regex_length(self, haystack: str) -> int:
        best = 0
        for _alias, pattern in self.patterns:
            match = pattern.search(haystack)
            if match and len(match.group(0)) > best:
                best = len(match.group(0))
        return best


@dataclass
class PriceTable:
    components: list[Component] = field(default_factory=list)
    defect_costs: dict[str, float] = field(default_factory=dict)
    platform_adjustments: dict[str, float] = field(default_factory=dict)
    condition_multipliers: dict[str, float] = field(default_factory=dict)
    version: str = ""

    # -- construction ------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path, db_prices: dict[str, dict[str, Any]] | None = None) -> PriceTable:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        components: list[Component] = []
        for raw in data.get("components", []):
            aliases = [str(a) for a in raw.get("aliases", [])]
            component = Component(
                id=str(raw["id"]),
                kind=str(raw.get("kind", "other")),
                label=str(raw.get("label", raw["id"])),
                fmv_eur=float(raw.get("fmv_eur", 0.0)),
                aliases=aliases,
                tdp_w=raw.get("tdp_w"),
                pinned=bool(raw.get("pinned", False)),
                source=str(raw.get("market_source", "curated")),
                sample_n=int(raw.get("market_samples", 0) or 0),
            )
            component.patterns = _build_patterns(aliases)
            components.append(component)

        if db_prices:
            _merge_db_prices(components, db_prices)

        # Longest aliases first so "rtx 4070 ti" wins over "rtx 4070".
        components.sort(key=lambda c: max((len(a) for a in c.aliases), default=0), reverse=True)
        return cls(
            components=components,
            defect_costs={k: float(v) for k, v in (data.get("defect_costs") or {}).items()},
            platform_adjustments={
                k: float(v) for k, v in (data.get("platform_adjustments") or {}).items()
            },
            condition_multipliers={
                k: float(v) for k, v in (data.get("condition_multipliers") or {}).items()
            },
            version=str(data.get("version", "")),
        )

    # -- lookups -----------------------------------------------------------
    def get(self, component_id: str) -> Component | None:
        for component in self.components:
            if component.id == component_id:
                return component
        return None

    def by_kind(self, kind: str) -> list[Component]:
        return [c for c in self.components if c.kind == kind]

    def match(self, text: str | None) -> Component | None:
        """Return the best matching component for a free-form model string.

        The longest matching alias wins, so 'ryzen 7 7800x3d' beats the bare
        '7800x3d' alias of another SKU.
        """
        if not text:
            return None
        best: Component | None = None
        best_length = 0
        for component in self.components:
            length = component.match_length(text)
            if length > best_length:
                best, best_length = component, length
        return best

    def match_regex(self, text: str | None) -> Component | None:
        """Alias-regex based match (used by the cheap triage stage)."""
        if not text:
            return None
        best: Component | None = None
        best_length = 0
        for component in self.components:
            length = component.match_regex_length(text)
            if length > best_length:
                best, best_length = component, length
        return best

    def match_all(self, texts: Iterable[str | None]) -> list[Component]:
        found: list[Component] = []
        seen: set[str] = set()
        for text in texts:
            component = self.match(text)
            if component is not None and component.id not in seen:
                seen.add(component.id)
                found.append(component)
        return found

    def condition_multiplier(self, condition_code: str) -> float:
        code = (condition_code or "").strip()
        if code in self.condition_multipliers:
            return self.condition_multipliers[code]
        return self.condition_multipliers.get("", 0.9)

    def defect_cost(self, key: str) -> float:
        return float(self.defect_costs.get(key, 0.0))


def _build_patterns(aliases: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for alias in aliases:
        tokens = [t for t in re.split(r"[\s\-_]+", alias.strip()) if t]
        if not tokens:
            continue
        body = r"[\s\-_]*".join(re.escape(t) for t in tokens)
        patterns.append((alias, re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.IGNORECASE)))
    return patterns


# Variant words that must not silently extend a match.
_VARIANT_SUFFIX = r"(?!\s*(?:ti|super|xtx|xt|3d)(?![a-z0-9]))"


def _exact_pattern(alias: str) -> re.Pattern[str]:
    tokens = [t for t in re.split(r"[\s\-_]+", alias.strip()) if t]
    body = r"[\s\-_]*".join(re.escape(t) for t in tokens)
    return re.compile(
        rf"(?<![a-z0-9]){body}(?![a-z0-9]){_VARIANT_SUFFIX}",
        re.IGNORECASE,
    )


def _merge_db_prices(components: list[Component], db_prices: dict[str, dict[str, Any]]) -> None:
    index = {c.id: c for c in components}
    for component_id, row in db_prices.items():
        component = index.get(component_id)
        if component is None:
            continue
        if component.pinned:
            continue
        component.fmv_eur = float(row.get("fmv_eur", component.fmv_eur))
        component.source = str(row.get("source", component.source))
        component.confidence = float(row.get("confidence", component.confidence))
        component.sample_n = int(row.get("sample_n", 0) or 0)
