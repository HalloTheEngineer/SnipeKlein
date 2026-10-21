"""Capture real pages as offline fixtures for the test suite.

Usage:  python tools/capture_fixtures.py [output_dir]

Pages are stored gzipped (``.html.gz``) - the raw detail pages are ~2 MB each.
"""

from __future__ import annotations

import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from snipeklein.config import load_config  # noqa: E402
from snipeklein.ingestion.client import KleinanzeigenClient  # noqa: E402
from snipeklein.ingestion.queries import build_search_url  # noqa: E402
from snipeklein.ingestion.search_parser import parse_cards  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures"


def write_gz(path: Path, text: str) -> None:
    with gzip.open(path.with_suffix(path.suffix + ".gz"), "wt", encoding="utf-8") as fh:
        fh.write(text)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURES
    out.mkdir(parents=True, exist_ok=True)
    config = load_config()
    client = KleinanzeigenClient(config.ingestion)
    base = config.ingestion.base_url

    searches = {
        "search_pcs_gaming.html": ("pcs", 228, "gaming pc"),
        "search_grafikkarten.html": ("grafikkarten", 225, "rtx 4070"),
    }
    for name, (category, category_id, keyword) in searches.items():
        url = build_search_url(
            type(
                "Q",
                (),
                {
                    "category": category,
                    "category_id": category_id,
                    "min_price": 300,
                    "max_price": 1800,
                },
            )(),
            keyword,
            1,
            base,
        )
        response = client.get(url)
        write_gz(out / name, response.text)
        cards = parse_cards(response.text, base)
        print(f"{name}: {len(cards)} cards -> {url}")

        for index, card in enumerate(cards[:6]):
            detail = client.get(card.url).text
            write_gz(out / f"detail_{index}_{card.ad_id}.html", detail)
            print(f"  detail_{index}_{card.ad_id}.html: {card.title[:60]}")

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
