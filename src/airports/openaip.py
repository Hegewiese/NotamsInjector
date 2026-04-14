"""
OpenAIP airport data fetcher and sync manager.

Fetches enrichment data (frequencies, PPR flag, fuel types, contact, remarks)
from the OpenAIP REST API and stores it in SQLite.

Update strategy
---------------
OpenAIP data follows the AIRAC cycle (28 days).  On startup the scheduler
calls ``ensure_fresh()`` which compares the last sync timestamp against a
28-day threshold and triggers a background refresh if stale.

The OurAirports CSV remains the primary source for airport positions and
runway threshold coordinates.  OpenAIP data is a supplementary layer that
adds fields OurAirports does not provide.

API auth
--------
Requests must include the header ``x-openaip-api-key: <key>``.
The key is read from ``settings.openaip_api_key``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import httpx
from loguru import logger

from src.config import settings

_BASE_URL = "https://api.core.openaip.net/api/airports"
_PAGE_SIZE = 100
_AIRAC_DAYS = 28

# ── Table DDL (created by NotamCache.init) ───────────────────────────────────

CREATE_OPENAIP_AIRPORTS = """
CREATE TABLE IF NOT EXISTS openaip_airports (
    icao          TEXT PRIMARY KEY,
    openaip_id    TEXT,
    name          TEXT,
    type          INTEGER,
    lat           REAL,
    lon           REAL,
    elevation_m   REAL,
    country       TEXT,
    ppr           INTEGER DEFAULT 0,
    private       INTEGER DEFAULT 0,
    frequencies   TEXT,   -- JSON array
    runways       TEXT,   -- JSON array (heading + dimensions, no coords)
    fuel_types    TEXT,   -- JSON array of ints
    contact       TEXT,
    remarks       TEXT,
    synced_at     TEXT NOT NULL
);
"""

CREATE_OPENAIP_META = """
CREATE TABLE IF NOT EXISTS openaip_meta (
    country       TEXT PRIMARY KEY,
    last_synced   TEXT NOT NULL,
    airport_count INTEGER DEFAULT 0
);
"""


# ── Public helpers ────────────────────────────────────────────────────────────

def airac_age_days(last_synced_iso: str) -> float:
    """Return how many days ago *last_synced_iso* was."""
    dt = datetime.fromisoformat(last_synced_iso)
    return (datetime.now(tz=timezone.utc) - dt).total_seconds() / 86400


# ── Fetcher ───────────────────────────────────────────────────────────────────

class OpenAIPFetcher:
    """
    Downloads OpenAIP airport data for a list of country codes and stores it
    in the notam_cache.db SQLite database.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ── Public API ─────────────────────────────────────────────────────────────

    async def ensure_fresh(self, countries: list[str]) -> None:
        """
        Called on startup.  Checks each country's last sync date; triggers
        a background refresh for any country older than AIRAC_DAYS.
        Runs non-blocking — callers do not await the actual download.
        """
        if not settings.openaip_api_key:
            logger.debug("[openaip] No API key configured — skipping enrichment sync.")
            return

        stale = []
        for country in countries:
            age = await self._sync_age_days(country)
            if age is None or age >= _AIRAC_DAYS:
                stale.append(country)
                logger.info(
                    f"[openaip] {country}: stale ({age:.0f}d old)" if age is not None
                    else f"[openaip] {country}: never synced"
                )
            else:
                logger.debug(f"[openaip] {country}: fresh ({age:.1f}d old, next sync in {_AIRAC_DAYS - age:.1f}d)")

        if stale:
            asyncio.create_task(self._sync_countries(stale))

    async def force_sync(self, countries: list[str]) -> dict[str, int]:
        """
        Synchronously sync all given countries.  Returns {country: count}.
        Used by CLI scripts.
        """
        results = {}
        for country in countries:
            count = await self._fetch_country(country)
            results[country] = count
        return results

    async def get_enrichment(self, icao: str) -> Optional[dict]:
        """Return the stored enrichment record for an ICAO code, or None."""
        cur = await self._db.execute(
            "SELECT * FROM openaip_airports WHERE icao = ?", (icao.upper(),)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "icao":        row["icao"],
            "openaip_id":  row["openaip_id"],
            "ppr":         bool(row["ppr"]),
            "private":     bool(row["private"]),
            "frequencies": json.loads(row["frequencies"] or "[]"),
            "runways":     json.loads(row["runways"] or "[]"),
            "fuel_types":  json.loads(row["fuel_types"] or "[]"),
            "contact":     row["contact"] or "",
            "remarks":     row["remarks"] or "",
            "synced_at":   row["synced_at"],
        }

    async def get_enrichments_bulk(self, icao_codes: list[str]) -> dict[str, dict]:
        """Return enrichment records for multiple ICAOs as {icao: record}."""
        if not icao_codes:
            return {}
        placeholders = ",".join("?" * len(icao_codes))
        cur = await self._db.execute(
            f"SELECT * FROM openaip_airports WHERE icao IN ({placeholders})",
            [c.upper() for c in icao_codes],
        )
        rows = await cur.fetchall()
        return {
            row["icao"]: {
                "icao":        row["icao"],
                "openaip_id":  row["openaip_id"],
                "ppr":         bool(row["ppr"]),
                "private":     bool(row["private"]),
                "frequencies": json.loads(row["frequencies"] or "[]"),
                "runways":     json.loads(row["runways"] or "[]"),
                "fuel_types":  json.loads(row["fuel_types"] or "[]"),
                "contact":     row["contact"] or "",
                "remarks":     row["remarks"] or "",
                "synced_at":   row["synced_at"],
            }
            for row in rows
        }

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _sync_age_days(self, country: str) -> Optional[float]:
        cur = await self._db.execute(
            "SELECT last_synced FROM openaip_meta WHERE country = ?", (country,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return airac_age_days(row["last_synced"])

    async def _sync_countries(self, countries: list[str]) -> None:
        for country in countries:
            try:
                count = await self._fetch_country(country)
                logger.info(f"[openaip] Synced {count} airports for {country}")
            except Exception as exc:
                logger.warning(f"[openaip] Sync failed for {country}: {exc}")

    async def _fetch_country(self, country: str) -> int:
        """Fetch all airports for *country* and upsert into SQLite. Returns count."""
        api_key = settings.openaip_api_key
        if not api_key:
            raise RuntimeError("openaip_api_key not configured")

        headers = {"x-openaip-api-key": api_key}
        count = 0
        page = 1
        now_iso = datetime.now(tz=timezone.utc).isoformat()

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                resp = await client.get(
                    _BASE_URL,
                    params={"country": country, "limit": _PAGE_SIZE, "page": page},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("items", [])
                if not items:
                    break

                rows = [_airport_to_row(ap, now_iso) for ap in items if _airport_to_row(ap, now_iso)]
                await self._db.executemany(
                    """
                    INSERT INTO openaip_airports
                        (icao, openaip_id, name, type, lat, lon, elevation_m,
                         country, ppr, private, frequencies, runways,
                         fuel_types, contact, remarks, synced_at)
                    VALUES
                        (:icao,:openaip_id,:name,:type,:lat,:lon,:elevation_m,
                         :country,:ppr,:private,:frequencies,:runways,
                         :fuel_types,:contact,:remarks,:synced_at)
                    ON CONFLICT(icao) DO UPDATE SET
                        openaip_id  = excluded.openaip_id,
                        name        = excluded.name,
                        type        = excluded.type,
                        lat         = excluded.lat,
                        lon         = excluded.lon,
                        elevation_m = excluded.elevation_m,
                        ppr         = excluded.ppr,
                        private     = excluded.private,
                        frequencies = excluded.frequencies,
                        runways     = excluded.runways,
                        fuel_types  = excluded.fuel_types,
                        contact     = excluded.contact,
                        remarks     = excluded.remarks,
                        synced_at   = excluded.synced_at
                    """,
                    rows,
                )
                await self._db.commit()

                count += len(rows)
                total_pages = data.get("totalPages", 1)
                logger.debug(f"[openaip] {country} page {page}/{total_pages} — {len(rows)} airports")

                if page >= total_pages:
                    break
                page += 1

        # Update sync metadata
        await self._db.execute(
            """
            INSERT INTO openaip_meta(country, last_synced, airport_count)
            VALUES(?, ?, ?)
            ON CONFLICT(country) DO UPDATE SET
                last_synced   = excluded.last_synced,
                airport_count = excluded.airport_count
            """,
            (country, now_iso, count),
        )
        await self._db.commit()
        return count


# ── Row converter ─────────────────────────────────────────────────────────────

def _airport_to_row(ap: dict, synced_at: str) -> Optional[dict]:
    """Convert an OpenAIP airport dict to a SQLite row dict. Returns None if no ICAO."""
    icao = (ap.get("icaoCode") or "").strip().upper()
    if not icao or len(icao) != 4:
        return None

    coords = ap.get("geometry", {}).get("coordinates", [None, None])
    lon = coords[0] if len(coords) > 1 else None
    lat = coords[1] if len(coords) > 1 else None

    elev = ap.get("elevation", {})
    elev_val = elev.get("value")  # unit 0 = metres

    services = ap.get("services", {})
    fuel_types = services.get("fuelTypes", [])

    contact_parts = []
    contact_raw = ap.get("contact") or ""
    if contact_raw:
        contact_parts.append(str(contact_raw).strip())
    tel = ap.get("telephoneServices") or []
    if tel:
        contact_parts.append(str(tel))
    contact = "\n".join(contact_parts) if contact_parts else ""

    # Runways: keep heading + dimensions only (no threshold coords in OpenAIP)
    runways = [
        {
            "designator":  r.get("designator", ""),
            "trueHeading": r.get("trueHeading"),
            "length_m":    r.get("dimension", {}).get("length", {}).get("value"),
            "width_m":     r.get("dimension", {}).get("width", {}).get("value"),
            "surface":     r.get("surface", {}).get("mainComposite"),
            "lighting":    r.get("lightingSystem", []),
            "mainRunway":  r.get("mainRunway", False),
            "takeOffOnly": r.get("takeOffOnly", False),
            "landingOnly": r.get("landingOnly", False),
        }
        for r in ap.get("runways", [])
    ]

    # Frequencies: keep value, type, name
    frequencies = [
        {
            "mhz":  f.get("value"),
            "type": f.get("type"),
            "name": f.get("name", ""),
        }
        for f in ap.get("frequencies", [])
    ]

    return {
        "icao":        icao,
        "openaip_id":  ap.get("_id", ""),
        "name":        ap.get("name", ""),
        "type":        ap.get("type"),
        "lat":         lat,
        "lon":         lon,
        "elevation_m": elev_val,
        "country":     ap.get("country", ""),
        "ppr":         int(bool(ap.get("ppr", False))),
        "private":     int(bool(ap.get("private", False))),
        "frequencies": json.dumps(frequencies),
        "runways":     json.dumps(runways),
        "fuel_types":  json.dumps(fuel_types),
        "contact":     contact,
        "remarks":     str(ap.get("remarks", "") or ""),
        "synced_at":   synced_at,
    }
