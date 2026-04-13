"""
NOTAM → MSFS action classifier.

Takes a parsed Notam and decides what (if anything) should happen
inside the simulator.  Extend this module as more action types are
implemented in src/msfs/.
"""

from __future__ import annotations

import re

from loguru import logger

from .models import MsfsAction, Notam, NotamCondition, NotamSubject

# Runway designator pattern: "RWY 09L/27R", "RWY 09/27", "RUNWAY 09", "RWY 09L"
_RUNWAY_DESIGNATOR = re.compile(
    r"(?:RWY|RUNWAY)\s+(\d{2}[LCR]?)(?:\s*/\s*(\d{2}[LCR]?))?",
    re.IGNORECASE,
)

# Natural features that cannot be represented as placed objects in MSFS
_NATURAL_KEYWORDS = {
    "TREE", "TREES", "BUSH", "BUSHES", "VEGETATION", "FOREST",
    "HEDGE", "HEDGES", "SHRUB", "SHRUBS", "GRASS",
}

# Keywords that confirm night lighting is present
_LIT_KEYWORDS     = {"NIGHT", "LGTD", "LIGHTED", "LIT"}
# Keywords that confirm it is NOT lit at night
_UNLIT_KEYWORDS   = {"UNMARKED", "UNLIT", "UNLIGHTED"}

_OBSTACLE_KIND_KEYWORDS: dict[str, set[str]] = {
    "crane": {"CRANE", "CRANES", "TOWER CRANE", "MOBILE CRANE"},
    "mast_tower_antenna": {"MAST", "MASTS", "TOWER", "TOWERS", "ANTENNA", "ANTENNAS", "POLE", "POLES"},
    "wind_turbine": {"WINDMILL", "WINDMILLS", "TURBINE", "TURBINES", "WIND TURBINE"},
    "chimney_stack": {"CHIMNEY", "CHIMNEYS", "STACK", "STACKS", "FLARE STACK"},
    "building": {"BUILDING", "BUILDINGS"},
}

# Keywords in the E-field that indicate a facility is out of service,
# used to override an incorrect Q-line condition code (some military
# airfields issue SERVICEABLE Q-codes alongside "U/S" descriptions).
_US_KEYWORDS = {"U/S", "UNSERVICEABLE", "NOT AVBL", "NOT AVAILABLE", "OUT OF SERVICE"}


def _is_natural_obstacle(description: str) -> bool:
    """Return True if the obstacle description refers to natural vegetation."""
    words = description.upper().split()
    return bool(_NATURAL_KEYWORDS.intersection(words))


def _is_lit(description: str) -> bool:
    """
    Return True if the NOTAM description indicates the obstacle is lit at night.

    Rules:
    - "DAY AND NIGHT MARKED" / "MARKED AND LGTD" / "LGTD" → lit
    - "NOT MARKED" alone, or "UNMARKED" → not lit
    - "DAY MARKED" without "NIGHT" → not lit at night
    - No mention → assume lit (aviation safety default)
    """
    import re
    text  = description.upper()
    # Strip punctuation so "MARKED." matches "MARKED"
    words = set(re.sub(r"[^A-Z ]", " ", text).split())

    if words.intersection(_UNLIT_KEYWORDS):
        return False
    # "NOT MARKED" without any lit qualifier
    if "NOT" in words and "MARKED" in words and not words.intersection(_LIT_KEYWORDS):
        return False
    # "DAY MARKED" without NIGHT or a lit keyword
    if "DAY" in words and "MARKED" in words and "NIGHT" not in words and not words.intersection(_LIT_KEYWORDS):
        return False

    # Default: assume lit (safer for aviation)
    return True


def _classify_obstacle_kind(description: str) -> tuple[str, str]:
    """Return (obstacle_kind, confidence) from free-text NOTAM description."""
    text = (description or "").upper()

    for kind, needles in _OBSTACLE_KIND_KEYWORDS.items():
        for needle in needles:
            if needle in text:
                return kind, "high"

    if "OBST" in text:
        return "generic_obstacle", "medium"
    return "generic_obstacle", "low"


def classify(notam: Notam) -> MsfsAction | None:
    """
    Map a :class:`Notam` to a :class:`MsfsAction`, or None if no
    simulator action is applicable.
    """

    if not notam.is_active:
        return None

    match notam.subject:

        case NotamSubject.ILS:
            # Some issuers (especially military) code AS/serviceable in the Q-line
            # but write "U/S" in the description — honour the description in that case.
            desc_upper = notam.description.upper()
            desc_says_us = any(kw in desc_upper for kw in _US_KEYWORDS)

            if notam.condition in (NotamCondition.UNSERVICEABLE, NotamCondition.CLOSED) \
                    or (notam.condition == NotamCondition.SERVICEABLE and desc_says_us):
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="disable_ils",
                    icao=notam.icao,
                    params={"description": notam.description},
                )
            if notam.condition in (NotamCondition.SERVICEABLE, NotamCondition.OPEN) \
                    and not desc_says_us:
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="enable_ils",
                    icao=notam.icao,
                    params={"description": notam.description},
                )

        case NotamSubject.VOR | NotamSubject.NDB:
            if notam.condition in (NotamCondition.UNSERVICEABLE, NotamCondition.CLOSED):
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="disable_navaid",
                    icao=notam.icao,
                    params={
                        "navaid_type": notam.subject.value,
                        "description": notam.description,
                    },
                )

        case NotamSubject.RUNWAY:
            if notam.condition == NotamCondition.CLOSED:
                rwy_match = _RUNWAY_DESIGNATOR.search(notam.description)
                rwy_designator = ""
                if rwy_match:
                    rwy_designator = rwy_match.group(1).upper()
                    if rwy_match.group(2):
                        rwy_designator += "/" + rwy_match.group(2).upper()
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="close_runway",
                    icao=notam.icao,
                    params={
                        "runway_designator": rwy_designator,
                        "description": notam.description,
                    },
                )

        case NotamSubject.OBSTACLE:
            if notam.condition in (NotamCondition.NEW, NotamCondition.CHANGED):
                if _is_natural_obstacle(notam.description):
                    logger.debug(
                        f"[classifier] Skipping natural obstacle {notam.id} "
                        f"(trees/vegetation — not placeable in MSFS)"
                    )
                    return None
                obstacle_kind, obstacle_confidence = _classify_obstacle_kind(notam.description)
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="place_obstacle",
                    icao=notam.icao,
                    params={
                        "lat": notam.lat,
                        "lon": notam.lon,
                        "upper_ft": notam.upper_ft,
                        "lit": _is_lit(notam.description),
                        "obstacle_kind": obstacle_kind,
                        "obstacle_confidence": obstacle_confidence,
                        "description": notam.description,
                    },
                )

        case NotamSubject.TFR:
            return MsfsAction(
                notam_id=notam.id,
                action_type="set_tfr",
                icao=notam.icao,
                params={
                    "lat": notam.lat,
                    "lon": notam.lon,
                    "radius_nm": notam.radius_nm,
                    "lower_ft": notam.lower_ft,
                    "upper_ft": notam.upper_ft,
                    "description": notam.description,
                },
            )

        case _:
            logger.debug(
                f"[classifier] No action for {notam.id} "
                f"(subject={notam.subject}, condition={notam.condition})"
            )

    return None


def classify_all(notams: list[Notam]) -> list[MsfsAction]:
    """Classify a list of NOTAMs, returning only actionable results."""
    actions: list[MsfsAction] = []
    for notam in notams:
        action = classify(notam)
        if action:
            actions.append(action)
    return actions
