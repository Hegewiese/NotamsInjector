"""
ICAO NOTAM parser.

Handles the standard ICAO format used by most AIS providers:

    (A0001/24 NOTAMN
    Q) EGTT/QILAU/IV/NBO/A/000/999/5130N00000W005
    A) EGLL
    B) 2401010000
    C) 2401312359
    E) ILS CAT III RWY 27L UNSERVICEABLE)

The Q-line is the machine-readable part; E) is free text.
Precise obstacle coordinates are extracted from the E-field when present.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from .models import (
    CONDITION_MAP,
    SUBJECT_MAP,
    Notam,
    NotamCondition,
    NotamSubject,
)

# ── NOTAM field patterns ──────────────────────────────────────────────────────

_NOTAM_ID = re.compile(r"\(?([A-Z]\d{4}/\d{2})", re.IGNORECASE)
_FIELD_Q  = re.compile(r"Q\)\s*(.+?)(?=\n[A-Z]\)|\Z)", re.DOTALL)
_FIELD_A  = re.compile(r"A\)\s*([A-Z]{4}(?:\s+[A-Z]{4})*)")
_FIELD_B  = re.compile(r"B\)\s*(\d{10})")
_FIELD_C  = re.compile(r"C\)\s*(PERM|\d{10})")
_FIELD_E  = re.compile(r"E\)\s*(.+?)(?=\nF\)|\nG\)|\)\Z|\Z)", re.DOTALL)

# Q-line: FIR/QCODE/TRAFFIC/PURPOSE/SCOPE/LOWER/UPPER/COORD
# Allow optional trailing spaces in each Q-line field (German/EDGG NOTAMs pad with spaces)
_QLINE = re.compile(
    r"([A-Z]{4})/Q([A-Z]{4,5})/([A-Z ]+?)\s*/([A-Z ]+?)\s*/([A-Z ]+?)\s*/(\d+)/(\d+)/(.+)"
)

# ── Coordinate patterns (all in E-field) ─────────────────────────────────────

# A single DMS coordinate pair, e.g. "512834N 0002826W" or "512834N0002826W"
# Lat: 6 digits + N/S   Lon: 7 digits + E/W
_SINGLE_COORD = re.compile(r"(\d{6}[NS])\s*(\d{7}[EW])", re.IGNORECASE)

# Q-line coarse coord: 4+2 digits, e.g. "5129N00028W" (degrees+minutes only)
_QLINE_COORD = re.compile(r"(\d{4}[NS])(\d{5}[EW])(\d{3})?", re.IGNORECASE)

# Labeled single point:  "POSITION: 512834N 0002826W"  or  "PSN: 512829N 0002926W"
_LABELED_POINT = re.compile(
    r"(?:POSITION|PSN)\s*:\s*(\d{6}[NS])\s*(\d{7}[EW])",
    re.IGNORECASE,
)

# Prefixed single point: "AT PSN 512740N 0002746W"
_AT_PSN = re.compile(
    r"AT\s+PSN\s+(\d{6}[NS])\s*(\d{7}[EW])",
    re.IGNORECASE,
)

# WI RADIUS pattern: "WI 0.2NM RADIUS OF 512932N 0001744W"
_WI_RADIUS = re.compile(
    r"WI\s+([\d.]+)\s*NM\s+RADIUS\s+OF\s+(\d{6}[NS])\s*(\d{7}[EW])",
    re.IGNORECASE,
)

# Polygon: "WI PSN 512714N 0002533W - 512715N 0002524W - ..."
# or       "OPR WI PSN 512714N ..."
_POLYGON_START = re.compile(
    r"(?:WI\s+)?PSN\s+(\d{6}[NS])\s*(\d{7}[EW])",
    re.IGNORECASE,
)


def _dms6_to_dec(lat_str: str, lon_str: str) -> tuple[float, float]:
    """
    Convert 6-digit lat + 7-digit lon DMS strings to decimal degrees.
    e.g. "512834N", "0002826W" → (51.4761, -0.4739)
    """
    def _conv(s: str) -> float:
        hemi   = s[-1].upper()
        digits = s[:-1]
        if len(digits) == 6:           # DDMMSS
            deg, mins, secs = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
        else:                           # DDDMMSS
            deg, mins, secs = int(digits[:3]), int(digits[3:5]), int(digits[5:7])
        val = deg + mins / 60.0 + secs / 3600.0
        return -val if hemi in ("S", "W") else val

    return _conv(lat_str), _conv(lon_str)


def _qline_coord_to_dec(
    lat_str: str, lon_str: str
) -> tuple[float, float]:
    """
    Convert Q-line coarse coord (DDMM + N/S, DDDMM + E/W) to decimal degrees.
    e.g. "5129N", "00028W" → (51.4833, -0.4667)
    """
    def _conv(s: str) -> float:
        hemi   = s[-1].upper()
        digits = s[:-1]
        if len(digits) == 4:           # DDmm
            deg, mins = int(digits[:2]), int(digits[2:])
        else:                           # DDDmm
            deg, mins = int(digits[:3]), int(digits[3:])
        val = deg + mins / 60.0
        return -val if hemi in ("S", "W") else val

    return _conv(lat_str), _conv(lon_str)


def _centroid(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    lats = [p[0] for p in pairs]
    lons = [p[1] for p in pairs]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def extract_position(
    etext: str,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Extract the best available (lat, lon, radius_nm) from the E-field text.

    Priority:
      1. WI X.XNM RADIUS OF coord          → precise centre + radius
      2. POSITION: coord  or  PSN: coord   → precise single point
      3. AT PSN coord                       → precise single point
      4. WI PSN coord - coord - ...         → polygon centroid
      5. Any bare coordinate pair           → first match

    Returns (None, None, None) if nothing found.
    """
    # 1. WI RADIUS (also captures radius_nm)
    m = _WI_RADIUS.search(etext)
    if m:
        radius_nm = float(m.group(1))
        lat, lon  = _dms6_to_dec(m.group(2), m.group(3))
        return lat, lon, radius_nm

    # 2. Labeled point: POSITION: or PSN:
    m = _LABELED_POINT.search(etext)
    if m:
        lat, lon = _dms6_to_dec(m.group(1), m.group(2))
        return lat, lon, None

    # 3. AT PSN
    m = _AT_PSN.search(etext)
    if m:
        lat, lon = _dms6_to_dec(m.group(1), m.group(2))
        return lat, lon, None

    # 4. Polygon — collect all coordinate pairs after "PSN"
    m = _POLYGON_START.search(etext)
    if m:
        # Find all coordinate pairs in the text from this point on
        all_pairs = _SINGLE_COORD.findall(etext)
        if all_pairs:
            points = [_dms6_to_dec(lat_s, lon_s) for lat_s, lon_s in all_pairs]
            lat, lon = _centroid(points)
            return lat, lon, None

    # 5. Bare coordinate pair anywhere in the E-field
    m = _SINGLE_COORD.search(etext)
    if m:
        lat, lon = _dms6_to_dec(m.group(1), m.group(2))
        return lat, lon, None

    return None, None, None


