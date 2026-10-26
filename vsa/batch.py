"""Persistent batch queue: URL lists processed with bounded concurrency, retries and leases.

Items survive restarts (SQLite). A worker leases an item; a lease left behind by a crashed
process expires and the item returns to the queue. Failed items are retried with backoff up to
`max_attempts`, then marked failed with their last error.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from .store import Store


@dataclass
class BatchSettings:
    concurrency: int = 2
    max_attempts: int = 3
    lease_seconds: float = 1800.0
    backoff_seconds: float = 30.0


class BatchQueue:
    def __init__(self, store: Store, settings: BatchSettings | None = None):
        self.store = store
        self.settings = settings or BatchSettings()
        self._lock = threading.Lock()

    def add(self, url: str, query: str = "", year: str = "") -> int:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"not an http(s) URL: {url!r}")
        now = time.time()
        cursor = self.store.execute("INSERT INTO queue (created, url, query, year, status, attempts, updated) "
                                    "VALUES (?, ?, ?, ?, 'pending', 0, ?)", (now, url, query, year, now))
        return int(cursor.lastrowid)

    def add_lines(self, lines: list[str]) -> list[int]:
        """Lines of `URL[<TAB>query[<TAB>year]]`; blank lines and #comments are ignored."""
        ids = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("\t")]
            ids.append(self.add(parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))
        return ids

    def lease(self) -> dict | None:
        now = time.time()
        with self._lock:
            rows = self.store.query(
                "SELECT * FROM queue WHERE (status = 'pending' AND (lease IS NULL OR lease <= ?)) "
                "OR (status = 'running' AND lease <= ?) ORDER BY id LIMIT 1", (now, now))
            if not rows:
                return None
            item = rows[0]
            self.store.execute("UPDATE queue SET status = 'running', attempts = attempts + 1, lease = ?, updated = ? "
                               "WHERE id = ?", (now + self.settings.lease_seconds, now, item["id"]))
            item["attempts"] += 1
            return item

    def finish(self, item_id: int, result: dict) -> None:
        self.store.execute("UPDATE queue SET status = 'done', result = ?, lease = NULL, last_error = NULL, updated = ? "
                           "WHERE id = ?", (json.dumps(result, default=str), time.time(), item_id))

    def fail(self, item: dict, error: str) -> None:
        now = time.time()
        if item["attempts"] >= self.settings.max_attempts:
            self.store.execute("UPDATE queue SET status = 'failed', last_error = ?, lease = NULL, updated = ? WHERE id = ?",
                               (error[:2000], now, item["id"]))
        else:
            # Back to pending, but not before the backoff has passed (the lease column doubles as "not before").
            delay = self.settings.backoff_seconds * 2 ** (item["attempts"] - 1)
            self.store.execute("UPDATE queue SET status = 'pending', last_error = ?, lease = ?, updated = ? WHERE id = ?",
                               (error[:2000], now + delay, now, item["id"]))

    def progress(self) -> dict[str, int]:
        counts = {s: 0 for s in ("pending", "running", "done", "failed")}
        for row in self.store.query("SELECT status, COUNT(*) AS n FROM queue GROUP BY status"):
            counts[row["status"]] = row["n"]
        counts["total"] = sum(counts.values())
        return counts

    def items(self, limit: int = 200) -> list[dict]:
        return self.store.query("SELECT id, url, query, year, status, attempts, last_error, updated FROM queue "
                                "ORDER BY id DESC LIMIT ?", (limit,))

    def run(self, work: Callable[[dict], dict], stop_when_idle: bool = True,
            on_progress: Callable[[dict], None] | None = None, poll_seconds: float = 1.0,
            should_stop: Callable[[], bool] = lambda: False) -> dict[str, int]:
        """Process items with `concurrency` workers. `work(item)` returns a result dict or raises."""

        def worker():
            while not should_stop():
                item = self.lease()
                if item is None:
                    if stop_when_idle and not self._waiting():
                        return
                    time.sleep(poll_seconds)
                    continue
                try:
                    result = work(item)
                    if result.get("ok", True):
                        self.finish(item["id"], result)
                    else:
                        self.fail(item, result.get("error", "run incomplete"))
                except Exception as error:  # one bad URL must not stop the batch
                    self.fail(item, f"{type(error).__name__}: {error}")
                if on_progress:
                    on_progress(self.progress())

        with ThreadPoolExecutor(max_workers=max(1, self.settings.concurrency)) as pool:
            futures = [pool.submit(worker) for _ in range(max(1, self.settings.concurrency))]
            for future in futures:
                future.result()  # surface programming errors instead of losing them in a thread
        return self.progress()

    def _waiting(self) -> bool:
        """Items still pending (in backoff) or running elsewhere."""
        counts = self.progress()
        return counts["pending"] > 0 or counts["running"] > 0
