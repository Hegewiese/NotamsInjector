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

from src.airports.lookup import AirportLookup, RunwayLookup, _haversine_nm
from src.airports.openaip import OpenAIPFetcher
from src.config import settings
from src.db.cache import NotamCache
from src.msfs.connector import SimConnectWrapper
from src.msfs.atis import AtisController
from src.msfs.navaids import NavaidController
from src.msfs import wasm_state
from src.msfs.notifier import NotamNotifier
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
    position_sampled = Signal(float, float, float)  # lat, lon, heading_deg (every poll)
    fetch_progress   = Signal(int, int)       # (done, total) airports fetched
    poll_progress    = Signal(int)            # movement progress dial (0-100)
    sim_status       = Signal(str)
    alert_overlay_batch = Signal(list, bool)  # list[(title,text,dist,icao,name,type,is_new)], pop_up


    def __init__(self, mock_position: bool = False) -> None:
        super().__init__()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

        self.airport_lookup = AirportLookup()
        self.runway_lookup  = RunwayLookup()
        self.cache          = NotamCache()
        self.openaip:       OpenAIPFetcher | None = None   # set after DB init
        self.fetcher        = build_aggregator(
            notams_online_enabled=settings.notams_online_enabled,
            checkwx_api_key=settings.checkwx_api_key,
        )
        self.object_placer = ObjectPlacer()
        self.notifier      = NotamNotifier()
        self.object_placer.set_notifier(self.notifier)
        self.navaid_ctrl   = NavaidController()
        self.atis_ctrl     = AtisController()

        self.mock_position = mock_position

        if not self.mock_position:
            self.connector = SimConnectWrapper(
                poll_interval_s=settings.position_poll_interval_s,
                min_move_nm=settings.min_move_nm,
                enabled=settings.simconnect_enabled,
            )
            self.connector.position_changed.connect(self._on_position_changed)
            self.connector.position_polled.connect(self._on_position_polled)
            self.connector.status_message.connect(self.sim_status)

        self._current_notams:  list[Notam]      = []
        self._current_actions: list[MsfsAction] = []

        # Last known aircraft position (set from SimConnect thread, read in asyncio loop)
        self._last_lat: float = 0.0
        self._last_lon: float = 0.0
        self._fetch_in_progress: bool = False
        self._pending_fetch: Optional[tuple[float, float, bool]] = None
        self._dial_cycle_origin: Optional[tuple[float, float]] = None
        self._dial_restart_pending: bool = False

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="scheduler-loop"
        )
        self._loop_thread.start()
        asyncio.run_coroutine_threadsafe(self._init_async(), self._loop)
        if not self.mock_position:
            self.connector.start()
        else:
            # EHVK (Volkel Air Base): 51.6561° N, 5.7074° E
            lat, lon, alt = 51.6561, 5.7074, 108.0
            self._last_lat = lat
            self._last_lon = lon
            self.position_updated.emit(lat, lon, alt)
            asyncio.run_coroutine_threadsafe(
                self._fetch_and_apply(lat, lon), self._loop
            )
        self.poll_progress.emit(0)
        logger.info("Scheduler started.")

    def stop(self) -> None:
        if not self.mock_position:
            self.connector.stop()
        sm = getattr(getattr(self, "connector", None), "_sm", None)
        self.object_placer.remove_all(sm)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("Scheduler stopped.")

    # ── Qt slot (called from SimConnect thread) ────────────────────────────────

    def _on_position_polled(self, lat: float, lon: float, alt: float, heading_deg: float) -> None:
        """Update movement dial: 0% at origin, 100% at min_move_nm, then restart cycle."""
        self.position_sampled.emit(lat, lon, heading_deg)

        if self._dial_cycle_origin is None:
            self._dial_cycle_origin = (lat, lon)
            self.poll_progress.emit(0)
            return

        if self._dial_restart_pending:
            self._dial_cycle_origin = (lat, lon)
            self._dial_restart_pending = False
            self.poll_progress.emit(0)
            return

        min_move = max(settings.min_move_nm, 0.01)
        moved_nm = _haversine_nm(self._dial_cycle_origin[0], self._dial_cycle_origin[1], lat, lon)
        progress = min(100, int((moved_nm / min_move) * 100))
        self.poll_progress.emit(progress)

        if progress >= 100:
            # Keep 100% visible until the next polled position starts a new cycle.
            self._dial_restart_pending = True

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

        # Boot OpenAIP enrichment: check AIRAC age, sync stale countries in background
        self.openaip = OpenAIPFetcher(self.cache.connection)
        await self.openaip.ensure_fresh(settings.openaip_countries)

        # Enrich in-memory airports with whatever is already in the DB
        await self._enrich_airports()

        self._ready.set()
        # Start the periodic NOTAM refresh timer
        self._refresh_task = asyncio.create_task(self._periodic_refresh())

    async def _enrich_airports(self) -> None:
        """Load OpenAIP enrichment from SQLite and apply to AirportLookup."""
        if self.openaip is None:
            return
        try:
            # Fetch all stored ICAO codes from the enrichment table
            cur = await self.cache.connection.execute(
                "SELECT icao FROM openaip_airports"
            )
            rows = await cur.fetchall()
            icao_codes = [r["icao"] for r in rows]
            if not icao_codes:
                return
            enrichments = await self.openaip.get_enrichments_bulk(icao_codes)
            self.airport_lookup.enrich_from_openaip(enrichments)
        except Exception as exc:
            logger.warning(f"[openaip] Enrichment apply failed: {exc}")

    async def _periodic_refresh(self) -> None:
        """Periodic pipeline run; fetches only newly encountered ICAOs."""
        interval_s = settings.notam_refresh_interval_min * 60
        while True:
            await asyncio.sleep(interval_s)
            if self._last_lat == 0.0 and self._last_lon == 0.0:
                continue   # no position yet
            logger.info(
                f"[scheduler] Periodic NOTAM refresh "
                f"(every {settings.notam_refresh_interval_min} min)"
            )
            await self._fetch_and_apply(self._last_lat, self._last_lon, force_fetch=False)

    async def _fetch_and_apply(self, lat: float, lon: float, force_fetch: bool = False) -> None:
        """Main pipeline: position → airports → NOTAMs → classify → MSFS."""
        if self._fetch_in_progress:
            # Coalesce bursts of position updates while a fetch is running.
            self._pending_fetch = (lat, lon, force_fetch)
            return

        self._fetch_in_progress = True
        try:
            await self._fetch_and_apply_inner(lat, lon, force_fetch=force_fetch)
        finally:
            self._fetch_in_progress = False

            pending = self._pending_fetch
            self._pending_fetch = None
            if pending is not None:
                p_lat, p_lon, p_force = pending
                await self._fetch_and_apply(p_lat, p_lon, force_fetch=p_force)

    async def _fetch_and_apply_inner(self, lat: float, lon: float, force_fetch: bool = False) -> None:
        """Inner implementation for one fetch/apply cycle."""
        await self._ready.wait()
        if not self.airport_lookup.loaded:
            logger.warning("Airport database not loaded — skipping NOTAM fetch.")
            return


        icao_codes = self.airport_lookup.icao_codes_within(lat, lon, settings.notam_radius_nm)
        if not icao_codes:
            logger.debug(f"No airports within {settings.notam_radius_nm} nm.")
            return

        if force_fetch:
            fetch_icaos = icao_codes
        else:
            fetch_icaos = await self.cache.get_stale_icaos(
                icao_codes,
                max_age_minutes=settings.notam_cache_ttl_min,
            )

        notams: list[Notam]
        if fetch_icaos:
            logger.info(
                f"Fetching NOTAMs for {len(fetch_icaos)}/{len(icao_codes)} airports "
                f"near {lat:.3f},{lon:.3f}"
            )
            total = len(fetch_icaos)
            self.fetch_progress.emit(0, total)

            def _on_progress(done: int, tot: int) -> None:
                self.fetch_progress.emit(done, tot)

            raw_texts = await self.fetcher.fetch_all(fetch_icaos, progress_cb=_on_progress)
            self.fetch_progress.emit(total, total)
            notams = parse_notams(raw_texts)
            await self.cache.mark_airports_fetched(fetch_icaos)
        else:
            logger.debug(
                f"[scheduler] Skipping network fetch (all nearby airports are fresh in cache "
                f"for {settings.notam_cache_ttl_min} min); reusing cache"
            )
            notams = []

        await self.cache.upsert_notams(notams)
        await self.cache.purge_expired(settings.max_notam_age_h)

        # Merge fresh results with still-valid cached NOTAMs (fallback for failed airports)
        fresh_ids = {n.id for n in notams}
        cached    = await self.cache.get_active_notams_for_icaos(icao_codes)
        cached_extra = [n for n in cached if n.id not in fresh_ids]
        if cached_extra:
            logger.info(
                f"[scheduler] Using {len(cached_extra)} cached NOTAMs "
                f"for airports not reached this cycle"
            )
        active_notams = [n for n in (notams + cached_extra) if n.is_active]
        self._current_notams = active_notams
        self.notams_updated.emit(active_notams)

        # Classify and apply FIRST so applied flags are accurate when the overlay titles are built.
        actions = classify_all(active_notams)
        self._current_actions = actions
        logger.info(f"[scheduler] Classified {len(actions)} MSFS action(s)")
        self.actions_updated.emit(actions)

        if settings.auto_apply_notams:
            try:
                await self._apply_actions(actions, aircraft_lat=lat, aircraft_lon=lon)
            except Exception as exc:
                logger.warning(f"[scheduler] _apply_actions failed: {exc}")

        # Emit again so UI reflects applied/error flags updated during _apply_actions.
        self.actions_updated.emit(actions)

        # Flush WASM state file with combined navaid + ATIS override snapshot.
        sm = getattr(getattr(self, "connector", None), "_sm", None)
        try:
            wasm_state.flush(
                self.navaid_ctrl.wasm_payload(),
                self.atis_ctrl.wasm_payload(),
            )
        except Exception as exc:
            logger.warning(f"[scheduler] WASM state flush failed: {exc}")

        # COM radio monitoring — alert if pilot has tuned a U/S ATIS frequency.
        try:
            icao_codes = self.airport_lookup.icao_codes_within(lat, lon, settings.notam_radius_nm)
            for alert_text, icao, notam_id in self.atis_ctrl.check_com_tuning(sm, icao_codes):
                logger.warning(f"[atis] COM tuning alert: {alert_text}")
                self.notifier.queue_notam(f"COM-ATIS-{notam_id}", icao, alert_text)
        except Exception as exc:
            logger.debug(f"[scheduler] COM tuning check failed: {exc}")

        # Notify AFTER apply so the injection status badge is accurate.
        if settings.notam_alert_enabled:
            try:
                self._notify_approaching_notams(active_notams, lat, lon, sm)
            except Exception as exc:
                logger.warning(f"[scheduler] NOTAM notifier failed: {exc}")

    async def _apply_actions(
        self,
        actions: list[MsfsAction],
        aircraft_lat: float,
        aircraft_lon: float,
    ) -> None:
        sm = getattr(getattr(self, "connector", None), "_sm", None)

        # Flush queued NOTAM popups again once SimConnect is definitely available
        # in the apply phase (startup timing can queue alerts before sm is ready).
        if settings.notam_alert_enabled:
            self.notifier.pump(sm)

        # Drive the SimConnect pump (resolves object IDs + lighting writes)
        self.object_placer.pump_dispatch(sm)

        # ── Obstacle lifecycle ─────────────────────────────────────────────────
        # Collect the set of active obstacle notam_ids that are close enough
        wanted: set[str] = set()
        for action in actions:
            if action.action_type == "place_obstacle":
                p = action.params
                if not (p.get("lat") and p.get("lon")):
                    continue
                dist_nm = _haversine_nm(aircraft_lat, aircraft_lon, p["lat"], p["lon"])
                if dist_nm <= settings.obstacle_placement_radius_nm:
                    wanted.add(action.notam_id)
                    if settings.highlight_obstacle_objects:
                        for _i in range(settings.highlight_beacon_count):
                            wanted.add(f"{action.notam_id}-hl-{_i}")
            elif action.action_type == "close_runway":
                # Runway barriers: resolve coordinates from runway DB
                rwy_desig = action.params.get("runway_designator", "")
                rwy = self.runway_lookup.find_runway(action.icao, rwy_desig) if rwy_desig else None
                if rwy is None and rwy_desig:
                    runways = self.runway_lookup.runways_for(action.icao)
                    if len(runways) == 1:
                        rwy = runways[0]
                if rwy is not None:
                    mid_lat = (rwy.le_lat + rwy.he_lat) / 2
                    mid_lon = (rwy.le_lon + rwy.he_lon) / 2
                    dist_nm = _haversine_nm(aircraft_lat, aircraft_lon, mid_lat, mid_lon)
                    if dist_nm <= settings.obstacle_placement_radius_nm:
                        wanted.add(action.notam_id)
                        for end in ("le", "he"):
                            wanted.add(f"{action.notam_id}-rwybar-{end}")
                            wanted.add(f"{action.notam_id}-rwyx-{end}")

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
                        obstacle_kind = str(p.get("obstacle_kind", "generic_obstacle"))
                        entry = self.object_placer.entry_for_kind(obstacle_kind)

                        placed = self.object_placer.place(
                            sm,
                            notam_id=action.notam_id,
                            lat=p["lat"],
                            lon=p["lon"],
                            alt_ft=p.get("upper_ft", 0),
                            lit=bool(p.get("lit", True)),
                            obstacle_kind=obstacle_kind,
                            titles_to_try=entry.titles,
                        )
                        if settings.highlight_obstacle_objects:
                            self.object_placer.highlight_column(
                                sm,
                                notam_id=action.notam_id,
                                lat=p["lat"],
                                lon=p["lon"],
                                base_alt_ft=settings.highlight_beacon_base_ft,
                                step_ft=settings.highlight_beacon_step_ft,
                                count=settings.highlight_beacon_count,
                                lit=bool(p.get("lit", True)),
                            )

                        if placed:
                            logger.info(
                                f"[scheduler] OBSTACLE PLACED: {action.icao} — "
                                f"{p.get('description','')[:80]} "
                                f"at {p.get('lat','?'):.4f},{p.get('lon','?'):.4f}"
                            )
                            action.applied = True
                        else:
                            logger.warning(
                                f"[scheduler] OBSTACLE ALERT: {action.icao} — "
                                f"{p.get('description','')[:80]} — "
                                f"Failed to place SimObject. Debug marker requested."
                            )
                            action.applied = False

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
                            component=action.params.get("component", "full"),
                        )
                        action.applied = ok

                    case "close_runway":
                        p = action.params
                        rwy_desig = p.get("runway_designator", "")
                        rwy = self.runway_lookup.find_runway(action.icao, rwy_desig) if rwy_desig else None

                        if rwy is None and rwy_desig:
                            # Try all runways for the airport
                            runways = self.runway_lookup.runways_for(action.icao)
                            if len(runways) == 1:
                                rwy = runways[0]

                        if rwy is None:
                            logger.warning(
                                f"[scheduler] RUNWAY CLOSURE: {action.icao} RWY {rwy_desig} — "
                                f"no runway coordinates found; cannot place barriers. "
                                f"{p.get('description','')[:80]}"
                            )
                            action.applied = False
                        else:
                            placed_count = self._place_runway_barriers(
                                sm, action.notam_id, rwy, rwy_desig,
                            )
                            if placed_count > 0:
                                logger.info(
                                    f"[scheduler] RUNWAY CLOSED: {action.icao} RWY {rwy_desig} — "
                                    f"{placed_count} barrier(s) placed. "
                                    f"{p.get('description','')[:60]}"
                                )
                                action.applied = True
                            else:
                                logger.warning(
                                    f"[scheduler] RUNWAY CLOSURE: {action.icao} RWY {rwy_desig} — "
                                    f"barrier placement failed. {p.get('description','')[:60]}"
                                )
                                action.applied = False

                    case "atis_unserviceable":
                        self.atis_ctrl.disable(
                            notam_id=action.notam_id,
                            icao=action.icao,
                            frequency_mhz=action.params.get("frequency_mhz"),
                        )
                        action.applied = True   # state tracked; WASM needed for dead-air

                    case "close_taxiway":
                        twy = action.params.get("taxiway_designator", "?")
                        logger.warning(
                            f"[scheduler] TAXIWAY CLOSED: {action.icao} TWY {twy} — "
                            f"{action.params.get('description','')[:80]}"
                        )
                        action.applied = False   # TODO: barrier placement (needs taxiway coords)

                    case "close_stand":
                        stand = action.params.get("stand_designator", "?")
                        logger.warning(
                            f"[scheduler] STAND CLOSED: {action.icao} Stand {stand} — "
                            f"{action.params.get('description','')[:80]}"
                        )
                        action.applied = False   # TODO: barrier placement (needs stand coords)

                    case "runway_limited":
                        rwy = action.params.get("runway_designator", "?")
                        logger.warning(
                            f"[scheduler] RUNWAY LIMITED: {action.icao} RWY {rwy} — "
                            f"{action.params.get('description','')[:80]}"
                        )
                        action.applied = False   # informational; no sim enforcement yet

                    case "fuel_unavailable":
                        fuel = action.params.get("fuel_type") or "unspecified"
                        logger.warning(
                            f"[scheduler] FUEL UNAVAILABLE: {action.icao} {fuel} — "
                            f"{action.params.get('description','')[:80]}"
                        )
                        action.applied = False   # informational; no sim equivalent

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
                logger.warning(f"[scheduler] Action {action.action_type} error: {exc}")

    def _place_runway_barriers(
        self,
        sm: object,
        notam_id: str,
        rwy: "RunwayLookup.Runway",
        rwy_desig: str,
    ) -> int:
        """
        Place 3 barrier objects at each runway threshold (LE and HE), inset
        ~30 m inside the outer end and spread perpendicular to the centreline.
        Returns the total number of objects placed (up to 6).
        """
        import math
        import random

        heading = rwy.le_heading or 0.0
        INSET_M = random.uniform(20, 60)  # metres inside from the threshold tip

        barrier_entry = self.object_placer.entry_for_kind("runway_closure")
        placed = 0

        ends = [
            ("le", rwy.le_lat, rwy.le_lon, math.radians(heading)),
            ("he", rwy.he_lat, rwy.he_lon, math.radians((heading + 180) % 360)),
        ]

        for end_tag, end_lat, end_lon, along_rad in ends:
            # Step inward along the centreline from this threshold
            anchor_lat = end_lat + (INSET_M * math.cos(along_rad)) / 111139.0
            anchor_lon = end_lon + (INSET_M * math.sin(along_rad)) / (
                111139.0 * math.cos(math.radians(end_lat))
            )

            # 3D barrier object (wind turbine placeholder)
            ok = self.object_placer.place(
                sm,
                notam_id=f"{notam_id}-rwybar-{end_tag}",
                lat=anchor_lat,
                lon=anchor_lon,
                alt_ft=0,
                lit=True,
                on_ground=True,
                heading=heading,
                obstacle_kind="runway_closure",
                titles_to_try=barrier_entry.titles,
            )
            if ok:
                placed += 1

        return placed

    def _format_notam_valid_until(self, notam: Notam) -> str:
        """Return validity text in UTC for overlay display."""
        if notam.valid_to is None:
            return "until PERM"
        dt_utc = notam.valid_to.astimezone(timezone.utc)
        return f"until {dt_utc.strftime('%d.%m.%Y %H:%M')} Z"

    def _format_overlay_title(self, notam: Notam, badge: str) -> str:
        """Build a user-friendly title, e.g. 'EDFE — Aerodrome Restricted (A1234/26)'."""
        subject_label = notam.subject.name.replace("_", " ").title()
        cond_label = notam.condition.name.replace("_", " ").title()
        return f"{subject_label} {cond_label} ({notam.id}){badge}"

    def _notify_approaching_notams(
        self,
        notams: list[Notam],
        aircraft_lat: float,
        aircraft_lon: float,
        sm: object,
    ) -> None:
        """
        Queue in-sim NOTAM popup notifications for active NOTAMs whose location
        is within ``settings.notam_alert_radius_nm`` of the aircraft.
        """
        radius = settings.notam_alert_radius_nm

        candidates: list[tuple[float, Notam]] = []
        for n in notams:
            if not n.is_active:
                continue
            lat = n.lat
            lon = n.lon
            if lat is None or lon is None:
                ap = self.airport_lookup.find(n.icao)
                if ap is None:
                    continue
                lat, lon = ap.lat, ap.lon

            try:
                lat = float(lat)
                lon = float(lon)
            except (TypeError, ValueError):
                continue

            dist_nm = _haversine_nm(aircraft_lat, aircraft_lon, lat, lon)
            if dist_nm <= radius:
                candidates.append((dist_nm, n))

        # Show nearest alerts first.
        candidates.sort(key=lambda item: item[0])
        logger.debug(
            f"[scheduler] NOTAM alert candidates: {len(candidates)} "
            f"within {radius:.1f}nm (sm={'yes' if sm is not None else 'no'})"
        )

        if not candidates:
            # Keep last shown alert view instead of pushing an empty frame.
            self.notifier.pump(sm)
            return
        # Build a lookup of notam_id → action (for injection status badge)
        action_by_id: dict[str, MsfsAction] = {a.notam_id: a for a in self._current_actions}

        overlay_payload: list[tuple[str, str, float, str, str, str, bool]] = []
        pop_up = False

        for dist_nm, n in candidates:
            desc = (n.description or n.raw).strip()[:200]
            valid_until = self._format_notam_valid_until(n)
            text = (
                f"{valid_until}\n[{n.subject.name}] {desc}"
                if desc
                else f"{valid_until}\n[{n.subject.name}] NOTAM {n.id}"
            )
            queued = self.notifier.queue_notam(n.id, n.icao, text)

            action = action_by_id.get(n.id)
            if action is None:
                badge = ""                         # informational NOTAM, no sim action
            elif action.applied:
                badge = "  ✓ injected"
            else:
                badge = "  ⚠ not in sim"
            title = self._format_overlay_title(n, badge)
            ap = self.airport_lookup.find(n.icao)
            airport_name = ap.name if ap else ""
            overlay_payload.append(
                (
                    title,
                    text,
                    dist_nm,
                    n.icao,
                    airport_name,
                    n.subject.name,
                    queued,
                )
            )
            pop_up = pop_up or queued

        self.alert_overlay_batch.emit(overlay_payload, pop_up)

        self.notifier.pump(sm)

    # ── UI accessors ───────────────────────────────────────────────────────────

    @property
    def current_notams(self) -> list[Notam]:
        return self._current_notams

    @property
    def current_actions(self) -> list[MsfsAction]:
        return self._current_actions
