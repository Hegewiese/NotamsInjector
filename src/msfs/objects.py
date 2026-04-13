"""
Place and remove crane/obstacle SimObjects in MSFS via SimConnect.

How placement works
-------------------
SimConnect_AICreateSimulatedObject spawns a named SimObject at a given
lat/lon/alt.  The object ID is returned *asynchronously* via a
SIMCONNECT_RECV_ASSIGNED_OBJECT_ID message through the SimConnect dispatch
pump.

This module piggy-backs on the SimConnect library's existing _run dispatch
loop (which already calls SimConnect_CallDispatch every 2 ms) rather than
running a second competing CallDispatch call.  We inject a combined handler
by replacing sm.my_dispatch_proc_rd with a wrapper that calls both the
original handler and our own ASSIGNED_OBJECT_ID tracker.

Why we access sm.dll.SimConnect (raw DLL) directly
----------------------------------------------------
The python-SimConnect library's SimConnectDll wrapper sets argtypes on its
function objects using types imported via ``from .Enum import *``.  Our code
imports the same types via a different import path (``from SimConnect import
Enum as SCEnum``).  Although both refer to the same Enum.py file, Python
sometimes creates distinct class objects (e.g. due to __pycache__ timing),
so ctypes rejects the cross-origin struct instance with:
  "expected SIMCONNECT_DATA_INITPOSITION instance instead of
   SIMCONNECT_DATA_INITPOSITION"
Calling ``sm.dll.SimConnect.SimConnect_XXX`` (the underlying windll) with
argtypes=None bypasses this entirely; we hand-construct correctly-typed
ctypes arguments ourselves.

Crane model title
-----------------
    "Pipelay_Crane_SK3000"            Confirmed present in MSFS2024
"""

from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from src.msfs.obstacle_catalog import ObstacleModelEntry, load_obstacle_catalog, resolve_obstacle_entry

# ── Platform guard ──────────────────────────────────────────────────────────────
_ON_WINDOWS = sys.platform == "win32"

# ── SimConnect constants ────────────────────────────────────────────────────────
_DEF_LIGHT_BEACON                      = 1   # arbitrary data-definition ID
_SIMCONNECT_DATATYPE_INT32             = 1   # SIMCONNECT_DATATYPE_INT32
_SIMCONNECT_UNUSED                     = 0xFFFFFFFF

# ── Local copy of SIMCONNECT_DATA_INITPOSITION ──────────────────────────────────
# We define our own struct rather than importing from SimConnect.Enum to avoid
# the "expected X instance instead of X" error caused by cross-import class
# identity mismatch.

class _InitPos(ctypes.Structure):
    """Local mirror of SIMCONNECT_DATA_INITPOSITION."""
    _fields_ = [
        ("Latitude",  ctypes.c_double),
        ("Longitude", ctypes.c_double),
        ("Altitude",  ctypes.c_double),
        ("Pitch",     ctypes.c_double),
        ("Bank",      ctypes.c_double),
        ("Heading",   ctypes.c_double),
        ("OnGround",  ctypes.c_uint32),
        ("Airspeed",  ctypes.c_uint32),
    ]


