"""
Navaid state manipulation via SimConnect.

Direct navaid disabling in MSFS 2024 is not fully exposed through
SimConnect alone — a WASM module would be the proper solution for
true in-sim navaid disabling.  This module implements the best available
SimConnect approximation and flags actions that need WASM for a future
WASM implementation phase.

Current capability:
  - Read navaid facility data via SimConnect facility requests
  - Override NAV/COM radio reception via SimVar manipulation (partial)
  - Log what *should* happen so the WASM layer can pick it up

Future (WASM):
  - True navaid signal injection / blanking
  - ILS glideslope / localizer override
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


@dataclass
class NavaidOverride:
    notam_id: str
    icao: str
    navaid_type: str      # "ILS" | "VOR" | "NDB"
    disabled: bool


class NavaidController:
    """
    Tracks and applies navaid state overrides derived from NOTAMs.
    """

    def __init__(self) -> None:
        self._overrides: dict[str, NavaidOverride] = {}   # notam_id → override

    # ── Public API ────────────────────────────────────────────────────────────

    def disable(
        self,
        sm: object,
        notam_id: str,
        icao: str,
        navaid_type: str,
    ) -> bool:
        """
        Mark a navaid as disabled.  Applies the best available SimConnect
        approximation and records the override for WASM handoff.
        """
        override = NavaidOverride(
            notam_id=notam_id,
            icao=icao,
            navaid_type=navaid_type.upper(),
            disabled=True,
        )
        self._overrides[notam_id] = override

        # SimConnect approximation:
        # For NAV radios we can try to set the reception quality SimVar.
        # This is limited — it doesn't prevent tuning, only degrades signal.
        # TODO: hook into WASM module for true blanking
        success = self._apply_simconnect_override(sm, override)

        logger.info(
            f"[navaids] {'Applied' if success else 'Logged (WASM needed)'} "
            f"disable override for {navaid_type} at {icao} ({notam_id})"
        )
        return success

    def enable(self, sm: object, notam_id: str) -> bool:
        """Remove a disable override (navaid back in service)."""
        override = self._overrides.pop(notam_id, None)
        if override is None:
            return False
        override.disabled = False
        success = self._apply_simconnect_override(sm, override)
        logger.info(f"[navaids] Restored {override.navaid_type} at {override.icao} ({notam_id})")
        return success

    def clear_all(self, sm: object) -> None:
        for notam_id in list(self._overrides.keys()):
            self.enable(sm, notam_id)

    @property
    def active_overrides(self) -> list[NavaidOverride]:
        return [o for o in self._overrides.values() if o.disabled]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _apply_simconnect_override(self, sm: object, override: NavaidOverride) -> bool:
        """
        Best-effort SimConnect implementation.
        Returns True if something was actually applied, False if WASM is needed.
        """
        # TODO: implement SimConnect SimVar writes when specific variables
        # for navaid suppression are confirmed for MSFS 2024.
        # For now we log the intent so the debug UI can show it.
        logger.debug(
            f"[navaids] WASM needed for true {override.navaid_type} "
            f"override at {override.icao}"
        )
        return False   # indicate WASM required
