"""
Central orchestrator.

Listens for position changes from SimConnect, fetches NOTAMs for
nearby airports, classifies them into MSFS actions, and dispatches
those actions to the appropriate MSFS subsystem.

All heavy I/O (HTTP, DB) runs in an asyncio event loop on a dedicated
thread so the Qt main thread stays responsive.

Object lifecycle
----------------
- Obstacle objects are placed only when within obstacle_placement_radius_nm.
- On each position update, any placed object that is now beyond that radius
  is removed from MSFS automatically.
- Any object whose NOTAM has expired (valid_to passed) is also removed.
- The NOTAM set is refreshed on a timer (notam_refresh_interval_min) so
  new NOTAMs published mid-flight are picked up without aircraft movement.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from PySide6.QtCore import QObject, Signal

from src.airports.lookup import AirportLookup, _haversine_nm
from src.config import settings
from src.db.cache import NotamCache
from src.msfs.connector import SimConnectWrapper
from src.msfs.navaids import NavaidController
from src.msfs.objects import ObjectPlacer
from src.notam.classifier import classify_all
from src.notam.fetcher import build_aggregator
from src.notam.models import MsfsAction, Notam
from src.notam.parser import parse_notams


class Scheduler(QObject):
    """Coordinates all background work and exposes Qt signals for the UI."""

    notams_updated   = Signal(list)          # list[Notam]
    actions_updated  = Signal(list)          # list[MsfsAction]
    position_updated = Signal(float, float, float)
    sim_status       = Signal(str)

    def __init__(self) -> None:
        super().__init__()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._refresh_task: Optional[asyncio.Task] = None

        self.airport_lookup = AirportLookup()
        self.cache          = NotamCache()
        self.fetcher        = build_aggregator(
            notams_online_enabled=settings.notams_online_enabled,
            checkwx_api_key=settings.checkwx_api_key,
        )
        self.object_placer = ObjectPlacer()
        self.navaid_ctrl   = NavaidController()

        self.connector = SimConnectWrapper(
            poll_interval_s=settings.position_poll_interval_s,
            min_move_nm=settings.min_move_nm,
            enabled=settings.simconnect_enabled,
        )
        self.connector.position_changed.connect(self._on_position_changed)
        self.connector.status_message.connect(self.sim_status)

        self._current_notams:  list[Notam]      = []
        self._current_actions: list[MsfsAction] = []

        # Last known aircraft position (set from SimConnect thread, read in asyncio loop)
        self._last_lat: float = 0.0
        self._last_lon: float = 0.0

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="scheduler-loop"
        )
        self._loop_thread.start()
        asyncio.run_coroutine_threadsafe(self._init_async(), self._loop)
        self.connector.start()
        logger.info("Scheduler started.")

    def stop(self) -> None:
        self.connector.stop()
        sm = getattr(self.connector, "_sm", None)
        self.object_placer.remove_all(sm)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("Scheduler stopped.")

    # ── Qt slot (called from SimConnect thread) ────────────────────────────────

    def _on_position_changed(self, lat: float, lon: float, alt: float) -> None:
        self._last_lat = lat
        self._last_lon = lon
        self.position_updated.emit(lat, lon, alt)
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._fetch_and_apply(lat, lon), self._loop
            )

    # ── Asyncio core ───────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _init_async(self) -> None:
        await self.cache.init()
        # Start the periodic NOTAM refresh timer
        self._refresh_task = asyncio.create_task(self._periodic_refresh())

    async def _periodic_refresh(self) -> None:
        """Re-fetch NOTAMs every notam_refresh_interval_min regardless of movement."""
        interval_s = settings.notam_refresh_interval_min * 60
        while True:
            await asyncio.sleep(interval_s)
            if self._last_lat == 0.0 and self._last_lon == 0.0:
                continue   # no position yet
            logger.info(
                f"[scheduler] Periodic NOTAM refresh "
                f"(every {settings.notam_refresh_interval_min} min)"
            )
            await self._fetch_and_apply(self._last_lat, self._last_lon)

    async def _fetch_and_apply(self, lat: float, lon: float) -> None:
        """Main pipeline: position → airports → NOTAMs → classify → MSFS."""
        if not self.airport_lookup.loaded:
            logger.warning("Airport database not loaded — skipping NOTAM fetch.")
            return

        icao_codes = self.airport_lookup.icao_codes_within(lat, lon, settings.notam_radius_nm)
        if not icao_codes:
            logger.debug(f"No airports within {settings.notam_radius_nm} nm.")
            return

        logger.info(f"Fetching NOTAMs for {len(icao_codes)} airports near {lat:.3f},{lon:.3f}")

        raw_texts = await self.fetcher.fetch_all(icao_codes)
        notams    = parse_notams(raw_texts)

        await self.cache.upsert_notams(notams)
        await self.cache.purge_expired(settings.max_notam_age_h)

        active_notams = [n for n in notams if n.is_active]
        self._current_notams = active_notams
        self.notams_updated.emit(active_notams)

        if settings.auto_apply_notams:
            actions = classify_all(active_notams)
            self._current_actions = actions
            await self._apply_actions(actions, aircraft_lat=lat, aircraft_lon=lon)
            self.actions_updated.emit(actions)

    async def _apply_actions(
        self,
        actions: list[MsfsAction],
        aircraft_lat: float,
        aircraft_lon: float,
    ) -> None:
        sm = getattr(self.connector, "_sm", None)

        # Drive the SimConnect pump (resolves object IDs + lighting writes)
        self.object_placer.pump_dispatch(sm)

        # ── Obstacle lifecycle ─────────────────────────────────────────────────
        # Collect the set of active obstacle notam_ids that are close enough
        wanted: set[str] = set()
        for action in actions:
            if action.action_type != "place_obstacle":
                continue
            p = action.params
            if not (p.get("lat") and p.get("lon")):
                continue
            dist_nm = _haversine_nm(aircraft_lat, aircraft_lon, p["lat"], p["lon"])
            if dist_nm <= settings.obstacle_placement_radius_nm:
                wanted.add(action.notam_id)

        # Remove objects that are now out of range or whose NOTAM is gone
        currently_placed = self.object_placer.placed_notam_ids
        for notam_id in currently_placed - wanted:
            logger.info(f"[scheduler] Removing obstacle {notam_id} — out of range or expired")
            self.object_placer.remove(sm, notam_id)

        # ── Apply each action ──────────────────────────────────────────────────
        for action in actions:
            try:
                match action.action_type:

                    case "place_obstacle":
                        if action.notam_id not in wanted:
                            continue   # too far away, already handled above
                        p = action.params
                        ok = self.object_placer.place(
                            sm,
                            notam_id=action.notam_id,
                            lat=p["lat"],
                            lon=p["lon"],
                            alt_ft=p.get("upper_ft", 0),
                            lit=p.get("lit", True),
                        )
                        action.applied = ok

                    case "disable_ils" | "disable_navaid":
                        navaid_type = (
                            "ILS" if action.action_type == "disable_ils"
                            else action.params.get("navaid_type", "VOR")
                        )
                        ok = self.navaid_ctrl.disable(
                            sm,
                            notam_id=action.notam_id,
                            icao=action.icao,
                            navaid_type=navaid_type,
                        )
                        action.applied = ok

                    case "close_runway":
                        # SimConnect has no direct runway-close API.
                        # Log prominently so the pilot is warned via the UI.
                        logger.warning(
                            f"[scheduler] RUNWAY CLOSURE: {action.icao} — "
                            f"{action.params.get('description','')[:80]}"
                        )
                        action.applied = False   # flagged but not applied in sim

                    case "set_tfr":
                        logger.warning(
                            f"[scheduler] TFR ACTIVE: {action.icao} "
                            f"{action.params.get('lower_ft',0)}–{action.params.get('upper_ft',0)}ft "
                            f"r={action.params.get('radius_nm','?')}nm — "
                            f"{action.params.get('description','')[:60]}"
                        )
                        action.applied = False   # displayed in UI, not enforced in sim

                    case _:
                        logger.debug(
                            f"Action type '{action.action_type}' not implemented."
                        )

                if action.applied:
                    action.applied_at = datetime.now(tz=timezone.utc)

            except Exception as exc:
                action.error = str(exc)
                logger.error(
                    f"Error applying {action.action_type} for {action.notam_id}: {exc}"
                )

            await self.cache.upsert_action(action)

    # ── UI accessors ───────────────────────────────────────────────────────────

    @property
    def current_notams(self) -> list[Notam]:
        return self._current_notams

    @property
    def current_actions(self) -> list[MsfsAction]:
        return self._current_actions
