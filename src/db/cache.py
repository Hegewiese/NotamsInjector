"""
SQLite-backed NOTAM cache using aiosqlite.

Stores fetched NOTAMs and tracks which MSFS actions have been applied.
This lets us:
  - Avoid re-fetching NOTAMs that haven't expired
  - Persist action state across restarts
  - Power the debug UI with historical data
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from loguru import logger

from src.notam.models import MsfsAction, Notam, NotamCondition, NotamSubject

DB_PATH = Path("notam_cache.db")

_CREATE_NOTAMS = """
CREATE TABLE IF NOT EXISTS notams (
    id          TEXT PRIMARY KEY,
    icao        TEXT NOT NULL,
    subject     TEXT,
    condition   TEXT,
    valid_from  TEXT,
    valid_to    TEXT,
    lower_ft    INTEGER DEFAULT 0,
    upper_ft    INTEGER DEFAULT 99999,
    lat         REAL,
    lon         REAL,
    radius_nm   REAL,
    description TEXT,
    raw         TEXT,
    source      TEXT,
    fetched_at  TEXT NOT NULL
);
"""

_CREATE_ACTIONS = """
CREATE TABLE IF NOT EXISTS msfs_actions (
    id          TEXT PRIMARY KEY,
    notam_id    TEXT NOT NULL,
    action_type TEXT NOT NULL,
    icao        TEXT NOT NULL,
    params      TEXT,
    applied     INTEGER DEFAULT 0,
    applied_at  TEXT,
    error       TEXT,
    FOREIGN KEY (notam_id) REFERENCES notams(id)
);
"""

_CREATE_AIRPORT_FETCHES = """
CREATE TABLE IF NOT EXISTS airport_fetches (
    icao            TEXT PRIMARY KEY,
    last_fetched_at TEXT NOT NULL
);
"""


def _notam_to_row(n: Notam) -> dict:
    return {
        "id":          n.id,
        "icao":        n.icao,
        "subject":     n.subject.value,
        "condition":   n.condition.value,
        "valid_from":  n.valid_from.isoformat(),
        "valid_to":    n.valid_to.isoformat() if n.valid_to else None,
        "lower_ft":    n.lower_ft,
        "upper_ft":    n.upper_ft,
        "lat":         n.lat,
        "lon":         n.lon,
        "radius_nm":   n.radius_nm,
        "description": n.description,
        "raw":         n.raw,
        "source":      n.source,
        "fetched_at":  datetime.now(tz=timezone.utc).isoformat(),
    }


def _row_to_notam(row: aiosqlite.Row) -> Notam:
    return Notam(
        id=row["id"],
        icao=row["icao"],
        subject=NotamSubject(row["subject"]),
        condition=NotamCondition(row["condition"]),
        valid_from=datetime.fromisoformat(row["valid_from"]),
        valid_to=datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
        lower_ft=row["lower_ft"] or 0,
        upper_ft=row["upper_ft"] or 99999,
        lat=row["lat"],
        lon=row["lon"],
        radius_nm=row["radius_nm"],
        description=row["description"] or "",
        raw=row["raw"] or "",
        source=row["source"] or "",
    )


class NotamCache:
    """Async SQLite cache.  Call `await cache.init()` before use."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(_CREATE_NOTAMS)
        await self._db.execute(_CREATE_ACTIONS)
        await self._db.execute(_CREATE_AIRPORT_FETCHES)
        await self._db.commit()
        logger.info(f"NOTAM cache initialised at {self.db_path}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── NOTAM ops ─────────────────────────────────────────────────────────────

    async def upsert_notams(self, notams: list[Notam]) -> None:
        rows = [_notam_to_row(n) for n in notams]
        await self._db.executemany(
            """
            INSERT INTO notams VALUES
              (:id,:icao,:subject,:condition,:valid_from,:valid_to,
               :lower_ft,:upper_ft,:lat,:lon,:radius_nm,:description,:raw,:source,:fetched_at)
            ON CONFLICT(id) DO UPDATE SET
              fetched_at=excluded.fetched_at,
              description=excluded.description
            """,
            rows,
        )
        await self._db.commit()

    async def get_active_notams(self, icao: Optional[str] = None) -> list[Notam]:
        now = datetime.now(tz=timezone.utc).isoformat()
        if icao:
            cur = await self._db.execute(
                "SELECT * FROM notams WHERE icao=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)",
                (icao, now, now),
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM notams WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>=?)",
                (now, now),
            )
        rows = await cur.fetchall()
        return [_row_to_notam(r) for r in rows]

    async def get_active_notams_for_icaos(self, icao_codes: list[str]) -> list[Notam]:
        """Return all still-valid cached NOTAMs for a set of ICAO codes."""
        if not icao_codes:
            return []
        now = datetime.now(tz=timezone.utc).isoformat()
        placeholders = ",".join("?" * len(icao_codes))
        cur = await self._db.execute(
            f"SELECT * FROM notams WHERE icao IN ({placeholders})"
            f" AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)",
            (*icao_codes, now, now),
        )
        rows = await cur.fetchall()
        return [_row_to_notam(r) for r in rows]

    async def get_stale_icaos(self, icao_codes: list[str], max_age_minutes: int) -> list[str]:
        """Return ICAOs never fetched or fetched earlier than max_age_minutes."""
        if not icao_codes:
            return []

        from datetime import timedelta

        cutoff = (datetime.now(tz=timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        placeholders = ",".join("?" * len(icao_codes))
        cur = await self._db.execute(
            f"SELECT icao FROM airport_fetches WHERE icao IN ({placeholders}) AND last_fetched_at >= ?",
            (*icao_codes, cutoff),
        )
        rows = await cur.fetchall()
        fresh_icaos = {str(r["icao"]) for r in rows}
        return [icao for icao in icao_codes if icao not in fresh_icaos]

    async def mark_airports_fetched(self, icao_codes: list[str]) -> None:
        """Persist fetch time for airports, even if they returned zero NOTAMs."""
        if not icao_codes:
            return

        now_iso = datetime.now(tz=timezone.utc).isoformat()
        await self._db.executemany(
            """
            INSERT INTO airport_fetches(icao,last_fetched_at)
            VALUES(?,?)
            ON CONFLICT(icao) DO UPDATE SET
              last_fetched_at=excluded.last_fetched_at
            """,
            [(icao, now_iso) for icao in icao_codes],
        )
        await self._db.commit()

    async def purge_expired(self, older_than_h: int = 24) -> int:
        """Delete NOTAMs that expired more than *older_than_h* hours ago."""
        from datetime import timedelta
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=older_than_h)).isoformat()
        cur = await self._db.execute(
            "DELETE FROM notams WHERE valid_to IS NOT NULL AND valid_to < ?",
            (cutoff,),
        )
        await self._db.commit()
        return cur.rowcount

    # ── Action ops ────────────────────────────────────────────────────────────

    async def upsert_action(self, action: MsfsAction) -> None:
        action_id = f"{action.notam_id}:{action.action_type}"
        await self._db.execute(
            """
            INSERT INTO msfs_actions(id,notam_id,action_type,icao,params,applied,applied_at,error)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              applied=excluded.applied,
              applied_at=excluded.applied_at,
              error=excluded.error
            """,
            (
                action_id,
                action.notam_id,
                action.action_type,
                action.icao,
                json.dumps(action.params),
                int(action.applied),
                action.applied_at.isoformat() if action.applied_at else None,
                action.error,
            ),
        )
        await self._db.commit()

    async def get_all_actions(self) -> list[MsfsAction]:
        cur = await self._db.execute("SELECT * FROM msfs_actions ORDER BY rowid DESC")
        rows = await cur.fetchall()
        actions = []
        for r in rows:
            a = MsfsAction(
                notam_id=r["notam_id"],
                action_type=r["action_type"],
                icao=r["icao"],
                params=json.loads(r["params"] or "{}"),
                applied=bool(r["applied"]),
                applied_at=datetime.fromisoformat(r["applied_at"]) if r["applied_at"] else None,
                error=r["error"],
            )
            actions.append(a)
        return actions
