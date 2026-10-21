"""Query URL construction."""

from __future__ import annotations

from snipeklein.config import QueryConfig
from snipeklein.ingestion.queries import build_search_url, slugify

QUERY = QueryConfig(
    tag="t",
    category="pcs",
    category_id=228,
    keywords=["gaming pc"],
    min_price=300,
    max_price=1800,
)


def test_slugify() -> None:
    assert slugify("Gaming PC") == "gaming-pc"
    assert slugify("RTX 4070 Ti") == "rtx-4070-ti"
    assert slugify("Grafikkarten & Zubehör") == "grafikkarten-zubehor"


def test_search_url_layout() -> None:
    url = build_search_url(QUERY, "gaming pc", 1)
    assert url == "https://www.kleinanzeigen.de/s-pcs/preis:300:1800/gaming-pc/k0c228"


def test_search_url_with_page() -> None:
    url = build_search_url(QUERY, "gaming pc", 3)
    assert "/seite:3/" in url


def test_keyword_comes_after_the_price_segment() -> None:
    """A wrong segment order silently returns unfiltered results."""
    url = build_search_url(QUERY, "rtx 4070", 1)
    assert url.index("/preis:300:1800/") < url.index("/rtx-4070/") < url.index("/k0c228")
