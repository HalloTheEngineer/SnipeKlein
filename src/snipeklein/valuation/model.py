"""Valuation engine (spec 5).

    FMV_gross = sum(Price(component))
    FMV_net   = (FMV_gross + adjustments) * (1 - delta_bundle) - sum(Cost(defects))
    Margin %  = (FMV_net - Price_asking) / FMV_net * 100

``FMV_gross`` only counts the components that were actually named in the ad.
Parts that are not mentioned are *not* added to the value (the buyer cannot
count on them) - the ``coverage`` metric quantifies how much of a complete
system is actually known.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from ..config import ValuationConfig
from ..logging_setup import get_logger
from ..models import (
    Band,
    ComponentValue,
    DefectCost,
    Extraction,
    Listing,
    Valuation,
)
from .table import Component, PriceTable

log = get_logger("snipeklein.valuation.model")

# How likely a kind of component is present in a complete system when the ad
# does not mention it. Used for the coverage estimate and for the cheap
# triage stage.
PRESENCE = {
    "cpu": 1.0,
    "gpu": 1.0,
    "ram": 1.0,
    "storage": 1.0,
    "board": 0.7,
    "psu": 0.6,
    "peripheral": 1.0,
}

EXPECTED_KINDS = ("cpu", "gpu", "ram", "storage", "board", "psu", "peripheral")

AM5_BOARDS = ("B650", "B650E", "X670", "X670E", "A620")

DEFECT_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ssd", "storage", "festplatte", "speicherplatz"), "missing_storage"),
    (("kühler", "kuehler", "cooler", "lüfter", "luefter"), "missing_cooler"),
    (("windows", "betriebssystem", "os "), "missing_os"),
    (("netzteil", "psu"), "missing_psu"),
    (("gehäuse", "gehause", "case", "chassis"), "missing_case"),
    (("gpu", "grafikkarte", "graka"), "damaged_gpu"),
)


def kind_median(table: PriceTable, kind: str) -> float:
    values = [c.fmv_eur for c in table.by_kind(kind)]
    if not values:
        return 0.0
    return float(statistics.median(values))


def expected_fmv(table: PriceTable, components: list[Component]) -> float:
    """What a complete system with these parts is worth (the unnamed parts
    are assumed to be present and worth their median)."""
    recognized: dict[str, float] = {}
    for component in components:
        recognized[component.kind] = recognized.get(component.kind, 0.0) + component.fmv_eur
    total = sum(recognized.values())
    for kind in EXPECTED_KINDS:
        if kind in recognized:
            continue
        total += kind_median(table, kind) * PRESENCE.get(kind, 1.0)
    return total


def coverage_of(table: PriceTable, components: list[Component]) -> float:
    """The share of a complete system that these parts represent."""
    named = sum(component.fmv_eur for component in components)
    if named <= 0:
        return 0.0
    expected = expected_fmv(table, components)
    if expected <= 0:
        return 0.0
    return min(1.0, named / expected)


@dataclass
class ValuationEngine:
    table: PriceTable
    config: ValuationConfig
    # optional: the complete-system market (calibrates the assembly factor)
    market: Any = None

    # -- public ------------------------------------------------------------
    def value(self, extraction: Extraction, listing: Listing) -> Valuation:
        components = self._resolve_components(extraction)
        condition = self.table.condition_multiplier(listing.condition_code)

        component_values: list[ComponentValue] = []
        gross = 0.0
        recognized_by_kind: dict[str, float] = {}

        for component in components:
            adjusted = component.fmv_eur * condition
            gross += adjusted
            recognized_by_kind[component.kind] = recognized_by_kind.get(component.kind, 0.0) + adjusted
            component_values.append(
                ComponentValue(
                    component_id=component.id,
                    label=component.label,
                    kind=component.kind,
                    fmv_eur=round(adjusted, 2),
                    source=component.source,
                )
            )

        missing = self._missing_kinds(recognized_by_kind)
        adjustments, adjustment_total = self._adjustments(extraction, recognized_by_kind, gross)
        defect_costs = self._defect_costs(extraction)

        coverage = self._coverage(recognized_by_kind, gross)

        # The value of the system is the sum of the parts the ad names.  The
        # market asks a calibrated multiple of that sum (the assembly factor),
        # and the accessories of a bundle are worth their own market value.
        factor, factor_n = self._assembly_factor(extraction)
        accessories = self._accessory_value(extraction)
        estimate = self.expected_fmv(extraction, listing.condition_code)
        net = (estimate + adjustment_total) * factor + accessories
        net -= sum(cost.cost_eur for cost in defect_costs)
        net = max(net, 0.0)

        asking = listing.price or 0.0
        margin_eur = net - asking
        margin_pct = (margin_eur / net * 100.0) if net > 0 else 0.0

        band = self._band(margin_pct)

        rank = self._price_rank(valuation_price=asking, extraction=extraction)

        notes: list[str] = []
        if coverage < self.config.min_coverage:
            notes.append(
                f"coverage {coverage:.0%} < {self.config.min_coverage:.0%}: too many unknown parts"
            )
        if not asking:
            notes.append("no asking price found")

        return Valuation(
            fmv_gross=round(gross, 2),
            fmv_net=round(net, 2),
            fmv_estimate=round(estimate, 2),
            assembly_factor=factor,
            assembly_samples=factor_n,
            accessories_eur=round(accessories, 2),
            asking_price=asking,
            margin_eur=round(margin_eur, 2),
            margin_pct=round(margin_pct, 2),
            band=band,
            coverage=round(coverage, 3),
            price_rank=rank,
            components=component_values,
            defect_costs=defect_costs,
            adjustments=adjustments,
            missing_components=missing,
            notes=notes,
        )

    def expected_fmv(self, extraction: Extraction, condition_code: str = "") -> float:
        """Value of a complete system, assuming the unnamed parts are present.

        Used by the cheap triage stage: it must not punish ads that simply do
        not spell out every component.
        """
        components = self._resolve_components(extraction)
        condition = self.table.condition_multiplier(condition_code)
        scaled = [
            Component(
                id=component.id,
                kind=component.kind,
                label=component.label,
                fmv_eur=component.fmv_eur * condition,
                aliases=component.aliases,
                tdp_w=component.tdp_w,
            )
            for component in components
        ]
        return expected_fmv(self.table, scaled)

    def expected_margin_pct(self, extraction: Extraction, price: float) -> float:
        """The margin the triage estimates - the same model the valuation uses."""
        expected = self.expected_fmv(extraction)
        factor, _ = self._assembly_factor(extraction)
        net = expected * factor
        if net <= 0:
            return 0.0
        return (net - price) / net * 100.0

    # -- internals ---------------------------------------------------------
    def _resolve_components(self, extraction: Extraction) -> list[Component]:
        found: list[Component] = []
        seen: set[str] = set()

        def add(component: Component | None) -> None:
            if component is not None and component.id not in seen:
                seen.add(component.id)
                found.append(component)

        add(self.table.match(extraction.cpu_model))
        add(self.table.match(extraction.gpu_model))
        add(self._ram_component(extraction))
        add(self._storage_component(extraction))
        add(self.table.match(extraction.motherboard_chipset))
        add(self._psu_component(extraction))

        # A complete system always contains case + cooler.
        if extraction.is_complete_system:
            add(self.table.get("PERIPHERAL_BUNDLE_GENERIC"))
        return found

    def _ram_component(self, extraction: Extraction) -> Component | None:
        if not extraction.ram_capacity_gb:
            return None
        generation = (extraction.ram_generation or "UNKNOWN").upper()
        if generation == "DDR5":
            return self.table.get(f"DDR5_{extraction.ram_capacity_gb}GB_STANDARD")
        if generation == "DDR4":
            return self.table.get(f"DDR4_{extraction.ram_capacity_gb}GB_STANDARD")
        return None

    def _storage_component(self, extraction: Extraction) -> Component | None:
        if not extraction.storage_capacity_gb:
            return None
        capacity = self._snap_capacity(extraction.storage_capacity_gb)
        if capacity >= 2048:
            return self.table.get("NVME_GEN4_2TB")
        if capacity >= 1024:
            return self.table.get("NVME_GEN4_1TB")
        if capacity >= 512:
            return self.table.get("NVME_GEN3_1TB")
        return None

    @staticmethod
    def _snap_capacity(capacity: int) -> int:
        """Snap a capacity to the next standard size.

        Sellers report the formatted capacity ('931 GB' for a 1 TB drive), so
        931 GB has to be treated as 1 TB.
        """
        for size in (512, 1024, 2048, 4096):
            if capacity <= size:
                return size
        return capacity

    def _psu_component(self, extraction: Extraction) -> Component | None:
        if not extraction.psu_wattage:
            return None
        for number in ("550", "650", "750", "850", "1000", "1200"):
            if f"{number} w" in str(extraction.psu_wattage):
                component = self.table.get(f"PSU_{number}W_GOLD")
                if component is None:
                    component = self.table.get(f"PSU_{number}W_BRONZE")
                if component is None:
                    component = self.table.get(f"PSU_{number}W_ATX30")
                if component is not None:
                    return component
        return self.table.get("PSU_750W_GOLD")

    def _missing_kinds(self, recognized: dict[str, float]) -> list[str]:
        return [kind for kind in EXPECTED_KINDS if kind not in recognized]

    def _coverage(self, recognized: dict[str, float], gross: float) -> float:
        if gross <= 0:
            return 0.0
        expected = sum(recognized.values())
        for kind in EXPECTED_KINDS:
            if kind in recognized:
                continue
            expected += self._kind_median(kind) * PRESENCE.get(kind, 1.0)
        if expected <= 0:
            return 0.0
        return min(1.0, gross / expected)

    def _kind_median(self, kind: str) -> float:
        return kind_median(self.table, kind)

    def _adjustments(
        self, extraction: Extraction, recognized: dict[str, float], gross: float
    ) -> tuple[list[str], float]:
        adjustments: list[str] = []
        total = 0.0
        cfg = self.table.platform_adjustments

        board = self.table.match(extraction.motherboard_chipset)
        if board is not None and any(token in board.label.upper() for token in AM5_BOARDS):
            bonus = gross * (float(cfg.get("am5_pcie4_bonus_pct", 0.0)) / 100.0)
            total += bonus
            adjustments.append(f"AM5/PCIe4 platform +{bonus:.0f} EUR")

        gpu = self.table.match(extraction.gpu_model)
        cpu = self.table.match(extraction.cpu_model)
        demand = 0
        if gpu is not None:
            demand += gpu.tdp_w or 0
        if cpu is not None:
            demand += cpu.tdp_w or 0
        psu_watt = extraction.psu_wattage or 0
        if demand and psu_watt and demand > psu_watt * 0.8:
            penalty = float(cfg.get("psu_undersized_penalty_eur", 0.0))
            total -= penalty
            adjustments.append(f"PSU too small ({psu_watt} W) -{penalty:.0f} EUR")

        if extraction.ram_capacity_gb and extraction.ram_capacity_gb < 16:
            penalty = float(cfg.get("ram_below_16gb_penalty_eur", 0.0))
            total -= penalty
            adjustments.append(f"RAM below 16 GB -{penalty:.0f} EUR")

        if extraction.storage_capacity_gb and extraction.storage_capacity_gb < 512:
            penalty = float(cfg.get("storage_below_512gb_penalty_eur", 0.0))
            total -= penalty
            adjustments.append(f"storage below 512 GB -{penalty:.0f} EUR")

        return adjustments, total

    def _defect_costs(self, extraction: Extraction) -> list[DefectCost]:
        costs: list[DefectCost] = []
        for defect in extraction.defects_or_missing_parts:
            lowered = defect.lower()
            for keywords, key in DEFECT_KEYWORDS:
                if any(keyword in lowered for keyword in keywords):
                    if key == "damaged_gpu":
                        gpu = self.table.match(extraction.gpu_model)
                        amount = round((gpu.fmv_eur if gpu else 0.0) * 0.8, 2)
                        if amount > 0:
                            costs.append(DefectCost(label=defect, cost_eur=amount))
                    else:
                        amount = self.table.defect_cost(key)
                        if amount > 0:
                            costs.append(DefectCost(label=defect, cost_eur=amount))
                    break
            else:
                amount = self.table.defect_cost("unknown_defect")
                if amount > 0:
                    costs.append(DefectCost(label=defect, cost_eur=amount))
        return costs

    def _assembly_factor(self, extraction: Extraction) -> tuple[float, int]:
        """The calibrated multiple of the parts' sum that the market asks for."""
        if self.market is None:
            return self.config.assembly_fallback, 0
        build_id = ""
        gpu = self.table.match(extraction.gpu_model)
        if gpu is not None:
            build_id = gpu.id
        return self.market.assembly_factor(
            build_id,
            min_samples=self.config.assembly_min_samples,
            days=self.config.assembly_window_days,
            fallback=self.config.assembly_fallback,
            sold_factor=self.config.asking_to_sold_factor,
        )

    def _price_rank(self, valuation_price: float, extraction: Extraction) -> float | None:
        """Where the asking price sits in the market of its configuration."""
        if self.market is None or valuation_price <= 0:
            return None
        gpu = self.table.match(extraction.gpu_model)
        build_id = gpu.id if gpu is not None else ""
        return self.market.price_rank(valuation_price, build_id, self.config.assembly_window_days)

    def _accessory_value(self, extraction: Extraction) -> float:
        """The market value of the accessories a bundle includes."""
        if not extraction.accessories:
            return 0.0
        total = 0.0
        for accessory in extraction.accessories:
            component = self.table.match(accessory)
            if component is not None and component.kind == "accessory":
                total += component.fmv_eur
        return total

    def _band(self, margin_pct: float) -> Band:
        if margin_pct >= self.config.extreme_margin_pct:
            return Band.EXTREME
        if (
            self.config.actionable_margin_min_pct
            <= margin_pct
            <= self.config.actionable_margin_max_pct
        ):
            return Band.ACTIONABLE
        if self.config.near_miss_alerts and margin_pct >= self.config.near_miss_margin_min_pct:
            return Band.NEAR_MISS
        return Band.NOT_A_DEAL
