import json
import unittest

from vsa import recipe_core
from vsa.capabilities import html, opensearch
from vsa.media import manifest, mp4


class HtmlTests(unittest.TestCase):
    def test_search_interfaces_from_schema_org_opensearch_and_forms(self):
        ld = {"@context": "https://schema.org", "@graph": [{"@type": "WebSite", "potentialAction": {
            "@type": "SearchAction", "target": {"urlTemplate": "https://s.example/find?k={term}"}}}]}
        page = f"""<html><head><link rel="search" type="application/opensearchdescription+xml" href="/os.xml">
        <script type="application/ld+json">{json.dumps(ld)}</script></head><body>
        <form action="/search" method="get"><input type="hidden" name="type" value="film"><input name="q"></form>
        <form action="/login" method="post"><input name="user"></form>
        <form action="/subscribe"><input name="email" type="email"></form></body></html>"""
        found = html.search_interfaces(html.parse(page, "https://s.example/home"))
        kinds = [(s.kind, s.url_template) for s in found]
        self.assertEqual(kinds[0], ("schema_search_action", "https://s.example/find?k={query}"))
        self.assertIn(("opensearch", "https://s.example/os.xml"), kinds)
        self.assertIn(("html_form", "https://s.example/search?type=film&q={query}"), kinds)
        self.assertEqual(len(found), 3)  # login (POST) and newsletter forms are not search forms

    def test_media_from_tags_structured_data_metadata_and_escaped_scripts(self):
        page = """<html><head><meta property="og:video" content="https://cdn.example/og.mp4">
        <script type="application/ld+json">{"@type":"VideoObject","contentUrl":"/v/main.m3u8"}</script></head>
        <body><video><source src="/v/a.mp4" label="720p"><track src="/subs.vtt"></video>
        <script>var cfg = {"dash":"https:\\/\\/cdn.example\\/x\\/manifest.mpd?sig=1"};</script></body></html>"""
        urls = html.media_urls(html.parse(page, "https://site.example/watch/1"))
        self.assertEqual(set(urls), {"https://site.example/v/a.mp4", "https://site.example/v/main.m3u8",
                                     "https://cdn.example/og.mp4", "https://cdn.example/x/manifest.mpd?sig=1"})

    def test_malformed_markup_does_not_raise(self):
        page = html.parse("<html><a href='/x'>unterminated <script>var a = '<b>", "https://e.example/")
        self.assertEqual(page.title, "")

    def test_opensearch_description(self):
        xml = """<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
        <Url type="text/html" template="https://e.example/s?q={searchTerms}&amp;p={startPage?}"/>
        <Url type="application/x-suggestions+json" template="https://e.example/sug?q={searchTerms}"/>
        </OpenSearchDescription>"""
        found = opensearch.parse_description(xml, "https://e.example/os.xml")
        self.assertEqual([s.url_template for s in found], ["https://e.example/s?q={query}"])
        self.assertEqual(opensearch.parse_description("<!DOCTYPE x [<!ENTITY a 'b'>]><x/>", "https://e.example/"), [])


class ManifestTests(unittest.TestCase):
    def test_hls_master_variants_and_drm_detection(self):
        master = manifest.parse_hls('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2"\n'
                                    'hd/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=640x360\nsd.m3u8\n',
                                    "https://c.example/v/master.m3u8")
        self.assertTrue(master.master)
        self.assertEqual([(v.height, v.uri) for v in master.variants],
                         [(720, "https://c.example/v/hd/index.m3u8"), (360, "https://c.example/v/sd.m3u8")])
        media = manifest.parse_hls("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"k.bin\"\n#EXTINF:9.5,\na.ts\n#EXTINF:10,\nb.ts\n"
                                   "#EXT-X-ENDLIST\n", "https://c.example/v/")
        self.assertAlmostEqual(media.duration, 19.5)
        self.assertTrue(media.vod)
        self.assertFalse(media.drm)  # clear AES-128 with a fetchable key is not DRM
        drm = manifest.parse_hls('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://x",KEYFORMAT="com.apple.streamingkeydelivery"\n'
                                 "#EXTINF:10,\na.ts\n", "https://c.example/")
        self.assertTrue(drm.drm)
        widevine = manifest.parse_hls('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="data:x",KEYFORMAT="urn:uuid:edef8ba9"\n#EXTINF:10,\na.ts\n',
                                      "https://c.example/")
        self.assertTrue(widevine.drm)
        with self.assertRaises(ValueError):
            manifest.parse_hls("<html>", "https://c.example/")

    def test_dash_templates_duration_and_protection(self):
        mpd = """<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT1H2M3.5S">
        <Period><AdaptationSet mimeType="video/mp4"><SegmentTemplate initialization="init-$RepresentationID$.m4s"
        media="chunk-$RepresentationID$-$Number%05d$.m4s" startNumber="1"/>
        <Representation id="0" bandwidth="500000" width="1280" height="720" codecs="avc1.64001f"/></AdaptationSet>
        <AdaptationSet mimeType="audio/mp4"><Representation id="1" bandwidth="64000"><BaseURL>audio.mp4</BaseURL>
        <SegmentBase indexRange="0-100"/></Representation></AdaptationSet></Period></MPD>"""
        parsed = manifest.parse_dash(mpd, "https://c.example/d/manifest.mpd")
        self.assertAlmostEqual(parsed.duration, 3723.5)
        self.assertFalse(parsed.drm)
        video = parsed.representations[0]
        self.assertEqual(video.init_url, "https://c.example/d/init-0.m4s")
        self.assertEqual(video.first_segment_url, "https://c.example/d/chunk-0-00001.m4s")
        self.assertEqual(parsed.representations[1].base_url, "https://c.example/d/audio.mp4")
        protected = mpd.replace("<Representation id=\"0\"", "<ContentProtection schemeIdUri=\"urn:uuid:x\"/><Representation id=\"0\"")
        self.assertTrue(manifest.parse_dash(protected, "https://c.example/").drm)
        with self.assertRaises(ValueError):
            manifest.parse_dash("<!DOCTYPE MPD [<!ENTITY a 'b'>]><MPD/>", "https://c.example/")

    def test_mp4_box_walk_finds_index_after_media(self):
        data = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00\x10\x00\x00mdat" + b"\x00" * 100
        self.assertEqual([b[0] for b in mp4.top_level_boxes(data)], ["ftyp", "mdat"])
        self.assertEqual(mp4.next_box_offset(data), 24 + 0x100000)
        self.assertTrue(mp4.is_mp4(data))


