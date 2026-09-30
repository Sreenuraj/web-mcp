"""SQLite persistence: sessions, daily counters, approvals (state.db) and content cache (cache.db)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, data TEXT NOT NULL, last_active REAL NOT NULL, ended INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS counters(day TEXT NOT NULL, key TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day,key));
CREATE TABLE IF NOT EXISTS session_log(ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL,
  options TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', decision TEXT, reply TEXT, created REAL NOT NULL,
  decided REAL, consumed INTEGER NOT NULL DEFAULT 0);
"""
_CACHE_SCHEMA = "CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL);"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


class Storage:
    def __init__(self, state_path: Path, cache_path: Path | None):
        self._lock = threading.RLock()
        self.state = _connect(state_path)
        cols = {r["name"] for r in self.state.execute("PRAGMA table_info(approvals)")}
        if cols and "key" not in cols:  # pre-0.2 layout: approvals are short-lived, safe to rebuild
            self.state.execute("DROP TABLE approvals")
        self.state.executescript(_STATE_SCHEMA)
        self.cache = None
        if cache_path is not None:
            self.cache = _connect(cache_path)
            self.cache.executescript(_CACHE_SCHEMA)

    # ---- sessions -------------------------------------------------------------------------
    def save_session(self, sid: str, data: dict, last_active: float, ended: bool) -> None:
        with self._lock:
            self.state.execute(
                "INSERT INTO sessions(id,data,last_active,ended) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data,last_active=excluded.last_active,ended=excluded.ended",
                (sid, json.dumps(data), last_active, int(ended)),
            )
            self.state.commit()

    def load_session(self, sid: str) -> dict | None:
        with self._lock:
            row = self.state.execute("SELECT data FROM sessions WHERE id=?", (sid,)).fetchone()
        return json.loads(row["data"]) if row else None

    def count_active_sessions(self, since: float) -> int:
        with self._lock:
            return self.state.execute(
                "SELECT COUNT(*) c FROM sessions WHERE ended=0 AND last_active>=?", (since,)
            ).fetchone()["c"]

    def log_session_start(self, ts: float) -> None:
        with self._lock:
            self.state.execute("INSERT INTO session_log(ts) VALUES(?)", (ts,))
            self.state.execute("DELETE FROM session_log WHERE ts<?", (ts - 86400,))
            self.state.commit()

    def count_sessions_since(self, ts: float) -> int:
        with self._lock:
            return self.state.execute("SELECT COUNT(*) c FROM session_log WHERE ts>=?", (ts,)).fetchone()["c"]

    def purge_sessions(self, older_than: float) -> None:
        with self._lock:
            self.state.execute("DELETE FROM sessions WHERE last_active<?", (older_than,))
            self.state.commit()

    # ---- counters -------------------------------------------------------------------------
    def incr(self, day: str, key: str, n: int = 1) -> int:
        with self._lock:
            self.state.execute(
                "INSERT INTO counters(day,key,n) VALUES(?,?,?) ON CONFLICT(day,key) DO UPDATE SET n=n+excluded.n",
                (day, key, n),
            )
            self.state.commit()
            return self.get_counter(day, key)

    def get_counter(self, day: str, key: str) -> int:
        with self._lock:
            row = self.state.execute("SELECT n FROM counters WHERE day=? AND key=?", (day, key)).fetchone()
        return int(row["n"]) if row else 0

    # ---- approvals ------------------------------------------------------------------------
    def create_approval(self, aid: str, session_id: str, kind: str, key: str, summary: str, options: list[str]) -> None:
        with self._lock:
            self.state.execute(
                "INSERT INTO approvals(id,session_id,kind,key,summary,options,created) VALUES(?,?,?,?,?,?,?)",
                (aid, session_id, kind, key, summary, json.dumps(options), time.time()),
            )
            self.state.commit()

    def get_approval(self, aid: str) -> dict | None:
        with self._lock:
            row = self.state.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        return dict(row) if row else None

    def pending_approvals(self) -> list[dict]:
        with self._lock:
            rows = self.state.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY created").fetchall()
        return [dict(r) for r in rows]

    def find_pending(self, session_id: str, kind: str, key: str, min_created: float) -> dict | None:
        with self._lock:
            row = self.state.execute(
                "SELECT * FROM approvals WHERE session_id=? AND kind=? AND key=? AND status='pending' AND created>=? "
                "ORDER BY created DESC LIMIT 1",
                (session_id, kind, key, min_created),
            ).fetchone()
        return dict(row) if row else None

    def decide_approval(self, aid: str, decision: str, reply: str, consumed: bool = False) -> bool:
        """Record the user's decision once. Returns False if it was not pending (already answered)."""
        with self._lock:
            cur = self.state.execute(
                "UPDATE approvals SET status='decided', decision=?, reply=?, decided=?, consumed=? "
                "WHERE id=? AND status='pending'",
                (decision, reply, time.time(), int(consumed), aid),
            )
            self.state.commit()
            return cur.rowcount > 0

    def take_decided(self, session_id: str, kind: str, key: str, max_age: float) -> dict | None:
        """Atomically consume (single use) a user decision made for exactly this request."""
        with self._lock:
            row = self.state.execute(
                "SELECT * FROM approvals WHERE session_id=? AND kind=? AND key=? AND status='decided' "
                "AND consumed=0 AND decided>=? ORDER BY decided DESC LIMIT 1",
                (session_id, kind, key, time.time() - max_age),
            ).fetchone()
            if not row:
                return None
            self.state.execute("UPDATE approvals SET consumed=1 WHERE id=?", (row["id"],))
            self.state.commit()
            return dict(row)

    def expire_approvals(self, older_than: float) -> None:
        with self._lock:
            self.state.execute("DELETE FROM approvals WHERE created<?", (older_than,))
            self.state.commit()

    # ---- cache ----------------------------------------------------------------------------
    def cache_get(self, key: str) -> Any | None:
        if self.cache is None:
            return None
        with self._lock:
            row = self.cache.execute("SELECT value,expires FROM cache WHERE key=?", (key,)).fetchone()
        if not row or row["expires"] < time.time():
            return None
        return json.loads(row["value"])

    def cache_put(self, key: str, value: Any, ttl_seconds: float) -> None:
        if self.cache is None:
            return
        with self._lock:
            self.cache.execute(
                "INSERT INTO cache(key,value,expires) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires=excluded.expires",
                (key, json.dumps(value), time.time() + ttl_seconds),
            )
            self.cache.commit()

    def cache_purge(self, everything: bool = False) -> int:
        if self.cache is None:
            return 0
        with self._lock:
            cur = self.cache.execute("DELETE FROM cache" + ("" if everything else " WHERE expires<?"), () if everything else (time.time(),))
            self.cache.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self.state.close()
            if self.cache is not None:
                self.cache.close()
