"""
One-shot NOTAM pull around EDDF (Frankfurt) at 150 nm.

Fetches live NOTAMs, parses them, classifies into MSFS actions,
and writes a summary JSON to notams_eddf_150nm.json for analysis.

Usage:
    python scripts/pull_notams_eddf.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Make sure project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from src.airports.lookup import AirportLookup
from src.notam.fetcher import build_aggregator
from src.notam.parser import parse_notams
from src.notam.classifier import classify_all
from src.config import settings

# ── Config ─────────────────────────────────────────────────────────────────────
CENTER_LAT   = 50.0333   # EDDF Frankfurt
CENTER_LON   =  8.5706
RADIUS_NM    = 150.0
OUTPUT_FILE  = Path("notams_eddf_150nm.json")
# ───────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    logger.info(f"Loading airport database…")
    airport_lookup = AirportLookup()
    if not airport_lookup.loaded:
        logger.error("Airport database failed to load.")
        return

    icao_codes = airport_lookup.icao_codes_within(CENTER_LAT, CENTER_LON, RADIUS_NM)
    logger.info(f"Found {len(icao_codes)} airports within {RADIUS_NM} nm of EDDF")

    fetcher = build_aggregator(
        notams_online_enabled=settings.notams_online_enabled,
        checkwx_api_key=settings.checkwx_api_key,
    )

    BAR_WIDTH = 40

    def _progress(done: int, tot: int) -> None:
        if tot == 0:
            return
        pct   = done / tot
        filled = int(BAR_WIDTH * pct)
        bar   = "█" * filled + "░" * (BAR_WIDTH - filled)
        print(f"\r  [{bar}] {done}/{tot} airports  ({pct:5.1%})", end="", flush=True)
        if done == tot:
            print()  # newline when complete

    logger.info("Fetching NOTAMs (this may take a minute)…")
    raw_texts = await fetcher.fetch_all(icao_codes, progress_cb=_progress)
    logger.info(f"Received {len(raw_texts)} raw NOTAM text(s)")

    notams = parse_notams(raw_texts)
    active  = [n for n in notams if n.is_active]
    logger.info(f"Parsed {len(notams)} NOTAMs, {len(active)} active")

    actions = classify_all(active)
    logger.info(f"Classified {len(actions)} MSFS action(s)")

    # ── Build output ────────────────────────────────────────────────────────────
    # Group actions by type for easy review
    by_type: dict[str, list[dict]] = {}
    for a in actions:
        entry = {
            "notam_id": a.notam_id,
            "icao":     a.icao,
            "params":   a.params,
        }
        by_type.setdefault(a.action_type, []).append(entry)

    # Include active NOTAMs that produced NO action (UNKNOWN/unhandled subjects)
    action_ids = {a.notam_id for a in actions}
    unhandled: list[dict] = []
    for n in active:
        if n.id not in action_ids:
            unhandled.append({
                "notam_id":           n.id,
                "icao":              n.icao,
                "subject":           n.subject.name,
                "raw_subject_code":  n.raw_subject_code,
                "condition":         n.condition.name,
                "description":       n.description[:200] if n.description else "",
            })

    output = {
        "center":          {"lat": CENTER_LAT, "lon": CENTER_LON, "radius_nm": RADIUS_NM},
        "airports_queried": len(icao_codes),
        "notams_fetched":  len(raw_texts),
        "notams_parsed":   len(notams),
        "notams_active":   len(active),
        "actions_total":   len(actions),
        "actions_by_type": by_type,
        "unhandled_active_notams": unhandled,
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2, default=str))
    logger.success(f"Written → {OUTPUT_FILE.resolve()}")

    # ── Console summary ─────────────────────────────────────────────────────────
    print("\n── Action summary ────────────────────────────────────────────")
    for atype, entries in sorted(by_type.items()):
        print(f"  {atype:30s}  {len(entries):>4}")
    print(f"  {'(unhandled/informational)':30s}  {len(unhandled):>4}")
    print(f"\nFull details → {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
