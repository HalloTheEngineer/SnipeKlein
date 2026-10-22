"""Price table, valuation engine and risk heuristics."""

from __future__ import annotations

import pytest

from snipeklein.config import FilterConfig, RiskConfig, ValuationConfig
from snipeklein.models import (
    Band,
    Extraction,
    Listing,
    RiskLevel,
    ShippingMode,
)
from snipeklein.valuation.model import ValuationEngine
from snipeklein.valuation.risk import RiskEngine
from snipeklein.valuation.table import PriceTable
from snipeklein.valuation.triage import Triage

PRICES = "data/prices.json"


@pytest.fixture(scope="module")
def table() -> PriceTable:
    return PriceTable.load(PRICES)


@pytest.fixture()
def engine(table: PriceTable) -> ValuationEngine:
    return ValuationEngine(table=table, config=ValuationConfig())


def _listing(price: float = 1000.0, **kwargs) -> Listing:
    defaults = {
        "ad_id": "1",
        "url": "https://example.invalid/1",
        "title": "Gaming PC",
        "price": price,
        "shipping_mode": ShippingMode.PICKUP,
        "condition_code": "like_new",
    }
    defaults.update(kwargs)
    return Listing(**defaults)


# -- table ----------------------------------------------------------------
def test_table_contains_the_spec_components(table: PriceTable) -> None:
    for component_id in (
        "AMD_RYZEN_5_7600X",
        "AMD_RYZEN_7_7800X3D",
        "NVIDIA_RTX_4070_12GB",
        "NVIDIA_RTX_4080_16GB",
        "DDR5_32GB_STANDARD",
        "NVME_GEN4_1TB",
        "B650_STANDARD",
        "PERIPHERAL_BUNDLE_GENERIC",
    ):
        assert table.get(component_id) is not None


def test_spec_baseline_is_preserved(table: PriceTable) -> None:
    """The values from project_plan.md are kept as documentation."""
    import json
    from pathlib import Path

    raw = json.loads(Path("data/prices.json").read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in raw["components"]}
    for component_id, baseline in {
        "AMD_RYZEN_5_7600X": 155,
        "AMD_RYZEN_7_7800X3D": 310,
        "NVIDIA_RTX_4070_12GB": 440,
        "NVIDIA_RTX_4080_16GB": 820,
        "DDR5_32GB_STANDARD": 85,
        "NVME_GEN4_1TB": 50,
        "B650_STANDARD": 105,
        "PERIPHERAL_BUNDLE_GENERIC": 90,
    }.items():
        assert by_id[component_id]["spec_baseline"] == baseline
        assert table.get(component_id) is not None


def test_longest_alias_wins(table: PriceTable) -> None:
    assert table.match("NVIDIA GeForce RTX 4070 Ti").id == "NVIDIA_RTX_4070_TI"
    assert table.match("NVIDIA GeForce RTX 4070").id == "NVIDIA_RTX_4070_12GB"
    assert table.match("AMD Ryzen 7 7800X3D").id == "AMD_RYZEN_7_7800X3D"


def test_regex_matching(table: PriceTable) -> None:
    assert table.match_regex("AMD Ryzen 7 7800X3D").id == "AMD_RYZEN_7_7800X3D"
    assert table.match_regex("i7-13700K").id == "INTEL_I7_13700K"


# -- valuation ------------------------------------------------------------
def test_valuation_of_a_ddr5_gaming_pc(engine: ValuationEngine) -> None:
    extraction = Extraction(
        is_complete_system=True,
        cpu_model="AMD Ryzen 7 7800X3D",
        gpu_model="NVIDIA GeForce RTX 4070",
        ram_generation="DDR5",
        ram_capacity_gb=32,
        storage_capacity_gb=1024,
        motherboard_chipset="B650",
        psu_wattage=750,
        operating_system_included=True,
    )
    valuation = engine.value(extraction, _listing(price=900.0))
    # gross = cpu + gpu + ram + storage + board + psu + case/cooler
    expected_gross = (
        engine.table.get("AMD_RYZEN_7_7800X3D").fmv_eur
        + engine.table.get("NVIDIA_RTX_4070_12GB").fmv_eur
        + engine.table.get("DDR5_32GB_STANDARD").fmv_eur
        + engine.table.get("NVME_GEN4_1TB").fmv_eur
        + engine.table.get("B650_STANDARD").fmv_eur
        + engine.table.get("PSU_750W_GOLD").fmv_eur
        + engine.table.get("PERIPHERAL_BUNDLE_GENERIC").fmv_eur
    )
    assert valuation.fmv_gross == pytest.approx(expected_gross, rel=0.01)
    # AM5 platform bonus: 3 % of gross, then the assembly factor (1.0 until
    # the system market has been measured)
    expected_net = (expected_gross + expected_gross * 0.03) * 1.0
    assert valuation.fmv_net == pytest.approx(expected_net, rel=0.01)
    assert valuation.margin_pct == pytest.approx(
        (expected_net - 900.0) / expected_net * 100, rel=0.01
    )
    # a system priced at its own value is no deal
    fair = engine.value(extraction, _listing(price=expected_net))
    assert fair.band is Band.NOT_A_DEAL


