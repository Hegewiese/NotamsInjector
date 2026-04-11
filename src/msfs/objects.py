"""
Place and remove crane/obstacle SimObjects in MSFS via SimConnect.

How placement works
-------------------
SimConnect_AICreateSimulatedObject spawns a named SimObject at a given
lat/lon/alt.  The object ID is returned *asynchronously* via a
SIMCONNECT_RECV_ASSIGNED_OBJECT_ID message through the SimConnect dispatch
pump.  We track pending requests by request-ID and resolve them on each
pump_dispatch() call.

IMPORTANT – ctypes callback lifetime
-------------------------------------
The CFUNCTYPE wrapper (_DispatchProc) must be stored as an instance
attribute.  If it is a local variable the GC will free it while C still
holds a pointer, causing a hard crash.

Crane model titles (tried in priority order)
--------------------------------------------
  "Asobo_TowerCrane_Construction"   MSFS 2024 / World Update scenery
  "Asobo_Crane_01"                  Earlier World Update variants
  "Construction Crane"              Generic title in some packages
  "Antenna"                         Universal fallback (always present)

The first title MSFS accepts is cached for all subsequent placements.

Lighting
--------
After the object ID is confirmed we write LIGHT BEACON (Bool SimVar) to
the placed object via SimConnect_SetDataOnSimObject.  Not every model
exposes this variable; the write is best-effort and logged either way.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass
from typing import Optional

from loguru import logger

# ── Platform guard ──────────────────────────────────────────────────────────────
_ON_WINDOWS = sys.platform == "win32"

# ── SimConnect constants ────────────────────────────────────────────────────────
_SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID = 6
_SIMCONNECT_DATATYPE_INT32             = 3   # SIMCONNECT_DATATYPE_INT32
_DEF_LIGHT_BEACON                      = 1   # arbitrary data-definition ID

# ── ctypes structs ──────────────────────────────────────────────────────────────

class SIMCONNECT_DATA_INITPOSITION(ctypes.Structure):
    """Mirrors SIMCONNECT_DATA_INITPOSITION from SimConnect.h"""
    _fields_ = [
        ("Latitude",  ctypes.c_double),   # degrees
        ("Longitude", ctypes.c_double),   # degrees
        ("Altitude",  ctypes.c_double),   # feet MSL
        ("Pitch",     ctypes.c_double),
        ("Bank",      ctypes.c_double),
        ("Heading",   ctypes.c_double),   # degrees true
        ("OnGround",  ctypes.c_uint32),   # 1 = on ground
        ("Airspeed",  ctypes.c_uint32),   # knots (0 for static)
    ]

class _RecvHdr(ctypes.Structure):
    _fields_ = [
        ("dwSize",    ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwID",      ctypes.c_uint32),
    ]

class _RecvAssignedObj(ctypes.Structure):
    _fields_ = [
        ("dwSize",      ctypes.c_uint32),
        ("dwVersion",   ctypes.c_uint32),
        ("dwID",        ctypes.c_uint32),
        ("dwRequestID", ctypes.c_uint32),
        ("dwObjectID",  ctypes.c_uint32),
    ]

# Dispatch callback type — must match DispatchProc signature exactly.
# Stored as a class attribute so it is never garbage-collected.
_DispatchProc = ctypes.CFUNCTYPE(
    None,             # return type: void
    ctypes.c_void_p,  # SIMCONNECT_RECV* pData
    ctypes.c_uint32,  # DWORD cbData
    ctypes.c_void_p,  # void* pContext
)

# ── Candidate crane model titles ────────────────────────────────────────────────
_CRANE_TITLES = [
    "Asobo_TowerCrane_Construction",
    "Asobo_Crane_01",
    "Construction Crane",
    "Antenna",   # always-present fallback
]


@dataclass
class PlacedObject:
    notam_id:  str
    title:     str
    lat:       float
    lon:       float
    alt_ft:    float
    lit:       bool
    object_id: int  = 0      # set when SimConnect confirms placement
    confirmed: bool = False  # True once object_id is known


class ObjectPlacer:
    """
    Manages the lifecycle of obstacle SimObjects placed on behalf of NOTAMs.

    Call ``pump_dispatch(sm)`` from the SimConnect poll loop so pending
    object-ID assignments and lighting writes are resolved.
    """

    def __init__(self) -> None:
        self._placed:        dict[str, PlacedObject] = {}  # notam_id → object
        self._pending:       dict[int, str]           = {}  # request_id → notam_id
        self._pending_light: list[PlacedObject]       = []  # confirmed, awaiting light write
        self._req_counter = 0
        self._lock = threading.Lock()
        self._working_title: Optional[str] = None
        self._light_registered = False

        # Keep a live reference to the CFUNCTYPE wrapper so it is never GC'd
        self._cb = _DispatchProc(self._dispatch_cb)

    # ── Public API ──────────────────────────────────────────────────────────────

    def place(
        self,
        sm: object,
        notam_id: str,
        lat: float,
        lon: float,
        alt_ft: float = 0.0,
        lit: bool = True,
    ) -> bool:
        """
        Place a crane SimObject.  Returns True if dispatched (or mocked).
        The actual object_id arrives later via pump_dispatch().
        """
        with self._lock:
            already = notam_id in self._placed
        if already:
            return True   # already placed, no-op

        title = self._working_title or _CRANE_TITLES[0]

        if not _ON_WINDOWS or sm is None:
            return self._mock_place(notam_id, title, lat, lon, alt_ft, lit)

        return self._sc_place(sm, notam_id, title, lat, lon, alt_ft, lit)

    def remove(self, sm: object, notam_id: str) -> bool:
        """Remove a previously placed object. Returns True if it existed."""
        with self._lock:
            obj = self._placed.pop(notam_id, None)
        if obj is None:
            return False

        if not _ON_WINDOWS or sm is None or not obj.confirmed:
            logger.info(f"[objects] Removed (mock/pending) object for {notam_id}")
            return True

        try:
            req_id = self._next_req()
            sm.dll.SimConnect_AIRemoveObject(
                sm.hSimConnect,
                ctypes.c_uint32(obj.object_id),
                ctypes.c_uint32(req_id),
            )
            logger.info(f"[objects] Removed SimObject {obj.object_id} for {notam_id}")
            return True
        except Exception as exc:
            logger.error(f"[objects] Remove failed for {notam_id}: {exc}")
            return False

    def remove_all(self, sm: object) -> None:
        for notam_id in list(self._placed.keys()):
            self.remove(sm, notam_id)

    def pump_dispatch(self, sm: object) -> None:
        """
        Drive the SimConnect message pump.  Call once per poll cycle.
        Resolves pending object-ID assignments and applies deferred lighting writes.
        """
        if not _ON_WINDOWS or sm is None:
            return
        try:
            sm.dll.SimConnect_CallDispatch(sm.hSimConnect, self._cb, None)
        except Exception as exc:
            logger.debug(f"[objects] pump_dispatch error: {exc}")

        # Apply deferred lighting writes for objects confirmed in _dispatch_cb
        with self._lock:
            pending = self._pending_light[:]
            self._pending_light.clear()
        for obj in pending:
            self._apply_light(sm, obj)

    @property
    def placed_notam_ids(self) -> set[str]:
        with self._lock:
            return set(self._placed.keys())

    @property
    def active(self) -> list[PlacedObject]:
        with self._lock:
            return list(self._placed.values())

    # ── Internal – SimConnect path ───────────────────────────────────────────────

    def _sc_place(
        self,
        sm: object,
        notam_id: str,
        title: str,
        lat: float,
        lon: float,
        alt_ft: float,
        lit: bool,
    ) -> bool:
        self._ensure_light_definition(sm)

        # If we already have a working title use only that; otherwise probe the list
        titles_to_try = [title] if self._working_title else _CRANE_TITLES

        for t in titles_to_try:
            req_id = self._next_req()
            init_pos = SIMCONNECT_DATA_INITPOSITION(
                Latitude  = lat,
                Longitude = lon,
                Altitude  = alt_ft,
                Pitch=0.0, Bank=0.0, Heading=0.0,
                OnGround=1, Airspeed=0,
            )
            try:
                hr = sm.dll.SimConnect_AICreateSimulatedObject(
                    sm.hSimConnect,
                    ctypes.c_char_p(t.encode()),
                    init_pos,
                    ctypes.c_uint32(req_id),
                )
                if hr == 0:   # S_OK
                    obj = PlacedObject(
                        notam_id=notam_id, title=t,
                        lat=lat, lon=lon, alt_ft=alt_ft, lit=lit,
                    )
                    with self._lock:
                        self._placed[notam_id] = obj
                        self._pending[req_id]  = notam_id
                    if not self._working_title:
                        self._working_title = t
                        logger.info(f"[objects] Crane model confirmed: '{t}'")
                    logger.info(
                        f"[objects] Placed '{t}' for {notam_id} "
                        f"at {lat:.4f},{lon:.4f}  lit={lit}"
                    )
                    return True
                logger.debug(f"[objects] Model '{t}' rejected (hr={hr:#010x})")
            except Exception as exc:
                logger.debug(f"[objects] Model '{t}' error: {exc}")

        logger.warning(f"[objects] No working crane model found for {notam_id}")
        return False

    def _ensure_light_definition(self, sm: object) -> None:
        """Register the LIGHT BEACON data definition once per session."""
        if self._light_registered:
            return
        try:
            sm.dll.SimConnect_AddToDataDefinition(
                sm.hSimConnect,
                ctypes.c_uint32(_DEF_LIGHT_BEACON),
                ctypes.c_char_p(b"LIGHT BEACON"),
                ctypes.c_char_p(b"Bool"),
                ctypes.c_uint32(_SIMCONNECT_DATATYPE_INT32),
                ctypes.c_float(0.0),
                ctypes.c_uint32(0xFFFFFFFF),  # SIMCONNECT_UNUSED
            )
            self._light_registered = True
            logger.debug("[objects] LIGHT BEACON data definition registered")
        except Exception as exc:
            logger.debug(f"[objects] Could not register LIGHT BEACON definition: {exc}")

    def _apply_light(self, sm: object, obj: PlacedObject) -> None:
        """Write LIGHT BEACON SimVar to a confirmed placed object."""
        if not _ON_WINDOWS or sm is None or not self._light_registered:
            return
        value = ctypes.c_int32(1 if obj.lit else 0)
        try:
            sm.dll.SimConnect_SetDataOnSimObject(
                sm.hSimConnect,
                ctypes.c_uint32(_DEF_LIGHT_BEACON),
                ctypes.c_uint32(obj.object_id),
                ctypes.c_uint32(0),   # SIMCONNECT_DATA_SET_FLAG_DEFAULT
                ctypes.c_uint32(0),   # array count
                ctypes.c_uint32(ctypes.sizeof(value)),
                ctypes.byref(value),
            )
            logger.debug(
                f"[objects] LIGHT BEACON={'ON' if obj.lit else 'OFF'} "
                f"for {obj.notam_id} (object_id={obj.object_id})"
            )
        except Exception as exc:
            logger.debug(f"[objects] LIGHT BEACON write failed: {exc}")

    # ── Dispatch callback ────────────────────────────────────────────────────────

    def _dispatch_cb(
        self,
        recv_p: ctypes.c_void_p,
        cb_data: ctypes.c_uint32,
        context: ctypes.c_void_p,
    ) -> None:
        """
        Called by SimConnect for every inbound message.
        Resolves ASSIGNED_OBJECT_ID → confirms placement and applies lighting.
        Must not raise — any exception is caught and logged.
        """
        try:
            hdr = ctypes.cast(recv_p, ctypes.POINTER(_RecvHdr)).contents
            if hdr.dwID != _SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID:
                return

            msg = ctypes.cast(recv_p, ctypes.POINTER(_RecvAssignedObj)).contents
            req_id = msg.dwRequestID
            obj_id = msg.dwObjectID

            with self._lock:
                notam_id = self._pending.pop(req_id, None)
                obj = self._placed.get(notam_id) if notam_id else None
                if obj:
                    obj.object_id = obj_id
                    obj.confirmed  = True

            if obj:
                logger.info(
                    f"[objects] Confirmed object_id={obj_id} for {notam_id} "
                    f"(lit={obj.lit})"
                )
                # sm is not available inside the callback; queue for the
                # next pump_dispatch() call where sm IS available.
                with self._lock:
                    self._pending_light.append(obj)
        except Exception as exc:
            logger.debug(f"[objects] dispatch_cb error: {exc}")

    # ── Mock path ────────────────────────────────────────────────────────────────

    def _mock_place(
        self,
        notam_id: str,
        title: str,
        lat: float,
        lon: float,
        alt_ft: float,
        lit: bool,
    ) -> bool:
        obj = PlacedObject(
            notam_id=notam_id, title=title,
            lat=lat, lon=lon, alt_ft=alt_ft, lit=lit,
            object_id=hash(notam_id) & 0xFFFF,
            confirmed=True,
        )
        with self._lock:
            self._placed[notam_id] = obj
        logger.info(
            f"[objects] MOCK placed '{title}' for {notam_id} "
            f"at {lat:.4f},{lon:.4f}  lit={lit}"
        )
        return True

    # ── Helpers ──────────────────────────────────────────────────────────────────

    def _next_req(self) -> int:
        with self._lock:
            self._req_counter += 1
            return self._req_counter
