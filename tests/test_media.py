"""Media verification against real ffmpeg-generated files served by the local fixture site."""
import unittest

from support import fixture_policy, media_tools
from support.fixture_site import SLUG, FilmsSite, MirrorSite, build_media

from vsa.contracts import MediaCandidate, MediaReport
from vsa.dedup import group
from vsa.http import EvidenceLog, Fetcher
from vsa.media.verify import Verifier


@unittest.skipUnless(media_tools(), "ffmpeg/ffprobe not installed")
class VerifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = build_media()
        cls.site = FilmsSite("127.0.0.1", cls.media).__enter__()
        cls.mirror = MirrorSite("localhost", cls.media).__enter__()
        cls.verifier = Verifier(Fetcher(fixture_policy("127.0.0.1", "localhost"), EvidenceLog()))

    @classmethod
    def tearDownClass(cls):
        cls.site.__exit__()
        cls.mirror.__exit__()

    def verify(self, path, kind="", base=None, **declared) -> MediaReport:
        return self.verifier.verify(MediaCandidate(url=(base or self.site.base) + path, kind=kind, site="fixture",
                                                   declared=declared))

    def test_faststart_mp4(self):
        report = self.verify(f"/media/{SLUG}/720.mp4")
        self.assertEqual(report.status, "playable", report.errors)
        self.assertEqual((report.width, report.height, report.video_codec, report.audio_codec), (1280, 720, "h264", "aac"))
        self.assertAlmostEqual(report.duration, 125, delta=1)
        self.assertGreater(report.decoded_frames, 0)
        self.assertEqual(report.size, (self.media / "720.mp4").stat().st_size)

    def test_mp4_with_index_at_the_end_is_probed_without_downloading_the_film(self):
        before = len(self.site.requests)
        report = self.verify(f"/media/{SLUG}/480.mp4")
        self.assertEqual(report.status, "playable", report.errors)
        self.assertTrue(report.evidence.get("moov_at_end"))
        self.assertEqual(report.height, 480)
        self.assertAlmostEqual(report.duration, 125, delta=1)
        self.assertEqual(len(self.site.requests) - before, 3)  # head, index header, index

    def test_hls_master_reports_variants_and_decodes_a_segment(self):
        report = self.verify(f"/media/{SLUG}/master.m3u8")
        self.assertEqual(report.status, "playable", report.errors)
        self.assertEqual(report.container, "hls")
        self.assertEqual([v.height for v in report.variants], [240, 360])
        self.assertEqual(report.height, 360)
        self.assertAlmostEqual(report.duration, 125, delta=1)

    def test_dash_manifest(self):
        report = self.verify(f"/media/{SLUG}/dash/manifest.mpd")
        self.assertEqual(report.status, "playable", report.errors)
        self.assertEqual(report.container, "dash")
        self.assertAlmostEqual(report.duration, 125, delta=1)
        self.assertEqual(report.height, 360)

    def test_drm_auth_and_html_are_reported_not_bypassed(self):
        self.assertEqual(self.verify("/drm/stream.m3u8").status, "drm")
        self.assertEqual(self.verify("/members/film.mp4").status, "auth_required")
        page = self.verify(f"/watch/{SLUG}", kind="progressive")
        self.assertEqual(page.status, "failed")
        self.assertIn("HTML page", " ".join(page.errors))
        missing = self.verify("/media/none.mp4")
        self.assertIn("HTTP 404", " ".join(missing.errors))

    def test_policy_blocks_media_on_unpermitted_hosts(self):
        verifier = Verifier(Fetcher(fixture_policy("127.0.0.1"), EvidenceLog()))
        report = verifier.verify(MediaCandidate(url=self.mirror.base + "/files/notld.mp4", kind="", site="m"))
        self.assertEqual(report.status, "blocked")

    def test_cross_site_identity_without_merging_different_files_from_one_host(self):
        reports = [self.verify(f"/media/{SLUG}/720.mp4"), self.verify(f"/media/{SLUG}/480.mp4"),
                   self.verifier.verify(MediaCandidate(url=self.mirror.base + "/files/notld.mp4", kind="", site="mirror"))]
        groups = group(reports)
        self.assertEqual(len(groups), 2)
        shared = next(g for g in groups if len(g.members) == 2)
        self.assertTrue(shared.cross_site)
        self.assertEqual({m.candidate.url for m in shared.members},
                         {self.site.base + f"/media/{SLUG}/720.mp4", self.mirror.base + "/files/notld.mp4"})
        other = next(g for g in groups if len(g.members) == 1)
        self.assertIn(shared.media_id, other.related)  # same length, different encode: related, not merged


class DedupUnitTests(unittest.TestCase):
    def report(self, site, url, **kwargs):
        declared = kwargs.pop("declared", {})
        return MediaReport(candidate=MediaCandidate(url=url, kind="progressive", site=site, declared=declared), **kwargs)

    def test_declared_checksums_link_sites_and_bridge_to_byte_fingerprints(self):
        a = self.report("a", "https://a/x.mp4", declared={"sha1": "ab" * 20}, fingerprint={"head_sha256": "h1", "size": "10"})
        b = self.report("b", "https://b/y.mp4", fingerprint={"head_sha256": "h1", "size": "10"})
        c = self.report("c", "https://c/z.mp4", declared={"sha1": "AB" * 20})
        d = self.report("a", "https://a/x2.mp4", fingerprint={"head_sha256": "h1", "size": "11"})
        groups = group([a, b, c, d])
        self.assertEqual(sorted(len(g.members) for g in groups), [1, 3])

    def test_unidentified_reports_are_never_merged(self):
        groups = group([self.report("a", "https://a/1.mp4"), self.report("a", "https://a/1.mp4")])
        self.assertEqual(len(groups), 2)


if __name__ == "__main__":
    unittest.main()
