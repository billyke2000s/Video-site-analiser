"""The complete workflow on the local fixture sites, through MovieHub's real code."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import MOVIEHUB, MOVIEHUB_PYTHON, fixture_policy, media_tools, moviehub_available
from support.fixture_site import FilmsSite, MirrorSite, build_media, json_recipe

from vsa.engine import EngineSettings
from vsa.pipeline import Pipeline, PipelineRequest


@unittest.skipUnless(media_tools() and moviehub_available(), "needs ffmpeg and a MovieHub checkout")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = build_media()

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="vsa-e2e-"))

    def run_pipeline(self, policy, settings=None, **request):
        request.setdefault("moviehub", MOVIEHUB)
        request.setdefault("python", MOVIEHUB_PYTHON)
        return Pipeline(policy, self.folder, settings).run(PipelineRequest(**request))

    def step(self, run, name):
        return next(s for s in run.steps if s["step"] == name)

    def test_generic_site_url_to_moviehub_integration_patch(self):
        with FilmsSite("127.0.0.1", self.media) as site:
            run = self.run_pipeline(fixture_policy("127.0.0.1"), url=site.base + "/", query="Night of the Living Dead",
                                    year="1968", adapter_id="example_films")
        self.assertTrue(run.ok, json.dumps(run.steps, indent=1, default=str)[-3000:])
        self.assertEqual(self.step(run, "analyse")["plugin"], "generic")
        self.assertEqual(len(self.step(run, "search")["results"]), 1)  # the 1990 remake is not selected
        verify = self.step(run, "verify")
        self.assertEqual((verify["playable"], verify["total"], verify["distinct_media"]), (4, 4, 4))
        query = self.step(run, "moviehub_query")
        self.assertEqual(query["comparison"], dict(analyser_playable=4, moviehub_sources=4, matched=4))
        self.assertTrue(all(s["passed"] for s in self.step(run, "moviehub_tests")["suites"]))
        patch = run.dir / self.step(run, "integration_patch")["patch"]
        self.assertIn("moviehub_native/example_films.py", patch.read_text())
        # The patch applies cleanly to an unmodified MovieHub checkout.
        check = subprocess.run(["git", "-C", str(MOVIEHUB), "apply", "--check", str(patch)], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        for name in ("evidence.jsonl", "profile.json", "media.json", "moviehub_result.json", "suites.txt", "summary.md"):
            self.assertTrue((run.dir / name).exists(), name)

    def test_plain_form_site_is_analysed_without_site_knowledge(self):
        with MirrorSite("localhost", self.media) as mirror:
            run = self.run_pipeline(fixture_policy("localhost"), url=mirror.base + "/", query="Night of the Living Dead",
                                    year="1968", adapter_id="mirror_films", integration_patch=False)
        self.assertTrue(run.ok, json.dumps(run.steps, default=str)[-2000:])
        self.assertEqual(run.profile.search_interfaces[0].kind, "html_form")
        self.assertIn("lang=en", run.profile.search_interfaces[0].url_template)
        self.assertEqual(run.recipe["search"]["link_prefix"], "/v/")

    def test_recipe_site_from_a_user_recipe_folder(self):
        with FilmsSite("127.0.0.1", self.media) as site:
            recipes = self.folder / "recipes"
            recipes.mkdir()
            (recipes / "example_api.json").write_text(json.dumps(json_recipe(site.base, "127.0.0.1")))
            run = self.run_pipeline(fixture_policy("127.0.0.1"), EngineSettings(recipe_dirs=[recipes]),
                                    url=site.base + "/", query="Night of the Living Dead", year="1968",
                                    integration_patch=False)
        self.assertTrue(run.ok, json.dumps(run.steps, default=str)[-2000:])
        self.assertEqual(self.step(run, "analyse")["plugin"], "recipe:example_api")
        # The members-only item is listed by search but rejected at detail; the JPEG is not media.
        self.assertEqual(self.step(run, "discover")["candidates"], 2)
        groups = json.loads((run.dir / "media.json").read_text())["groups"]
        self.assertTrue(all(g["media_id"].startswith("sha1:") for g in groups))

    def test_cancelled_by_policy_when_site_not_permitted(self):
        with FilmsSite("127.0.0.1", self.media) as site:
            run = self.run_pipeline(fixture_policy("localhost"), url=site.base + "/", query="x")
        self.assertFalse(run.ok)
        self.assertIn("AccessDenied", run.steps[-1]["error"])


if __name__ == "__main__":
    unittest.main()
