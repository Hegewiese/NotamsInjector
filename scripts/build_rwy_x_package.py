#!/usr/bin/env python3
"""
Build the NotamsInjector runway-closed-X Community Package for MSFS.

Generates three size-variant SimObjects (S / M / L) and attempts to compile
them using the MSFS SDK fspackagetool.exe.  Raw glTF files alone are NOT
loadable by AICreateSimulatedObject in MSFS 2024 -- the SDK compilation step
converts glTF to MSFS internal binary format.

ICAO Annex 14 / FAA AC 150/5340-1M compliance notes
-----------------------------------------------------
- Two arms cross at 90 degrees, oriented 45 degrees to the runway centreline
  (baked into the glTF node rotations; scheduler sets Heading = rwy.le_heading).
- Arm width ~= 1/8 of arm length (standard ICAO/FAA proportionality rule).
- For temporary closures: X at each end only (no mid-runway repetition).
- Colour: yellow (ICAO Doc 9157 / FAA AC 150/5340-1M Section 3.2).

Usage
-----
    python scripts/build_rwy_x_package.py

Output
------
    dist/source/notamsinjector-rwy-x/   <- raw source (glTF + configs)
    dist/community/notamsinjector-rwy-x/ <- compiled output (copy to Community2024 for MSFS 2024)

If fspackagetool.exe is found it is called automatically.
Otherwise the script prints manual MSFS Developer Mode instructions.

SimObject titles after install:
    NotamsInjector_Runway_Closed_X_S   (runway < 30 m / < 100 ft)
    NotamsInjector_Runway_Closed_X_M   (30-45 m / 100-150 ft)
    NotamsInjector_Runway_Closed_X_L   (> 45 m / > 150 ft, e.g. EDDF)
"""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import struct
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

# ── Package identity ──────────────────────────────────────────────────────────
PACKAGE_NAME = "notamsinjector-rwy-x"
_DIST        = Path(__file__).resolve().parent.parent / "dist"
SOURCE_DIR   = _DIST / "source"  / PACKAGE_NAME
OUT_DIR      = _DIST / "community" / PACKAGE_NAME

# ── ICAO-proportional size variants ──────────────────────────────────────────
@dataclass
class XVariant:
    suffix:       str
    arm_length_m: float   # full arm length
    arm_width_m:  float   # arm width (~= arm_length / 8)

VARIANTS: list[XVariant] = [
    XVariant("S", 10.0, 1.8),   # runway < 30 m
    XVariant("M", 18.0, 3.0),   # 30-45 m
    XVariant("L", 27.0, 4.5),   # > 45 m (EDDF etc.)
]

Y_OFFSET_M = 0.05          # metres above ground to avoid z-fighting
_YELLOW    = [1.0, 0.78, 0.0]


# ── glTF builder ──────────────────────────────────────────────────────────────

def _build_gltf(v: XVariant) -> dict:
    """
    glTF 2.0 scene: one rectangle mesh shared by two nodes rotated +-45 deg
    around Y so the arms form an X.  Binary buffer is base-64 embedded.
    The scheduler sets Heading = rwy.le_heading so arms sit at +-45 deg to
    the runway centreline (ICAO/FAA standard orientation).
    """
    half_len = v.arm_length_m / 2.0
    half_wid = v.arm_width_m  / 2.0
    y        = Y_OFFSET_M

    positions = [
        (-half_wid, y,  half_len),
        ( half_wid, y,  half_len),
        ( half_wid, y, -half_len),
        (-half_wid, y, -half_len),
    ]
    normals = [(0.0, 1.0, 0.0)] * 4
    indices = [0, 1, 2, 0, 2, 3]

    pos_bytes = b"".join(struct.pack("<fff", *p) for p in positions)
    nor_bytes = b"".join(struct.pack("<fff", *n) for n in normals)
    idx_bytes = b"".join(struct.pack("<H",   i) for i in indices)
    if len(idx_bytes) % 4:
        idx_bytes += b"\x00" * (4 - len(idx_bytes) % 4)

    buf  = pos_bytes + nor_bytes + idx_bytes
    uri  = "data:application/octet-stream;base64," + base64.b64encode(buf).decode()
    p_off, n_off, i_off = 0, len(pos_bytes), len(pos_bytes) + len(nor_bytes)

    sin22 = math.sin(math.radians(22.5))
    cos22 = math.cos(math.radians(22.5))

    return {
        "asset": {"version": "2.0", "generator": "NotamsInjector build_rwy_x_package.py"},
        "scene":  0,
        "scenes": [{"name": "Scene", "nodes": [0, 1]}],
        "nodes": [
            {"name": "arm_pos45", "mesh": 0, "rotation": [ 0.0,  sin22, 0.0, cos22]},
            {"name": "arm_neg45", "mesh": 0, "rotation": [ 0.0, -sin22, 0.0, cos22]},
        ],
        "meshes": [{"name": "arm", "primitives": [{
            "attributes": {"POSITION": 0, "NORMAL": 1},
            "indices": 2, "material": 0, "mode": 4,
        }]}],
        "extensionsUsed": ["ASOBO_material_unlit"],
        "materials": [{
            "name": "yellow_icao",
            "pbrMetallicRoughness": {
                "baseColorFactor": _YELLOW + [1.0],
                "metallicFactor": 0.0, "roughnessFactor": 1.0,
            },
            "emissiveFactor": [c * 0.4 for c in _YELLOW],
            "doubleSided": True,
            "extensions": {"ASOBO_material_unlit": {}},
        }],
        "accessors": [
            {"bufferView": 0, "byteOffset": 0, "componentType": 5126,
             "count": 4, "type": "VEC3",
             "min": [-half_wid, y, -half_len], "max": [half_wid, y, half_len]},
            {"bufferView": 1, "byteOffset": 0, "componentType": 5126,
             "count": 4, "type": "VEC3"},
            {"bufferView": 2, "byteOffset": 0, "componentType": 5123,
             "count": 6, "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": p_off, "byteLength": len(pos_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": n_off, "byteLength": len(nor_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": i_off, "byteLength": len(indices) * 2, "target": 34963},
        ],
        "buffers": [{"uri": uri, "byteLength": len(buf)}],
    }


# ── model XML (LOD descriptor) ────────────────────────────────────────────────

def _build_model_xml(simobj_name: str) -> str:
    guid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"notamsinjector.{simobj_name}")).upper()
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<ModelInfo version="1.1" guid="{{{guid}}}">\n'
        '    <LODS>\n'
        f'        <LOD minSize="0" ModelFile="{simobj_name}.gltf"/>\n'
        '    </LODS>\n'
        '</ModelInfo>\n'
    )


