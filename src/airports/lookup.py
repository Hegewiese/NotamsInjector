"""
Local airport lookup using the OurAirports dataset.

airports.csv is downloaded automatically on first run if it is not present.

Only medium and large airports are indexed by default to keep the
spatial search fast and avoid flooding NOTAM APIs with tiny strips.
"""

from __future__ import annotations

import csv
import math
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "airports.csv"
_AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

# OurAirports types we care about (filter out heliports, seaplane bases, etc.)
INCLUDED_TYPES = {"medium_airport", "large_airport", "small_airport"}


@dataclass(slots=True)
class Airport:
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
    In-memory airport database loaded from airports.csv.
    Typical load time: <1 s for ~70 k entries.
    """

    def __init__(self, csv_path: Path = DATA_PATH) -> None:
        self._airports: list[Airport] = []
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

                self._airports.append(
                    Airport(
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
                )

        logger.info(f"Loaded {len(self._airports):,} airports from {csv_path.name}")

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
        icao = icao.upper().strip()
        for ap in self._airports:
            if ap.icao == icao:
                return ap
        return None

    @property
    def loaded(self) -> bool:
        return len(self._airports) > 0