def test_valuation_flags_a_real_deal(engine: ValuationEngine) -> None:
    extraction = Extraction(
        is_complete_system=True,
        cpu_model="AMD Ryzen 7 7800X3D",
        gpu_model="NVIDIA GeForce RTX 4070",
        ram_generation="DDR5",
        ram_capacity_gb=32,
        storage_capacity_gb=1024,
        motherboard_chipset="B650",
        psu_wattage=750,
    )
    # the price is derived from the table, so the test survives a price update
    fair = engine.value(extraction, _listing(price=1000.0))
    valuation = engine.value(extraction, _listing(price=fair.fmv_net * 0.8))
    assert valuation.margin_pct == pytest.approx(20.0, abs=1.0)
    assert valuation.band is Band.ACTIONABLE


def test_defects_reduce_the_value(engine: ValuationEngine) -> None:
    base = Extraction(
        is_complete_system=True,
        cpu_model="AMD Ryzen 7 7800X3D",
        gpu_model="NVIDIA GeForce RTX 4070",
        ram_generation="DDR5",
        ram_capacity_gb=32,
        storage_capacity_gb=1024,
        motherboard_chipset="B650",
        psu_wattage=750,
    )
    with_defects = base.model_copy(
        update={"defects_or_missing_parts": ["Netzteil defekt", "kein Betriebssystem dabei"]}
    )
    clean = engine.value(base, _listing(price=600.0))
    damaged = engine.value(with_defects, _listing(price=600.0))
    assert damaged.defect_costs
    assert damaged.fmv_net < clean.fmv_net


def test_coverage_penalises_unknown_parts(engine: ValuationEngine) -> None:
    extraction = Extraction(
        is_complete_system=True,
        cpu_model="AMD Ryzen 7 7800X3D",
        gpu_model="NVIDIA GeForce RTX 4070",
        ram_generation="DDR5",
        ram_capacity_gb=32,
        storage_capacity_gb=1024,
        motherboard_chipset="B650",
        psu_wattage=750,
    )
    full = engine.value(extraction, _listing(price=600.0))
    partial = engine.value(
        Extraction(is_complete_system=True, cpu_model="AMD Ryzen 7 7800X3D"),
        _listing(price=600.0),
    )
    assert partial.coverage < full.coverage


# -- triage ---------------------------------------------------------------
def test_triage_rejects_cheap_irrelevant_listings(
    table: PriceTable, engine: ValuationEngine
) -> None:
    from snipeklein.models import SearchCard

    triage = Triage(table, FilterConfig(), engine)
    card = SearchCard(
        ad_id="1",
        url="https://example.invalid",
        title="Gaming PC RTX 4070 Ryzen 7800X3D DDR5",
        price=1800.0,
    )
    result = triage.evaluate(card)
    assert not result.passed
    assert result.reason.startswith("triage_margin")


def test_triage_accepts_a_cheap_gaming_pc(table: PriceTable, engine: ValuationEngine) -> None:
    from snipeklein.models import SearchCard

    triage = Triage(table, FilterConfig(), engine)
    card = SearchCard(
        ad_id="1",
        url="https://example.invalid",
        title="Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5 1TB NVMe",
        price=700.0,
    )
    result = triage.evaluate(card)
    assert result.passed
    assert result.coarse_margin_pct > 20


# -- risk -----------------------------------------------------------------
def test_risk_flags_a_brand_new_account() -> None:
    from datetime import datetime, timedelta

    engine = RiskEngine(RiskConfig())
    listing = _listing(
        price=300.0,
        seller_since=(datetime.now() - timedelta(days=5)).strftime("%d.%m.%Y"),
        seller_since_date=datetime.now() - timedelta(days=5),
        seller_badges=[],
        shipping_mode=ShippingMode.SHIPPING,
    )
    extraction = Extraction(is_complete_system=True, ram_generation="DDR5")
    from snipeklein.models import Valuation

    valuation = Valuation(fmv_net=1200.0, asking_price=300.0, margin_pct=75.0)
    risk = engine.assess(listing, extraction, valuation)
    assert risk.level is RiskLevel.RED
    assert risk.score >= 50


def test_risk_is_green_for_an_established_private_seller() -> None:
    from datetime import datetime, timedelta

    engine = RiskEngine(RiskConfig())
    listing = _listing(
        price=700.0,
        seller_since="01.01.2015",
        seller_since_date=datetime.now() - timedelta(days=3000),
        seller_badges=["TOP", "Sehr freundlich"],
        shipping_mode=ShippingMode.PICKUP,
    )
    extraction = Extraction(is_complete_system=True, ram_generation="DDR5")
    from snipeklein.models import Valuation

    valuation = Valuation(fmv_net=1000.0, asking_price=700.0, margin_pct=30.0)
    risk = engine.assess(listing, extraction, valuation)
    assert risk.level is RiskLevel.GREEN
