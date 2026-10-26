"""Generated adapters against MovieHub's real code (needs a MovieHub checkout; see TESTING.md)."""
import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from support import MOVIEHUB, MOVIEHUB_PYTHON, moviehub_available
from support.fixture_site import build_media

from vsa.media import mp4
from vsa.moviehub import contract, generate, harness, integrate
from vsa.plugins.base import builtin_recipes

ARCHIVE = next(r for r in builtin_recipes() if r["id"] == "archive_org")


class GeneratorTests(unittest.TestCase):
    def test_adapter_compiles_and_embeds_the_interpreter_verbatim(self):
        source = generate.render_adapter(ARCHIVE)
        compile(source, "archive_org.py", "exec")
        core = Path(generate.__file__).resolve().parents[1] / "recipe_core.py"
        body = core.read_text()
        self.assertIn(body[body.index('"""', 3) + 3:].strip(), source)
        self.assertIn("class source:", source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)

    def test_recipe_validation(self):
        for change, message in ((dict(id="9bad"), "module name"), (dict(base="http://x"), "https"),
                                (dict(series=True), "series"), (dict(kind="other"), "kind"),
                                (dict(inferred={"media_needed_rendering": True}), "JavaScript")):
            with self.subTest(change=change), self.assertRaisesRegex(generate.GenerationError, message):
                generate.validate_recipe(dict(ARCHIVE, **change))
        with self.assertRaisesRegex(generate.GenerationError, "already has"):
            generate.validate_recipe(ARCHIVE, {"native:archive_org"})

    def test_provider_record_insertion_keeps_moviehub_layout(self):
        text = ("PROVIDERS = [dict(id='native:a', module='moviehub_native.a', family='native',\n"
                "    name='a', site='a.example', movies=True, series=False)]\n")
        out = generate.add_record_to_providers(text, generate.provider_record(ARCHIVE))
        namespace = {}
        exec(out, namespace)
        self.assertEqual([r["id"] for r in namespace["PROVIDERS"]], ["native:a", "native:archive_org"])
        self.assertEqual(namespace["PROVIDERS"][1], generate.provider_record(ARCHIVE))

    def test_inventory_counts_update_only_exact_assertions(self):
        text = ("        self.assertEqual(len(everything), 65)\n        self.assertEqual(len(rows), 36)\n"
                "        self.assertEqual(vod.validate(), 70)\nfrom test_native_vod_sites import NativeSiteTests\n")
        update = integrate.update_python_inventory(text, ARCHIVE, site_exists=False)
        self.assertIn("len(everything), 66", update.text)
        self.assertIn("len(rows), 37", update.text)
        self.assertIn("vod.validate(), 71", update.text)
        self.assertIn("from test_native_archive_org import GeneratedArchiveOrgTests", update.text)
        self.assertTrue(update.manual)  # assertions missing from this sample are reported, not guessed
        existing = integrate.update_python_inventory(text, ARCHIVE, site_exists=True)
        self.assertIn("len(everything), 65", existing.text)
        self.assertIn("vod.validate(), 71", existing.text)


@unittest.skipUnless(moviehub_available(), "MovieHub checkout or its Python requirements not available")
class MovieHubContractTests(unittest.TestCase):
    def test_contract_is_read_from_the_checkout(self):
        found = contract.inspect_checkout(MOVIEHUB)
        self.assertIn("native:vixsrc", found.native_ids)
        self.assertEqual(found.calls["movie"], ["self", "imdb", "tmdb", "title", "localtitle", "aliases", "year"])

    def test_changed_contract_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "moviehub"
            shutil.copytree(MOVIEHUB / "android-tv-launcher/app/src/main/python",
                            copy / "android-tv-launcher/app/src/main/python")
            vod = copy / "android-tv-launcher/app/src/main/python/moviehub_vod.py"
            vod.write_text(vod.read_text().replace("def query_site(provider_id, raw, cancelled, emit=None)",
                                                   "def query_site(provider_id, raw)"))
            with self.assertRaisesRegex(contract.ContractMismatch, "query_site"):
                contract.inspect_checkout(copy)


