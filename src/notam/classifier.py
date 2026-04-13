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

# Taxiway designator: "TWY A", "TWY C2", "TAXIWAY B3" — captures first occurrence
_TAXIWAY_DESIGNATOR = re.compile(
    r"(?:TWY|TAXIWAY)\s+([A-Z]\d{0,2}(?:/[A-Z]\d{0,2})*)",
    re.IGNORECASE,
)

# Aircraft stand / gate: "ACFT STAND A26", "STAND V44A", "GATE B3", "STANDS V44A AND V45A"
_STAND_DESIGNATOR = re.compile(
    r"(?:ACFT\s+)?(?:STANDS?|GATES?|BAYS?)\s+([A-Z0-9]+(?:\s+AND\s+[A-Z0-9]+)*)",
    re.IGNORECASE,
)

# Fuel types commonly referenced in NOTAMs
_FUEL_TYPES = re.compile(
    r"\b(JET[\s\-]?A1?|AVGAS|100LL|UL91|MOGAS|AVTUR|JETA)\b",
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
_US_KEYWORDS = {"U/S", "UNSERVICEABLE", "NOT AVBL", "NOT AVAILABLE", "OUT OF SERVICE", "NOT IN SVC"}

# VHF civil aviation frequency: 118.000 – 136.975 MHz
_VHF_FREQ = re.compile(r"\b(1[123]\d\.\d{2,3})\b")

# ── ILS component classification ──────────────────────────────────────────────
#
# ILS Q-line subject codes that identify a specific component.
# These take priority over free-text detection.
_ILS_COMPONENT_BY_QCODE: dict[str, str] = {
    "IC": "full",        # ILS complete system
    "IG": "glideslope",  # ILS glide path
    "IS": "localizer",   # ILS localiser
    "ID": "dme",         # ILS DME
    "IO": "marker",      # ILS outer marker
    "IM": "marker",      # ILS middle marker
    "II": "marker",      # ILS inner marker
    "IL": "full",        # generic ILS (no component info in Q-code)
}

# Regex patterns for free-text component detection (checked in priority order).
# Each entry is (component_string, compiled_pattern).
_ILS_COMPONENT_TEXT_RULES: list[tuple[str, re.Pattern]] = [
    # Markers first — most specific, unambiguous abbreviations
    ("marker",      re.compile(r"\b(?:OM|MM|IM|OUTER\s+MARK(?:ER|R)|MIDDLE\s+MARK(?:ER|R)|INNER\s+MARK(?:ER|R))\b", re.IGNORECASE)),
    # Glideslope — GP / G/S / GS / GLIDE
    ("glideslope",  re.compile(r"\b(?:GP|G/S|GS|GLIDE(?:SLOPE|PATH| SLOPE| PATH)?)\b", re.IGNORECASE)),
    # Localizer — LOC or LOCALIZ(ER/ER)
    ("localizer",   re.compile(r"\b(?:LOC|LOCALIZ(?:ER|ER)?)\b", re.IGNORECASE)),
    # DME — only when NOT part of "ILS/DME" system name (i.e. not preceded by /)
    ("dme",         re.compile(r"(?<!/)\bDME\b", re.IGNORECASE)),
]


def _extract_vhf_frequency(description: str) -> float | None:
    """Return the first VHF frequency (MHz) found in the description, or None."""
    m = _VHF_FREQ.search(description)
    return float(m.group(1)) if m else None


def _classify_ils_component(notam_raw_subject_code: str, description: str) -> str:
    """
    Return the specific ILS component that is unserviceable.

    Uses the Q-line subject code when it carries component-level detail
    (IG=glideslope, IS=localizer, ID=dme, IO/IM/II=marker).
    Falls back to free-text keyword matching for generic IL/IC codes.

    Returns one of: "full" | "glideslope" | "localizer" | "dme" | "marker"
    """
    # Q-code is authoritative when it identifies a specific component
    qcode_component = _ILS_COMPONENT_BY_QCODE.get(notam_raw_subject_code.upper())
    if qcode_component and qcode_component != "full":
        return qcode_component

    # Free-text fallback
    for component, pattern in _ILS_COMPONENT_TEXT_RULES:
        if pattern.search(description):
            return component

    return "full"


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
                component = _classify_ils_component(
                    notam.raw_subject_code, notam.description
                )
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="disable_ils",
                    icao=notam.icao,
                    params={
                        "component": component,
                        "lat": notam.lat,
                        "lon": notam.lon,
                        "description": notam.description,
                    },
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
                        "lat": notam.lat,
                        "lon": notam.lon,
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

        case NotamSubject.TAXIWAY:
            if notam.condition in (NotamCondition.CLOSED, NotamCondition.LIMITED):
                twy_match = _TAXIWAY_DESIGNATOR.search(notam.description)
                twy_designator = twy_match.group(1).upper() if twy_match else ""
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="close_taxiway",
                    icao=notam.icao,
                    params={
                        "taxiway_designator": twy_designator,
                        "condition": notam.condition.value,
                        "lat": notam.lat,
                        "lon": notam.lon,
                        "description": notam.description,
                    },
                )

        case NotamSubject.APRON:
            if notam.condition in (NotamCondition.CLOSED, NotamCondition.LIMITED):
                stand_match = _STAND_DESIGNATOR.search(notam.description)
                stand_designator = stand_match.group(1).upper() if stand_match else ""
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="close_stand",
                    icao=notam.icao,
                    params={
                        "stand_designator": stand_designator,
                        "lat": notam.lat,
                        "lon": notam.lon,
                        "description": notam.description,
                    },
                )

        case NotamSubject.RUNWAY if notam.condition in (
            NotamCondition.LIMITED, NotamCondition.RESTRICTED, NotamCondition.LIMITED_WX
        ):
            rwy_match = _RUNWAY_DESIGNATOR.search(notam.description)
            rwy_designator = ""
            if rwy_match:
                rwy_designator = rwy_match.group(1).upper()
                if rwy_match.group(2):
                    rwy_designator += "/" + rwy_match.group(2).upper()
            return MsfsAction(
                notam_id=notam.id,
                action_type="runway_limited",
                icao=notam.icao,
                params={
                    "runway_designator": rwy_designator,
                    "lat": notam.lat,
                    "lon": notam.lon,
                    "description": notam.description,
                },
            )

        case NotamSubject.FUEL:
            desc_upper = notam.description.upper()
            desc_says_us = any(kw in desc_upper for kw in _US_KEYWORDS) or "NOT AVBL" in desc_upper
            if notam.condition in (
                NotamCondition.UNSERVICEABLE, NotamCondition.LIMITED, NotamCondition.CLOSED
            ) or desc_says_us:
                fuel_match = _FUEL_TYPES.search(notam.description)
                fuel_type = fuel_match.group(1).upper() if fuel_match else ""
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="fuel_unavailable",
                    icao=notam.icao,
                    params={
                        "fuel_type": fuel_type,
                        "lat": notam.lat,
                        "lon": notam.lon,
                        "description": notam.description,
                    },
                )

        case NotamSubject.COMMS:
            desc_upper = notam.description.upper()
            is_atis = "ATIS" in desc_upper
            desc_says_us = any(kw in desc_upper for kw in _US_KEYWORDS)

            if is_atis and (
                notam.condition in (NotamCondition.UNSERVICEABLE, NotamCondition.CLOSED)
                or desc_says_us
            ):
                return MsfsAction(
                    notam_id=notam.id,
                    action_type="atis_unserviceable",
                    icao=notam.icao,
                    params={
                        "frequency_mhz": _extract_vhf_frequency(notam.description),
                        "lat": notam.lat,
                        "lon": notam.lon,
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