# ── sim.cfg ──────────────────────────────────────────────────────────────────

def _sim_cfg(title: str, suffix: str, v: XVariant) -> str:
    return (
        "[VERSION]\n"
        "Major=1\n"
        "Minor=0\n\n"
        "[General]\n"
        "category=StaticObject\n"
        "DistanceToNotAnimate=2000\n\n"
        f"; ICAO Annex 14 runway-closed X marker  variant={suffix}\n"
        f"; arm {v.arm_length_m:.0f} m x {v.arm_width_m:.1f} m, arms at +-45 deg to runway\n\n"
        "[fltsim.0]\n"
        f"title={title}\n"
        "model=\n"
        "texture=\n"
    )


# ── manifest.json ─────────────────────────────────────────────────────────────

def _manifest() -> dict:
    return {
        "dependencies":         [],
        "content_type":         "SCENERY",
        "title":                "NotamsInjector Runway Closed X Markers",
        "description":          "ICAO Annex 14 yellow X ground markers (S/M/L) for runway closure NOTAMs.",
        "manufacturer":         "",
        "creator":              "NotamsInjector",
        "package_version":      "1.0.0",
        "minimum_game_version": "2.28.0",
        "release_notes":        {"neutral": {"LastUpdate": "", "OlderHistory": ""}},
    }


# ── layout.json ───────────────────────────────────────────────────────────────

def _make_layout(package_root: Path) -> dict:
    import time
    FILETIME_OFFSET = 116444736000000000
    now = int(time.time() * 10_000_000) + FILETIME_OFFSET
    content = []
    for path in sorted(package_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in {"manifest.json", "layout.json"}:
            continue
        content.append({
            "path": path.relative_to(package_root).as_posix(),
            "size": path.stat().st_size,
            "date": now,
        })
    return {"content": content}


# ── PackageDef.xml + fspackagetool ────────────────────────────────────────────

def _package_def_xml() -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<AssetPackage Version="2.0.0" id="{PACKAGE_NAME}">\n'
        '    <ItemSettings>\n'
        '        <ContentType>SCENERY</ContentType>\n'
        '        <Title>NotamsInjector Runway Closed X</Title>\n'
        '        <CreatorName>NotamsInjector</CreatorName>\n'
        '        <Version>1.0.0</Version>\n'
        '        <MinGameVersion>2.28.0</MinGameVersion>\n'
        '    </ItemSettings>\n'
        '    <Flags>\n'
        '        <Flag ID="SPLIT_TEXTURES" Value="true"/>\n'
        '    </Flags>\n'
        f'    <AssetDir Path="{SOURCE_DIR.as_posix()}" />\n'
        f'    <PackageDir Path="{OUT_DIR.as_posix()}" />\n'
        '</AssetPackage>\n'
    )