def _entry(url, body, status=200, headers=None, final_url=None, byte_range=None):
    return dict(method="GET", url=url, final_url=final_url or url, range=byte_range, status=status,
                headers=headers or {}, body=base64.b64encode(body).decode())


@unittest.skipUnless(moviehub_available(), "MovieHub checkout or its Python requirements not available")
class ArchiveAdapterOfflineTests(unittest.TestCase):
    """The archive.org adapter through MovieHub's query_site(), on synthetic archive-shaped responses.

    The response shapes follow the recipe's assumptions about the public API. Only the live CI job
    (tests/live/test_archive_live.py) checks them against the real site.
    """

    def test_restricted_items_private_files_and_short_media_are_excluded(self):
        media = build_media()
        film = (media / "480.mp4").read_bytes()  # index stored at the end, like many archive.org uploads
        search = {"response": {"docs": [
            {"identifier": "night_of_the_living_dead", "title": "Night of the Living Dead", "year": "1968"},
            {"identifier": "notld_lending", "title": "Night of the Living Dead", "year": "1968"},
            {"identifier": "notld_remake", "title": "Night of the Living Dead", "year": "1990"}]}}
        detail = {"metadata": {"identifier": "night_of_the_living_dead"}, "files": [
            {"name": "notld.mp4", "format": "h.264", "height": "480", "sha1": "a" * 40},
            {"name": "notld_private.mp4", "format": "h.264", "private": "true"},
            {"name": "notld.ogv", "format": "Ogg Video"}, {"name": "notld.jpg", "format": "JPEG"}]}
        restricted = {"metadata": {"access-restricted-item": "true"}, "files": [{"name": "x.mp4", "format": "h.264"}]}
        search_url = generate_search_url()
        node = "https://ia800.us.archive.org/1/items/night_of_the_living_dead/notld.mp4"
        offset = mp4.next_box_offset(film[:65536])  # where the index (moov) starts
        entries = [
            _entry(search_url, json.dumps(search).encode(), headers={"content-type": "application/json"}),
            _entry("https://archive.org/metadata/night_of_the_living_dead", json.dumps(detail).encode(),
                   headers={"content-type": "application/json"}),
            _entry("https://archive.org/metadata/notld_lending", json.dumps(restricted).encode(),
                   headers={"content-type": "application/json"}),
            _entry("https://archive.org/download/night_of_the_living_dead/notld.mp4", film[:65536], 206,
                   {"content-type": "video/mp4", "content-range": f"bytes 0-65535/{len(film)}"}, node, "bytes=0-65535"),
            _entry("https://archive.org/download/night_of_the_living_dead/notld.mp4", film[offset:offset + 65536], 206,
                   {"content-type": "video/mp4", "content-range": f"bytes {offset}-{len(film) - 1}/{len(film)}"}, node,
                   f"bytes={offset}-{offset + 65535}"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            generate.write_package(ARCHIVE, folder / "pkg", None)
            replay = folder / "replay.json"
            replay.write_text(json.dumps(dict(entries=entries)))
            tree = harness.stage(MOVIEHUB, folder / "pkg", ARCHIVE)
            try:
                info = dict(imdb="tt0063350", tmdb=10331, title="Night of the Living Dead", year="1968", season=-1,
                            episode=-1, http=False)
                result = harness.run_query(tree, "native:archive_org", info, python=MOVIEHUB_PYTHON, replay=replay)
            finally:
                tree.cleanup()
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(result["sources"]), 1)
        source = result["sources"][0]
        self.assertEqual(source["url"], "ia800.us.archive.org")  # MovieHub followed the redirect itself
        self.assertEqual((source["videoHeight"], source["quality"]), (480, "480p"))
        self.assertTrue(source["qualityVerified"])
        self.assertAlmostEqual(source["durationSeconds"], 125, delta=1)


def generate_search_url():
    from vsa import recipe_core

    return recipe_core.search_request(ARCHIVE, {"title": "Night of the Living Dead", "year": "1968"})


if __name__ == "__main__":
    unittest.main()
