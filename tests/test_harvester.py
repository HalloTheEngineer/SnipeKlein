"""Price harvester (Kleinanzeigen only)."""

from __future__ import annotations

from pathlib import Path

from snipeklein.config import ValuationConfig
from snipeklein.ingestion.client import Response
from snipeklein.store.db import connect, migrate
from snipeklein.store.repo import PriceRepo
from snipeklein.valuation.harvester import PriceHarvester, import_prices_from_file
from snipeklein.valuation.table import PriceTable

BASE = "https://www.kleinanzeigen.de"


def _card_html(title: str, price: str) -> str:
    return f"""
    <article data-adid="1" data-href="/s-anzeige/x/1-225-1">
      <h3><a href="/s-anzeige/x/1-225-1">{title}</a></h3>
      <p>Ein Angebot</p>
      <p class="font-strong">{price} &euro;</p>
    </article>
    """


class FakeClient:
    def __init__(self, html: str) -> None:
        self.html = html
        self.urls: list[str] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> Response:
        self.urls.append(url)
        return Response(url=url, status=200, text=self.html, elapsed_s=0.1)

    def close(self) -> None:
        pass


def _harvester(tmp_path: Path, html: str) -> tuple[PriceHarvester, PriceRepo]:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    repo = PriceRepo(conn)
    table = PriceTable.load("data/prices.json")
    return (
        PriceHarvester(FakeClient(html), table, ValuationConfig(), repo, BASE),
        repo,
    )


def test_robust_price_uses_the_median(tmp_path: Path) -> None:
    html = "".join(
        _card_html("RTX 4070 Grafikarte", price) for price in ("400", "420", "440", "460", "480")
    )
    harvester, repo = _harvester(tmp_path, html)
    result = harvester._harvest_component(  # noqa: SLF001
        "NVIDIA_RTX_4070_12GB", "rtx 4070", "gpu", harvester.table.get("NVIDIA_RTX_4070_12GB")
    )
    assert result.samples == 5
    # median 440 * asking_to_sold_factor 0.92
    assert result.fmv_eur == 404.8
    stored = repo.all_prices()["NVIDIA_RTX_4070_12GB"]
    assert stored["fmv_eur"] == 404.8
    assert stored["source"] == "kleinanzeigen"


def test_outliers_are_trimmed(tmp_path: Path) -> None:
    prices = ["400", "410", "420", "430", "440", "5000"]
    html = "".join(_card_html("RTX 4070 Grafikarte", price) for price in prices)
    harvester, _ = _harvester(tmp_path, html)
    result = harvester._harvest_component(  # noqa: SLF001
        "NVIDIA_RTX_4070_12GB", "rtx 4070", "gpu", harvester.table.get("NVIDIA_RTX_4070_12GB")
    )
    assert result.samples < len(prices)
    assert result.fmv_eur is not None and result.fmv_eur < 1000


def test_too_few_samples_are_ignored(tmp_path: Path) -> None:
    html = _card_html("RTX 4070 Grafikarte", "400")
    harvester, repo = _harvester(tmp_path, html)
    result = harvester._harvest_component(  # noqa: SLF001
        "NVIDIA_RTX_4070_12GB", "rtx 4070", "gpu", harvester.table.get("NVIDIA_RTX_4070_12GB")
    )
    assert result.fmv_eur is None
    assert "NVIDIA_RTX_4070_12GB" not in repo.all_prices()


def test_csv_import(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    repo = PriceRepo(conn)
    csv_path = tmp_path / "prices.csv"
    csv_path.write_text(
        "component_id;fmv_eur\nNVIDIA_RTX_4070_12GB;435,50\n", encoding="utf-8"
    )
    count = import_prices_from_file(str(csv_path), repo)
    assert count == 1
    assert repo.all_prices()["NVIDIA_RTX_4070_12GB"]["fmv_eur"] == 435.5
