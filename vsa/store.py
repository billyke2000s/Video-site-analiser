"""SQLite persistence: HTTP cache, runs and reports, reviews, recipe versions and the batch queue."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .http import Response

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    url TEXT PRIMARY KEY, stored REAL, status INTEGER, headers TEXT, body BLOB, final_url TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, kind TEXT, site TEXT, input TEXT,
    status TEXT, result TEXT, evidence_path TEXT);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, run_id INTEGER, target TEXT,
    verdict TEXT CHECK (verdict IN ('working', 'wrong')), note TEXT);
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, recipe_id TEXT, version INTEGER,
    status TEXT CHECK (status IN ('candidate', 'working', 'failed', 'superseded')),
    body TEXT, note TEXT, UNIQUE (recipe_id, version));
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, url TEXT, query TEXT, year TEXT,
    status TEXT CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts INTEGER DEFAULT 0, lease REAL, last_error TEXT, result TEXT, updated REAL);
"""


class Store:
    def __init__(self, path: Path | str = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._db.execute(sql, args)
            names = [c[0] for c in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    # --- runs ------------------------------------------------------------------------------
    def add_run(self, kind: str, site: str, input_: Any, status: str, result: Any, evidence_path: str = "") -> int:
        cursor = self.execute("INSERT INTO runs (created, kind, site, input, status, result, evidence_path) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (time.time(), kind, site, json.dumps(input_), status, json.dumps(result), evidence_path))
        return int(cursor.lastrowid)

    def run(self, run_id: int) -> dict | None:
        rows = self.query("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not rows:
            return None
        row = rows[0]
        row["input"], row["result"] = json.loads(row["input"]), json.loads(row["result"])
        return row

    # --- reviews ---------------------------------------------------------------------------
    def add_review(self, run_id: int, target: str, verdict: str, note: str = "") -> int:
        if verdict not in ("working", "wrong"):
            raise ValueError("verdict must be 'working' or 'wrong'")
        cursor = self.execute("INSERT INTO reviews (created, run_id, target, verdict, note) VALUES (?, ?, ?, ?, ?)",
                              (time.time(), run_id, target, verdict, note[:2000]))
        return int(cursor.lastrowid)

    def reviews(self, run_id: int) -> list[dict]:
        return self.query("SELECT * FROM reviews WHERE run_id = ? ORDER BY id", (run_id,))


class SqliteHttpCache:
    """Response cache for page and API GETs (never media bodies)."""

    def __init__(self, store: Store, ttl_seconds: float = 3600):
        self.store = store
        self.ttl = ttl_seconds

    def get(self, url: str) -> Response | None:
        rows = self.store.query("SELECT * FROM cache WHERE url = ?", (url,))
        if not rows or time.time() - rows[0]["stored"] > self.ttl:
            return None
        row = rows[0]
        return Response(url=url, final_url=row["final_url"], status=row["status"], headers=json.loads(row["headers"]),
                        body=row["body"], elapsed_ms=0, from_cache=True)

    def put(self, url: str, response: Response) -> None:
        self.store.execute("INSERT OR REPLACE INTO cache (url, stored, status, headers, body, final_url) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (url, time.time(), response.status, json.dumps(response.headers), response.body,
                            response.final_url))
