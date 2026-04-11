from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class NotamSubject(str, Enum):
    """ICAO Q-line subject codes we handle (chars 0-1 of captured QCODE group)."""
    ILS       = "IL"   # Instrument Landing System
    VOR       = "NV"   # VOR navaid
    NDB       = "NN"   # NDB (non-directional beacon)
    RUNWAY    = "RW"   # Runway
    OBSTACLE  = "OB"   # Obstacle (crane, tower, …)
    APRON     = "AP"   # Apron / stand
    TAXIWAY   = "TX"   # Taxiway
    TFR       = "TF"   # Temporary flight restriction
    AIRSPACE  = "AS"   # Airspace
    LIGHTING  = "LT"   # Airfield lighting
    AERODROME = "FA"   # Aerodrome general (procedures, ATIS, systems)
    PROCEDURE = "PI"   # Instrument approach procedure
    COMMS     = "ST"   # ATC communications / frequencies
    UAV       = "WU"   # UAV / drone operations
    UNKNOWN   = "??"


class NotamCondition(str, Enum):
    """ICAO Q-line condition codes (chars 2-3 of captured QCODE group)."""
    UNSERVICEABLE = "AU"   # Unserviceable
    SERVICEABLE   = "AS"   # Serviceable again / restored
    CLOSED        = "CL"   # Closed
    OPEN          = "CO"   # Open again
    NEW           = "CE"   # New / erected
    CHANGED       = "CH"   # Changed
    RESTRICTED    = "RA"   # Restricted activity
    LIMITED       = "LT"   # Limited (capacity, size, hours)
    LIMITED_PROC  = "LP"   # Limitation of procedure
    LIMITED_WX    = "LW"   # Limitation due to weather/environment
    FREQ_CHANGED  = "CF"   # Frequency changed
    UNKNOWN       = "??"


# Maps raw Q-line codes → our enums
SUBJECT_MAP: dict[str, NotamSubject] = {
    # Navigation aids
    "IL": NotamSubject.ILS,
    "IC": NotamSubject.ILS,    # ILS complete system (calibration / U/S)
    "ID": NotamSubject.ILS,    # ILS DME component
    "IG": NotamSubject.ILS,    # ILS glide path
    "II": NotamSubject.ILS,    # ILS inner marker
    "IM": NotamSubject.ILS,    # ILS middle marker
    "IO": NotamSubject.ILS,    # ILS outer marker
    "IS": NotamSubject.ILS,    # ILS localiser
    "PI": NotamSubject.ILS,    # Precision approach (ILS/MLS/GLS) procedure
    "NV": NotamSubject.VOR,
    "NM": NotamSubject.VOR,    # DVOR / VOR-DME / TACAN
    "NN": NotamSubject.NDB,
    "NB": NotamSubject.NDB,    # NDB beacon (alternate code)
    # Obstacle
    "OB": NotamSubject.OBSTACLE,
    # Movement area
    "RW": NotamSubject.RUNWAY,
    "MR": NotamSubject.RUNWAY,  # Runway (ICAO movement area code)
    "TX": NotamSubject.TAXIWAY,
    "MN": NotamSubject.TAXIWAY, # Taxilane / movement area general
    "MX": NotamSubject.TAXIWAY, # Taxiway system / multiple TWYs
    "MA": NotamSubject.TAXIWAY, # Movement area (general hold-short etc.)
    "AP": NotamSubject.APRON,
    "MK": NotamSubject.APRON,   # Parking / gate / stand
    "MP": NotamSubject.APRON,   # Parking stand / hardstanding
    # Airspace / restrictions
    "TF": NotamSubject.TFR,
    "RO": NotamSubject.TFR,     # Restricted operations area
    "AS": NotamSubject.AIRSPACE,
    "WB": NotamSubject.AIRSPACE, # Glider aerobatics area
    "WG": NotamSubject.AIRSPACE, # Glider / winch launch area
    "PK": NotamSubject.AIRSPACE, # VFR departure / arrival routes
    # Lighting
    "LT": NotamSubject.LIGHTING,
    "LF": NotamSubject.LIGHTING, # Approach / runway flashing lights
    "LX": NotamSubject.LIGHTING, # Aerodrome beacon / apron guidance lights
    # Aerodrome / procedural (no direct MSFS action, but tracked)
    "FA": NotamSubject.AERODROME,
    "AA": NotamSubject.AERODROME, # Airspace / MSA changes
    "CG": NotamSubject.AERODROME, # Approach radar / SRA
    "CP": NotamSubject.AERODROME, # PAR / precision approach radar procedure
    "FF": NotamSubject.AERODROME, # Fire fighting and rescue services
    "FM": NotamSubject.AERODROME, # Met / wind measurement facility
    "FU": NotamSubject.AERODROME, # Fuel and oil availability
    "NA": NotamSubject.AERODROME, # VDF / nav radio transmitter
    "AF": NotamSubject.PROCEDURE, # SID / STAR / instrument procedure suspension
    "PD": NotamSubject.PROCEDURE, # Departure procedure / GNSS approach
    "PO": NotamSubject.PROCEDURE, # OCA / approach minima change
    "PM": NotamSubject.PROCEDURE, # Instrument approach minima change
    "CA": NotamSubject.COMMS,     # ATIS
    "ST": NotamSubject.COMMS,
    "WU": NotamSubject.UAV,
}