def _parse_qline_coord(
    coord_str: str,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Parse Q-line coarse coordinate (DDMM format) → (lat, lon, radius_nm)."""
    m = _QLINE_COORD.match(coord_str.strip())
    if not m:
        return None, None, None
    lat, lon = _qline_coord_to_dec(m.group(1), m.group(2))
    radius   = float(m.group(3)) if m.group(3) else None
    return lat, lon, radius


def _parse_dt(raw: str) -> Optional[datetime]:
    """Parse a 10-digit NOTAM datetime (YYMMDDHHmm)."""
    if raw.upper() == "PERM":
        return None
    try:
        return datetime.strptime(raw, "%y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_notam(raw_text: str, source: str = "") -> Optional[Notam]:
    """
    Parse a single raw NOTAM string into a :class:`Notam`.
    Returns None if the text cannot be parsed.
    """
    text = raw_text.strip()

    # ── NOTAM ID ──────────────────────────────────────────────────────────────
    id_match = _NOTAM_ID.search(text)
    if not id_match:
        logger.debug("Could not extract NOTAM ID, skipping.")
        return None
    notam_id = id_match.group(1)

    # ── A) location ───────────────────────────────────────────────────────────
    a_match = _FIELD_A.search(text)
    icao    = a_match.group(1).split()[0] if a_match else "ZZZZ"

    # ── B) / C) validity ──────────────────────────────────────────────────────
    b_match    = _FIELD_B.search(text)
    c_match    = _FIELD_C.search(text)
    valid_from = _parse_dt(b_match.group(1)) if b_match else datetime.now(tz=timezone.utc)
    valid_to   = _parse_dt(c_match.group(1)) if c_match else None

    # ── Q) line ───────────────────────────────────────────────────────────────
    subject          = NotamSubject.UNKNOWN
    condition        = NotamCondition.UNKNOWN
    lower_ft         = 0
    upper_ft         = 99999
    raw_subject_code = ""
    # Coarse position from Q-line (degrees+minutes only, airport-level precision)
    q_lat = q_lon = q_radius = None

    q_match = _FIELD_Q.search(text)
    if q_match:
        q_line = q_match.group(1).replace("\n", "").strip()
        ql     = _QLINE.match(q_line)
        if ql:
            qcode    = ql.group(2)
            lower_ft = int(ql.group(6)) * 100   # Q-line encodes altitude in hundreds of ft
            upper_ft = int(ql.group(7)) * 100

            subject_code   = qcode[0:2]
            condition_code = qcode[2:4]
            subject        = SUBJECT_MAP.get(subject_code,   NotamSubject.UNKNOWN)
            condition      = CONDITION_MAP.get(condition_code, NotamCondition.UNKNOWN)
            # Preserve for downstream ILS component classification (IG/IS/ID/IO/IM/II)
            raw_subject_code = subject_code
            q_lat, q_lon, q_radius = _parse_qline_coord(ql.group(8))
        else:
            logger.debug(f"[{notam_id}] Q-line pattern mismatch: {q_line!r}")

    # ── E) free text + precise coordinates ────────────────────────────────────
    e_match     = _FIELD_E.search(text)
    description = e_match.group(1).strip().rstrip(")") if e_match else ""

    # Try to get a precise position from the E-field; fall back to Q-line
    e_lat = e_lon = e_radius = None
    if e_match:
        e_lat, e_lon, e_radius = extract_position(description)

    lat       = e_lat      if e_lat      is not None else q_lat
    lon       = e_lon      if e_lon      is not None else q_lon
    radius_nm = e_radius   if e_radius   is not None else q_radius

    return Notam(
        id=notam_id,
        icao=icao,
        subject=subject,
        condition=condition,
        valid_from=valid_from or datetime.now(tz=timezone.utc),
        valid_to=valid_to,
        lower_ft=lower_ft,
        upper_ft=upper_ft,
        lat=lat,
        lon=lon,
        radius_nm=radius_nm,
        description=description,
        raw=text,
        source=source,
        raw_subject_code=raw_subject_code,
    )


# ── FAA NOTAM parser ─────────────────────────────────────────────────────────
#
# FAA domestic format:
#   !LOC YY/NNN ICAO SUBJECT [FREE_TEXT] STARTTIME-ENDTIME
#
# Examples:
#   !JFK 04/184 JFK TWY K CLSD 2604110712-2608312300
#   !JFK 04/099 JFK OBST CRANE 403831N0734112W (524FT AMSL) FLAGGED AND LGTD 2604010000-2608012359
#   !EWR 03/044 EWR ILS RWY 04R LOC U/S 2603211400-2606212359

_FAA_HEADER = re.compile(
    r"^!(\w+)\s+(\d{2}/\d{3,5})\s+([A-Z]{3,4})\s+(.+?)\s+(\d{10})-(\d{10}[A-Z]*)\s*$",
    re.DOTALL,
)

# FAA validity: YYMMDDHHMM (10 digits)
_FAA_DT = re.compile(r"^(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")

# FAA coordinates embedded in free text: 403831N0734112W
_FAA_COORD = re.compile(r"(\d{6}[NS])\s*(\d{7}[EW])")

# Height in FAA obstacle NOTAMs: (524FT AMSL) or (524FT AGL)
_FAA_HEIGHT = re.compile(r"\((\d+)FT\s+(?:AMSL|AGL|MSL)\)", re.IGNORECASE)

# Subject keyword → NotamSubject (checked in order; first match wins)
_FAA_SUBJECT_RULES: list[tuple[re.Pattern, "NotamSubject"]] = []
# Condition keyword → NotamCondition (checked in order)
_FAA_CONDITION_RULES: list[tuple[re.Pattern, "NotamCondition"]] = []


def _build_faa_rules() -> None:
    """Populate rule lists after models are imported (avoids circular deps)."""
    S = NotamSubject
    C = NotamCondition
    _FAA_SUBJECT_RULES.extend([
        (re.compile(r'\bOBST\b'),                                     S.OBSTACLE),
        (re.compile(r'\bILS\b|\bLOC\b|\bGLIDESLOPE\b|\bG/S\b'),      S.ILS),
        (re.compile(r'\bVOR\b|\bTACAN\b|\bVORTAC\b'),                 S.VOR),
        (re.compile(r'\bNDB\b'),                                       S.NDB),
        (re.compile(r'\bRWY\b|\bRUNWAY\b'),                           S.RUNWAY),
        (re.compile(r'\bTWY\b|\bTAXIWAY\b|\bTXLN\b'),                S.TAXIWAY),
        (re.compile(r'\bAPRON\b|\bRAMP\b|\bPARKING\b|\bSTAND\b'),     S.APRON),
        (re.compile(r'\bPAPI\b|\bVASI\b|\bREIL\b|\bALS\b|\bLGT\b'),   S.LIGHTING),
        (re.compile(r'\bTFR\b|\bFLT RESTR\b'),                        S.TFR),
        (re.compile(r'\bUAS\b|\bDRONE\b|\bUAV\b'),                    S.UAV),
        (re.compile(r'\bATIS\b|\bGND\b|\bFREQ\b|\bCOMM\b'),           S.COMMS),
    ])
    _FAA_CONDITION_RULES.extend([
        (re.compile(r'\bCLSD\b'),                                                  C.CLOSED),
        (re.compile(r'\bU/S\b|\bUNSERVICEABLE\b|\bOUT OF SVC\b|\bNOT AVBL\b'),   C.UNSERVICEABLE),
        (re.compile(r'\bWIP\b|\bWORK IN PROGRESS\b|\bCONSTRCN\b'),               C.CHANGED),
        (re.compile(r'\bERCTD\b|\bERECTED\b|\bERRECTED\b|\bNEW OBST\b'),         C.NEW),
        (re.compile(r'\bACTIVATED\b|\bIN SVC\b|\bSERVICEABLE\b|\bRSTRD\b'),      C.RESTRICTED),
        (re.compile(r'\bCHGD\b|\bCHANGED\b|\bAMND\b'),                           C.CHANGED),
        (re.compile(r'\bLIMTD\b|\bLIMITED\b'),                                    C.LIMITED),
    ])


def _parse_faa_dt(raw: str) -> Optional[datetime]:
    """Parse a 10-digit FAA datetime YYMMDDHHMM → UTC datetime."""
    m = _FAA_DT.match(raw)
    if not m:
        return None
    yy, mo, dd, hh, mn = (int(x) for x in m.groups())
    year = 2000 + yy
    try:
        return datetime(year, mo, dd, hh, mn, tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_faa_notam(raw_text: str, source: str = "") -> Optional[Notam]:
    """
    Parse a single FAA-format NOTAM string.
    Returns None if the text is not a recognisable FAA NOTAM.
    """
    text = raw_text.strip()
    if not text.startswith("!"):
        return None

    m = _FAA_HEADER.match(text)
    if not m:
        logger.debug(f"[faa] No header match: {text[:60]!r}")
        return None

    fir, seq, icao, body, start_raw, end_raw = m.groups()

    # Pad ICAO to 4 chars: FAA sometimes uses 3-letter location codes
    if len(icao) == 3:
        icao = "K" + icao

    notam_id = f"{icao[:4]}-{seq}"
    valid_from = _parse_faa_dt(start_raw) or datetime.now(tz=timezone.utc)
    valid_to   = _parse_faa_dt(end_raw[:10])  # strip trailing timezone letters

    body_upper = body.upper()

    # Detect subject
    if not _FAA_SUBJECT_RULES:
        _build_faa_rules()

    subject = NotamSubject.UNKNOWN
    for pattern, subj in _FAA_SUBJECT_RULES:
        if pattern.search(body_upper):
            subject = subj
            break

    # Detect condition
    condition = NotamCondition.UNKNOWN
    for pattern, cond in _FAA_CONDITION_RULES:
        if pattern.search(body_upper):
            condition = cond
            break

    # Extract coordinates from body
    lat = lon = radius_nm = None
    coord_m = _FAA_COORD.search(body)
    if coord_m:
        lat, lon = _dms6_to_dec(coord_m.group(1), coord_m.group(2))

    # Extract height for obstacles
    upper_ft = 99999
    ht_m = _FAA_HEIGHT.search(body_upper)
    if ht_m:
        upper_ft = int(ht_m.group(1))

    return Notam(
        id=notam_id,
        icao=icao,
        subject=subject,
        condition=condition,
        valid_from=valid_from,
        valid_to=valid_to,
        lower_ft=0,
        upper_ft=upper_ft,
        lat=lat,
        lon=lon,
        radius_nm=radius_nm,
        description=body.strip(),
        raw=text,
        source=source,
    )


def parse_notams(raw_texts: list[str], source: str = "") -> list[Notam]:
    """Parse a list of raw NOTAM strings, silently skipping unparseable ones.
    Tries ICAO format first, then FAA domestic format."""
    results: list[Notam] = []
    for raw in raw_texts:
        n = parse_notam(raw, source=source)
        if not n:
            n = parse_faa_notam(raw, source=source)
        if n:
            results.append(n)
    return results
