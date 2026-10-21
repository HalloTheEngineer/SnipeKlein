"""Small text helpers shared by parsers and the valuation engine."""

from __future__ import annotations

import re
from datetime import datetime

_PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+)")

_GERMAN_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

_NORMALIZE_RE = re.compile(r"[^A-Z0-9]+")


def parse_price_eur(text: str | None) -> float | None:
    """Parse a German price string ('1.200 € VB', '999,50 €') into a float."""
    if not text:
        return None
    match = _PRICE_RE.search(text.replace(" ", " "))
    if not match:
        return None
    raw = match.group(1)
    raw = raw.replace(".", "").replace(",", ".") if "," in raw else raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def has_price_suffix(text: str) -> bool:
    return "€" in (text or "")


def parse_german_date(text: str | None) -> datetime | None:
    """Parse '30.12.2025' into a datetime."""
    if not text:
        return None
    match = _GERMAN_DATE_RE.search(text)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def normalize_key(text: str | None) -> str:
    """Uppercase alphanumeric key used to match component aliases."""
    if not text:
        return ""
    return _NORMALIZE_RE.sub("", text.upper())


def slug(text: str | None) -> str:
    if not text:
        return ""
    return _NORMALIZE_RE.sub("_", text.upper()).strip("_")
