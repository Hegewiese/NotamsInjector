"""
ATIS frequency state tracking.

Maintains a registry of ATIS frequencies that are unserviceable based on
active NOTAMs, and provides COM radio monitoring so the pilot is alerted
the moment they tune a dead ATIS frequency.

In-sim effects
--------------
Now (SimConnect):
  - Alert notification when approaching an airport with a U/S ATIS
  - Active cockpit warning when COM1 or COM2 is tuned to the U/S frequency

Future (WASM):
  - Suppress squelch / inject static on the U/S ATIS frequency so the
    pilot hears dead air instead of the auto-generated MSFS ATIS
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger


@dataclass
class AtisOverride:
    notam_id:      str
    icao:          str
    frequency_mhz: Optional[float]   # None if frequency not parsed from NOTAM
    disabled:      bool


class AtisController:
    """
    Tracks ATIS U/S overrides and monitors COM radio tuning.
    """

    def __init__(self) -> None:
        self._overrides: dict[str, AtisOverride] = {}   # notam_id → override
        # Debounce: keys of overrides that have already triggered a COM alert
        # in the current tuning session.  Cleared when the frequency is untuned.
        self._alerted_keys: set[str] = set()

    # ── Public API ────────────────────────────────────────────────────────────

    def disable(self, notam_id: str, icao: str, frequency_mhz: Optional[float]) -> None:
        """Register an ATIS frequency as unserviceable."""
        self._overrides[notam_id] = AtisOverride(
            notam_id=notam_id,
            icao=icao,
            frequency_mhz=frequency_mhz,
            disabled=True,
        )
        freq_label = f" {frequency_mhz:.3f} MHz" if frequency_mhz else ""
        logger.warning(
            f"ATIS UNSERVICEABLE: ATIS{freq_label} at {icao} ({notam_id}) is out of service."
        )
        logger.info(f"[atis] Registered U/S override for ATIS at {icao} ({notam_id})")

    def enable(self, notam_id: str) -> None:
        """Remove a U/S override (ATIS back in service)."""
        override = self._overrides.pop(notam_id, None)
        if override is None:
            return
        # Clear any debounce state for this override
        self._alerted_keys.discard(self._override_key(override))
        freq_label = f" {override.frequency_mhz:.3f} MHz" if override.frequency_mhz else ""
        logger.info(f"[atis] Restored ATIS{freq_label} at {override.icao} ({notam_id})")

    def clear_all(self) -> None:
        for notam_id in list(self._overrides.keys()):
            self.enable(notam_id)

    @property
    def active_overrides(self) -> list[AtisOverride]:
        return [o for o in self._overrides.values() if o.disabled]

    def wasm_payload(self) -> list[dict]:
        """Return serialisable override list for the WASM state file."""
        return [
            {
                "notam_id":      o.notam_id,
                "icao":          o.icao,
                "frequency_mhz": o.frequency_mhz,
                "disabled":      o.disabled,
            }
            for o in self._overrides.values()
            if o.disabled
        ]

    # ── COM radio monitoring ──────────────────────────────────────────────────

    def check_com_tuning(
        self,
        sm: object,
        nearby_icaos: list[str],
    ) -> list[tuple[str, str, str]]:
        """
        Read COM1/COM2 active frequencies via SimConnect and return a list of
        ``(alert_text, icao, notam_id)`` tuples for any U/S ATIS frequency
        currently tuned.

        Uses edge-detection: alerts once when the pilot tunes onto the dead
        frequency, clears when they tune away so they are alerted again if
        they return.

        Returns [] when SimConnect is unavailable (mock / disconnected).

        TODO (WASM): replace alert with injected dead-air squelch.
        """
        if sm is None:
            return []

        com_freqs = self._read_com_frequencies(sm)
        if not com_freqs:
            return []

        currently_tuned_keys: set[str] = set()
        alerts: list[tuple[str, str, str]] = []

        for override in self._overrides.values():
            if not override.disabled or override.frequency_mhz is None:
                continue
            if override.icao not in nearby_icaos:
                continue

            key = self._override_key(override)
            tuned = any(abs(f - override.frequency_mhz) < 0.005 for f in com_freqs)

            if tuned:
                currently_tuned_keys.add(key)
                if key not in self._alerted_keys:
                    self._alerted_keys.add(key)
                    freq_label = f"{override.frequency_mhz:.3f} MHz"
                    text = (
                        f"WARNING: ATIS {freq_label} at {override.icao} is UNSERVICEABLE — "
                        f"do not use, expect no service ({override.notam_id})"
                    )
                    alerts.append((text, override.icao, override.notam_id))

        # Clear debounce for keys no longer tuned so re-tuning fires again
        self._alerted_keys &= currently_tuned_keys

        return alerts

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _override_key(override: AtisOverride) -> str:
        return f"{override.icao}:{override.frequency_mhz}"

    @staticmethod
    def _read_com_frequencies(sm: object) -> list[float]:
        """
        Read COM1 and COM2 active frequencies from SimConnect.
        Returns MHz values (e.g. 119.56).  Returns [] on any error.
        """
        try:
            from SimConnect import AircraftRequests  # type: ignore
            aq = AircraftRequests(sm, _time=200)
            freqs: list[float] = []
            for idx in (1, 2):
                raw = aq.get(f"COM_ACTIVE_FREQUENCY:{idx}")
                if raw is not None:
                    freqs.append(round(float(raw), 3))
            return freqs
        except Exception as exc:
            logger.debug(f"[atis] COM frequency read failed: {exc}")
            return []
