"""
SimConnect wrapper with automatic mock fallback for non-Windows / no-sim environments.

Position updates are emitted as Qt signals so the rest of the app
doesn't need to know anything about threading.
"""

from __future__ import annotations

import platform
import threading
import time
from typing import Callable, Optional

from loguru import logger
from PySide6.QtCore import QObject, Signal

# Attempt to import SimConnect (Windows + MSFS must be running)
_SIMCONNECT_AVAILABLE = False
try:
    from SimConnect import SimConnect, AircraftRequests  # type: ignore
    _SIMCONNECT_AVAILABLE = True
except Exception:
    pass


class SimConnectWrapper(QObject):
    """
    Polls SimConnect for aircraft position and emits signals.

    Runs its poll loop in a background daemon thread.
    Falls back to a mock (fixed position) if SimConnect is not available.
    """

    position_changed = Signal(float, float, float)   # lat, lon, alt_ft
    position_polled  = Signal(float, float, float, float)  # lat, lon, alt_ft, heading_deg
    connected       = Signal()
    disconnected    = Signal()
    status_message  = Signal(str)

    def __init__(
        self,
        poll_interval_s: int = 30,
        min_move_nm: float = 5.0,
        enabled: bool = True,
    ) -> None:
        super().__init__()
        self.poll_interval_s = poll_interval_s
        self.min_move_nm = min_move_nm
        self.enabled = enabled

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_lat: Optional[float] = None
        self._last_lon: Optional[float] = None
        self._is_connected = False

        # Will be initialised in the thread
        self._sm: Optional[object] = None
        self._aq: Optional[object] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="simconnect-poll"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    # ── Background thread ─────────────────────────────────────────────────────

    def _run(self) -> None:
        if not self.enabled:
            logger.info("SimConnect disabled in config — running in mock mode.")
            self._run_mock()
            return

        if not _SIMCONNECT_AVAILABLE:
            logger.warning(
                "SimConnect library not available (non-Windows or not installed). "
                "Running in mock mode."
            )
            self._run_mock()
            return

        self._run_live()

    def _run_live(self) -> None:
        """Real SimConnect polling loop."""
        self.status_message.emit("Waiting for MSFS / SimConnect")
        while not self._stop_event.is_set():
            try:
                if not self._is_connected:
                    logger.info("Connecting to SimConnect…")
                    self._sm = SimConnect()
                    self._aq = AircraftRequests(self._sm, _time=2000)
                    self._is_connected = True
                    self.connected.emit()
                    self.status_message.emit("SimConnect connected")
                    logger.info("SimConnect connected.")

                lat = self._aq.get("PLANE_LATITUDE")
                lon = self._aq.get("PLANE_LONGITUDE")
                alt = self._aq.get("PLANE_ALTITUDE")
                heading = self._aq.get("PLANE_HEADING_DEGREES_TRUE")

                if lat is not None and lon is not None:
                    heading_deg = self._normalize_heading_deg(heading)
                    self._maybe_emit(float(lat), float(lon), float(alt or 0), heading_deg)

            except Exception as exc:
                if self._is_connected:
                    logger.warning(f"SimConnect lost: {exc}")
                    self.disconnected.emit()
                    self.status_message.emit(f"SimConnect disconnected: {exc}")
                    self._is_connected = False
                    self._sm = None
                    self._aq = None
                else:
                    logger.debug(f"SimConnect not available yet: {exc}")
                    self.status_message.emit("Waiting for MSFS / SimConnect")

            wait_s = self.poll_interval_s if self._is_connected else min(5, self.poll_interval_s)
            self._stop_event.wait(wait_s)

    def _run_mock(self) -> None:
        """
        Mock loop — emits a fixed position (EGLL / London Heathrow) so the
        rest of the application can be developed and tested without MSFS.
        """
        # EDDF (Frankfurt): 50.0379, 8.5622, approx 100ft alt
        MOCK_LAT, MOCK_LON, MOCK_ALT = 50.0379, 8.5622, 100.0
        self._is_connected = True
        self.connected.emit()
        self.status_message.emit("SimConnect unavailable — running mock mode")
        logger.info(f"Mock SimConnect: fixed at {MOCK_LAT}, {MOCK_LON}")

        while not self._stop_event.is_set():
            self._maybe_emit(MOCK_LAT, MOCK_LON, MOCK_ALT, 0.0)
            self._stop_event.wait(self.poll_interval_s)

    def _normalize_heading_deg(self, heading_value: object) -> float:
        """Normalize heading from SimConnect to degrees in [0, 360)."""
        try:
            h = float(heading_value)
        except (TypeError, ValueError):
            return 0.0

        # SimConnect heading vars are often radians; convert when value range indicates radians.
        if -6.5 <= h <= 6.5:
            h = h * 57.29577951308232
        return h % 360.0

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_placeholder_position(self, lat: float, lon: float) -> bool:
        """Filter known default/non-flight coordinates reported before a flight is loaded."""
        if abs(lat) < 0.01 and abs(lon) < 0.01:
            return True
        if abs(lat) < 0.01 and abs(lon - 90.0) < 0.5:
            return True
        return False

    def _maybe_emit(self, lat: float, lon: float, alt: float, heading_deg: float) -> None:
        """Only emit position_changed when the aircraft has moved enough."""
        from src.airports.lookup import _haversine_nm  # lazy import

        if self._is_placeholder_position(lat, lon):
            logger.debug(
                f"Ignoring placeholder SimConnect position lat={lat:.4f}, lon={lon:.4f}"
            )
            return

        self.position_polled.emit(lat, lon, alt, heading_deg)

        if (
            self._last_lat is None
            or _haversine_nm(self._last_lat, self._last_lon, lat, lon) >= self.min_move_nm
        ):
            self._last_lat = lat
            self._last_lon = lon
            self.position_changed.emit(lat, lon, alt)
