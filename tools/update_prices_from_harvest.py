"""Fold harvested market prices into ``data/prices.json``.

Usage:
    snipeklein prices refresh --reset-observations --force --json > harvest.json
    python tools/update_prices_from_harvest.py harvest.json

Components with too few samples keep their curated value.  The previous
(curated) value is kept as ``curated_fmv_eur`` so the change stays visible.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

PRICES = Path(__file__).resolve().parent.parent / "data" / "prices.json"

MIN_SAMPLES = 4

# The baseline values from project_plan.md (spec 5.1).
SPEC_BASELINE = {
    "AMD_RYZEN_5_7600X": 155,
    "AMD_RYZEN_7_7800X3D": 310,
    "NVIDIA_RTX_4070_12GB": 440,
    "NVIDIA_RTX_4080_16GB": 820,
    "DDR5_32GB_STANDARD": 85,
    "NVME_GEN4_1TB": 50,
    "B650_STANDARD": 105,
    "PERIPHERAL_BUNDLE_GENERIC": 90,
}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: update_prices_from_harvest.py <harvest.json>")
        return 2
    harvest = {row["component_id"]: row for row in json.loads(Path(sys.argv[1]).read_text())}
    data = json.loads(PRICES.read_text(encoding="utf-8"))

    today = date.today().isoformat()
    updated = 0
    for component in data["components"]:
        component_id = component["id"]
        if component_id in SPEC_BASELINE:
            component.setdefault("spec_baseline", SPEC_BASELINE[component_id])
        row = harvest.get(component_id)
        if not row or not row.get("fmv_eur") or row.get("samples", 0) < MIN_SAMPLES:
            continue
        component.setdefault("curated_fmv_eur", component["fmv_eur"])
        component["fmv_eur"] = round(float(row["fmv_eur"]), 2)
        component["market_source"] = "kleinanzeigen"
        component["market_samples"] = int(row["samples"])
        component["market_observed"] = today
        updated += 1

    data["version"] = today
    PRICES.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"updated {updated} components in {PRICES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