class _RecvEvent(ctypes.Structure):
    """Local mirror of SIMCONNECT_RECV_EVENT (used for SimConnect_Text results)."""
    _fields_ = [
        ("dwSize",    ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwID",      ctypes.c_uint32),
        ("uGroupID",  ctypes.c_uint32),
        ("uEventID",  ctypes.c_uint32),
        ("dwData",    ctypes.c_uint32),   # SIMCONNECT_TEXT_RESULT when from SimConnect_Text
    ]


_FALLBACK_TITLE = "Wind_Turbine_2"


@dataclass
class PlacedObject:
    notam_id:  str
    title:     str
    lat:       float
    lon:       float
    alt_ft:    float
    lit:       bool
    heading:   float = 0.0   # degrees true
    object_id: int   = 0     # set when SimConnect confirms placement
    confirmed: bool  = False  # True once object_id is known


@dataclass
class _PendingPlacement:
    notam_id: str
    lat: float
    lon: float
    alt_ft: float
    lit: bool
    on_ground: bool
    heading: float
    remaining_titles: list[str]


class ObjectPlacer:
    """
    Manages the lifecycle of obstacle SimObjects placed on behalf of NOTAMs.

    Call ``pump_dispatch(sm)`` from the SimConnect poll loop to hook the
    SimConnect library's dispatch and flush deferred lighting writes.
    """

    def __init__(self) -> None:
        self._placed:        dict[str, PlacedObject] = {}  # notam_id → object
        self._pending:       dict[int, str]           = {}  # request_id → notam_id
        self._pending_meta:  dict[int, _PendingPlacement] = {}
        self._pending_packet: dict[int, int]           = {}  # packet_id → req_id
        self._packet_by_req:  dict[int, int]           = {}  # req_id → packet_id
        self._pending_light: list[PlacedObject]       = []  # confirmed, awaiting light write
        self._req_counter = 0
        self._lock = threading.Lock()
        self._working_title: Optional[str] = None
        self._light_registered = False
        self._notifier: Optional[object] = None   # NotamNotifier, set externally
        self._catalog: dict[str, ObstacleModelEntry] = load_obstacle_catalog()

    def set_notifier(self, notifier: object) -> None:
        """Attach a NotamNotifier so text-result events are forwarded to it."""
        self._notifier = notifier

    # ── Public API ──────────────────────────────────────────────────────────────

    def place(
        self,
        sm: object,
        notam_id: str,
        lat: float,
        lon: float,
        alt_ft: float = 0.0,
        lit: bool = True,
        on_ground: bool = True,
        heading: float = 0.0,
        obstacle_kind: str = "generic_obstacle",
        titles_to_try: Optional[list[str]] = None,
    ) -> bool:
        """
        Place a crane SimObject.  Returns True if dispatched (or mocked).
        The actual object_id arrives later via the SimConnect dispatch hook.

        ``on_ground=True``  → MSFS snaps the object to terrain (alt_ft ignored).
        ``on_ground=False`` → MSFS uses alt_ft as MSL altitude (object floats).
        ``heading``         → degrees true; controls yaw of the placed object.
        """
        with self._lock:
            already = notam_id in self._placed
        if already:
            return True   # already placed, no-op

        resolved_titles = titles_to_try or self._titles_for_kind(obstacle_kind)
        title = self._working_title or resolved_titles[0]
        if titles_to_try is None:
            titles_to_try = (
                [self._working_title] + [t for t in resolved_titles if t != self._working_title]
                if self._working_title
                else resolved_titles
            )

        if not _ON_WINDOWS or sm is None:
            return self._mock_place(notam_id, title, lat, lon, alt_ft, lit, on_ground, heading)

        return self._sc_place(
            sm,
            notam_id,
            title,
            lat,
            lon,
            alt_ft,
            lit,
            on_ground=on_ground,
            heading=heading,
            titles_to_try=titles_to_try,
        )

    def highlight_column(
        self,
        sm: object,
        notam_id: str,
        lat: float,
        lon: float,
        base_alt_ft: float = 1500.0,
        step_ft: float = 500.0,
        count: int = 6,
        lit: bool = True,
    ) -> int:
        """
        Place a vertical column of *count* beacons floating above the obstacle.

        Each beacon is placed with on_ground=False at altitudes:
            base_alt_ft, base_alt_ft + step_ft, …, base_alt_ft + (count-1)*step_ft

        IDs are ``{notam_id}-hl-0`` … ``{notam_id}-hl-{count-1}``.

        Returns the number of beacons successfully dispatched.
        """
        placed = 0
        for i in range(count):
            marker_id = f"{notam_id}-hl-{i}"
            alt = base_alt_ft + i * step_ft
            ok = self.place(
                sm,
                notam_id=marker_id,
                lat=lat,
                lon=lon,
                alt_ft=alt,
                lit=lit,
                on_ground=False,
                titles_to_try=self._titles_for_kind("beacon"),
            )
            if ok:
                placed += 1
        if placed:
            logger.info(
                f"[objects] Beacon column ({placed}/{count}) for {notam_id} "
                f"at {lat:.4f},{lon:.4f}  "
                f"{base_alt_ft:.0f}–{base_alt_ft + (count-1)*step_ft:.0f} ft MSL"
            )
        return placed

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
            fn = self._raw_fn(sm, "AIRemoveObject")
            fn(
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
        Called once per poll cycle.
        Hooks our ASSIGNED_OBJECT_ID handler into SimConnect's dispatch loop
        (first call only), then flushes deferred lighting writes.
        """
        if not _ON_WINDOWS or sm is None:
            return

        # Inject our handler into the library's existing dispatch loop
        self._hook_simconnect_dispatch(sm)

        # Apply deferred lighting writes for objects confirmed in the callback
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

    def entry_for_kind(self, obstacle_kind: str) -> ObstacleModelEntry:
        return resolve_obstacle_entry(obstacle_kind, self._catalog)

    # ── Raw DLL helper ───────────────────────────────────────────────────────────

    def _raw_fn(self, sm: object, name: str):
        """
        Return the raw DLL function (no argtypes) for SimConnect_<name>.

        Accessing sm.dll.SimConnect (the underlying windll) bypasses the
        SimConnectDll wrapper's argtypes, which prevents type-mismatch errors
        caused by cross-import class identity issues.
        """
        fn = getattr(sm.dll.SimConnect, f"SimConnect_{name}")
        fn.restype = ctypes.c_long   # HRESULT
        fn.argtypes = None           # disable type checking — we pass explicit types
        return fn

    def _get_last_sent_packet_id(self, sm: object) -> int:
        try:
            fn = self._raw_fn(sm, "GetLastSentPacketID")
            packet = ctypes.c_uint32(0)
            hr = fn(sm.hSimConnect, ctypes.byref(packet))
            if hr == 0:
                return int(packet.value)
            logger.debug(
                f"[objects] GetLastSentPacketID failed (hr={hr:#010x})"
            )
        except Exception as exc:
            logger.debug(f"[objects] GetLastSentPacketID error: {exc}")
        return 0

    # ── SimConnect dispatch hook ─────────────────────────────────────────────────

    def _hook_simconnect_dispatch(self, sm: object) -> None:
        """
        Extend SimConnect's dispatch loop to forward ASSIGNED_OBJECT_ID
        messages to _on_assigned_object().

        The SimConnect library's _run() thread calls:
            sm.dll.CallDispatch(sm.hSimConnect, sm.my_dispatch_proc_rd, None)
        on every iteration.  Because my_dispatch_proc_rd is a live attribute
        lookup, replacing it between iterations makes the next call pick up
        our combined handler.  The original handler is called first so the
        library's own bookkeeping is not disrupted.
        """
        if getattr(sm, "_notam_injector_hooked", False):
            return

        try:
            from SimConnect.Enum import (
                SIMCONNECT_EXCEPTION,
                SIMCONNECT_RECV_ID,
                SIMCONNECT_RECV_ASSIGNED_OBJECT_ID,
                SIMCONNECT_RECV_EXCEPTION,
            )
        except Exception as exc:
            logger.warning(f"[objects] Cannot hook dispatch: {exc}")
            return

        RECV_ASSIGNED  = SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID.value
        RECV_EXCEPTION = SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_EXCEPTION.value
        RECV_EVENT     = SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_EVENT.value
        placer = self
        original_proc = sm.my_dispatch_proc  # bound method — kept alive via sm

        def _combined(pData, cbData, pContext):
            original_proc(pData, cbData, pContext)
            try:
                if pData and pData.contents.dwID == RECV_ASSIGNED:
                    msg = ctypes.cast(
                        pData,
                        ctypes.POINTER(SIMCONNECT_RECV_ASSIGNED_OBJECT_ID),
                    ).contents
                    placer._on_assigned_object(sm, int(msg.dwRequestID), int(msg.dwObjectID))
                elif pData and pData.contents.dwID == RECV_EXCEPTION:
                    exc = ctypes.cast(
                        pData,
                        ctypes.POINTER(SIMCONNECT_RECV_EXCEPTION),
                    ).contents
                    placer._on_dispatch_exception(sm, exc)
                elif pData and pData.contents.dwID == RECV_EVENT:
                    ev = ctypes.cast(pData, ctypes.POINTER(_RecvEvent)).contents
                    notifier = placer._notifier
                    if notifier is not None:
                        notifier.on_text_result(int(ev.uEventID), int(ev.dwData))
            except Exception:
                pass   # never let our code crash the sim dispatch

        # Keep the Python closure alive so GC doesn't free the C pointer
        sm._notam_dispatch_fn = _combined
        sm.my_dispatch_proc_rd = sm.dll.DispatchProc(_combined)
        sm._notam_injector_hooked = True
        logger.debug("[objects] SimConnect dispatch hook installed")

    def _on_assigned_object(self, sm: object, req_id: int, obj_id: int) -> None:
        """Called from the SimConnect dispatch thread when an object ID arrives."""
        with self._lock:
            notam_id = self._pending.pop(req_id, None)
            meta = self._pending_meta.pop(req_id, None)
            obj = self._placed.get(notam_id) if notam_id else None
            packet_id = self._packet_by_req.pop(req_id, None)
            if packet_id is not None:
                self._pending_packet.pop(packet_id, None)

            if obj:
                obj.object_id = obj_id

        if obj is None:
            return

        if obj_id == 0:
            obj.confirmed = False
            if meta and meta.remaining_titles:
                next_title = meta.remaining_titles[0]
                next_remaining = meta.remaining_titles[1:]
                old_title = obj.title
                obj.title = next_title
                logger.warning(
                    f"[objects] Assigned object_id=0 for {notam_id} using '{old_title}'; "
                    f"retrying with '{next_title}'"
                )
                self._retry_create_request(
                    sm,
                    obj,
                    notam_id,
                    next_title,
                    meta.lat,
                    meta.lon,
                    meta.alt_ft,
                    meta.lit,
                    meta.on_ground,
                    meta.heading,
                    next_remaining,
                )
                return

            logger.warning(
                f"[objects] Assigned object_id=0 for {notam_id} using '{obj.title}'; "
                "no fallback models left"
            )
            with self._lock:
                self._placed.pop(notam_id, None)
            return

        obj.confirmed = True
        if self._working_title is None:
            self._working_title = obj.title

        logger.info(
            f"[objects] Confirmed object_id={obj_id} for {notam_id} "
            f"(lit={obj.lit})"
        )
        with self._lock:
            self._pending_light.append(obj)

    def _on_dispatch_exception(self, sm: object, exc: object) -> None:
        """Handle SimConnect exception messages for placement requests."""
        try:
            from SimConnect.Enum import (
                SIMCONNECT_EXCEPTION,
            )
        except Exception:
            return

        code = int(exc.dwException)
        packet_id = int(getattr(exc, "UNKNOWN_SENDID", 0) or 0)
        fallback_req_id = int(getattr(exc, "dwSendID", 0) or 0)
        exception_name = SIMCONNECT_EXCEPTION(code).name

        with self._lock:
            req_id = None
            if packet_id:
                req_id = self._pending_packet.pop(packet_id, None)
            if req_id is None and fallback_req_id in self._pending:
                req_id = fallback_req_id

            if req_id is not None:
                self._packet_by_req.pop(req_id, None)
                notam_id = self._pending.pop(req_id, None)
                meta = self._pending_meta.pop(req_id, None)
            else:
                notam_id = None
                meta = None

        if code != SIMCONNECT_EXCEPTION.SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED.value:
            logger.debug(
                f"[objects] SimConnect exception {exception_name} "
                f"for req_id={req_id} "
                f"(UNKNOWN_SENDID={packet_id}, dwSendID={fallback_req_id})"
            )
            return

        obj = self._placed.get(notam_id) if notam_id else None
        if obj is None:
            logger.warning(
                f"[objects] Create-object failed for unknown request {req_id} "
                f"({exception_name}) - UNKNOWN_SENDID={packet_id}, dwSendID={fallback_req_id}"
            )
            return

        if meta and meta.remaining_titles:
            next_title = meta.remaining_titles[0]
            next_remaining = meta.remaining_titles[1:]
            old_title = obj.title
            obj.title = next_title
            obj.object_id = 0
            obj.confirmed = False
            logger.warning(
                f"[objects] Create failed for '{notam_id}' using "
                f"'{old_title}' ({exception_name}); retrying with '{next_title}'"
            )
            self._retry_create_request(
                sm,
                obj,
                notam_id,
                next_title,
                meta.lat,
                meta.lon,
                meta.alt_ft,
                meta.lit,
                meta.on_ground,
                meta.heading,
                next_remaining,
            )
            return

        logger.warning(
            f"[objects] Create-object failed for '{obj.title}' on {notam_id} "
            f"({exception_name}); no fallback models left"
        )
        with self._lock:
            self._placed.pop(notam_id, None)

    def _retry_create_request(
        self,
        sm: object,
        obj: PlacedObject,
        notam_id: str,
        title: str,
        lat: float,
        lon: float,
        alt_ft: float,
        lit: bool,
        on_ground: bool,
        heading: float,
        remaining_titles: list[str],
    ) -> bool:
        req_id = self._next_req()
        init_pos = _InitPos(
            Latitude=lat,
            Longitude=lon,
            Altitude=alt_ft,
            Pitch=0.0,
            Bank=0.0,
            Heading=heading,
            OnGround=1 if on_ground else 0,
            Airspeed=0,
        )
        with self._lock:
            self._pending[req_id] = notam_id
            self._pending_meta[req_id] = _PendingPlacement(
                notam_id=notam_id,
                lat=lat,
                lon=lon,
                alt_ft=alt_ft,
                lit=lit,
                on_ground=on_ground,
                heading=heading,
                remaining_titles=remaining_titles,
            )
        hr = None
        api_name = "AICreateSimulatedObject"
        try:
            hr, api_name = self._create_request(sm, title, init_pos, req_id)
            if hr == 0:
                packet_id = self._get_last_sent_packet_id(sm)
                if packet_id:
                    with self._lock:
                        self._packet_by_req[req_id] = packet_id
                        self._pending_packet[packet_id] = req_id
                logger.info(
                    f"[objects] Retry placed '{title}' for {notam_id} "
                    f"at {lat:.4f},{lon:.4f}  lit={lit} "
                    f"(api={api_name}, packet_id={packet_id}, req_id={req_id})"
                )
                return True
            logger.warning(
                f"[objects] Retry model '{title}' rejected (hr={hr:#010x}) "
                f"for {notam_id} via {api_name}"
            )
        except Exception as exc:
            logger.warning(
                f"[objects] Error retrying model '{title}' for {notam_id}: {exc}"
            )
        finally:
            if hr is not None and hr != 0:
                with self._lock:
                    self._pending.pop(req_id, None)
                    self._pending_meta.pop(req_id, None)

        if remaining_titles:
            return self._retry_create_request(
                sm,
                obj,
                notam_id,
                remaining_titles[0],
                lat,
                lon,
                alt_ft,
                lit,
                on_ground,
                heading,
                remaining_titles[1:],
            )

        logger.warning(
            f"[objects] No fallback models left for {notam_id} after create failure"
        )
        with self._lock:
            self._placed.pop(notam_id, None)
        return False

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
        on_ground: bool = True,
        heading: float = 0.0,
        titles_to_try: Optional[list[str]] = None,
    ) -> bool:
        self._ensure_light_definition(sm)

        titles_to_try = titles_to_try or [title]

        for index, t in enumerate(titles_to_try):
            remaining = titles_to_try[index + 1 :]
            req_id = self._next_req()
            init_pos = _InitPos(
                Latitude=lat,
                Longitude=lon,
                Altitude=alt_ft,
                Pitch=0.0,
                Bank=0.0,
                Heading=heading,
                OnGround=1 if on_ground else 0,
                Airspeed=0,
            )
            obj = PlacedObject(
                notam_id=notam_id,
                title=t,
                lat=lat,
                lon=lon,
                alt_ft=alt_ft,
                lit=lit,
                heading=heading,
            )
            with self._lock:
                self._pending[req_id] = notam_id
                self._pending_meta[req_id] = _PendingPlacement(
                    notam_id=notam_id,
                    lat=lat,
                    lon=lon,
                    alt_ft=alt_ft,
                    lit=lit,
                    on_ground=on_ground,
                    heading=heading,
                    remaining_titles=remaining,
                )
            hr = None
            api_name = "AICreateSimulatedObject"
            try:
                hr, api_name = self._create_request(sm, t, init_pos, req_id)
                if hr == 0:   # S_OK
                    packet_id = self._get_last_sent_packet_id(sm)
                    if packet_id:
                        with self._lock:
                            self._packet_by_req[req_id] = packet_id
                            self._pending_packet[packet_id] = req_id
                    with self._lock:
                        self._placed[notam_id] = obj
                    logger.info(
                        f"[objects] Placed '{t}' for {notam_id} "
                        f"at {lat:.4f},{lon:.4f}  lit={lit} "
                        f"(api={api_name}, packet_id={packet_id}, req_id={req_id})"
                    )
                    return True
                logger.warning(
                    f"[objects] Model '{t}' rejected (hr={hr:#010x}) "
                    f"for {notam_id} via {api_name}"
                )
            except Exception as exc:
                logger.warning(
                    f"[objects] Error placing model '{t}' for {notam_id}: {exc}"
                )
            finally:
                if hr is not None and hr != 0:
                    with self._lock:
                        self._pending.pop(req_id, None)
                        self._pending_meta.pop(req_id, None)

        logger.warning(f"[objects] No working obstacle model found for {notam_id}")
        return False

    def _titles_for_kind(self, obstacle_kind: str) -> list[str]:
        entry = self.entry_for_kind(obstacle_kind)
        return entry.titles if entry.titles else [_FALLBACK_TITLE]

    def _create_request(self, sm: object, title: str, init_pos: _InitPos, req_id: int) -> tuple[int, str]:
        """Create a SimObject request using AICreateSimulatedObject."""
        fn = self._raw_fn(sm, "AICreateSimulatedObject")
        hr = fn(
            sm.hSimConnect,
            ctypes.c_char_p(title.encode()),
            init_pos,
            ctypes.c_uint32(req_id),
        )
        return int(hr), "AICreateSimulatedObject"

    def _ensure_light_definition(self, sm: object) -> None:
        """Register the LIGHT BEACON data definition once per session."""
        if self._light_registered:
            return
        try:
            fn = self._raw_fn(sm, "AddToDataDefinition")
            hr = fn(
                sm.hSimConnect,
                ctypes.c_uint32(_DEF_LIGHT_BEACON),
                ctypes.c_char_p(b"LIGHT BEACON"),
                ctypes.c_char_p(b"Bool"),
                ctypes.c_uint32(_SIMCONNECT_DATATYPE_INT32),
                ctypes.c_float(0.0),
                ctypes.c_uint32(_SIMCONNECT_UNUSED),
            )
            if hr == 0:
                self._light_registered = True
                logger.debug("[objects] LIGHT BEACON data definition registered")
            else:
                logger.debug(f"[objects] LIGHT BEACON definition rejected (hr={hr:#010x})")
        except Exception as exc:
            logger.debug(f"[objects] Could not register LIGHT BEACON definition: {exc}")

    def _apply_light(self, sm: object, obj: PlacedObject) -> None:
        """Write LIGHT BEACON SimVar to a confirmed placed object."""
        if not _ON_WINDOWS or sm is None or not self._light_registered:
            return
        value = ctypes.c_int32(1 if obj.lit else 0)
        try:
            fn = self._raw_fn(sm, "SetDataOnSimObject")
            fn(
                sm.hSimConnect,
                ctypes.c_uint32(_DEF_LIGHT_BEACON),
                ctypes.c_uint32(obj.object_id),
                ctypes.c_uint32(0),   # SIMCONNECT_DATA_SET_FLAG_DEFAULT
                ctypes.c_uint32(1),   # one scalar value
                ctypes.c_uint32(ctypes.sizeof(value)),
                ctypes.byref(value),
            )
            logger.debug(
                f"[objects] LIGHT BEACON={'ON' if obj.lit else 'OFF'} "
                f"for {obj.notam_id} (object_id={obj.object_id})"
            )
        except Exception as exc:
            logger.debug(f"[objects] LIGHT BEACON write failed: {exc}")

    # ── Mock path ────────────────────────────────────────────────────────────────

    def _mock_place(
        self,
        notam_id: str,
        title: str,
        lat: float,
        lon: float,
        alt_ft: float,
        lit: bool,
        on_ground: bool = True,
        heading: float = 0.0,
    ) -> bool:
        obj = PlacedObject(
            notam_id=notam_id, title=title,
            lat=lat, lon=lon, alt_ft=alt_ft, lit=lit, heading=heading,
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
