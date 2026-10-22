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

# The notations sellers actually use.  The plain aliases only match
# contiguous strings, so "32GB (2x 16384MB) ... DDR4-3200" would hide the
# memory completely - and the listing would be dropped before the LLM ever
# sees it.
_RAM_KIT_RE = re.compile(r"(\d{1,2})\s?x\s*(\d{1,5})\s*(gb|mb)\b", re.IGNORECASE)
_RAM_SIZE_RE = re.compile(r"(\d{1,3})\s?gb\b", re.IGNORECASE)
_RAM_TOKEN_RE = re.compile(r"(ddr\s?[345]|arbeitsspeicher|\bram\b)", re.IGNORECASE)
_DDR_TOKEN_RE = re.compile(r"ddr\s?([345])", re.IGNORECASE)

_STORAGE_DECIMAL_TB_RE = re.compile(r"(\d{1,2})[.,](\d)\s?tb\b", re.IGNORECASE)
_STORAGE_GB_RE = re.compile(r"(\d{3,4})\s?gb\b", re.IGNORECASE)
_STORAGE_TB_RE = re.compile(r"(\d{1,2})\s?tb\b", re.IGNORECASE)
_NVME_TOKEN_RE = re.compile(r"\bnvme\b", re.IGNORECASE)
_SSD_TOKEN_RE = re.compile(r"\bssd\b", re.IGNORECASE)


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

    # -- matching ----------------------------------------------------------
    def _match_parts(self, text: str) -> list[Component]:
        """The parts a listing mentions - one per kind.

        A system has one CPU, one GPU, one memory kit, one storage, one board
        and one power supply, so the best match per kind wins: the longest
        matching alias first (the more specific one), and on a tie the higher
        value (a 32 GB kit over a single 16 GB stick, a Gen4 drive over a
        Gen3 one).
        """
        best: dict[str, Component] = {}
        for component in self.table.components:
            if component.kind in ("peripheral", "accessory"):
                continue
            if not component.patterns:
                continue
            # exact_match keeps the variants apart: a "4070 Ti" title must not
            # be counted as a 4070 *and* a 4070 Ti
            if not component.exact_match(text):
                continue
            current = best.get(component.kind)
            if current is None or self._is_better(component, current, text):
                best[component.kind] = component

        # the notations the plain aliases cannot see
        ram = self._ram_from_text(text)
        if ram is not None:
            best["ram"] = ram
        storage = self._storage_from_text(text)
        if storage is not None:
            best["storage"] = storage
        return list(best.values())

    def _is_better(self, candidate: Component, current: Component, text: str) -> bool:
        candidate_length = candidate.match_length(text)
        current_length = current.match_length(text)
        if candidate_length != current_length:
            return candidate_length > current_length
        return candidate.fmv_eur > current.fmv_eur

    def _ram_from_text(self, text: str) -> Component | None:
        """Read the memory kit out of the text, even in the German notation."""
        generation_match = _DDR_TOKEN_RE.search(text)
        if not generation_match:
            return None
        generation = f"DDR{generation_match.group(1)}"
        capacity = self._ram_capacity(text)
        if not capacity:
            return None
        return self.table.get(f"{generation}_{capacity}GB_STANDARD")

    def _ram_capacity(self, text: str) -> int | None:
        # "2x 16GB" or "2x 16384MB" -> the whole kit
        for match in _RAM_KIT_RE.finditer(text):
            count = int(match.group(1))
            size = int(match.group(2))
            unit = match.group(3).lower()
            total = size * count
            if unit == "mb":
                total = total // 1024
            if 1 <= total <= 256:
                return total
        # otherwise the largest "32 GB" that sits near a memory token
        sizes = [(match.start(), int(match.group(1))) for match in _RAM_SIZE_RE.finditer(text)]
        if not sizes:
            return None
        tokens = [match.start() for match in _RAM_TOKEN_RE.finditer(text)]
        if tokens:
            for position, size in sorted(sizes, key=lambda item: -item[1]):
                if any(abs(position - token) <= 40 for token in tokens):
                    return size
        return max(size for _, size in sizes)

    def _storage_from_text(self, text: str) -> Component | None:
        """Read the storage out of the text: "1,5TB", "1000 GB", "2 TB"."""
        capacity_tb: int | None = None
        match = _STORAGE_DECIMAL_TB_RE.search(text)
        if match:
            capacity_tb = int(match.group(1)) + (1 if int(match.group(2)) else 0)
        else:
            match = _STORAGE_GB_RE.search(text)
            if match:
                gigabytes = int(match.group(1))
                capacity_tb = max(1, round(gigabytes / 1024))
            else:
                match = _STORAGE_TB_RE.search(text)
                if match:
                    capacity_tb = int(match.group(1))
        if not capacity_tb:
            return None
        if _NVME_TOKEN_RE.search(text):
            if capacity_tb >= 2:
                return self.table.get("NVME_GEN4_2TB")
            return self.table.get("NVME_GEN4_1TB")
        if _SSD_TOKEN_RE.search(text):
            return self.table.get("SATA_SSD_1TB")
        return None

    def evaluate(self, card: SearchCard) -> TriageResult:
        text = f"{card.title} {card.snippet}"
        components = self._match_parts(text)

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
