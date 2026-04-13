from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


TITLE_RE = re.compile(r"^\s*title\s*=\s*(.+?)\s*$", re.IGNORECASE)


def candidate_roots() -> list[Path]:
    local_appdata = Path(os.environ.get("LOCALAPPDATA", ""))
    appdata = Path(os.environ.get("APPDATA", ""))
    userprofile = Path(os.environ.get("USERPROFILE", ""))

    roots = [
        local_appdata / "Packages" / "Microsoft.Limitless_8wekyb3d8bbwe" / "LocalCache" / "Packages",
        local_appdata / "Packages" / "Microsoft.FlightSimulator_8wekyb3d8bbwe" / "LocalCache" / "Packages",
        appdata / "Microsoft Flight Simulator" / "Packages",
        userprofile / "AppData" / "Roaming" / "Microsoft Flight Simulator" / "Packages",
        Path("C:/XboxGames/Microsoft Flight Simulator/Content"),
        Path("D:/XboxGames/Microsoft Flight Simulator/Content"),
    ]

    dedup: list[Path] = []
    seen: set[str] = set()
    for p in roots:
        key = str(p).lower()
        if key not in seen:
            dedup.append(p)
            seen.add(key)
    return dedup


def official_roots(package_root: Path) -> list[Path]:
    """Return official content directories for a given MSFS package root."""
    official_dir = package_root / "Official"
    if official_dir.exists():
        return [official_dir]

    # Fallback for installs where Official is nested with variant names.
    found: list[Path] = []
    try:
        for child in package_root.iterdir():
            if child.is_dir() and child.name.lower().startswith("official"):
                found.append(child)
    except Exception:
        return []
    return found


def official_onestore_roots(package_root: Path) -> list[Path]:
    roots: list[Path] = []
    for off in official_roots(package_root):
        one_store = off / "OneStore"
        if one_store.exists():
            roots.append(one_store)
    return roots


def read_titles(sim_cfg: Path) -> list[str]:
    titles: list[str] = []
    try:
        for line in sim_cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = TITLE_RE.match(line)
            if m:
                titles.append(m.group(1).strip())
    except Exception:
        return titles
    return titles


def collect_simobject_folder_names(root: Path) -> set[str]:
    """Collect candidate SimObject names from Official package folder structure."""
    names: set[str] = set()
    try:
        for sim_cfg in root.rglob("sim.cfg"):
            # Typical structure ends with .../SimObjects/<category>/<object_name>/sim.cfg
            parent = sim_cfg.parent
            if parent.name:
                names.add(parent.name)
    except Exception:
        return names
    return names


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan MSFS Official package inventory for SimObject titles."
    )
    parser.add_argument(
        "--packages-path",
        type=Path,
        default=None,
        help=(
            "Optional explicit InstalledPackagesPath (contains Official2020/Official2024). "
            "Use this if UserCfg.opt points to a different location."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.packages_path is not None:
        package_roots = [args.packages_path]
    else:
        package_roots = [r for r in candidate_roots() if r.exists()]

    print("MSFS package roots found:")
    for r in package_roots:
        print(f"  - {r}")
    if not package_roots:
        print("No package roots found. Set one manually in this script and rerun.")
        return

    roots: list[Path] = []
    for pkg_root in package_roots:
        roots.extend(official_onestore_roots(pkg_root))

    # Deduplicate official roots while preserving order.
    official_unique: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            official_unique.append(r)
            seen.add(key)
    roots = official_unique

    print("\nOfficial OneStore roots used:")
    for r in roots:
        print(f"  - {r}")
    if not roots:
        print("No Official OneStore roots found under detected package roots.")
        return

    print("\nOfficial package counts:")
    any_nonempty = False
    for r in roots:
        pkg_count = 0
        try:
            pkg_count = sum(1 for p in r.iterdir() if p.is_dir())
        except Exception:
            pkg_count = 0
        print(f"  - {r}: {pkg_count} package folder(s)")
        if pkg_count > 0:
            any_nonempty = True

    title_to_cfg: dict[str, Path] = {}
    folder_name_candidates: set[str] = set()
    for root in roots:
        for sim_cfg in root.rglob("sim.cfg"):
            if "community" in str(sim_cfg).lower():
                continue
            for title in read_titles(sim_cfg):
                title_to_cfg.setdefault(title, sim_cfg)
        folder_name_candidates.update(collect_simobject_folder_names(root))

    if not title_to_cfg:
        print("No title= entries found in sim.cfg files.")

    titles = sorted(title_to_cfg.keys(), key=str.lower)
    print(f"\nFound {len(titles)} unique simobject titles from cfg files.")
    print(f"Found {len(folder_name_candidates)} SimObject folder-name candidates.\n")

    preview_terms = ("airbus", "boeing", "a320", "747", "748", "crane", "antenna")
    print("Likely candidates:")
    for t in titles:
        low = t.lower()
        if any(k in low for k in preview_terms):
            print(f"  {t}")

    print("\nLikely folder-name candidates:")
    for t in sorted(folder_name_candidates, key=str.lower):
        low = t.lower()
        if any(k in low for k in preview_terms):
            print(f"  {t}")

    out = Path("data/simobject_titles_official.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for t in titles:
            f.write(f"{t}\n")

    out_folders = Path("data/simobject_folder_candidates_official.txt")
    with out_folders.open("w", encoding="utf-8") as f:
        for t in sorted(folder_name_candidates, key=str.lower):
            f.write(f"{t}\n")

    print(f"\nWrote full title list to: {out}")
    print(f"Wrote folder-name candidate list to: {out_folders}")
    if not any_nonempty:
        print(
            "\nNote: Official OneStore folders are empty on disk in this install. "
            "This usually means streamed/protected content, so local inventory extraction is not possible from filesystem."
        )
        print(
            "Use a manual --packages-path if you installed content elsewhere, or use DevMode object browser export."
        )
    print("Pick one exact title from that list and set WT_748_TITLE or MSFS_TEST_MODEL_TITLE.")


if __name__ == "__main__":
    main()
