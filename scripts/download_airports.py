"""
Download the OurAirports airports.csv dataset to data/airports.csv.
Run once before first use, or periodically to refresh.

Usage:
    python scripts/download_airports.py
"""

import urllib.request
from pathlib import Path

URL      = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "airports.csv"


def main() -> None:
    OUT_PATH.parent.mkdir(exist_ok=True)
    print(f"Downloading airports.csv from {URL} …")
    urllib.request.urlretrieve(URL, OUT_PATH)
    lines = OUT_PATH.read_text(encoding="utf-8").splitlines()
    print(f"Done. {len(lines) - 1:,} airports saved to {OUT_PATH}")


if __name__ == "__main__":
    main()
