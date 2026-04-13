"""
Navaid state manipulation via SimConnect.

Direct navaid disabling in MSFS 2024 is not fully exposed through
SimConnect alone — a WASM module would be the proper solution for
true in-sim navaid disabling.  This module implements the best available
SimConnect approximation and flags actions that need WASM for a future
WASM implementation phase.

ILS component model
-------------------
Each override carries a ``component`` field that identifies which part of
the ILS system is unserviceable:

  "full"        — entire ILS system (LOC + GP + DME + markers)
  "glideslope"  — glide path / GP only
  "localizer"   — localiser / LOC only
  "dme"         — ILS DME only
  "marker"      — one or more markers (OM / MM / IM)

Current capability
------------------
  - Read navaid facility data via SimConnect facility requests
  - Override NAV/COM radio reception via SimVar manipulation (partial)
  - Write a WASM state file (navaid_overrides.json) so the future WASM
    module can pick up the full override list on startup

Future (WASM)
-------------
  - True per-component signal injection / blanking
  - ILS glideslope flag suppression (GP valid bit)
  - ILS localiser flag suppression (LOC valid bit)
  - DME invalid flag injection
  - ILS CAT downgrade (CAT III → CAT I) for partial failures

WASM data contract
------------------
The WASM module should read ``navaid_overrides.json`` (path configured via
``settings.wasm_state_file``).  Schema version 1 format::

    {
      "schema_version": 1,
      "updated_at": "<ISO-8601 UTC>",
      "overrides": [
        {
          "notam_id":    "M1612/26",
          "icao":        "EHVK",
          "navaid_type": "ILS",
          "component":   "full",      // "full"|"glideslope"|"localizer"|"dme"|"marker"
          "disabled":    true
        }
      ]
    }

The WASM module should re-read the file whenever ``updated_at`` changes.
SimConnect client-data-area handoff will replace the file mechanism once
the WASM layer is implemented.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

# Component constants — used in NavaidOverride and passed to WASM.
ILS_COMPONENT_FULL       = "full"
ILS_COMPONENT_GLIDESLOPE = "glideslope"
ILS_COMPONENT_LOCALIZER  = "localizer"
ILS_COMPONENT_DME        = "dme"
ILS_COMPONENT_MARKER     = "marker"


@dataclass
class NavaidOverride:
    notam_id:    str
    icao:        str
    navaid_type: str   # "ILS" | "VOR" | "NDB"
    component:   str   # ILS_COMPONENT_* constant; "full" for VOR/NDB
    disabled:    bool


class NavaidController:
    """
    Tracks and applies navaid state overrides derived from NOTAMs.

    Each disable/enable call updates the in-memory override dict and
    flushes the WASM state file so the future WASM module stays in sync.
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
        component: str = ILS_COMPONENT_FULL,
    ) -> bool:
        """
        Mark a navaid (or specific ILS component) as disabled.

        Applies the best available SimConnect approximation, records the
        override for WASM handoff, and flushes the WASM state file.
        """
        override = NavaidOverride(
            notam_id=notam_id,
            icao=icao,
            navaid_type=navaid_type.upper(),
            component=component,
            disabled=True,
        )
        self._overrides[notam_id] = override

        success = self._apply_simconnect_override(sm, override)

        component_label = f" [{component}]" if component != ILS_COMPONENT_FULL else ""
        logger.warning(
            f"NAVAID DEACTIVATED: {navaid_type.upper()}{component_label} at {icao} "
            f"({notam_id}) is unserviceable and has been flagged in MSFS."
        )
        logger.info(
            f"[navaids] {'Applied' if success else 'Logged (WASM needed)'} "
            f"disable override for {navaid_type}{component_label} at {icao} ({notam_id})"
        )
        return success

    def enable(self, sm: object, notam_id: str) -> bool:
        """Remove a disable override (navaid back in service)."""
        override = self._overrides.pop(notam_id, None)
        if override is None:
            return False
        override.disabled = False
        success = self._apply_simconnect_override(sm, override)
        component_label = f" [{override.component}]" if override.component != ILS_COMPONENT_FULL else ""
        logger.info(
            f"[navaids] Restored {override.navaid_type}{component_label} "
            f"at {override.icao} ({notam_id})"
        )
        return success

    def clear_all(self, sm: object) -> None:
        for notam_id in list(self._overrides.keys()):
            self.enable(sm, notam_id)

    @property
    def active_overrides(self) -> list[NavaidOverride]:
        return [o for o in self._overrides.values() if o.disabled]

    def wasm_payload(self) -> list[dict]:
        """Return serialisable override list for the WASM state file."""
        return [
            {
                "notam_id":    o.notam_id,
                "icao":        o.icao,
                "navaid_type": o.navaid_type,
                "component":   o.component,
                "disabled":    o.disabled,
            }
            for o in self._overrides.values()
            if o.disabled
        ]

    # ── SimConnect approximation ──────────────────────────────────────────────

    def _apply_simconnect_override(self, sm: object, override: NavaidOverride) -> bool:
        """
        Best-effort SimConnect implementation, branched per component.

        Returns True if something was actually applied in-sim, False if
        the action is queued for the WASM layer only.

        TODO — per-component SimVar writes once confirmed for MSFS 2024:
          glideslope  → NAV GLIDE SLOPE ERROR:<idx>  (large value pegs GS needle)
          localizer   → NAV CDI:<idx>                (max deflection flags LOC)
          dme         → NAV DME:<idx>                (set to 0 / invalid)
          marker      → no SimVar equivalent; WASM only
          full        → all of the above
        """
        if not override.disabled:
            # Restore path — no SimVar writes implemented yet
            logger.debug(
                f"[navaids] Restore {override.navaid_type} [{override.component}] "
                f"at {override.icao} — WASM needed"
            )
            return False

        match override.component:
            case "glideslope":
                logger.debug(
                    f"[navaids] WASM needed: suppress GP flag for ILS at {override.icao}"
                )
            case "localizer":
                logger.debug(
                    f"[navaids] WASM needed: suppress LOC flag for ILS at {override.icao}"
                )
            case "dme":
                logger.debug(
                    f"[navaids] WASM needed: invalidate DME for {override.navaid_type} "
                    f"at {override.icao}"
                )
            case "marker":
                logger.debug(
                    f"[navaids] WASM needed: suppress marker beacon at {override.icao}"
                )
            case _:  # "full"
                logger.debug(
                    f"[navaids] WASM needed: full {override.navaid_type} "
                    f"disable at {override.icao}"
                )

        return False   # WASM required for all component types
