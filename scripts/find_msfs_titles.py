from __future__ import annotations

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


def main() -> None:
    roots = [r for r in candidate_roots() if r.exists()]
    print("MSFS package roots found:")
    for r in roots:
        print(f"  - {r}")
    if not roots:
        print("No package roots found. Set one manually in this script and rerun.")
        return

    title_to_cfg: dict[str, Path] = {}
    for root in roots:
        for sim_cfg in root.rglob("sim.cfg"):
            for title in read_titles(sim_cfg):
                title_to_cfg.setdefault(title, sim_cfg)

    if not title_to_cfg:
        print("No title= entries found in sim.cfg files.")
        return

    titles = sorted(title_to_cfg.keys(), key=str.lower)
    print(f"\nFound {len(titles)} unique simobject titles.\n")

    preview_terms = ("airbus", "boeing", "a320", "747", "748", "crane", "antenna")
    print("Likely candidates:")
    for t in titles:
        low = t.lower()
        if any(k in low for k in preview_terms):
            print(f"  {t}")

    out = Path("data/simobject_titles.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for t in titles:
            f.write(f"{t}\n")

    print(f"\nWrote full title list to: {out}")
    print("Pick one exact title from that list and set WT_748_TITLE or MSFS_TEST_MODEL_TITLE.")


if __name__ == "__main__":
    main()
