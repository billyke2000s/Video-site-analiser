"""Live end-to-end run against the real Internet Archive (opt-in: VSA_LIVE=1).

URL -> analyse -> search -> select -> discover -> verify -> generate adapter -> MovieHub's real
moviehub_vod.query_site() on the real network -> MovieHub test suites -> integration patch.
The evidence bundle is written to VSA_LIVE_OUT (default: ./live-evidence) for inspection.
"""
import json
import os
import unittest
from pathlib import Path

from support import MOVIEHUB, MOVIEHUB_PYTHON, media_tools, moviehub_available

from vsa.pipeline import PipelineRequest
from vsa.workspace import Workspace


@unittest.skipUnless(os.environ.get("VSA_LIVE") == "1", "live test: set VSA_LIVE=1 (contacts archive.org)")
class LiveArchiveTests(unittest.TestCase):
    def test_night_of_the_living_dead_end_to_end(self):
        self.assertTrue(media_tools(), "ffmpeg/ffprobe required")
        self.assertTrue(moviehub_available(), "MovieHub checkout and requirements required")
        ws = Workspace(Path(os.environ.get("VSA_LIVE_OUT", "live-evidence")))
        run = ws.pipeline(PipelineRequest(
            url="https://archive.org/", query="Night of the Living Dead", year="1968", imdb="tt0063350",
            tmdb="10331", moviehub=MOVIEHUB, moviehub_mode="live", python=MOVIEHUB_PYTHON))
        print((run.dir / "summary.md").read_text())
        if not run.ok:
            print(json.dumps([f.__dict__ for f in ws.diagnose(run)], default=str, indent=1))
        self.assertTrue(run.ok, f"see {run.dir}")
        verify = next(s for s in run.steps if s["step"] == "verify")
        self.assertGreaterEqual(verify["playable"], 1)
        query = next(s for s in run.steps if s["step"] == "moviehub_query")
        self.assertEqual(query["moviehub_status"], "complete")


if __name__ == "__main__":
    unittest.main()
