"""Cheap triage stage.

Runs on the search card (title + snippet) *before* the detail page is fetched
and before the LLM is called. A listing is only promoted to the expensive
stages when the regex based component guess already looks like a deal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import FilterConfig
from ..filters.prefilter import is_modern_platform
from ..models import Extraction, SearchCard
from .model import ValuationEngine
from .table import Component, PriceTable

_RAM_ID_RE = re.compile(r"DDR[45]_(\d+)GB")
_STORAGE_ID_RE = re.compile(r"_(\d+)TB")
_PSU_ID_RE = re.compile(r"PSU_(\d+)W")


@dataclass
class TriageResult:
    passed: bool
    reason: str = ""
    coarse_fmv: float = 0.0
    coarse_margin_pct: float = 0.0
    components: list[Component] = field(default_factory=list)
    note: str = ""


class Triage:
    def __init__(
        self, table: PriceTable, config: FilterConfig, engine: ValuationEngine
    ) -> None:
        self.table = table
        self.config = config
        self.engine = engine

    def evaluate(self, card: SearchCard) -> TriageResult:
        text = f"{card.title} {card.snippet}"
        components: list[Component] = []
        seen: set[str] = set()
        for component in self.table.components:
            if component.kind == "peripheral":
                continue
            if not component.patterns:
                continue
            if component.search(text) and component.id not in seen:
                seen.add(component.id)
                components.append(component)

        if len(components) < self.config.triage_min_components:
            # The price table does not know this system yet.  If the title
            # hints at a current generation platform it is still worth an
            # extraction - the description is where the spec usually is.
            if self.config.modern_platform_tier and is_modern_platform(text):
                return TriageResult(
                    passed=True,
                    reason="triage_modern_tier",
                    components=components,
                    note=(
                        "the price table does not know this system, the title hints at a "
                        "current platform - the extraction decides"
                    ),
                )
            return TriageResult(
                passed=False,
                reason=f"triage_components:{len(components)}",
                components=components,
            )

        price = card.price or 0.0
        if price <= 0:
            return TriageResult(
                passed=False, reason="triage_no_price", components=components
            )

        extraction = pseudo_extraction(components)
        expected = self.engine.expected_fmv(extraction)
        if expected <= 0:
            return TriageResult(
                passed=False, reason="triage_no_value", components=components
            )

        margin = self.engine.expected_margin_pct(extraction, price)
        if margin < self.config.triage_min_margin_pct:
            return TriageResult(
                passed=False,
                reason=f"triage_margin:{margin:.1f}",
                coarse_fmv=expected,
                coarse_margin_pct=margin,
                components=components,
            )
        return TriageResult(
            passed=True,
            reason="triage_ok",
            coarse_fmv=expected,
            coarse_margin_pct=margin,
            components=components,
        )


def pseudo_extraction(components: list[Component]) -> Extraction:
    """Build a rough extraction from the regex matches of a card."""
    extraction = Extraction(is_complete_system=True)
    for component in components:
        if component.kind == "cpu" and not extraction.cpu_model:
            extraction.cpu_model = component.label
        elif component.kind == "gpu" and not extraction.gpu_model:
            extraction.gpu_model = component.label
        elif component.kind == "ram" and extraction.ram_capacity_gb is None:
            match = _RAM_ID_RE.search(component.id)
            if match:
                extraction.ram_capacity_gb = int(match.group(1))
                extraction.ram_generation = "DDR5" if "DDR5" in component.id else "DDR4"
        elif component.kind == "storage" and extraction.storage_capacity_gb is None:
            match = _STORAGE_ID_RE.search(component.id)
            if match:
                extraction.storage_capacity_gb = int(match.group(1)) * 1024
        elif component.kind == "board" and not extraction.motherboard_chipset:
            extraction.motherboard_chipset = component.label
        elif component.kind == "psu" and extraction.psu_wattage is None:
            match = _PSU_ID_RE.search(component.id)
            if match:
                extraction.psu_wattage = int(match.group(1))
    return extraction
