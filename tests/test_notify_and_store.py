"""Discord embed construction and the store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

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
from snipeklein.notify.templates import (
    COLOR_GREEN,
    COLOR_RED,
    COLOR_YELLOW,
    border_color,
    build_embed,
    build_payload,
)
from snipeklein.store.db import connect, migrate
from snipeklein.store.repo import AlertRepo, ListingRepo, SeenStore, UsageRepo


def _deal(
    margin: float = 40.0,
    band: Band = Band.ACTIONABLE,
    risk_level: RiskLevel = RiskLevel.GREEN,
    shipping: ShippingMode = ShippingMode.PICKUP,
    badges: list[str] | None = None,
) -> Deal:
    listing = Listing(
        ad_id="3547058096",
        url="https://www.kleinanzeigen.de/s-anzeige/x/3547058096-228-1",
        title="Gaming PC Ryzen 7 7800X3D RTX 4070",
        price=800.0,
        shipping_mode=shipping,
        seller_badges=badges if badges is not None else ["TOP"],
        seller_since="01.01.2015",
        seller_since_date=datetime.now() - timedelta(days=3000),
        seller_name="Max Mustermann",
        plz="12345",
        city="Berlin",
        seller_active_ads=2,
    )
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
    valuation = Valuation(
        fmv_gross=1150.0,
        fmv_net=1012.0,
        asking_price=800.0,
        margin_eur=212.0,
        margin_pct=margin,
        band=band,
        coverage=0.9,
    )
    risk = RiskAssessment(score=0, level=risk_level)
    return Deal(listing=listing, extraction=extraction, valuation=valuation, risk=risk)


def test_embed_contains_the_required_payload() -> None:
    embed = build_embed(_deal())
    assert embed["title"].startswith("Gaming PC")
    assert embed["url"].startswith("https://")
    names = [field["name"] for field in embed["fields"]]
    assert any("Preis" in name for name in names)
    assert any("FMV" in name for name in names)
    assert any("Marge" in name for name in names)
    assert any("Hardware" in name for name in names)
    assert any("Verkäufer" in name for name in names)
    assert any("Risiko" in name for name in names)


def test_green_border_for_a_verified_pickup_deal() -> None:
    assert border_color(_deal(margin=35.0)) == COLOR_GREEN


def test_yellow_border_for_a_shipping_deal() -> None:
    assert border_color(_deal(margin=45.0, shipping=ShippingMode.SHIPPING)) == COLOR_YELLOW


def test_red_border_for_an_extreme_outlier() -> None:
    assert border_color(_deal(margin=70.0, band=Band.EXTREME)) == COLOR_RED
    assert border_color(_deal(risk_level=RiskLevel.RED)) == COLOR_RED


def test_payload_shape() -> None:
    payload = build_payload(_deal(), "SnipeKlein")
    assert payload["username"] == "SnipeKlein"
    assert len(payload["embeds"]) == 1


def _db(tmp_path) -> sqlite3.Connection:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    return conn


def test_seen_store_dedup_and_ttl(tmp_path) -> None:
    conn = _db(tmp_path)
    store = SeenStore(conn)
    assert store.claim("1", "tag") is True
    assert store.claim("1", "tag") is False
    conn.execute("UPDATE seen_ads SET last_seen_at = '2000-01-01T00:00:00+00:00'")
    assert store.purge(30) == 1
    conn.close()


def test_listing_repo_roundtrip(tmp_path) -> None:
    conn = _db(tmp_path)
    repo = ListingRepo(conn)
    listing = Listing(
        ad_id="42",
        url="https://example.invalid/42",
        title="Gaming PC",
        price=123.0,
        seller_badges=["TOP"],
    )
    repo.upsert(listing)
    stored = repo.get("42")
    assert stored is not None
    assert stored.title == "Gaming PC"
    assert stored.seller_badges == ["TOP"]
    conn.close()


def test_alert_repo_is_idempotent(tmp_path) -> None:
    conn = _db(tmp_path)
    repo = AlertRepo(conn)
    repo.record("1", "actionable", 40.0, "green", "hash")
    repo.record("1", "actionable", 40.0, "green", "hash")
    assert len(repo.recent()) == 1
    conn.close()


def test_usage_repo_accumulates(tmp_path) -> None:
    conn = _db(tmp_path)
    repo = UsageRepo(conn)
    repo.record("m", requests=1, in_tokens=100, out_tokens=20)
    repo.record("m", requests=2, in_tokens=50, out_tokens=10)
    rows = repo.daily()
    assert rows[0]["requests"] == 3
    assert rows[0]["in_tokens"] == 150
    conn.close()
