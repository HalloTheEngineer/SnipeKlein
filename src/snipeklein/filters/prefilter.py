"""Tier-1 coarse keyword rejection (spec 4.1).

Runs on the search card title (+ snippet) so that no detail page is fetched and
no LLM call is spent on obviously irrelevant listings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Titles that clearly are not an offer of the requested system.
EXCLUDE_TITLE_PATTERNS = [
    r"\bsuche\b",
    r"\bgesucht\b",
    r"\btausch\b",
    r"\btausche\b",
    r"\bdefekt\b",
    r"\bkaputt\b",
    r"\bersatzteil",
    r"\bnur geh\u00e4use\b",
    r"\bnur gehause\b",
    r"\bleerkauf\b",
    r"\breparatur\b",
    r"\bdemonstrator\b",
    r"\bgewerbe\b",
    r"\babo\b",
]

# Older architectural markers: the target profile is a current-gen DDR5 build.
EXCLUDE_SPEC_PATTERNS = [
    r"\bddr\s?3\b",
    r"\bddr\s?4\b",
    r"\bam4\b",
    r"\blga\s?1200\b",
    r"\bgtx\b",
    r"\bfx[- ]?\d",
    r"\brx\s?5[0-4]\d0\b",
    r"\bpentium\b",
    r"\bceleron\b",
    r"\bryzen\s?3\s?3200g\b",
    r"\bryzen\s?[3579]\s?(2600|3600|5600|3700x|5700x|3900x|5900x)\b",
    r"\bi[579]-?10[4679]00\b",
    r"\bi[579]-?11[4679]00\b",
]

# A system has to look like a system.
SYSTEM_PATTERNS = [
    r"\bgaming[\s-]?pc\b",
    r"\bpc\b",
    r"\bcomputer\b",
    r"\brechner\b",
    r"\bsystem\b",
    r"\bworkstation\b",
    r"\bkomplett",
    r"\bdesktop\b",
    r"\btower\b",
    r"\bhome\s?office\b",
    r"\babuild\b",
]

# ... and it has to contain at least one component hint.
COMPONENT_PATTERNS = [
    r"\brtx\s?\d{4}",
    r"\bgtx\s?\d{3,4}",
    r"\brx\s?[56789]\d{3}",
    r"\bgeforce\b",
    r"\bradeon\b",
    r"\bryzen\s?[3579]\s?\d{4}",
    r"\bcore\s?i[579]\b",
    r"\bi[579]-?\d{4,5}\b",
    r"\bddr5\b",
]

# A title that hints at a current generation (DDR5 capable) platform.  Ads
# like this are worth an extraction even when the price table does not know
# the exact model yet - the description is where the spec usually is.
MODERN_PLATFORM_PATTERNS = [
    r"\brtx\s?40[6-9]0\b",
    r"\brtx\s?50[0-9]0\b",
    r"\brx\s?7[6-9]00\b",
    r"\bryzen\s?[3579]\s?(7[0-9]00|9[0-9]00)\b",
    r"\b(7[0-9]00x3d|9800x3d|7900x3d|9700x|9600x|7700x|7500f|8400f)\b",
    r"\bi[579]-?1[234][0-9]00\b",
    r"\bam5\b",
    r"\b(b650|b650e|x670|x670e|z790|b760)\b",
    r"\bddr5\b",
]


@dataclass(frozen=True)
class PrefilterResult:
    passed: bool
    reason: str = ""

    @classmethod
    def reject(cls, reason: str) -> PrefilterResult:
        return cls(passed=False, reason=reason)

    @classmethod
    def accept(cls) -> PrefilterResult:
        return cls(passed=True)


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_EXCLUDE_TITLE = _compile(EXCLUDE_TITLE_PATTERNS)
_EXCLUDE_SPEC = _compile(EXCLUDE_SPEC_PATTERNS)
_SYSTEM = _compile(SYSTEM_PATTERNS)
_COMPONENT = _compile(COMPONENT_PATTERNS)
_MODERN = _compile(MODERN_PLATFORM_PATTERNS)


def is_modern_platform(text: str) -> bool:
    """Does the title hint at a current generation (DDR5 capable) platform?"""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _MODERN)


def evaluate(text: str) -> PrefilterResult:
    """Apply the tier-1 rules to a title (or title + snippet)."""
    if not text or not text.strip():
        return PrefilterResult.reject("empty_title")

    for pattern in _EXCLUDE_TITLE:
        if pattern.search(text):
            return PrefilterResult.reject(f"title_exclude:{pattern.pattern}")

    for pattern in _EXCLUDE_SPEC:
        if pattern.search(text):
            return PrefilterResult.reject(f"spec_exclude:{pattern.pattern}")

    if not any(p.search(text) for p in _SYSTEM):
        return PrefilterResult.reject("no_system_marker")

    if not any(p.search(text) for p in _COMPONENT):
        return PrefilterResult.reject("no_component_marker")

    return PrefilterResult.accept()
