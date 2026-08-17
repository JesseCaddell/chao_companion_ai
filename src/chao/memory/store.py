"""SQLite persistence for phase 6 memory (design doc §4.4).

Deliberately smaller than §4.4's four-table schema: this ships `viewers`
and `episodes` only. `sessions` is dropped (nothing consumes it yet --
`episodes.session_id` in the design doc is a FK to a table with no
purpose until session-scoped summaries are built) and `viewers.affinity`
is dropped too (session 11/12's design pass: affinity is now a *label*
derived at read time from `interactions`/`days_seen`, not a tuned scalar
that needs a persisted column -- storing one would just invite
re-deriving the threshold-tuning problem the de-gating decision removed).
`beliefs` and `episode_vec` come later, if at all -- see SESSION_STATE.md.

One `sqlite3` connection, opened with `check_same_thread=False` so it can
be used from whichever worker thread `asyncio.to_thread` picks, guarded
by a single `asyncio.Lock` so no two calls ever touch it concurrently --
the smallest thing that can't block the event loop, per CLAUDE.md's
"no threads except where forced" (this doesn't spawn a thread of its
own, it borrows the default executor's, same as any other
`asyncio.to_thread` call already would).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path("data") / "chao.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS viewers (
    id           INTEGER PRIMARY KEY,
    login        TEXT UNIQUE NOT NULL,
    display_name TEXT,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    last_seen_day TEXT NOT NULL,   -- ISO date (YYYY-MM-DD) of last_seen,
                                    -- used to bump days_seen at most once
                                    -- per calendar day -- see touch_viewer.
    interactions INTEGER NOT NULL DEFAULT 0,
    days_seen    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS episodes (
    id            INTEGER PRIMARY KEY,
    ts            REAL NOT NULL,
    source        TEXT NOT NULL,   -- 'voice' | 'chat' | 'ambient' | 'manual'
    viewer_id     INTEGER REFERENCES viewers(id),
    content       TEXT NOT NULL,
    chao_response TEXT,
    importance    REAL NOT NULL DEFAULT 0.5
    -- Placeholder constant -- nothing reads this yet. Real importance
    -- scoring is reflection's job (design doc §16.2), not this slice's.
);

CREATE INDEX IF NOT EXISTS idx_episodes_viewer_ts ON episodes(viewer_id, ts DESC);
"""


@dataclass(frozen=True, slots=True)
class ViewerSummary:
    id: int
    login: str
    interactions: int
    days_seen: int
    first_seen: float


@dataclass(frozen=True, slots=True)
class EpisodeRow:
    ts: float
    content: str
    chao_response: str | None


class MemoryStore:
    def __init__(self, path: Path = DB_PATH, *, clock=time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()
        self._clock = clock

    async def touch_viewer(self, login: str, display_name: str | None) -> int:
        """Upserts a `viewers` row for every chat message, not just ones
        that trigger a full turn -- session 12's design pass wants
        `interactions` to reflect real chat activity. `days_seen` only
        increments the first time a given calendar day sees this login,
        so a single-session flood can't move someone from "new here" to
        "a regular" the way an uncapped raw interaction count could
        (advisor's flood concern from the phase 6 design pass).
        """
        now = self._clock()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))

        async with self._lock:
            return await asyncio.to_thread(self._touch_viewer_sync, login, display_name, now, day)

    def _touch_viewer_sync(self, login: str, display_name: str | None, now: float, day: str) -> int:
        cur = self._conn.execute("SELECT id, last_seen_day FROM viewers WHERE login = ?", (login,))
        row = cur.fetchone()
        if row is None:
            cur = self._conn.execute(
                "INSERT INTO viewers "
                "(login, display_name, first_seen, last_seen, last_seen_day, interactions, days_seen) "
                "VALUES (?, ?, ?, ?, ?, 1, 1)",
                (login, display_name, now, now, day),
            )
            self._conn.commit()
            return cur.lastrowid

        viewer_id, last_seen_day = row
        bump_days = 1 if day != last_seen_day else 0
        self._conn.execute(
            "UPDATE viewers SET display_name = COALESCE(?, display_name), last_seen = ?, "
            "last_seen_day = ?, interactions = interactions + 1, days_seen = days_seen + ? "
            "WHERE id = ?",
            (display_name, now, day, bump_days, viewer_id),
        )
        self._conn.commit()
        return viewer_id

    async def viewer_summary(self, login: str) -> ViewerSummary | None:
        async with self._lock:
            return await asyncio.to_thread(self._viewer_summary_sync, login)

    def _viewer_summary_sync(self, login: str) -> ViewerSummary | None:
        cur = self._conn.execute(
            "SELECT id, login, interactions, days_seen, first_seen FROM viewers WHERE login = ?",
            (login,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return ViewerSummary(
            id=row[0], login=row[1], interactions=row[2], days_seen=row[3], first_seen=row[4]
        )

    async def write_episode(
        self,
        *,
        source: str,
        viewer_id: int | None,
        content: str,
        chao_response: str | None,
        importance: float = 0.5,
    ) -> None:
        now = self._clock()
        async with self._lock:
            await asyncio.to_thread(
                self._write_episode_sync, now, source, viewer_id, content, chao_response, importance
            )

    def _write_episode_sync(
        self,
        ts: float,
        source: str,
        viewer_id: int | None,
        content: str,
        chao_response: str | None,
        importance: float,
    ) -> None:
        self._conn.execute(
            "INSERT INTO episodes (ts, source, viewer_id, content, chao_response, importance) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, source, viewer_id, content, chao_response, importance),
        )
        self._conn.commit()

    async def recent_episodes(self, viewer_id: int, limit: int) -> list[EpisodeRow]:
        async with self._lock:
            return await asyncio.to_thread(self._recent_episodes_sync, viewer_id, limit)

    def _recent_episodes_sync(self, viewer_id: int, limit: int) -> list[EpisodeRow]:
        cur = self._conn.execute(
            "SELECT ts, content, chao_response FROM episodes "
            "WHERE viewer_id = ? ORDER BY ts DESC LIMIT ?",
            (viewer_id, limit),
        )
        return [EpisodeRow(ts=r[0], content=r[1], chao_response=r[2]) for r in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
