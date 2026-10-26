"""Local fixture websites with real media, for integration tests without Internet access.

Two independent sites on loopback (127.0.0.1 and localhost, different ports):
  - "Example Films" declares a schema.org SearchAction, lists results under /watch/, and offers
    MP4 (faststart 720p and moov-at-end 480p), HLS (two renditions) and DASH for one film, plus a
    JSON API (search + item files with SHA-1) and a DRM-flagged HLS stream.
  - "Mirror" exposes only a plain HTML search form and serves a byte-identical copy of the 720p file.
Media is generated once by ffmpeg (125 s; MovieHub rejects anything under 120 s) and cached.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MEDIA_VERSION = "2"
CACHE = Path(__file__).resolve().parents[1] / ".fixture-media" / MEDIA_VERSION
SLUG = "night-of-the-living-dead-1968"


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args], check=True, timeout=600)


def _source(size: str) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=5", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=22050", "-t", "125", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "38", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "32k"]


def build_media(folder: Path = CACHE) -> Path:
    marker = folder / "complete"
    if marker.exists():
        return folder
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    _ffmpeg(*_source("1280x720"), "-movflags", "+faststart", str(folder / "720.mp4"))
    _ffmpeg(*_source("854x480"), str(folder / "480.mp4"))  # index (moov) written at the end
    for height, size in ((360, "640x360"), (240, "426x240")):
        _ffmpeg(*_source(size), "-f", "hls", "-hls_time", "10", "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(folder / f"hls{height}_%03d.ts"), str(folder / f"hls{height}.m3u8"))
    (folder / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360,CODECS="avc1.42c01e,mp4a.40.2"\nhls360.m3u8\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=426x240,CODECS="avc1.42c015,mp4a.40.2"\nhls240.m3u8\n')
    dash = folder / "dash"
    dash.mkdir()
    _ffmpeg(*_source("640x360"), "-f", "dash", "-seg_duration", "10", "-use_template", "1", "-use_timeline", "0",
            "-init_seg_name", "init-$RepresentationID$.m4s", "-media_seg_name", "chunk-$RepresentationID$-$Number%05d$.m4s",
            str(dash / "manifest.mpd"))
    (folder / "drm.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:5\n#EXT-X-TARGETDURATION:10\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key",KEYFORMAT="com.apple.streamingkeydelivery"\n'
        "#EXTINF:10.0,\nhls360_000.ts\n#EXT-X-ENDLIST\n")
    marker.write_text("ok")
    return folder


CONTENT_TYPES = {".mp4": "video/mp4", ".ts": "video/mp2t", ".m3u8": "application/vnd.apple.mpegurl",
                 ".mpd": "application/dash+xml", ".m4s": "video/iso.segment"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, site: "FixtureSite", *args, **kwargs):
        self.site = site
        super().__init__(*args, **kwargs)

    def log_message(self, *args):  # keep test output quiet
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head: bool = False):
        self.site.requests.append(self.path)
        parts = urlsplit(self.path)
        route = self.site.routes(parts.path, parse_qs(parts.query))
        if route is None:
            return self._send(404, "text/html", b"<h1>Not found</h1>", head)
        status, content_type, body = route
        if isinstance(body, Path):
            return self._file(body, content_type, head)
        extra = {"WWW-Authenticate": 'Basic realm="members"'} if status == 401 else {}
        return self._send(status, content_type, body if isinstance(body, bytes) else body.encode(), head, extra)

    def _send(self, status, content_type, body, head, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _file(self, path: Path, content_type: str, head: bool):
        size = path.stat().st_size
        match = re.match(r"bytes=(\d+)-(\d*)$", self.headers.get("Range", ""))
        start, end, status = 0, size - 1, 200
        if match:
            start = int(match.group(1))
            end = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # browsers and probes close media connections early; that is expected here


class FixtureSite:
    def __init__(self, host: str, media: Path):
        self.host = host
        self.media = media
        self.requests: list[str] = []
        self.server = QuietServer((host, 0), partial(Handler, self))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://{host}:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def media_file(self, name: str):
        path = self.media / name
        if not path.resolve().is_relative_to(self.media.resolve()) or not path.is_file():
            return None
        return 200, CONTENT_TYPES.get(path.suffix, "application/octet-stream"), path

    def routes(self, path: str, query: dict):
        raise NotImplementedError


class FilmsSite(FixtureSite):
    def routes(self, path: str, query: dict):
        b = self.base
        if path == "/robots.txt":
            return 200, "text/plain", "User-agent: *\nDisallow: /private/\n"
        if path == "/":
            ld = {"@context": "https://schema.org", "@type": "WebSite", "url": b + "/",
                  "potentialAction": {"@type": "SearchAction", "target": b + "/search?q={search_term_string}",
                                      "query-input": "required name=search_term_string"}}
            return 200, "text/html; charset=utf-8", f"""<!doctype html><html><head><title>Example Films</title>
