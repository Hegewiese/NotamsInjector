"""
Full pipeline smoke test — no Qt or SimConnect needed.
Tests: airport lookup → NOTAM fetch → parse → classify
for the mock position (London Heathrow, EGLL).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.airports.lookup import AirportLookup
from src.notam.fetcher import NotamsOnlineFetcher
from src.notam.parser import parse_notams
from src.notam.classifier import classify_all

MOCK_LAT, MOCK_LON = 47.2602, 11.3439  # LOWI — Innsbruck Airport
RADIUS_NM = 50.0


async def main():
    print(f"\n{'='*60}")
    print(f"  NOTAM Injector — pipeline test")
    print(f"  Position: {MOCK_LAT}, {MOCK_LON}  (Innsbruck, LOWI)")
    print(f"  Radius:   {RADIUS_NM} nm")
    print(f"{'='*60}\n")

    # 1. Airport lookup
    print("[1/4] Loading airport database…")
    lookup   = AirportLookup()
    airports = lookup.within_radius(MOCK_LAT, MOCK_LON, RADIUS_NM)
    icao_codes = [a.icao for a in airports]
    print(f"      {len(airports)} airports within {RADIUS_NM} nm")

    # 2. Fetch NOTAMs
    print(f"\n[2/4] Fetching NOTAMs from notams.online for {len(icao_codes)} airports…")
    fetcher   = NotamsOnlineFetcher()
    raw_notams = await fetcher.fetch(icao_codes)
    print(f"      Received {len(raw_notams)} raw NOTAM(s)")

    # 3. Parse
    print(f"\n[3/4] Parsing NOTAMs…")
    notams = parse_notams(raw_notams, source="notams.online")
    active = [n for n in notams if n.is_active]
    print(f"      Parsed:  {len(notams)}")
    print(f"      Active:  {len(active)}")

    # 4. Classify
    print(f"\n[4/4] Classifying into MSFS actions…")
    actions = classify_all(active)

    # Group by action type
    by_type: dict[str, list] = {}
    for a in actions:
        by_type.setdefault(a.action_type, []).append(a)

    print(f"      {len(actions)} actionable NOTAM(s)\n")

    for action_type, items in sorted(by_type.items()):
        print(f"  ── {action_type.upper().replace('_',' ')} ({len(items)}) ──")
        for a in items:
            lat = a.params.get("lat")
            lon = a.params.get("lon")
            pos = f"lat={lat:+.4f} lon={lon:+.4f}" if lat else "no position"
            hgt = a.params.get("upper_ft", "?")
            desc = a.params.get("description", "")[:60]
            print(f"    [{a.notam_id:>10}]  {a.icao}  {pos}  {hgt}ft")
            if desc:
                print(f"                   {desc}")
        print()

    print(f"{'='*60}")
    print(f"  Pipeline OK")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
