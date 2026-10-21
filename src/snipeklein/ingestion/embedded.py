"""Helpers to read the JSON state that the site embeds into its pages.

The detail page renders an Astro island whose ``props`` attribute contains the
complete ad state (price, condition, seller, location, ...).  The values use
the Astro serialisation where every value is wrapped in ``[0, value]``.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

_PROPS_RE = re.compile(r'props="([^"]*)"')


def unwrap(value: Any) -> Any:
    """Unwrap the Astro ``[0, value]`` encoding."""
    while isinstance(value, list) and len(value) == 2 and value[0] == 0:
        value = value[1]
    return value


def deep_unwrap(value: Any) -> Any:
    """Recursively unwrap every ``[0, value]`` wrapper."""
    value = unwrap(value)
    if isinstance(value, dict):
        return {key: deep_unwrap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [deep_unwrap(item) for item in value]
    return value


def extract_props(html: str) -> dict[str, Any]:
    """Return all successfully parsed ``props`` payloads of the page."""
    payloads: list[Any] = []
    for match in _PROPS_RE.finditer(html):
        raw = match.group(1)
        if len(raw) < 2 or not raw.startswith("{"):
            continue
        try:
            payloads.append(json.loads(html_lib.unescape(raw)))
        except (ValueError, TypeError):
            continue
    return {"payloads": payloads}


def find_key(payloads: Any, key: str) -> Any:
    """Depth first search for ``key`` in the embedded payloads."""
    if isinstance(payloads, dict):
        if key in payloads:
            return unwrap(payloads[key])
        for value in payloads.values():
            found = find_key(value, key)
            if found is not None:
                return found
    elif isinstance(payloads, list):
        for value in payloads:
            found = find_key(value, key)
            if found is not None:
                return found
    return None


def find_all(payloads: Any, key: str) -> list[Any]:
    """All values stored under ``key``."""
    found: list[Any] = []
    if isinstance(payloads, dict):
        if key in payloads:
            found.append(unwrap(payloads[key]))
        for value in payloads.values():
            found.extend(find_all(value, key))
    elif isinstance(payloads, list):
        for value in payloads:
            found.extend(find_all(value, key))
    return found


_AD_MARKERS = ("userDetails", "formattedCreationDate", "localizedAttributes", "adMedias")


def extract_ad_payload(props: dict[str, Any]) -> dict[str, Any]:
    """The island payload that describes the ad itself.

    The page contains many islands (navigation, footer, ...); only one of them
    carries the ad state.
    """
    for payload in props.get("payloads", []):
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        data = unwrap(data)
        if not isinstance(data, dict):
            continue
        if any(marker in data for marker in _AD_MARKERS):
            return deep_unwrap(data)
    return {}
