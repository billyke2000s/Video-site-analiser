import tempfile
import threading
import time
import unittest
from pathlib import Path

from vsa.batch import BatchQueue, BatchSettings
from vsa.contracts import Evidence, MediaCandidate, MediaReport
from vsa.diagnostics import RecipeVersions, Repair, diagnose
from vsa.store import Store


class BatchTests(unittest.TestCase):
    def test_queue_persists_retries_with_backoff_and_respects_concurrency(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "q.sqlite3"
            queue = BatchQueue(Store(path), BatchSettings(concurrency=2, max_attempts=2, backoff_seconds=0.05))
            queue.add_lines(["# comment", "", "https://a.example/\tFilm\t2000", "https://b.example/",
                             "https://c.example/\tBroken"])
            with self.assertRaises(ValueError):
                queue.add("ftp://x")
            # A second queue object on the same file sees the same items (persistence).
            self.assertEqual(BatchQueue(Store(path)).progress()["pending"], 3)
            active, peak, lock = [0], [0], threading.Lock()

            def work(item):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.05)
                with lock:
                    active[0] -= 1
                if item["query"] == "Broken":
                    raise RuntimeError("site down")
                return {"ok": True, "query": item["query"]}

            final = queue.run(work, poll_seconds=0.01)
            self.assertEqual((final["done"], final["failed"], final["pending"]), (2, 1, 0))
            self.assertLessEqual(peak[0], 2)
            failed = next(i for i in queue.items() if i["status"] == "failed")
            self.assertEqual(failed["attempts"], 2)
            self.assertIn("site down", failed["last_error"])

    def test_expired_lease_returns_to_the_queue(self):
        queue = BatchQueue(Store(), BatchSettings(lease_seconds=0.01))
        queue.add("https://a.example/")
        self.assertIsNotNone(queue.lease())
        time.sleep(0.02)
        again = queue.lease()  # the first worker "crashed"; its lease expired
        self.assertEqual(again["attempts"], 2)


class DiagnosticsTests(unittest.TestCase):
    def report(self, status, errors=(), duration=None):
        return MediaReport(candidate=MediaCandidate(url="https://m.example/f.mp4", kind="progressive", site="m"),
                           status=status, errors=list(errors), duration=duration, evidence={"url": "https://m.example/f.mp4"})

    def test_restrictions_have_no_repair_and_timeouts_do(self):
        evidence = [Evidence(0, "GET", "https://s.example/private", 0, error="access_denied", note="disallowed by robots.txt"),
                    Evidence(0, "GET", "https://s.example/slow", 0, error="ReadTimeout")]
        reports = [self.report("drm", ["DRM-protected"]), self.report("auth_required", ["HTTP 401"]),
                   self.report("failed", ["RuntimeError: HTTP 403 for media head"]), self.report("playable", duration=30)]
        found = {f.code: f for f in diagnose(reports, evidence)}
        for code in ("robots", "drm", "auth", "forbidden"):
            self.assertIsNone(found[code].repair, code)
        self.assertEqual(found["timeout"].repair.settings["retries"], 3)
        self.assertEqual(found["short"].severity, "info")

    def test_empty_search_with_broad_matches_proposes_a_recipe_repair(self):
        recipe = {"search": {"link_prefix": "/watch/"}, "inferred": {"broad_matches": 3}}
        found = diagnose([], [], [{"step": "search", "results": []}], recipe)
        self.assertEqual(found[0].code, "search_strict")
        self.assertEqual(found[0].repair.recipe_patch["search"]["link_prefix"], "/")

    def test_moviehub_rejections_are_reported(self):
        found = diagnose([self.report("playable"), self.report("playable")], [], moviehub={
            "sources": [{}], "hostAttempts": [{"provider": "m.example", "status": "rejected_media"}]})
        self.assertEqual(found[0].code, "moviehub_rejected")

    def test_failed_repair_keeps_the_previous_working_version(self):
        versions = RecipeVersions(Store())
        recipe = {"id": "site", "match": {"year_tolerance": 1}}
        versions.promote(recipe, "baseline")
        outcome = versions.try_repair(recipe, Repair("worse", recipe_patch={"match": {"year_tolerance": 5}}),
                                      lambda r, s: {"score": 0}, baseline_score=2)
        self.assertFalse(outcome["promoted"])
        self.assertEqual(versions.working("site")[0], 1)
        good = versions.try_repair(recipe, Repair("better", recipe_patch={"match": {"year_tolerance": 2}}),
                                   lambda r, s: {"score": 3}, baseline_score=2)
        self.assertTrue(good["promoted"])
        version, body = versions.working("site")
        self.assertEqual((version, body["match"]["year_tolerance"]), (3, 2))
        self.assertEqual([h["status"] for h in versions.history("site")], ["superseded", "failed", "working"])


if __name__ == "__main__":
    unittest.main()
