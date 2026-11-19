"""Tier-2 semantic extraction (spec 4.2).

The LLM only *extracts* the specification - prices always come from the
deterministic table. If no LLM is available (no API key, quota exhausted,
network error) the deterministic regex extractor takes over so the bot keeps
running.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..config import LlmConfig
from ..logging_setup import get_logger
from ..models import Extraction
from .llm import LlmError, ProviderFactory, QuotaExhausted, parse_json_reply
from .schema import EXTRACTION_SCHEMA, SYSTEM_PROMPT, build_user_prompt

log = get_logger("snipeklein.extract.extractor")

PROMPT_VERSION = "2026-10-20.v1"


@dataclass
class ExtractionOutcome:
    extraction: Extraction
    source: str
    model: str = ""
    rejected: str = ""
    degraded: bool = False
    overrides: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected


class Extractor:
    def __init__(
        self,
        config: LlmConfig,
        required_ram_generation: str = "DDR5",
        require_complete_system: bool = True,
    ) -> None:
        self.config = config
        self.required_ram_generation = required_ram_generation.upper()
        self.require_complete_system = require_complete_system
        self.factory = ProviderFactory(config)
        self.factory.build()

    def extract(self, title: str, description: str = "") -> ExtractionOutcome:
        text = f"{title}\n{description}"
        try:
            reply = self.factory.complete(
                SYSTEM_PROMPT,
                build_user_prompt(title, description),
                EXTRACTION_SCHEMA,
            )
        except QuotaExhausted:
            log.warning("LLM quota exhausted - using the deterministic fallback")
            return self._fallback(text, source="fallback", model="regex")
        except LlmError as exc:
            log.warning("LLM unavailable (%s) - using the deterministic fallback", exc)
            return self._fallback(text, source="fallback", model="regex")

        try:
            payload = parse_json_reply(reply)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("could not parse LLM output (%s) - using the fallback", exc)
            return self._fallback(text, source="fallback", model="regex", degraded=True)

        extraction = _coerce_extraction(payload)
        overrides = apply_spec_override(extraction, f"{title}\n{description}")
        rejected = self._apply_rules(extraction)
        return ExtractionOutcome(
            extraction=extraction,
            source="llm",
            model=reply.model,
            rejected=rejected,
            degraded=reply.degraded,
            overrides=overrides,
        )

    # -- rules -------------------------------------------------------------
    def _apply_rules(self, extraction: Extraction) -> str:
        if self.require_complete_system and not extraction.is_complete_system:
            return "not_a_complete_system"
        ram = (extraction.ram_generation or "UNKNOWN").upper()
        if ram != self.required_ram_generation:
            return f"ram_generation:{ram}"
        return ""

    # -- fallback ----------------------------------------------------------
    def _fallback(
        self, text: str, source: str = "fallback", model: str = "regex", degraded: bool = False
    ) -> ExtractionOutcome:
        extraction = regex_extract(text)
        overrides = apply_spec_override(extraction, text)
        rejected = self._apply_rules(extraction)
        return ExtractionOutcome(
            extraction=extraction,
            source=source,
            model=model,
            rejected=rejected,
            degraded=degraded,
            overrides=overrides,
        )


def _coerce_extraction(payload: dict) -> Extraction:
    """Validate the LLM payload, tolerating stringified numbers."""
    data = dict(payload)
    for key in ("ram_capacity_gb", "storage_capacity_gb", "psu_wattage"):
        value = data.get(key)
        if isinstance(value, str):
            digits = re.search(r"\d+", value)
            data[key] = int(digits.group(0)) if digits else None
        elif isinstance(value, float):
            data[key] = int(value)
    generation = data.get("ram_generation")
    if generation not in ("DDR4", "DDR5", "UNKNOWN"):
        upper = str(generation or "").upper()
        data["ram_generation"] = upper if upper in ("DDR4", "DDR5") else "UNKNOWN"
    defects = data.get("defects_or_missing_parts")
    if isinstance(defects, str):
        data["defects_or_missing_parts"] = [defects] if defects.strip() else []
    elif not isinstance(defects, list):
        data["defects_or_missing_parts"] = []
    data["defects_or_missing_parts"] = [str(d) for d in data["defects_or_missing_parts"]]
    try:
        return Extraction.model_validate(data)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("LLM payload did not validate (%s) - using defaults", exc)
        return Extraction()


# -- deterministic fallback ------------------------------------------------
_CPU_AMD = re.compile(r"\b(ryzen\s?[3579]\s?\d{4}[a-z0-9]{0,3})\b", re.IGNORECASE)
_CPU_INTEL = re.compile(r"\b(i[579][-\s]?\d{4,5}[a-z]{0,2})\b", re.IGNORECASE)
# GTX belongs here as much as RTX: DDR5 builds often keep an old card.
_GPU_NVIDIA = re.compile(
    r"\b((?:rtx|gtx)\s?\d{3,4}(?:\s?(?:ti|super)){0,2})\b", re.IGNORECASE
)
_GPU_AMD = re.compile(r"\b(rx\s?\d{3,4}(?:\s?(?:xtx?|gre|s))?)\b", re.IGNORECASE)
_RAM_GEN = re.compile(r"\bddr\s?([45])\b", re.IGNORECASE)
_RAM_CAP = re.compile(
    r"(\d{1,3})\s?gb\s?(?:ram|arbeitsspeicher|ddr[45])|(?:ram|arbeitsspeicher)\s?(\d{1,3})\s?gb",
    re.IGNORECASE,
)
_RAM_KIT = re.compile(r"\b(\d)\s?x\s?(\d{1,3})\s?gb\b", re.IGNORECASE)
_STORAGE_WORD = re.compile(r"\b(?:ssd|nvme|m\.2|festplatte|hdd)\b", re.IGNORECASE)
_STORAGE_SIZE = re.compile(r"\b(\d{1,3}(?:[.,]\d)?)\s?(gb|tb)\b", re.IGNORECASE)
_BOARD = re.compile(r"\b(b\d{3}e?|x\d{3}e?|a\d{3}|z\d{3}|h\d{3}e?)\b", re.IGNORECASE)
_PSU = re.compile(r"(\d{3,4})\s?w(?:att)?\b", re.IGNORECASE)
_DEFECT = re.compile(
    r"(defekt|kaputt|beschaedigt|beschädigt|defekter|fehlerhaft|goes out|fliegt raus|"
    r"kein\s+\w+\s+dabei|ohne\s+\w+)",
    re.IGNORECASE,
)
_PICKUP = re.compile(r"(nur\s+abholung|nur\s+selbstabholung|kein\s+versand|abholung\s+nur)", re.IGNORECASE)
_SYSTEM = re.compile(
    r"(gaming[\s-]?pc|\bpc\b|computer|rechner|komplett|workstation|system|tower|desktop)",
    re.IGNORECASE,
)
_WINDOWS = re.compile(r"(windows\s?(10|11)|win\s?(10|11))", re.IGNORECASE)
_NO_OS = re.compile(r"(ohne\s+(windows|betriebssystem|os)|kein\s+betriebssystem)", re.IGNORECASE)

# Sentences that sell an upgrade path, not the installed part: "aufrüstbar mit
# RTX 4070/5070", "kompatibel mit", "ready for".  Those mentions must never
# become the GPU of the system.
_UPGRADE_MARKERS = re.compile(
    r"(aufr(?:[uü]|ue)stbar|aufr(?:[uü]|ue)sten|aufger(?:[uü]|ue)stet|upgrade|"
    r"kompatibel|geeignet|unterst(?:[uü]|ue)tzt|ready\s+(?:for|to)|optional|"
    r"erweiterbar|nachr(?:[uü]|ue)sten|z\.\s?b\.)",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")

# A labelled specification line is authoritative: "* Grafikkarte: GTX 1060".
_SPEC_LINE = re.compile(r"^\s*[-*•·]?\s*([^:\n]{2,40}?)\s*[:\-–]\s*(.+)$", re.MULTILINE)
_SPEC_LABELS: dict[str, tuple[str, ...]] = {
    "gpu": ("grafikkarte", "gpu", "graphics card"),
    "cpu": ("prozessor", "cpu", "processor"),
    "ram": ("arbeitsspeicher", "ram", "memory"),
    "storage": ("speicher", "ssd", "festplatte", "nvme", "storage"),
    "board": ("mainboard", "motherboard", "board"),
    "psu": ("netzteil", "psu", "power supply"),
}


def _spec_lines(text: str) -> dict[str, str]:
    """The values of labelled spec lines, keyed by component kind."""
    found: dict[str, str] = {}
    for match in _SPEC_LINE.finditer(text or ""):
        label = match.group(1).strip().lower()
        value = match.group(2).strip()
        if not value:
            continue
        for kind, names in _SPEC_LABELS.items():
            if kind in found:
                continue
            if any(name in label for name in names):
                found[kind] = value[:200]
                break
    return found


def _ram_from_value(value: str) -> tuple[str | None, int | None]:
    generation = None
    gen_match = _RAM_GEN.search(value)
    if gen_match:
        generation = f"DDR{gen_match.group(1)}"
    kit = _RAM_KIT.search(value)
    if kit:
        return generation, int(kit.group(1)) * int(kit.group(2))
    size = re.search(r"\b(\d{1,3})\s?gb\b", value, re.IGNORECASE)
    return generation, int(size.group(1)) if size else None


def _storage_from_value(value: str) -> int | None:
    match = _STORAGE_SIZE.search(value)
    if not match:
        return None
    amount = float(match.group(1).replace(",", "."))
    if match.group(2).lower() == "tb":
        return int(round(amount * 1024))
    return int(round(amount))


def _storage_scan(text: str) -> int | None:
    """Find the drive size - but only in segments that talk about a drive.

    "32 GB DDR5 RAM, 1 TB NVMe SSD": scanning the whole text would take the
    RAM size, so each segment is checked for a storage word first.
    """
    for segment in re.split(r"[,;+&\n]", text):
        if not _STORAGE_WORD.search(segment):
            continue
        capacity = _storage_from_value(segment)
        if capacity:
            return capacity
    return None


def _spec_fields(text: str) -> dict[str, object]:
    """Extract the component fields from the labelled spec lines."""
    fields: dict[str, object] = {}
    for kind, value in _spec_lines(text).items():
        if kind == "gpu":
            match = _GPU_NVIDIA.search(value) or _GPU_AMD.search(value)
            if match:
                fields["gpu_model"] = match.group(1).strip()
        elif kind == "cpu":
            match = _CPU_AMD.search(value) or _CPU_INTEL.search(value)
            if match:
                fields["cpu_model"] = match.group(1).strip()
        elif kind == "ram":
            generation, capacity = _ram_from_value(value)
            if generation:
                fields["ram_generation"] = generation
            if capacity:
                fields["ram_capacity_gb"] = capacity
        elif kind == "storage":
            capacity = _storage_from_value(value)
            if capacity:
                fields["storage_capacity_gb"] = capacity
        elif kind == "board":
            match = _BOARD.search(value)
            if match:
                fields["motherboard_chipset"] = match.group(1).strip()
        elif kind == "psu":
            match = _PSU.search(value)
            if match:
                fields["psu_wattage"] = int(match.group(1))
    return fields


def _informative_text(text: str) -> str:
    """The text without the sentences that only describe upgrade options."""
    sentences = _SENTENCE.split(text or "")
    return "\n".join(part for part in sentences if part and not _UPGRADE_MARKERS.search(part))


def apply_spec_override(extraction: Extraction, text: str) -> list[str]:
    """Prefer the labelled spec lines over what was read in the marketing text.

    Returns the field names that were changed, so the decision can record it.
    """
    changed: list[str] = []
    for field_name, value in _spec_fields(text).items():
        if value in (None, ""):
            continue
        if getattr(extraction, field_name, None) == value:
            continue
        setattr(extraction, field_name, value)
        changed.append(field_name)
    return changed


def regex_extract(text: str) -> Extraction:
    """Best effort extraction without an LLM."""
    spec = _spec_fields(text)
    scan = _informative_text(text)

    cpu_match = _CPU_AMD.search(scan) or _CPU_INTEL.search(scan)
    gpu_match = _GPU_NVIDIA.search(scan) or _GPU_AMD.search(scan)

    ram_gen = "UNKNOWN"
    gen_match = _RAM_GEN.search(scan)
    if gen_match:
        ram_gen = f"DDR{gen_match.group(1)}"

    ram_capacity = spec.get("ram_capacity_gb")
    if not isinstance(ram_capacity, int):
        ram_capacity = None
        cap_match = _RAM_CAP.search(scan)
        if cap_match:
            value = cap_match.group(1) or cap_match.group(2)
            if value:
                ram_capacity = int(value)

    storage = spec.get("storage_capacity_gb")
    if not isinstance(storage, int):
        storage = _storage_scan(scan)

    board = spec.get("motherboard_chipset")
    if not isinstance(board, str) or not board:
        board_match = _BOARD.search(scan)
        board = board_match.group(1).strip() if board_match else None

    psu = spec.get("psu_wattage")
    if not isinstance(psu, int):
        psu_match = _PSU.search(scan)
        psu = int(psu_match.group(1)) if psu_match else None

    cpu_model = spec.get("cpu_model")
    if not isinstance(cpu_model, str) or not cpu_model:
        cpu_model = cpu_match.group(1).strip() if cpu_match else None
    gpu_model = spec.get("gpu_model")
    if not isinstance(gpu_model, str) or not gpu_model:
        gpu_model = gpu_match.group(1).strip() if gpu_match else None
    generation = spec.get("ram_generation")
    if isinstance(generation, str) and generation:
        ram_gen = generation

    defects: list[str] = []
    for match in _DEFECT.finditer(text):
        snippet = match.group(0).strip()
        if snippet.lower().startswith(("kein ", "ohne ")):
            continue
        if snippet not in defects:
            defects.append(snippet)

    is_system = bool(_SYSTEM.search(text)) and bool(cpu_model or gpu_model)

    return Extraction(
        is_complete_system=is_system,
        cpu_model=cpu_model,
        gpu_model=gpu_model,
        ram_generation=ram_gen,
        ram_capacity_gb=ram_capacity,
        storage_capacity_gb=storage,
        motherboard_chipset=board,
        psu_wattage=psu,
        operating_system_included=bool(_WINDOWS.search(text)) and not bool(_NO_OS.search(text)),
        defects_or_missing_parts=defects,
        pickup_only=bool(_PICKUP.search(text)),
    )