<meta property="og:site_name" content="Example Films">
<script type="application/ld+json">{json.dumps(ld)}</script></head><body>
<nav><a href="/">Home</a> <a href="/browse">Browse</a> <a href="/about">About</a></nav>
<form role="search" action="/search" method="get"><input type="search" name="q"></form></body></html>"""
        if path == "/search":
            q = " ".join(query.get("q", [""])).lower()
            cards = []
            if "dead" in q:
                cards = [(f"/watch/{SLUG}", "Night of the Living Dead (1968)"),
                         ("/watch/night-of-the-living-dead-1990", "Night of the Living Dead (1990)"),
                         ("/watch/dawn-of-the-dead-1978", "Dawn of the Dead (1978)")]
            links = "".join(f'<li><a href="{h}">{t}</a></li>' for h, t in cards)
            return 200, "text/html", f"<html><body><nav><a href='/'>Home</a></nav><ul>{links}</ul></body></html>"
        if path == f"/watch/{SLUG}":
            ld = {"@context": "https://schema.org", "@type": "VideoObject", "name": "HLS",
                  "contentUrl": b + f"/media/{SLUG}/master.m3u8"}
            return 200, "text/html", f"""<html><head><title>Night of the Living Dead (1968)</title>
<script type="application/ld+json">{json.dumps(ld)}</script></head><body>
<video controls><source src="/media/{SLUG}/720.mp4" type="video/mp4" label="720p">
<source src="/media/{SLUG}/480.mp4" type="video/mp4" label="480p"></video>
<script>var player = {{"dash": "{b}/media/{SLUG}/dash/manifest.mpd"}};</script></body></html>"""
        if path == "/js-player":
            # The media address only exists after the script runs (split so static scans cannot see it).
            return 200, "text/html", f"""<html><body><div id="player"></div><script>
var parts = ["/media/", "{SLUG}", "/720", ".m" + "p4"];
var v = document.createElement("video"); v.src = parts.join(""); v.preload = "metadata";
document.getElementById("player").appendChild(v);</script>
<img src="https://tracker.invalid/pixel.gif" alt=""></body></html>"""
        if path.startswith("/watch/"):
            return 200, "text/html", "<html><body><p>Not available in this fixture.</p></body></html>"
        if path == "/api/search":
            q = " ".join(query.get("q", [""])).lower()
            rows = [{"key": "notld-1968", "name": "Night of the Living Dead", "released": "1968"},
                    {"key": "notld-members", "name": "Night of the Living Dead", "released": "1968"}] if "dead" in q else []
            return 200, "application/json", json.dumps({"data": {"items": rows}})
        if path == "/api/item/notld-1968":
            files = [{"file": "720.mp4", "kind": "MP4", "sha1": sha1(self.media / "720.mp4"), "h": "720"},
                     {"file": "480.mp4", "kind": "MP4", "sha1": sha1(self.media / "480.mp4"), "h": "480"},
                     {"file": "poster.jpg", "kind": "JPEG"}]
            return 200, "application/json", json.dumps({"members_only": False, "files": files})
        if path == "/api/item/notld-members":
            return 200, "application/json", json.dumps({"members_only": True, "files": [
                {"file": "720.mp4", "kind": "MP4", "h": "720"}]})
        if path.startswith("/files/"):
            return self.media_file(path.removeprefix("/files/"))
        if path == "/drm/stream.m3u8":
            return self.media_file("drm.m3u8")
        if path == "/members/film.mp4":
            return 401, "text/html", "<h1>Sign in</h1>"
        if path.startswith(f"/media/{SLUG}/"):
            name = path.removeprefix(f"/media/{SLUG}/")
            return self.media_file(name)
        if path.startswith("/private/"):
            return 200, "text/html", "secret"
        return None


class MirrorSite(FixtureSite):
    def routes(self, path: str, query: dict):
        if path == "/robots.txt":
            return 404, "text/plain", "none"
        if path == "/":
            return 200, "text/html", """<html><head><title>Mirror</title></head><body>
<form class="site-search" action="/find" method="get"><input type="text" name="term" placeholder="Search">
<input type="hidden" name="lang" value="en"></form></body></html>"""
        if path == "/find":
            term = " ".join(query.get("term", [""])).lower()
            body = '<a href="/v/notld">Night of the Living Dead (1968)</a>' if "night" in term else ""
            return 200, "text/html", f"<html><body>{body}<a href='/about'>About</a></body></html>"
        if path == "/v/notld":
            return 200, "text/html", '<html><body><video src="/files/notld.mp4"></video></body></html>'
        if path == "/files/notld.mp4":
            return self.media_file("720.mp4")
        return None


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


JSON_RECIPE = {
    "schema": 1, "id": "example_api", "name": "Example API", "kind": "json_api", "movies": True, "series": False,
    "match": {"year_tolerance": 0, "max_items": 5},
    "search": {"url": "{base}/api/search", "params": [["q", "{title|terms}"]], "results": "data.items",
               "fields": {"id": "key", "title": "name", "year": "released"}},
    "detail": {"url": "{base}/api/item/{id|path}", "reject": [{"path": "members_only", "values": [True]}],
               "files": "files", "fields": {"name": "file", "format": "kind", "sha1": "sha1", "height": "h"},
               "include": {"format": ["MP4"]}, "media_url": "{base}/files/{name|path}", "label": "{format} {height}p",
               "server": "example-files"},
}


def json_recipe(base: str, host: str) -> dict:
    text = json.dumps(JSON_RECIPE).replace("{base}", base)
    recipe = json.loads(text)
    recipe.update(site=host, base=base)
    return recipe