CONDITION_MAP: dict[str, NotamCondition] = {
    "AU": NotamCondition.UNSERVICEABLE,
    "CT": NotamCondition.UNSERVICEABLE, # on test / calibration (do not use)
    "AS": NotamCondition.SERVICEABLE,
    "CL": NotamCondition.CLOSED,
    "LC": NotamCondition.CLOSED,    # movement-area closure variant (runway/TWY/stand CLSD)
    "CO": NotamCondition.OPEN,
    "CE": NotamCondition.NEW,
    "CR": NotamCondition.NEW,       # route / procedure created
    "CH": NotamCondition.CHANGED,
    "AH": NotamCondition.CHANGED,   # hours of service changed
    "CG": NotamCondition.CHANGED,   # category downgraded
    "HW": NotamCondition.CHANGED,   # work in progress
    "RA": NotamCondition.RESTRICTED,
    "AP": NotamCondition.RESTRICTED, # available by prior permission (PPR)
    "LT": NotamCondition.LIMITED,
    "LL": NotamCondition.LIMITED,   # usable length / capacity reduced
    "AR": NotamCondition.LIMITED,   # available on request
    "LP": NotamCondition.LIMITED_PROC,
    "LW": NotamCondition.LIMITED_WX,
    "CF": NotamCondition.FREQ_CHANGED,
}


@dataclass
class Notam:
    id: str                          # e.g. A0001/24
    icao: str                        # affected airport ICAO
    subject: NotamSubject
    condition: NotamCondition
    valid_from: datetime
    valid_to: Optional[datetime]     # None = PERM
    lower_ft: int = 0
    upper_ft: int = 99999
    lat: Optional[float] = None      # centre of affected area
    lon: Optional[float] = None
    radius_nm: Optional[float] = None
    description: str = ""            # E-field free text
    raw: str = ""                    # original text
    source: str = ""                 # fetcher that produced this

    @property
    def is_active(self) -> bool:
        from datetime import timezone
        now = datetime.now(tz=timezone.utc)
        if self.valid_from > now:
            return False
        if self.valid_to and self.valid_to < now:
            return False
        return True

    @property
    def affects_navaid(self) -> bool:
        return self.subject in (NotamSubject.ILS, NotamSubject.VOR, NotamSubject.NDB)

    @property
    def affects_runway(self) -> bool:
        return self.subject == NotamSubject.RUNWAY

    @property
    def is_obstacle(self) -> bool:
        return self.subject == NotamSubject.OBSTACLE


@dataclass
class MsfsAction:
    """A concrete action to perform inside MSFS derived from a NOTAM."""

    notam_id: str
    action_type: str   # "disable_navaid" | "place_obstacle" | "close_runway" | "set_tfr"
    icao: str
    params: dict = field(default_factory=dict)
    applied: bool = False
    applied_at: Optional[datetime] = None
    error: Optional[str] = None