class RecipeCoreTests(unittest.TestCase):
    RECIPE = {"schema": 1, "id": "t", "site": "api.example", "base": "https://api.example", "kind": "json_api",
              "movies": True, "series": False, "match": {"year_tolerance": 1, "max_items": 5},
              "search": {"url": "https://api.example/search", "params": [["q", "title:({title|terms})"], ["f[]", "id"]],
                         "results": "res.docs", "fields": {"id": "identifier", "title": "title", "year": "year"}},
              "detail": {"url": "https://api.example/meta/{id|path}", "reject": [{"path": "meta.restricted", "values": ["true"]}],
                         "files": "files", "fields": {"name": "name", "format": "format", "height": "height", "private": "private"},
                         "include": {"format": ["MPEG4"]}, "exclude": {"private": ["true"]},
                         "media_url": "https://api.example/dl/{id|path}/{name|path}", "label": "{format}", "server": "api.example"}}

    def test_template_filters_and_title_rules(self):
        self.assertEqual(recipe_core.fill("{t|query}|{t|path}|{t|terms}", {"t": "Dr. Strangelove: Or/How"}),
                         "Dr.+Strangelove%3A+Or%2FHow|Dr.%20Strangelove%3A%20Or/How|Dr Strangelove Or How")
        self.assertTrue(recipe_core.title_ok("The Night of the Living Dead (1968)", ["Night of the Living Dead"]))
        self.assertTrue(recipe_core.title_ok("Amélie", ["Amelie"]))
        self.assertFalse(recipe_core.title_ok("Night of the Living Dead 3D", ["Night of the Living Dead"]))
        self.assertFalse(recipe_core.title_ok("Blade Runner 2049", ["Blade Runner"]))
        self.assertTrue(recipe_core.year_ok("1969", "1968", 1))
        self.assertFalse(recipe_core.year_ok("1990", "1968", 1))
        self.assertTrue(recipe_core.year_ok("", "1968", 0))
        self.assertEqual(recipe_core.quality("1080"), "1080p")
        self.assertEqual(recipe_core.quality(None), "SD")

    def test_json_api_search_and_files(self):
        url = recipe_core.search_request(self.RECIPE, {"title": "Night: Dead", "year": "1968"})
        self.assertEqual(url, "https://api.example/search?q=title%3A%28Night+Dead%29&f%5B%5D=id")
        body = {"res": {"docs": [{"identifier": "a b", "title": ["Night: Dead"], "year": 1968},
                                 {"identifier": "c", "title": "Night: Dead", "year": "1990"},
                                 {"identifier": "d", "title": "Other", "year": "1968"}]}}
        items = recipe_core.pick_items(self.RECIPE, json.dumps(body), {"title": "Night: Dead", "year": "1968"}, url)
        self.assertEqual([(i["id"], i["url"]) for i in items], [("a b", "https://api.example/meta/a%20b")])
        detail = {"files": [{"name": "dir/f 1.mp4", "format": "MPEG4", "height": "720"},
                            {"name": "x.mp4", "format": "MPEG4", "private": "true"},
                            {"name": "t.jpg", "format": "JPEG"}]}
        entries = recipe_core.media_entries(self.RECIPE, items[0], json.dumps(detail), "")
        self.assertEqual([(e["url"], e["quality"]) for e in entries], [("https://api.example/dl/a%20b/dir/f%201.mp4", "720p")])
        self.assertEqual(recipe_core.media_entries(self.RECIPE, items[0], json.dumps(dict(detail, meta={"restricted": "true"})), ""), [])

    def test_html_results_respect_host_prefix_title_and_year(self):
        recipe = {"kind": "html", "search": {"link_prefix": "/watch/"}, "detail": {}, "match": {}}
        page = """<a href="/watch/1">Night of the Living Dead (1968)</a><a href="/watch/2">Night of the Living Dead (1990)</a>
        <a href="/news/3">Night of the Living Dead (1968)</a><a href="https://other.example/watch/4">Night of the Living Dead</a>"""
        items = recipe_core.pick_items(recipe, page, {"title": "Night of the Living Dead", "year": "1968"},
                                       "https://f.example/search?q=x")
        self.assertEqual([i["url"] for i in items], ["https://f.example/watch/1"])
        detail = """<video><source src="/m/a.mp4" label="1080p"></video><a href="/m/not-media.html">x</a>
        <script>var s = "https://c.example/b.m3u8";</script>"""
        entries = recipe_core.media_entries({"kind": "html", "detail": {}}, items[0], detail, items[0]["url"])
        self.assertEqual([(e["url"], e["quality"]) for e in entries],
                         [("https://f.example/m/a.mp4", "1080p"), ("https://c.example/b.m3u8", "SD")])


if __name__ == "__main__":
    unittest.main()