def _find_fspackagetool() -> Path | None:
    candidates = [
        Path(r"C:\MSFS 2024 SDK\Tools\bin\fspackagetool.exe"),   # confirmed location
        Path(r"C:\MSFS SDK\Tools\fspackagetool\fspackagetool.exe"),
        Path(r"C:\Program Files\Microsoft Games\Microsoft Flight Simulator 2024\SDK\Tools\fspackagetool\fspackagetool.exe"),
        Path(r"C:\Program Files (x86)\Microsoft Games\Microsoft Flight Simulator 2024\SDK\Tools\fspackagetool\fspackagetool.exe"),
        Path(r"C:\Program Files\Microsoft Games\Microsoft Flight Simulator\SDK\Tools\fspackagetool\fspackagetool.exe"),
    ]
    for env_var in ("MSFS_SDK", "MSFS2024_SDK"):
        val = os.environ.get(env_var)
        if val:
            candidates.insert(0, Path(val) / "Tools" / "fspackagetool" / "fspackagetool.exe")
    return next((p for p in candidates if p.exists()), None)


def _compiled_package_has_assets() -> bool:
    layout_path = OUT_DIR / "layout.json"
    if not layout_path.exists():
        return False

    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    content = layout.get("content")
    return isinstance(content, list) and bool(content)


def _compile_with_sdk() -> bool:
    tool = _find_fspackagetool()
    if tool is None:
        return False

    pkg_def = SOURCE_DIR / "PackageDef.xml"
    pkg_def.write_text(_package_def_xml(), encoding="utf-8")
    print(f"  wrote  PackageDef.xml")
    print(f"  running {tool.name} ...")

    result = subprocess.run([str(tool), str(pkg_def)], capture_output=True, text=True)
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip():
            print(f"    {line}")
    if result.returncode != 0:
        print(f"  FAILED (exit {result.returncode})")
        return False
    if not _compiled_package_has_assets():
        print("  FAILED (SDK returned success but produced an empty package)")
        return False
    print("  compilation OK")
    return True


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if SOURCE_DIR.exists():
        shutil.rmtree(SOURCE_DIR)
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)

    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Write source files
    (SOURCE_DIR / "manifest.json").write_text(
        json.dumps(_manifest(), indent=2), encoding="utf-8"
    )
    print("  wrote  manifest.json")

    for v in VARIANTS:
        simobj_name = f"notamsinjector_rwy_closed_x_{v.suffix.lower()}"
        title       = f"NotamsInjector_Runway_Closed_X_{v.suffix}"
        simobj_dir  = SOURCE_DIR / "SimObjects" / "Misc" / simobj_name
        model_dir   = simobj_dir / "model"
        model_dir.mkdir(parents=True, exist_ok=True)

        for path, content in [
            (simobj_dir / "sim.cfg",               _sim_cfg(title, v.suffix, v)),
            (model_dir  / "model.cfg",              f"[models]\nnormal = {simobj_name}.xml\n"),
            (model_dir  / f"{simobj_name}.xml",     _build_model_xml(simobj_name)),
            (model_dir  / f"{simobj_name}.gltf",    json.dumps(_build_gltf(v), indent=2)),
        ]:
            path.write_text(content, encoding="utf-8")
            print(f"  wrote  {path.relative_to(SOURCE_DIR)}")

    print(f"\nSource: {SOURCE_DIR}")
    print("Compiling with MSFS SDK ...")

    compiled = _compile_with_sdk()

    if compiled:
        (OUT_DIR / "layout.json").write_text(
            json.dumps(_make_layout(OUT_DIR), indent=2), encoding="utf-8"
        )
        print(f"\nDone.  Compiled package: {OUT_DIR}")
        print(">> Copy 'notamsinjector-rwy-x/' into your MSFS 2024 Community2024 folder.")
        print(">> Restart MSFS.\n")
    else:
        # Write PackageDef.xml anyway so user can open it in MSFS Project Editor
        pkg_def = SOURCE_DIR / "PackageDef.xml"
        pkg_def.write_text(_package_def_xml(), encoding="utf-8")
        print("\nAutomatic SDK build did not produce a valid package. Manual steps:")
        print("  1. MSFS: Options > General > Developer Mode = ON, restart MSFS")
        print("  2. MSFS toolbar: Tools > Project Editor")
        print(f"  3. Open project: {pkg_def}")
        print("  4. Click 'Build package'")
        print(f"  5. Copy '{OUT_DIR.name}/' into your MSFS 2024 Community2024 folder")
        print("  6. Restart MSFS\n")

    print("SimObject titles (after compilation + install):")
    for v in VARIANTS:
        print(f"  NotamsInjector_Runway_Closed_X_{v.suffix}"
              f"  ({v.arm_length_m:.0f} m x {v.arm_width_m:.1f} m arm)")


if __name__ == "__main__":
    print(f"Building: {PACKAGE_NAME}\n")
    main()
