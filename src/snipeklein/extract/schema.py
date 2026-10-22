"""Extraction schema and prompt (spec 4.2).

The JSON schema is hand written instead of being generated from the pydantic
model: Gemini's structured output only supports a subset of JSON Schema
(``anyOf``/``oneOf`` are rejected), so nullables are expressed as type arrays.
"""

from __future__ import annotations

from typing import Any

# How much of the description goes into the prompt.
MAX_DESCRIPTION_CHARS = 12000

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_complete_system": {
            "type": "boolean",
            "description": "true if the ad sells a complete, ready to use computer.",
        },
        "cpu_model": {
            "type": ["string", "null"],
            "description": "Exact CPU model, e.g. 'AMD Ryzen 7 7800X3D'.",
        },
        "gpu_model": {
            "type": ["string", "null"],
            "description": "Exact GPU model, e.g. 'NVIDIA GeForce RTX 4070'.",
        },
        "ram_generation": {
            "type": "string",
            "enum": ["DDR4", "DDR5", "UNKNOWN"],
            "description": "RAM generation. UNKNOWN if the ad does not say.",
        },
        "ram_capacity_gb": {"type": ["integer", "null"]},
        "storage_capacity_gb": {
            "type": ["integer", "null"],
            "description": "Total primary storage capacity in GB.",
        },
        "motherboard_chipset": {
            "type": ["string", "null"],
            "description": "Chipset or board name, e.g. 'B650' or 'B550'.",
        },
        "psu_wattage": {"type": ["integer", "null"], "description": "PSU wattage."},
        "operating_system_included": {"type": "boolean"},
        "defects_or_missing_parts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Damaged, missing or not included parts. Empty if none.",
        },
        "pickup_only": {
            "type": "boolean",
            "description": "true if the seller only offers pickup ('Nur Abholung').",
        },
        "accessories": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Accessories that are included in the price, e.g. '24 Zoll Monitor', "
                "'Tastatur und Maus', 'Buerostuhl'. Empty if the ad is just the computer."
            ),
        },
    },
    "required": [
        "is_complete_system",
        "cpu_model",
        "gpu_model",
        "ram_generation",
        "ram_capacity_gb",
        "storage_capacity_gb",
        "motherboard_chipset",
        "psu_wattage",
        "operating_system_included",
        "defects_or_missing_parts",
        "pickup_only",
        "accessories",
    ],
}

SYSTEM_PROMPT = """\
Du bist ein technischer Ad-Parser fuer kleinanzeigen.de.
Extrahiere aus dem Inhalt einer Anzeige ausschliesslich Komponenten, die \
explizit genannt werden. Erfinde nichts: Wenn etwas nicht genannt wird, setze \
den Wert auf null (oder UNKNOWN bei ram_generation).

Regeln:
- is_complete_system = true nur, wenn ein vollstaendiger, einsatzbereiter \
Computer verkauft wird (keine Einzelkomponente, kein Wunschgesuch).
- ram_generation: DDR5, DDR4 oder UNKNOWN. Nur ausdruecklich genannte \
RAM-Angaben zaehlen. Wenn die Anzeige die RAM-Generation nicht nennt, \
immer UNKNOWN - niemals aus dem verbauten Board oder Prozessor ableiten.
- ram_capacity_gb / storage_capacity_gb: Zahlen in GB ohne Einheit. \
"1 TB" = 1024 GB, "2 TB" = 2048 GB.
- psu_wattage: nur wenn eine Wattzahl genannt wird (z.B. "750W").
- defects_or_missing_parts: nur echte Maengel oder fehlende Teile, z.B. \
"Netzteil defekt", "kein RAM dabei", "kein Betriebssystem". Leere Liste, \
wenn nichts auffaellig ist.
- pickup_only: true bei "Nur Abholung" / "Nur Selbstabholung".
- accessories: nur das auffuehren, was im Preis enthalten ist (Monitor, Tastatur,
  Maus, Stuhl, Kopfhoerer, Lautsprecher, Webcam, Kamera). Leere Liste, wenn nur
  der Computer verkauft wird.
- Antworte ausschliesslich mit einem JSON-Objekt, das dem vorgegebenen Schema \
entspricht.
"""

USER_PROMPT_TEMPLATE = """\
Titel: {title}

Beschreibung:
{description}
"""


def build_user_prompt(title: str, description: str) -> str:
    """Build the extraction prompt.

    Long descriptions are cut down, but both ends are kept: the introduction
    is often at the top and the specification list at the bottom.
    """
    description = (description or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        head = MAX_DESCRIPTION_CHARS * 2 // 3
        tail = MAX_DESCRIPTION_CHARS - head
        description = (
            description[:head]
            + f"\n[... {len(description) - MAX_DESCRIPTION_CHARS} Zeichen ausgelassen ...]\n"
            + description[-tail:]
        )
    return USER_PROMPT_TEMPLATE.format(title=title.strip(), description=description or "(leer)")
