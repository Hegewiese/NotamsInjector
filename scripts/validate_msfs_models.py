from __future__ import annotations

import argparse
import csv
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from src.msfs.obstacle_catalog import load_obstacle_catalog
from src.msfs.objects import ObjectPlacer

try:
    from SimConnect import SimConnect  # type: ignore
except Exception:  # pragma: no cover - runtime environment dependent
    SimConnect = None  # type: ignore


@dataclass
class ValidationResult:
    title: str
    status: str
    detail: str


def load_titles_from_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    titles: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        title = line.strip()
        if title:
            titles.append(title)
    return titles


def load_titles_from_catalog() -> list[str]:
    catalog = load_obstacle_catalog()
    dedup: list[str] = []
    seen: set[str] = set()
    for entry in catalog.values():
        for title in entry.titles:
            key = title.lower()
            if key not in seen:
                seen.add(key)
                dedup.append(title)
    return dedup


def validate_title(
    placer: ObjectPlacer,
    sm: object,
    title: str,
    index: int,
    lat: float,
    lon: float,
    timeout_s: float,
) -> ValidationResult:
    notam_id = f"model-validate-{index}"
    dispatched = placer.place(
        sm,
        notam_id=notam_id,
        lat=lat,
        lon=lon,
        alt_ft=0.0,
        lit=True,
        on_ground=True,
        obstacle_kind="generic_obstacle",
        titles_to_try=[title],
    )
    if not dispatched:
        return ValidationResult(title=title, status="dispatch_failed", detail="Create request rejected")

    deadline = time.time() + max(0.5, timeout_s)
    while time.time() < deadline:
        placer.pump_dispatch(sm)
        obj = next((o for o in placer.active if o.notam_id == notam_id), None)
        if obj is None:
            return ValidationResult(title=title, status="failed", detail="Object not retained after create")
        if obj.confirmed and obj.object_id > 0:
            placer.remove(sm, notam_id)
            return ValidationResult(title=title, status="ok", detail=f"object_id={obj.object_id}")
        time.sleep(0.1)

    placer.remove(sm, notam_id)
    return ValidationResult(title=title, status="timeout", detail="No confirmation before timeout")


def write_results(path: Path, results: list[ValidationResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["title", "status", "detail"])
        for r in results:
            writer.writerow([r.title, r.status, r.detail])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate SimObject titles against a live MSFS SimConnect session."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/simobject_titles_official.txt"),
        help="Path to a newline-delimited title list.",
    )
    parser.add_argument(
        "--include-catalog",
        action="store_true",
        help="Also validate all unique titles from obstacle_catalog.yaml.",
    )
    parser.add_argument("--lat", type=float, required=True, help="Placement latitude.")
    parser.add_argument("--lon", type=float, required=True, help="Placement longitude.")
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=3.0,
        help="Seconds to wait for each title confirmation.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Optional max titles to test (0 means all).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/msfs_model_validation_report.csv"),
        help="CSV output report path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    titles = load_titles_from_file(args.input)
    if args.include_catalog:
        titles.extend(load_titles_from_catalog())

    dedup: list[str] = []
    seen: set[str] = set()
    for t in titles:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            dedup.append(t)
    titles = dedup

    if args.max and args.max > 0:
        titles = titles[: args.max]

    if not titles:
        logger.error("No titles to validate. Provide --input and/or --include-catalog.")
        return 2

    if SimConnect is None:
        logger.error("SimConnect Python package is unavailable in this environment.")
        return 3

    logger.info(f"Preparing to validate {len(titles)} title(s) at {args.lat:.5f},{args.lon:.5f}")
    logger.info("Ensure MSFS is running and loaded into a flight.")

    try:
        sm = SimConnect()
    except Exception as exc:
        logger.error(f"Unable to connect to SimConnect: {exc}")
        return 4

    placer = ObjectPlacer()

    results: list[ValidationResult] = []
    for i, title in enumerate(titles, start=1):
        logger.info(f"[{i}/{len(titles)}] Testing '{title}'")
        try:
            res = validate_title(
                placer=placer,
                sm=sm,
                title=title,
                index=i,
                lat=args.lat,
                lon=args.lon,
                timeout_s=args.timeout_s,
            )
        except Exception as exc:
            res = ValidationResult(title=title, status="error", detail=str(exc))
        results.append(res)
        logger.info(f"  -> {res.status}: {res.detail}")

    write_results(args.out, results)
    ok_count = sum(1 for r in results if r.status == "ok")
    logger.info(f"Validation complete: {ok_count}/{len(results)} titles confirmed")
    logger.info(f"Wrote report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
