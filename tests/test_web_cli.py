import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from support import MOVIEHUB, MOVIEHUB_PYTHON, media_tools, moviehub_available
from support.fixture_site import SLUG, FilmsSite, build_media

from vsa import cli
from vsa.capabilities import render
from vsa.http import EvidenceLog
from vsa.policy import SitePermission
from vsa.web.server import serve
from vsa.workspace import Workspace


def fixture_permission(host):
    return SitePermission(domain=host, reason="local fixture", rate_per_second=0, allow_http=True,
                          allow_private_network=True)


@unittest.skipUnless(media_tools(), "ffmpeg/ffprobe not installed")
class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.ws = Workspace(Path(cls.folder.name) / "data")
        cls.ws.permit(fixture_permission("127.0.0.1"))
        cls.site = FilmsSite("127.0.0.1", build_media()).__enter__()
        moviehub = MOVIEHUB if moviehub_available() else None
        cls.server, cls.app = serve(cls.ws, 0, moviehub, MOVIEHUB_PYTHON)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.site.__exit__()
        cls.folder.cleanup()

    def call(self, path, body=None, token=None, host=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + "/api/" + path, data=data, method="POST" if data else "GET")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-VSA-Token", self.app.token if token is None else token)
        if host:
            request.add_header("Host", host)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_page_and_request_protection(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            page = response.read().decode()
        self.assertIn(self.app.token, page)
        self.assertEqual(self.call("analyse", {"url": "x"}, token="wrong")[0], 403)
        self.assertEqual(self.call("state", host="evil.example")[0], 403)
        self.assertEqual(self.call("nothing", {})[0], 404)
        status, body = self.call("analyse", {"url": "https://not-permitted.example/"})
        self.assertEqual(status, 403)
        self.assertIn("Access policy", body["error"])

    def test_interactive_workflow_review_and_diagnosis(self):
        status, analysed = self.call("analyse", {"url": self.site.base + "/"})
        self.assertEqual(status, 200, analysed)
        run = analysed["run"]
        status, found = self.call("search", {"run": run, "query": "Night of the Living Dead", "year": "1968"})
        self.assertEqual(len(found["results"]), 1)
        status, discovered = self.call("discover", {"run": run, "indices": [0]})
        self.assertEqual(len(discovered["candidates"]), 4)
        status, verified = self.call("verify", {"run": run})
        self.assertEqual(sorted(r["status"] for r in verified["reports"]), ["playable"] * 4)
        status, reviewed = self.call("review", {"run": run, "verdict": "wrong", "target": verified["reports"][0]["candidate"]["url"],
                                                "note": "picture freezes"})
        self.assertEqual(status, 200)
        self.assertIn("findings", reviewed)
        self.assertEqual(self.ws.store.query("SELECT verdict, note FROM reviews")[-1], {"verdict": "wrong", "note": "picture freezes"})
        status, files = self.call(f"run/{run}")
        self.assertIn("reviews.jsonl", files["files"])
        if self.app.moviehub:
            status, generated = self.call("generate", {"run": run, "adapter_id": "web_fixture", "patch": False})
            self.assertTrue(generated["ok"], generated)

    def test_batch_queue_via_api(self):
        status, added = self.call("batch/add", {"lines": f"{self.site.base}/\n# comment\n"})
        self.assertEqual(added["added"], 1)
        status, started = self.call("batch/start", {"concurrency": 1})
        self.assertTrue(started["started"])
        self.app.batch_thread.join(120)
        status, state = self.call("batch")
        self.assertEqual(state["progress"]["done"], 1, state)


class CliTests(unittest.TestCase):
    def test_permit_and_contract_commands(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["--data-dir", folder, "permit", "example.org", "--reason", "test"])
            self.assertIn("example.org", out.getvalue())
            saved = json.loads((Path(folder) / "permissions.json").read_text())
            self.assertEqual({s["domain"] for s in saved["sites"]}, {"archive.org", "example.org"})
            if moviehub_available():
                cli.main(["contract", str(MOVIEHUB)])
                self.assertIn("native:vixsrc", out.getvalue())


@unittest.skipUnless(render.available() and render.browser_executable(), "Playwright or Chrome not available")
class RenderTests(unittest.TestCase):
    def test_script_inserted_media_is_found_and_unpermitted_requests_are_blocked(self):
        from support import fixture_policy

        with FilmsSite("127.0.0.1", build_media()) as site:
            log = EvidenceLog()
            page = render.render(site.base + "/js-player", fixture_policy("127.0.0.1"), log, settle_ms=500)
        self.assertIn(f"/media/{SLUG}/720.mp4", page.html)
        self.assertTrue(any(url.endswith("/720.mp4") for url in page.network_media))
        self.assertEqual(page.blocked, ["https://tracker.invalid/pixel.gif"])
        self.assertTrue(any(row.error == "access_denied" and "tracker.invalid" in row.url for row in log.rows))


if __name__ == "__main__":
    unittest.main()
