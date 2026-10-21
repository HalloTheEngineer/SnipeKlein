"""Tier-2 semantic extraction (spec 4.2).

The LLM only *extracts* the specification - prices always come from the
deterministic table. If no LLM is available (no API key, quota exhausted,
network error) the deterministic regex extractor takes over so the bot keeps
running.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

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
        rejected = self._apply_rules(extraction)
        return ExtractionOutcome(
            extraction=extraction,
            source="llm",
            model=reply.model,
            rejected=rejected,
            degraded=reply.degraded,
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
        rejected = self._apply_rules(extraction)
        return ExtractionOutcome(
            extraction=extraction, source=source, model=model, rejected=rejected, degraded=degraded
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
_GPU_NVIDIA = re.compile(r"\b(rtx\s?\d{4}(?:\s?(?:ti|super))?)\b", re.IGNORECASE)
_GPU_AMD = re.compile(r"\b(rx\s?[56789]\d{3}\s?(?:xtx?|s)?)\b", re.IGNORECASE)
_RAM_GEN = re.compile(r"\bddr\s?([45])\b", re.IGNORECASE)
_RAM_CAP = re.compile(
    r"(\d{1,3})\s?gb\s?(?:ram|arbeitsspeicher|ddr[45])|(?:ram|arbeitsspeicher)\s?(\d{1,3})\s?gb",
    re.IGNORECASE,
)
_STORAGE = re.compile(r"(\d{1,3})\s?(gb|tb)\s?(?:ssd|nvme|spiegelplatte|storage)", re.IGNORECASE)
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


def regex_extract(text: str) -> Extraction:
    """Best effort extraction without an LLM."""
    cpu = _CPU_AMD.search(text) or _CPU_INTEL.search(text)
    gpu = _GPU_NVIDIA.search(text) or _GPU_AMD.search(text)

    ram_gen = "UNKNOWN"
    gen_match = _RAM_GEN.search(text)
    if gen_match:
        ram_gen = f"DDR{gen_match.group(1)}"

    ram_capacity: int | None = None
    cap_match = _RAM_CAP.search(text)
    if cap_match:
        value = cap_match.group(1) or cap_match.group(2)
        if value:
            ram_capacity = int(value)

    storage: int | None = None
    storage_match = _STORAGE.search(text)
    if storage_match:
        amount = int(storage_match.group(1))
        unit = storage_match.group(2).lower()
        storage = amount * 1024 if unit == "tb" else amount

    board = _BOARD.search(text)
    psu = _PSU.search(text)

    defects: list[str] = []
    for match in _DEFECT.finditer(text):
        snippet = match.group(0).strip()
        if snippet.lower().startswith(("kein ", "ohne ")):
            continue
        if snippet not in defects:
            defects.append(snippet)

    is_system = bool(_SYSTEM.search(text)) and bool(cpu or gpu)

    return Extraction(
        is_complete_system=is_system,
        cpu_model=cpu.group(1).strip() if cpu else None,
        gpu_model=gpu.group(1).strip() if gpu else None,
        ram_generation=ram_gen,
        ram_capacity_gb=ram_capacity,
        storage_capacity_gb=storage,
        motherboard_chipset=board.group(1).strip() if board else None,
        psu_wattage=int(psu.group(1)) if psu else None,
        operating_system_included=bool(_WINDOWS.search(text)) and not bool(_NO_OS.search(text)),
        defects_or_missing_parts=defects,
        pickup_only=bool(_PICKUP.search(text)),
    )
