from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
import yaml

PLACEMENT_SIMOBJECT = "simobject"
PLACEMENT_SCENERY_LIBRARY = "scenery_library"


@dataclass(frozen=True)
class ObstacleModelEntry:
    placement_type: str
    titles: list[str]


_DEFAULT_CATALOG: dict[str, ObstacleModelEntry] = {
    "crane": ObstacleModelEntry(PLACEMENT_SIMOBJECT, ["Wind_Turbine_2"]),
    "mast_tower_antenna": ObstacleModelEntry(PLACEMENT_SIMOBJECT, ["Wind_Turbine_2"]),
    "wind_turbine": ObstacleModelEntry(PLACEMENT_SIMOBJECT, ["Wind_Turbine_2"]),
    "chimney_stack": ObstacleModelEntry(PLACEMENT_SIMOBJECT, ["Wind_Turbine_2"]),
    "building": ObstacleModelEntry(PLACEMENT_SIMOBJECT, ["Wind_Turbine_2"]),
    "generic_obstacle": ObstacleModelEntry(PLACEMENT_SIMOBJECT, ["Wind_Turbine_2"]),
    "beacon": ObstacleModelEntry(PLACEMENT_SIMOBJECT, ["Wind_Turbine_2"]),
    "runway_closure": ObstacleModelEntry(PLACEMENT_SIMOBJECT, ["Wind_Turbine_2"]),
}


def _catalog_path() -> Path:
    # src/msfs/obstacle_catalog.py -> project root
    return Path(__file__).resolve().parents[2] / "obstacle_catalog.yaml"


def _normalize_entry(raw: Any) -> ObstacleModelEntry | None:
    if isinstance(raw, list):
        titles = [str(t).strip() for t in raw if str(t).strip()]
        if titles:
            return ObstacleModelEntry(PLACEMENT_SIMOBJECT, titles)
        return None

    if not isinstance(raw, dict):
        return None

    placement_type = str(
        raw.get("placement_type")
        or raw.get("placement_backend")
        or PLACEMENT_SIMOBJECT
    ).strip().lower()
    if placement_type not in {PLACEMENT_SIMOBJECT, PLACEMENT_SCENERY_LIBRARY}:
        placement_type = PLACEMENT_SIMOBJECT

    titles_raw = raw.get("titles")
    if not isinstance(titles_raw, list):
        return None

    titles = [str(t).strip() for t in titles_raw if str(t).strip()]
    if not titles:
        return None

    return ObstacleModelEntry(placement_type, titles)


def _normalize_catalog(raw: Any) -> dict[str, ObstacleModelEntry]:
    if not isinstance(raw, dict):
        return dict(_DEFAULT_CATALOG)

    obstacle_models = raw.get("obstacle_models")
    if not isinstance(obstacle_models, dict):
        return dict(_DEFAULT_CATALOG)

    catalog = dict(_DEFAULT_CATALOG)
    for kind, entry_raw in obstacle_models.items():
        if not isinstance(kind, str):
            continue
        entry = _normalize_entry(entry_raw)
        if entry is not None:
            catalog[kind.strip()] = entry

    return catalog


def load_obstacle_catalog() -> dict[str, ObstacleModelEntry]:
    path = _catalog_path()
    if not path.exists():
        return dict(_DEFAULT_CATALOG)

    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        catalog = _normalize_catalog(raw)
        logger.info(
            f"[obstacles] Loaded obstacle catalog from {path.name} "
            f"({len(catalog)} categories)"
        )
        return catalog
    except Exception as exc:
        logger.warning(f"[obstacles] Failed to load obstacle catalog: {exc}")
        return dict(_DEFAULT_CATALOG)


def resolve_obstacle_entry(
    obstacle_kind: str,
    catalog: dict[str, ObstacleModelEntry] | None = None,
) -> ObstacleModelEntry:
    source = catalog if catalog is not None else load_obstacle_catalog()
    kind = (obstacle_kind or "generic_obstacle").strip()
    entry = source.get(kind)
    if entry is not None:
        return entry
    fallback = source.get("generic_obstacle")
    if fallback is not None:
        return fallback
    return _DEFAULT_CATALOG["generic_obstacle"]
