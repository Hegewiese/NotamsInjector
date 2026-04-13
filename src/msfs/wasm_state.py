"""
WASM state file writer.

Maintains a single ``navaid_overrides.json`` that the future WASM module
will consume.  Both NavaidController and AtisController feed into it so
the WASM side has one consistent source of truth.

Schema (version 1)::

    {
      "schema_version": 1,
      "updated_at": "<ISO-8601 UTC>",
      "navaid_overrides": [
        {
          "notam_id":    "M1612/26",
          "icao":        "EHVK",
          "navaid_type": "ILS",
          "component":   "full",
          "disabled":    true
        }
      ],
      "atis_overrides": [
        {
          "notam_id":       "C1598/26",
          "icao":           "EDQD",
          "frequency_mhz":  119.56,
          "disabled":       true
        }
      ]
    }

The WASM module should re-read the file whenever ``updated_at`` changes.
SimConnect client-data-area handoff will replace this file mechanism once
the WASM layer is implemented.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from src.config import settings


def flush(
    navaid_overrides: list[dict],
    atis_overrides: list[dict],
) -> None:
    """
    Write the combined override state to the WASM state file.

    Called by the scheduler after each apply cycle so the file always
    reflects a consistent snapshot of all active overrides.
    """
    path = Path(settings.wasm_state_file)
    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        "navaid_overrides": navaid_overrides,
        "atis_overrides": atis_overrides,
    }
    try:
        path.write_text(json.dumps(payload, indent=2))
        logger.debug(
            f"[wasm_state] Written → {path} "
            f"({len(navaid_overrides)} navaid, {len(atis_overrides)} ATIS override(s))"
        )
    except OSError as exc:
        logger.warning(f"[wasm_state] Could not write {path}: {exc}")
