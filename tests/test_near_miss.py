"""The near miss tier: 15-30 % margin, clearly not a deal."""

from __future__ import annotations

import pytest

from snipeklein.config import (
    ValuationConfig,
)
from snipeklein.models import (
    Band,
    Deal,
    Extraction,
    Listing,
    RiskAssessment,
    RiskLevel,
    ShippingMode,
    Valuation,
)
from snipeklein.notify.templates import COLOR_NEAR_MISS, COLOR_RED, build_embed
from snipeklein.valuation.model import ValuationEngine
from snipeklein.valuation.table import PriceTable


@pytest.fixture(scope="module")
def table() -> PriceTable:
    return PriceTable.load("data/prices.json")


@pytest.fixture()
def engine(table: PriceTable) -> ValuationEngine:
    return ValuationEngine(table=table, config=ValuationConfig())


def _extraction() -> Extraction:
    return Extraction(
        is_complete_system=True,
        cpu_model="AMD Ryzen 7 7800X3D",
        gpu_model="NVIDIA GeForce RTX 4070",
        ram_generation="DDR5",
        ram_capacity_gb=32,
        storage_capacity_gb=1024,
        motherboard_chipset="B650",
        psu_wattage=750,
    )


def _listing(price: float) -> Listing:
    return Listing(
        ad_id="1",
        url="https://example.invalid/1",
        title="Gaming PC",
        price=price,
        shipping_mode=ShippingMode.PICKUP,
        condition_code="like_new",
    )


def _band_at(engine: ValuationEngine, margin_pct: float) -> Band:
    """Value a listing so that it lands on the wanted margin."""
    extraction = _extraction()
    fair = engine.value(extraction, _listing(1000.0))
    # margin = (net - price) / net  ->  price = net * (1 - margin)
    price = fair.fmv_net * (1 - margin_pct / 100.0)
    return engine.value(extraction, _listing(price)).band


def test_bands(engine: ValuationEngine) -> None:
    assert _band_at(engine, 5.0) is Band.NOT_A_DEAL
    assert _band_at(engine, 20.0) is Band.NEAR_MISS
    assert _band_at(engine, 40.0) is Band.ACTIONABLE
    assert _band_at(engine, 65.0) is Band.EXTREME


def test_near_miss_can_be_switched_off(table: PriceTable) -> None:
    engine = ValuationEngine(
        table=table,
        config=ValuationConfig(near_miss_alerts=False),
    )
    assert _band_at(engine, 20.0) is Band.NOT_A_DEAL


def _deal(band: Band, margin: float) -> Deal:
    listing = Listing(
        ad_id="1",
        url="https://example.invalid/1",
        title="Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5",
        price=900.0,
        shipping_mode=ShippingMode.PICKUP,
        condition_code="like_new",
        seller_badges=["TOP"],
    )
    valuation = Valuation(
        fmv_gross=1400.0,
        fmv_net=1232.0,
        asking_price=900.0,
        margin_eur=332.0,
        margin_pct=margin,
        band=band,
        coverage=1.0,
    )
    return Deal(
        listing=listing,
        extraction=_extraction(),
        valuation=valuation,
        risk=RiskAssessment(score=0, level=RiskLevel.GREEN),
    )


def test_near_miss_embed_has_its_own_colour_and_wording() -> None:
    deal = _deal(Band.NEAR_MISS, 22.0)
    embed = build_embed(deal)
    assert embed["color"] == COLOR_NEAR_MISS
    assert embed["color"] != COLOR_RED
    assert "kein Deal" in embed["fields"][3]["value"]
    assert "Near Miss" in embed["description"]
    assert "kein echter Rabatt" in embed["description"]


def test_deal_embed_is_not_blue() -> None:
    embed = build_embed(_deal(Band.ACTIONABLE, 40.0))
    assert embed["color"] != COLOR_NEAR_MISS
