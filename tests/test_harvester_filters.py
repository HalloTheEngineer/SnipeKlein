"""The filters that decide whether a search card is a usable price sample."""

from __future__ import annotations

from snipeklein.config import ValuationConfig
from snipeklein.ingestion.client import Response
from snipeklein.models import SearchCard
from snipeklein.store.db import connect, migrate
from snipeklein.store.repo import PriceRepo
from snipeklein.valuation.harvester import PriceHarvester
from snipeklein.valuation.table import PriceTable

BASE = "https://www.kleinanzeigen.de"


def _harvester(tmp_path):
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    repo = PriceRepo(conn)
    table = PriceTable.load("data/prices.json")
    return PriceHarvester(
        type("C", (), {"get": lambda self, url, params=None: Response(url, 200, "", 0.0)})(),
        table,
        ValuationConfig(),
        repo,
        BASE,
    ), table


def _card(title: str, price: float = 100.0, snippet: str = "", is_pro: bool = False) -> SearchCard:
    return SearchCard(
        ad_id="1",
        url=f"{BASE}/s-anzeige/x/1-225-1",
        title=title,
        price=price,
        snippet=snippet,
        is_pro=is_pro,
    )


def test_complete_systems_are_rejected(tmp_path) -> None:
    harvester, _ = _harvester(tmp_path)
    assert harvester._is_complete_system(_card("Gaming PC RTX 4070 Ryzen 7 7800X3D"))
    assert harvester._is_complete_system(_card("High-End Gaming-PC mit RTX 4090"))
    assert not harvester._is_complete_system(_card("NVIDIA GeForce RTX 4070"))


def test_wanted_ads_are_rejected(tmp_path) -> None:
    harvester, _ = _harvester(tmp_path)
    assert harvester._is_wanted_ad(_card("Suche RTX 4090"))
    assert harvester._is_wanted_ad(_card("Grafikkarte tausche 4070 gegen 3080"))
    assert not harvester._is_wanted_ad(_card("NVIDIA GeForce RTX 4070 gebraucht"))


def test_accessories_are_rejected_but_bundled_coolers_are_not(tmp_path) -> None:
    harvester, _ = _harvester(tmp_path)
    assert harvester._is_accessory(_card("CPU Kühler für Intel Core i5-12400F"))
    assert harvester._is_accessory(_card("Zozac RTX 4090 Kühler (ein Lüfter fehlt)"))
    assert not harvester._is_accessory(_card("Intel Core i5-12400F inkl. Kühler"))


def test_new_stock_and_pro_sellers_are_rejected(tmp_path) -> None:
    harvester, _ = _harvester(tmp_path)
    assert harvester._is_new_stock(_card("Intel Core i5-12400F - Neu & unbenutzt"))
    assert harvester._is_pro_seller(_card("RTX 4070", is_pro=True))
    assert not harvester._is_pro_seller(_card("RTX 4070"))


def test_bundles_are_rejected_per_kind(tmp_path) -> None:
    harvester, _ = _harvester(tmp_path)
    bundle = _card("Intel Core i9-14900K + ASUS ROG Strix Z790-E Mainboard")
    assert harvester._is_bundle(bundle, "cpu")
    system = _card("Gaming PC Ryzen 7 7800X3D RTX 3080 32GB RAM DDR5")
    assert harvester._is_bundle(system, "cpu")
    # a board listing mentions the chipset - that is the component itself
    board = _card("MSI B650 Gaming Plus WIFI Mainboard")
    assert not harvester._is_bundle(board, "board")


def test_exact_match_ignores_other_variants(tmp_path) -> None:
    harvester, table = _harvester(tmp_path)
    rtx_4070 = table.get("NVIDIA_RTX_4070_12GB")
    assert rtx_4070.exact_match("NVIDIA GeForce RTX 4070 12GB")
    assert not rtx_4070.exact_match("NVIDIA GeForce RTX 4070 Ti")
    assert not rtx_4070.exact_match("NVIDIA GeForce RTX 4070 SUPER")

    ti = table.get("NVIDIA_RTX_4070_TI")
    assert ti.exact_match("ASUS TUF Gaming RTX 4070 Ti 12G")
    assert not ti.exact_match("RTX 4070 Ti SUPER 16GB")

    ryzen = table.get("AMD_RYZEN_7_7800X3D")
    assert ryzen.exact_match("AMD Ryzen 7 7800X3D")
    assert not ryzen.exact_match("AMD Ryzen 7 7800X")

    xtx = table.get("AMD_RX_7900_XTX")
    assert xtx.exact_match("Sapphire RX 7900 XTX")
    assert not xtx.exact_match("Sapphire RX 7900 XT")
