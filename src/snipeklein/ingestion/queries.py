"""Query URL construction (spec 3.2).

The live site encodes filters into the path:

    https://www.kleinanzeigen.de/s-{category}/preis:{min}:{max}[/seite:N]/{keyword}/k{loc}c{cat}

Putting the keyword into the wrong slot silently returns unfiltered results,
so the builder is strict about the order of the segments.
"""

from __future__ import annotations

import re
import unicodedata

from ..config import QueryConfig

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase, ASCII-fold, hyphen separated (umlauts become ae/oe/ue)."""
    folded = unicodedata.normalize("NFKD", value)
    ascii_text = folded.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("ß", "ss")
    return _NON_WORD.sub("-", ascii_text.lower()).strip("-")


def build_search_url(
    query: QueryConfig,
    keyword: str | None = None,
    page: int = 1,
    base_url: str = "https://www.kleinanzeigen.de",
    sort_segment: str = "",
) -> str:
    """Build a search URL for one keyword (defaults to the first keyword)."""
    kw = keyword if keyword is not None else (query.keywords[0] if query.keywords else "")
    parts = [f"/s-{slugify(query.category)}"]
    if query.max_price > 0:
        parts.append(f"preis:{query.min_price}:{query.max_price}")
    if page > 1:
        parts.append(f"seite:{page}")
    if kw:
        parts.append(slugify(kw))
    parts.append(f"k0c{query.category_id}")
    if sort_segment:
        parts.insert(1, sort_segment)
    return base_url.rstrip("/") + "/".join(parts)


def listing_url(base_url: str, href: str) -> str:
    """Absolute URL of a listing detail page."""
    if href.startswith("http"):
        return href
    return base_url.rstrip("/") + href


def iter_query_urls(
    query: QueryConfig,
    base_url: str = "https://www.kleinanzeigen.de",
    sort_segment: str = "",
) -> list[str]:
    """All URLs (keyword x page) that belong to a query."""
    urls: list[str] = []
    for keyword in query.keywords or [""]:
        for page in range(1, max(1, query.max_pages) + 1):
            urls.append(build_search_url(query, keyword, page, base_url, sort_segment))
    return urls
