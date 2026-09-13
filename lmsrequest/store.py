"""Persistence. sqlite3 from the stdlib -- no ORM, no migrations to babysit.

Holds three things: the host's runtime choices (player, playlist), the request
log (what was asked for and by whom), and the guests' rate-limit buckets.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    url       TEXT NOT NULL,
    title     TEXT NOT NULL,
    artist    TEXT NOT NULL DEFAULT '',
    art       TEXT,
    guest_id  TEXT NOT NULL,
    created   REAL NOT NULL,
    -- pending -> played once it falls behind the current index.
    -- 'vetoed'  = the host removed it; stays blocked so it cannot come back.
    -- 'retired' = a playlist reload invalidated it; may be requested again.
    state     TEXT NOT NULL DEFAULT 'pending',
    ip        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS requests_url ON requests(url);

CREATE TABLE IF NOT EXISTS buckets (
    guest_id TEXT PRIMARY KEY,
    tokens   REAL NOT NULL,
    updated  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bans (
    ip      TEXT PRIMARY KEY,
    reason  TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns to a database created by an earlier version."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(requests)")}
        if "ip" not in cols:
            self.db.execute(
                "ALTER TABLE requests ADD COLUMN ip TEXT NOT NULL DEFAULT ''"
            )

    def close(self) -> None:
        self.db.close()

    # -- settings ----------------------------------------------------------

    def get(self, key: str, default=None):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()

    # -- requests ----------------------------------------------------------

    def add_request(
        self,
        url: str,
        title: str,
        artist: str,
        art: str | None,
        guest_id: str,
        ip: str = "",
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO requests (url, title, artist, art, guest_id, created, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, title, artist, art, guest_id, time.time(), ip),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def recent_requests(self, limit: int = 80) -> list[sqlite3.Row]:
        """Newest first, for the host's activity feed."""
        return self.db.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM requests WHERE state = 'pending' ORDER BY created"
        ).fetchall()

    def pending_urls(self) -> set[str]:
        return {r["url"] for r in self.pending()}

    def pending_count_for(self, guest_id: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM requests "
            "WHERE state = 'pending' AND guest_id = ?",
            (guest_id,),
        ).fetchone()
        return int(row["n"])

    def request_state(self, url: str) -> str | None:
        """The state that speaks for this track tonight, or None if it's free.

        Whether each state actually blocks a fresh request is the party's
        decision, not the store's -- see Party.blocked(). Retired rows never
        speak: a playlist reload invalidated them.

        A song can hold several rows once repeats are allowed, so they're
        ranked rather than taken in id order: a veto outranks everything (the
        whole point of vetoing is that it doesn't come straight back), and a
        copy still waiting outranks one that has already played.
        """
        row = self.db.execute(
            "SELECT state FROM requests WHERE url = ? AND state != 'retired' "
            "ORDER BY CASE state WHEN 'vetoed' THEN 0 WHEN 'pending' THEN 1 "
            "ELSE 2 END LIMIT 1",
            (url,),
        ).fetchone()
        return row["state"] if row else None

    def requester_of(self, url: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM requests WHERE url = ? ORDER BY created DESC LIMIT 1",
            (url,),
        ).fetchone()

    def mark(self, url: str, state: str) -> None:
        self.db.execute(
            "UPDATE requests SET state = ? WHERE url = ? AND state = 'pending'",
            (state, url),
        )
        self.db.commit()

    def reset_party(self) -> None:
        """Fresh start: forget requests and buckets.

        Keeps the player, playlist and bans -- a ban is a deliberate act and
        shouldn't quietly lapse because the host cleared the history.
        """
        self.db.execute("DELETE FROM requests")
        self.db.execute("DELETE FROM buckets")
        self.db.commit()

    # -- bans --------------------------------------------------------------

    def add_ban(self, ip: str, reason: str = "") -> None:
        self.db.execute(
            "INSERT INTO bans (ip, reason, created) VALUES (?, ?, ?) "
            "ON CONFLICT(ip) DO UPDATE SET reason = excluded.reason",
            (ip, reason, time.time()),
        )
        self.db.commit()

    def remove_ban(self, ip: str) -> None:
        self.db.execute("DELETE FROM bans WHERE ip = ?", (ip,))
        self.db.commit()

    def is_banned(self, ip: str) -> bool:
        if not ip:
            return False
        row = self.db.execute(
            "SELECT 1 FROM bans WHERE ip = ? LIMIT 1", (ip,)
        ).fetchone()
        return row is not None

    def bans(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM bans ORDER BY created DESC"
        ).fetchall()

    # -- rate-limit buckets ------------------------------------------------

    def bucket(self, guest_id: str) -> tuple[float, float] | None:
        row = self.db.execute(
            "SELECT tokens, updated FROM buckets WHERE guest_id = ?", (guest_id,)
        ).fetchone()
        return (row["tokens"], row["updated"]) if row else None

    def save_bucket(self, guest_id: str, tokens: float, updated: float) -> None:
        self.db.execute(
            "INSERT INTO buckets (guest_id, tokens, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(guest_id) DO UPDATE SET "
            "tokens = excluded.tokens, updated = excluded.updated",
            (guest_id, tokens, updated),
        )
        self.db.commit()
