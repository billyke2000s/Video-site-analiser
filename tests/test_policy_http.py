import socket
import unittest

from support.fixture_site import FilmsSite, build_media
from support import fixture_policy

from vsa.http import EvidenceLog, Fetcher, Recorder, redact
from vsa.policy import AccessDenied, AccessPolicy, SitePermission
from vsa.store import SqliteHttpCache, Store


def public_resolver(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]


def private_resolver(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]


class PolicyTests(unittest.TestCase):
    def policy(self, resolver=public_resolver, **kwargs):
        return AccessPolicy([SitePermission(domain="archive.org", reason="test", respect_robots=False, **kwargs)],
                            resolver=resolver)

    def test_unlisted_hosts_are_denied_and_subdomains_follow_the_entry(self):
        policy = self.policy()
        self.assertTrue(policy.check("https://archive.org/x").allowed)
        self.assertTrue(policy.check("https://ia800.us.archive.org/file.mp4").allowed)
        self.assertFalse(policy.check("https://notarchive.org/").allowed)
        self.assertFalse(policy.check("https://archive.org.evil.example/").allowed)

    def test_https_only_unless_permitted_and_no_credentials_in_urls(self):
        policy = self.policy()
        self.assertIn("plain HTTP", policy.check("http://archive.org/").reason)
        self.assertIn("credentials", policy.check("https://user:pw@archive.org/").reason)
        self.assertFalse(policy.check("ftp://archive.org/").allowed)
        self.assertFalse(policy.check("https://archive.org/\r\nx").allowed)

    def test_private_addresses_are_refused(self):
        decision = self.policy(resolver=private_resolver).check("https://archive.org/")
        self.assertFalse(decision.allowed)
        self.assertIn("non-public", decision.reason)

    def test_extra_hosts_and_most_specific_entry(self):
        policy = AccessPolicy([SitePermission(domain="example.org", reason="a", extra_hosts=["*.cdn-example.net"],
                                              respect_robots=False),
                               SitePermission(domain="media.example.org", reason="b", rate_per_second=5,
                                              respect_robots=False)], resolver=public_resolver)
        self.assertTrue(policy.check("https://x.cdn-example.net/a.mp4").allowed)
        self.assertEqual(policy.permission_for("media.example.org").reason, "b")

    def test_robots_txt_is_honoured(self):
        with FilmsSite("127.0.0.1", build_media()) as site:
            policy = fixture_policy("127.0.0.1")
            self.assertTrue(policy.check(site.base + "/watch/x").allowed)
            decision = policy.check(site.base + "/private/secret")
            self.assertFalse(decision.allowed)
            self.assertIn("robots", decision.reason)


class FetcherTests(unittest.TestCase):
    def test_redaction_keeps_structure_and_hides_tokens(self):
        url = "https://cdn.example/v.m3u8?token=abc&expires=123&q=night&name=" + "x" * 60
        out = redact(url)
        self.assertIn("q=night", out)
        self.assertNotIn("abc", out)
        self.assertNotIn("123", out)
        self.assertNotIn("x" * 60, out)

    def test_policy_applies_to_every_request_and_is_logged(self):
        with FilmsSite("127.0.0.1", build_media()) as site:
            log = EvidenceLog()
            fetcher = Fetcher(fixture_policy("127.0.0.1"), log)
            with self.assertRaises(AccessDenied):
                fetcher.get(site.base + "/private/secret")
            self.assertEqual(log.rows[-1].error, "access_denied")
            other = Fetcher(fixture_policy("localhost"), EvidenceLog())
            with self.assertRaises(AccessDenied):
                other.get(site.base + "/")

    def test_size_limit_ranges_cache_and_recording(self):
        with FilmsSite("127.0.0.1", build_media()) as site:
            store = Store()
            recorder = Recorder()
            fetcher = Fetcher(fixture_policy("127.0.0.1"), EvidenceLog(), cache=SqliteHttpCache(store), recorder=recorder)
            media = site.base + "/media/night-of-the-living-dead-1968/720.mp4"
            head = fetcher.get(media, byte_range=(0, 999), max_bytes=1000)
            self.assertEqual(head.status, 206)
            self.assertEqual(len(head.body), 1000)
            capped = fetcher.get(media, max_bytes=5000, use_cache=False)
            self.assertTrue(capped.truncated)
            self.assertEqual(len(capped.body), 5000)
            first = fetcher.get(site.base + "/")
            second = fetcher.get(site.base + "/")
            self.assertFalse(first.from_cache)
            self.assertTrue(second.from_cache)
            self.assertEqual(sum(1 for path in site.requests if path == "/"), 1)
            self.assertEqual(recorder.entries[0]["range"], "bytes=0-999")


if __name__ == "__main__":
    unittest.main()
