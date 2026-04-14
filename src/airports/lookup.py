"""
Local airport lookup using the OurAirports dataset, enriched with OpenAIP data.

airports.csv is downloaded automatically on first run if it is not present.

Only medium and large airports are indexed by default to keep the
spatial search fast and avoid flooding NOTAM APIs with tiny strips.

OpenAIP enrichment
------------------
After the CSV is loaded, ``enrich_from_openaip()`` can be called with a
dict of OpenAIP records (keyed by ICAO).  This fills the optional fields
(frequencies, ppr, fuel_types, contact, remarks) without touching the
OurAirports-sourced position and runway data.
"""

from __future__ import annotations

import csv
import math
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "airports.csv"
RUNWAYS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "runways.csv"
_AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
_RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"

# OurAirports types we care about (filter out heliports, seaplane bases, etc.)
INCLUDED_TYPES = {"medium_airport", "large_airport", "small_airport"}


@dataclass
class Airport:
    # ── OurAirports fields (always present) ───────────────────────────────────
    icao: str
    name: str
    lat: float
    lon: float
    type: str
    country: str
    municipality: str
    region: str
    continent: str
    elevation_ft: int | None
    home_link: str
    wikipedia_link: str

    # ── OpenAIP enrichment fields (filled by enrich_from_openaip) ─────────────
    ppr:         bool            = False   # Prior Permission Required
    private:     bool            = False
    frequencies: list[dict]      = field(default_factory=list)
    fuel_types:  list[int]       = field(default_factory=list)
    contact:     str             = ""
    remarks:     str             = ""
    openaip_id:  str             = ""


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    R = 3440.065  # Earth radius in NM
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class AirportLookup:
    """
    In-memory airport database loaded from airports.csv,
    optionally enriched with OpenAIP data.

    Typical load time: <1 s for ~70 k entries.
    """

    def __init__(self, csv_path: Path = DATA_PATH) -> None:
        self._airports: list[Airport] = []
        self._index:    dict[str, Airport] = {}   # ICAO → Airport (fast lookup)
        self._load(csv_path)

    def _download(self, csv_path: Path) -> None:
        logger.info(f"airports.csv not found — downloading from OurAirports…")
        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(_AIRPORTS_URL, csv_path)
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            logger.info(f"Downloaded {len(lines) - 1:,} airports to {csv_path}")
        except Exception as exc:
            logger.error(f"Failed to download airports.csv: {exc}")

    def _load(self, csv_path: Path) -> None:
        if not csv_path.exists():
            self._download(csv_path)
        if not csv_path.exists():
            logger.warning("Airport database unavailable — NOTAM fetching will be skipped.")
            return

        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("type") not in INCLUDED_TYPES:
                    continue
                icao = row.get("ident", "").strip()
                if not icao or len(icao) != 4 or not icao.isalpha():
                    continue  # skip non-ICAO identifiers like "0NJ5", "CT52"
                try:
                    lat = float(row["latitude_deg"])
                    lon = float(row["longitude_deg"])
                except (ValueError, KeyError):
                    continue

                elevation_ft: int | None
                try:
                    elevation_ft = int(float(row.get("elevation_ft", "")))
                except (TypeError, ValueError):
                    elevation_ft = None

                ap = Airport(
                    icao=icao,
                    name=row.get("name", ""),
                    lat=lat,
                    lon=lon,
                    type=row.get("type", ""),
                    country=row.get("iso_country", ""),
                    municipality=row.get("municipality", ""),
                    region=row.get("iso_region", ""),
                    continent=row.get("continent", ""),
                    elevation_ft=elevation_ft,
                    home_link=row.get("home_link", ""),
                    wikipedia_link=row.get("wikipedia_link", ""),
                )
                self._airports.append(ap)
                self._index[icao] = ap

        logger.info(f"Loaded {len(self._airports):,} airports from {csv_path.name}")

    def enrich_from_openaip(self, enrichments: dict[str, dict]) -> int:
        """
        Apply OpenAIP enrichment data to in-memory Airport objects.

        ``enrichments`` is a dict {icao: record} as returned by
        ``OpenAIPFetcher.get_enrichments_bulk()``.

        Returns the number of airports enriched.
        """
        count = 0
        for icao, rec in enrichments.items():
            ap = self._index.get(icao.upper())
            if ap is None:
                continue
            ap.ppr         = rec.get("ppr", False)
            ap.private     = rec.get("private", False)
            ap.frequencies = rec.get("frequencies", [])
            ap.fuel_types  = rec.get("fuel_types", [])
            ap.contact     = rec.get("contact", "")
            ap.remarks     = rec.get("remarks", "")
            ap.openaip_id  = rec.get("openaip_id", "")
            count += 1
        if count:
            logger.debug(f"[openaip] Enriched {count} airports with OpenAIP data")
        return count

    def within_radius(self, lat: float, lon: float, radius_nm: float) -> list[Airport]:
        """Return all airports within *radius_nm* of the given coordinates."""
        results: list[Airport] = []
        for ap in self._airports:
            if _haversine_nm(lat, lon, ap.lat, ap.lon) <= radius_nm:
                results.append(ap)
        return results

    def icao_codes_within(self, lat: float, lon: float, radius_nm: float) -> list[str]:
        return [ap.icao for ap in self.within_radius(lat, lon, radius_nm)]

    def find(self, icao: str) -> Airport | None:
        return self._index.get(icao.upper().strip())

    @property
    def loaded(self) -> bool:
        return len(self._airports) > 0


