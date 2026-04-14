"""
SimVar write-access tester.

Run this on Windows with MSFS open and a flight loaded.
Tune NAV1 to an ILS frequency before running for the most useful results.

Usage:
    python scripts/test_simvars.py [--nav-index 1]

What it does:
  1. Reads current values of all ILS/VOR/NDB-related SimVars
  2. Attempts to write each one (set to an "invalid" or "disabled" value)
  3. Re-reads and reports whether the write took effect
  4. Restores original values at the end

Output: test_simvars_report.txt  (also printed to console)
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

try:
    from SimConnect import SimConnect, AircraftRequests
except ImportError:
    print("ERROR: SimConnect library not found. Run on Windows with MSFS open.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# SimVar catalogue — what we want to test
# ---------------------------------------------------------------------------

@dataclass
class SimVarTest:
    name: str                    # SimConnect variable name (with :<idx> placeholder)
    unit: str                    # SimConnect unit string
    description: str             # Human-readable purpose
    write_value: float           # Value that should represent "disabled / invalid"
    # Results filled in at runtime
    read_ok:    bool = False
    write_ok:   bool = False
    took_effect: bool = False
    original_value: Optional[float] = None
    value_after:    Optional[float] = None
    error: str = ""


# NAV index is substituted into names at test time (default: 1)
SIMVAR_TESTS = [
    # ── ILS / Localizer ──────────────────────────────────────────────────────
    SimVarTest(
        "NAV HAS NAV:{n}",        "Bool",
        "ILS/VOR signal present flag",
        write_value=0.0,
    ),
    SimVarTest(
        "NAV SIGNAL:{n}",         "Number",
        "Signal strength (0 = no signal)",
        write_value=0.0,
    ),
    SimVarTest(
        "NAV CDI:{n}",            "Number",
        "Course deviation (127 = max deflection = LOC unusable)",
        write_value=127.0,
    ),
    SimVarTest(
        "NAV GLIDE SLOPE ERROR:{n}", "Degrees",
        "Glideslope deviation (large value pegs GS needle off-scale)",
        write_value=99.0,
    ),
    SimVarTest(
        "NAV HAS GLIDE SLOPE:{n}", "Bool",
        "Glideslope available flag",
        write_value=0.0,
    ),
    SimVarTest(
        "NAV GSI:{n}",            "Number",
        "Glide slope indicator (127 = off-scale)",
        write_value=127.0,
    ),
    SimVarTest(
        "NAV DME:{n}",            "Nautical miles",
        "DME distance (0 = no DME lock)",
        write_value=0.0,
    ),
    SimVarTest(
        "NAV HAS DME:{n}",        "Bool",
        "DME available flag",
        write_value=0.0,
    ),
    SimVarTest(
        "NAV ACTIVE FREQUENCY:{n}", "MHz",
        "Active NAV frequency (set to 0 = detune)",
        write_value=0.0,
    ),
    SimVarTest(
        "NAV STANDBY FREQUENCY:{n}", "MHz",
        "Standby NAV frequency",
        write_value=0.0,
    ),
    # ── Marker beacons ───────────────────────────────────────────────────────
    SimVarTest(
        "MARKER BEACON STATE",    "Enum",
        "Marker beacon state (0=none, 1=outer, 2=middle, 3=inner)",
        write_value=0.0,
    ),
    # ── VOR radial ───────────────────────────────────────────────────────────
    SimVarTest(
        "NAV RADIAL ERROR:{n}",   "Degrees",
        "VOR radial error (large = OBS unusable)",
        write_value=180.0,
    ),
    SimVarTest(
        "NAV HAS NAV:{n}",        "Bool",   # duplicate intentional — test restore
        "ILS/VOR signal present (second pass — verify restore)",
        write_value=0.0,
    ),
    # ── Comm / ATIS ──────────────────────────────────────────────────────────
    SimVarTest(
        "COM ACTIVE FREQUENCY:1", "MHz",
        "COM1 active frequency (detune test)",
        write_value=0.0,
    ),
]


# ---------------------------------------------------------------------------
# Raw SimConnect writer (same pattern as objects.py)
# ---------------------------------------------------------------------------

_DEF_ID  = 200   # arbitrary data-definition ID for our test writes
_OBJ_USER = 0    # SIMCONNECT_OBJECT_ID_USER

class _RawWriter:
    """Writes a single SimVar to the user aircraft via raw DLL calls."""

    def __init__(self, sm: SimConnect) -> None:
        self._sm = sm
        self._def_seq = _DEF_ID

    def _dll(self, name: str):
        fn = getattr(self._sm.dll.SimConnect, f"SimConnect_{name}")
        fn.restype  = ctypes.c_long
        fn.argtypes = None
        return fn

    def read(self, var: str, unit: str, aq: AircraftRequests) -> Optional[float]:
        try:
            val = aq.get(var)
            if val is None:
                return None
            return float(val)
        except Exception as exc:
            return None

    def write(self, var: str, unit: str, value: float) -> tuple[bool, str]:
        """
        Attempt to write *value* to *var*.
        Returns (success, error_message).
        """
        def_id = self._def_seq
        self._def_seq += 1

        try:
            # 1. Define a one-field data definition
            hr_add = self._dll("AddToDataDefinition")(
                self._sm.hSimConnect,
                ctypes.c_uint32(def_id),
                ctypes.c_char_p(var.encode()),
                ctypes.c_char_p(unit.encode()),
                ctypes.c_uint32(2),    # SIMCONNECT_DATATYPE_FLOAT64
                ctypes.c_float(0.0),
                ctypes.c_uint32(0xFFFFFFFF),
            )
            if hr_add != 0:
                return False, f"AddToDataDefinition HRESULT={hr_add:#010x}"

            # 2. Pack the value as a double
            buf = ctypes.c_double(value)

            # 3. SetDataOnSimObject for the user aircraft
            hr_set = self._dll("SetDataOnSimObject")(
                self._sm.hSimConnect,
                ctypes.c_uint32(def_id),
                ctypes.c_uint32(_OBJ_USER),
                ctypes.c_uint32(0),    # SIMCONNECT_DATA_SET_FLAG_DEFAULT
                ctypes.c_uint32(0),
                ctypes.c_uint32(ctypes.sizeof(buf)),
                ctypes.byref(buf),
            )
            if hr_set != 0:
                return False, f"SetDataOnSimObject HRESULT={hr_set:#010x}"

            return True, ""

        except Exception as exc:
            return False, str(exc)


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

def run_tests(nav_index: int) -> list[SimVarTest]:
    print(f"\nConnecting to SimConnect (NAV index = {nav_index})…")
    sm = SimConnect()
    aq = AircraftRequests(sm, _time=500)
    writer = _RawWriter(sm)

    print("Connected. Waiting for sim to settle…")
    time.sleep(1.0)

    results: list[SimVarTest] = []

    for test in SIMVAR_TESTS:
        var_name = test.name.replace("{n}", str(nav_index))
        t = SimVarTest(
            name        = var_name,
            unit        = test.unit,
            description = test.description,
            write_value = test.write_value,
        )

        print(f"\n  Testing: {var_name} [{test.unit}]")
        print(f"           {test.description}")

        # 1. Read original value
        orig = writer.read(var_name, test.unit, aq)
        t.original_value = orig
        t.read_ok = orig is not None
        if orig is not None:
            print(f"    READ   → {orig:.4f}")
        else:
            print(f"    READ   → (not available)")

        # 2. Attempt write
        ok, err = writer.write(var_name, test.unit, test.write_value)
        t.write_ok = ok
        t.error = err
        if ok:
            print(f"    WRITE  → {test.write_value}  (no error)")
        else:
            print(f"    WRITE  → FAILED: {err}")

        # 3. Re-read after short delay
        time.sleep(0.3)
        after = writer.read(var_name, test.unit, aq)
        t.value_after = after

        if ok and after is not None and orig is not None:
            delta = abs(after - test.write_value)
            t.took_effect = delta < 0.5
            if t.took_effect:
                print(f"    VERIFY → {after:.4f}  ✓ WRITE TOOK EFFECT")
            else:
                print(f"    VERIFY → {after:.4f}  ✗ value unchanged (sim rejected write)")
        elif ok and after is None:
            print(f"    VERIFY → (unreadable after write)")
        elif not ok:
            print(f"    VERIFY → skipped (write failed)")

        # 4. Restore original
        if ok and orig is not None:
            writer.write(var_name, test.unit, orig)
            time.sleep(0.2)
            print(f"    RESTORE→ {orig:.4f}")

        results.append(t)

    sm.exit()
    return results


def write_report(results: list[SimVarTest], nav_index: int) -> None:
    lines = [
        "SimVar Write-Access Test Report",
        f"NAV index: {nav_index}",
        "=" * 70,
        "",
        f"{'SimVar':<40} {'Read':>5} {'Write':>6} {'Effect':>7}  Notes",
        "-" * 70,
    ]
    for t in results:
        read_s  = "OK"   if t.read_ok       else "FAIL"
        write_s = "OK"   if t.write_ok      else "FAIL"
        eff_s   = "YES"  if t.took_effect   else ("NO" if t.write_ok else "---")
        note = ""
        if t.error:
            note = f"err: {t.error[:40]}"
        elif t.took_effect:
            note = f"orig={t.original_value:.3f} → set to {t.write_value} → confirmed"
        elif t.write_ok and not t.took_effect:
            note = f"write accepted but value stayed at {t.value_after}"
        lines.append(
            f"{t.name:<40} {read_s:>5} {write_s:>6} {eff_s:>7}  {note}"
        )

    lines += [
        "",
        "=" * 70,
        "SUMMARY",
        "-" * 70,
    ]

    effective = [t for t in results if t.took_effect]
    write_only = [t for t in results if t.write_ok and not t.took_effect]
    failed = [t for t in results if not t.write_ok]

    lines.append(f"  Writes that TOOK EFFECT (SimConnect-only is enough):")
    for t in effective:
        lines.append(f"    ✓  {t.name}  ({t.description})")

    lines.append(f"")
    lines.append(f"  Writes ACCEPTED but no effect (WASM likely needed):")
    for t in write_only:
        lines.append(f"    ~  {t.name}  ({t.description})")

    lines.append(f"")
    lines.append(f"  Writes REJECTED (read-only or not available):")
    for t in failed:
        lines.append(f"    ✗  {t.name}  — {t.error[:60]}")

    report = "\n".join(lines)
    print("\n\n" + report)

    out = Path("test_simvars_report.txt")
    out.write_text(report, encoding="utf-8")
    print(f"\nReport written to {out.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test SimVar write access for navaid overrides")
    parser.add_argument("--nav-index", type=int, default=1,
                        help="NAV radio index to test (default: 1)")
    args = parser.parse_args()

    print("=" * 60)
    print("  MSFS SimVar Write-Access Tester")
    print("=" * 60)
    print("Prerequisites:")
    print("  - MSFS is running with a flight loaded")
    print("  - Aircraft is on the ground or in flight near an ILS")
    print(f"  - NAV{args.nav_index} is tuned to an ILS frequency (e.g. 110.30)")
    print()
    input("Press ENTER when ready…")

    results = run_tests(args.nav_index)
    write_report(results, args.nav_index)


if __name__ == "__main__":
    main()