# ── Runway lookup ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class Runway:
    airport_icao: str
    le_ident: str           # e.g. "09L"
    he_ident: str           # e.g. "27R"
    le_lat: float
    le_lon: float
    he_lat: float
    he_lon: float
    le_heading: float
    he_heading: float
    length_ft: int
    width_ft: int


class RunwayLookup:
    """In-memory runway database loaded from OurAirports runways.csv."""

    def __init__(self, csv_path: Path = RUNWAYS_PATH) -> None:
        self._runways: dict[str, list[Runway]] = {}   # ICAO → list of runways
        self._load(csv_path)

    def _download(self, csv_path: Path) -> None:
        logger.info("runways.csv not found — downloading from OurAirports…")
        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(_RUNWAYS_URL, csv_path)
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            logger.info(f"Downloaded {len(lines) - 1:,} runways to {csv_path}")
        except Exception as exc:
            logger.error(f"Failed to download runways.csv: {exc}")

    def _load(self, csv_path: Path) -> None:
        if not csv_path.exists():
            self._download(csv_path)
        if not csv_path.exists():
            logger.warning("Runway database unavailable.")
            return

        count = 0
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                icao = (row.get("airport_ident") or "").strip().upper()
                if not icao or len(icao) != 4:
                    continue
                try:
                    le_lat = float(row["le_latitude_deg"])
                    le_lon = float(row["le_longitude_deg"])
                    he_lat = float(row["he_latitude_deg"])
                    he_lon = float(row["he_longitude_deg"])
                except (ValueError, KeyError, TypeError):
                    continue

                def _float(val: str, default: float = 0.0) -> float:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        return default

                def _int(val: str, default: int = 0) -> int:
                    try:
                        return int(float(val))
                    except (ValueError, TypeError):
                        return default

                rwy = Runway(
                    airport_icao=icao,
                    le_ident=(row.get("le_ident") or "").strip().upper(),
                    he_ident=(row.get("he_ident") or "").strip().upper(),
                    le_lat=le_lat,
                    le_lon=le_lon,
                    he_lat=he_lat,
                    he_lon=he_lon,
                    le_heading=_float(row.get("le_heading_degT", "")),
                    he_heading=_float(row.get("he_heading_degT", "")),
                    length_ft=_int(row.get("length_ft", "")),
                    width_ft=_int(row.get("width_ft", "")),
                )
                self._runways.setdefault(icao, []).append(rwy)
                count += 1

        logger.info(f"Loaded {count:,} runways from {csv_path.name}")

    def find_runway(self, icao: str, designator: str) -> Runway | None:
        """
        Find a runway by ICAO and designator (e.g. "09L", "27R", "09/27").

        If designator contains "/" (e.g. "09/27"), both ends are checked.
        Returns the first matching Runway or None.
        """
        icao = icao.upper().strip()
        designator = designator.upper().strip()
        runways = self._runways.get(icao, [])
        if not runways:
            return None

        parts = [d.strip() for d in designator.split("/")]
        for rwy in runways:
            if rwy.le_ident in parts or rwy.he_ident in parts:
                return rwy

        return None

    def runways_for(self, icao: str) -> list[Runway]:
        """Return all runways for an airport ICAO code."""
        return self._runways.get(icao.upper().strip(), [])

    @property
    def loaded(self) -> bool:
        return len(self._runways) > 0
