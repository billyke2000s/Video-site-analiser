"""Verify that a media candidate is real, accessible and decodable, using bounded downloads.

Only small samples are fetched: a progressive file's first bytes (plus its MP4 index when stored
at the end), or a playlist and its first segment(s). Samples are inspected by ffprobe and decoded
for a few seconds by ffmpeg, both restricted to local files (no network access of their own).
DRM-protected media is reported as such and never decrypted.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..contracts import MediaCandidate, MediaReport
from ..http import Fetcher, redact
from ..policy import AccessDenied
from . import manifest, mp4

HEAD_BYTES = 2 * 1024 * 1024
FINGERPRINT_BYTES = 64 * 1024
MAX_INDEX = 32 * 1024 * 1024
MAX_SEGMENT = 12 * 1024 * 1024


@dataclass
class VerifySettings:
    decode_seconds: float = 2.0
    tool_timeout: float = 60.0
    head_bytes: int = HEAD_BYTES


def tools_available() -> bool:
    return bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))


def detect_kind(url: str, content_type: str = "", head: bytes = b"") -> str:
    path = urlsplit(url).path.lower()
    content_type = content_type.lower()
    if path.endswith(".m3u8") or "mpegurl" in content_type or head.lstrip(b"\xef\xbb\xbf \r\n").startswith(b"#EXTM3U"):
        return "hls"
    if path.endswith(".mpd") or "dash+xml" in content_type or b"<MPD" in head[:4096]:
        return "dash"
    return "progressive"


def ffprobe(path: Path, settings: VerifySettings, extra: list[str] | None = None) -> dict:
    command = ["ffprobe", "-v", "error", "-protocol_whitelist", "file,crypto", *(extra or []),
               "-show_streams", "-show_format", "-of", "json", str(path)]
    done = subprocess.run(command, capture_output=True, timeout=settings.tool_timeout, text=True)
    if done.returncode != 0:
        raise RuntimeError("ffprobe: " + (done.stderr.strip().splitlines() or ["failed"])[-1][:300])
    return json.loads(done.stdout or "{}")


def decode(path: Path, settings: VerifySettings, extra: list[str] | None = None) -> tuple[int, str]:
    """Decode the first seconds of video; return (frames, last error line)."""
    command = ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,crypto", *(extra or []),
               "-i", str(path), "-t", str(settings.decode_seconds), "-map", "0:v:0?", "-f", "null", "-",
               "-progress", "pipe:1"]
    done = subprocess.run(command, capture_output=True, timeout=settings.tool_timeout, text=True)
    frames = [int(x) for x in re.findall(r"^frame=(\d+)$", done.stdout, re.M)]
    error = (done.stderr.strip().splitlines() or [""])[-1][:300]
    return (frames[-1] if frames else 0), error


def _apply_probe(report: MediaReport, info: dict) -> None:
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and
                  not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = info.get("format", {})
    report.container = report.container or fmt.get("format_name", "")
    if video:
        report.video_codec = video.get("codec_name", "")
        report.width, report.height = int(video.get("width") or 0), int(video.get("height") or 0)
    if audio:
        report.audio_codec = audio.get("codec_name", "")
    if report.duration is None:
        try:
            report.duration = float(fmt["duration"])
        except (KeyError, TypeError, ValueError):
            pass
    if not report.bitrate and str(fmt.get("bit_rate", "")).isdigit():
        report.bitrate = int(fmt["bit_rate"])


class Verifier:
    def __init__(self, fetcher: Fetcher, settings: VerifySettings | None = None):
        self.fetcher = fetcher
        self.settings = settings or VerifySettings()

    def verify(self, candidate: MediaCandidate) -> MediaReport:
        report = MediaReport(candidate=candidate)
        report.evidence["url"] = redact(candidate.url)
        if not tools_available():
            report.status = "unverified"
            report.errors.append("ffprobe/ffmpeg not installed: decoding not checked")
        try:
            with tempfile.TemporaryDirectory(prefix="vsa-media-") as work:
                kind = candidate.kind or detect_kind(candidate.url)
                if kind == "hls":
                    self._hls(candidate, report, Path(work))
                elif kind == "dash":
                    self._dash(candidate, report, Path(work))
                else:
                    self._progressive(candidate, report, Path(work))
        except AccessDenied as denied:
            report.status = "blocked"
            report.errors.append("access policy: " + denied.reason)
        except Exception as error:  # report every failure; never let one source stop the run
            if report.status not in ("drm", "auth_required"):
                report.status = "failed"
            report.errors.append(f"{type(error).__name__}: {str(error)[:300]}")
        if report.status in ("unknown", "unverified") and report.decoded_frames > 0 and not report.errors:
            report.status = "playable"
        elif report.status == "unknown":
            report.status = "failed"
        return report

    # --- shared ---------------------------------------------------------------------------
    def _fetch(self, url: str, candidate: MediaCandidate, report: MediaReport, note: str, **kwargs):
        response = self.fetcher.get(url, headers=candidate.headers, note=note, use_cache=False, **kwargs)
        if response.auth_required or response.status == 403:
            report.status = "auth_required" if response.auth_required else "failed"
            raise RuntimeError(f"HTTP {response.status} for {note} (access restricted; not bypassed)")
        if response.status not in (200, 206):
            raise RuntimeError(f"HTTP {response.status} for {note}")
        if "text/html" in response.content_type:
            raise RuntimeError(f"{note} returned an HTML page instead of media")
        return response

    def _finish(self, report: MediaReport, sample: Path, extra: list[str] | None = None) -> None:
        if not tools_available():
            return
        _apply_probe(report, ffprobe(sample, self.settings, extra))
        if not report.video_codec and not report.audio_codec:
            raise RuntimeError("no audio or video stream found")
        frames, error = decode(sample, self.settings, extra)
        report.decoded_frames = frames
        report.evidence["decode"] = dict(seconds=self.settings.decode_seconds, frames=frames, error=error)
        if report.video_codec and frames == 0:
            raise RuntimeError("video stream did not decode" + (f": {error}" if error else ""))

    # --- progressive files ------------------------------------------------------------------
    def _progressive(self, candidate: MediaCandidate, report: MediaReport, work: Path) -> None:
        head = self._fetch(candidate.url, candidate, report, "media head", byte_range=(0, self.settings.head_bytes - 1),
                           max_bytes=self.settings.head_bytes)
        kind = detect_kind(head.final_url, head.content_type, head.body[:4096])
        if kind != "progressive":
            candidate.kind = kind
            return self._hls(candidate, report, work) if kind == "hls" else self._dash(candidate, report, work)
        total = None
        match = re.search(r"/(\d+)\s*$", head.headers.get("content-range", ""))
        if match:
            total = int(match.group(1))
        elif head.status == 200 and head.headers.get("content-length", "").isdigit():
            total = int(head.headers["content-length"])
        report.size = total
        report.evidence.update(range_supported=head.status == 206, content_type=head.content_type,
                               final_host=urlsplit(head.final_url).hostname,
                               final_url_sha256=hashlib.sha256(head.final_url.encode()).hexdigest())
        report.fingerprint["head_sha256"] = hashlib.sha256(head.body[:FINGERPRINT_BYTES]).hexdigest()
        if total:
            report.fingerprint["size"] = str(total)
        sample = work / "sample.bin"
        with sample.open("wb") as handle:
            handle.write(head.body)
            if mp4.is_mp4(head.body):
                report.container = "mp4"
                boxes = mp4.top_level_boxes(head.body, total)
                names = [b[0] for b in boxes]
                report.evidence["mp4_boxes"] = names[:20]
                if "moov" not in names:
                    offset = mp4.next_box_offset(head.body)
                    if offset is None or (total and offset >= total):
                        raise RuntimeError("MP4 index (moov) not found in the sampled bytes")
                    if head.status != 206:
                        raise RuntimeError("MP4 index is at the end and the server ignores byte ranges")
                    peek = self._fetch(candidate.url, candidate, report, "mp4 index header",
                                       byte_range=(offset, offset + 15), max_bytes=16)
                    header = mp4.read_box_header(peek.body)
                    if not header or header[0] != "moov" or header[1] > MAX_INDEX:
                        raise RuntimeError(f"unexpected box at index offset: {header}")
                    index = self._fetch(candidate.url, candidate, report, "mp4 index",
                                        byte_range=(offset, offset + header[1] - 1), max_bytes=header[1])
                    handle.seek(offset)
                    handle.write(index.body)  # sparse file: real offsets, unread media left as holes
                    report.evidence["moov_at_end"] = True
        self._finish(report, sample)

    # --- HLS ----------------------------------------------------------------------------------
    def _hls(self, candidate: MediaCandidate, report: MediaReport, work: Path) -> None:
        response = self._fetch(candidate.url, candidate, report, "HLS playlist", max_bytes=2 * 1024 * 1024)
        playlist = manifest.parse_hls(response.text(), response.final_url)
        report.evidence["final_url_sha256"] = hashlib.sha256(response.final_url.encode()).hexdigest()
        report.container = "hls"
        media_url = response.final_url
        if playlist.master:
            report.variants = sorted(playlist.variants, key=lambda v: (v.height, v.bandwidth))
            if not report.variants:
                raise RuntimeError("master playlist lists no variants")
            best = report.variants[-1]
            report.width, report.height = best.width, best.height
            # Decode the lowest rendition: it is the cheapest proof that the stream plays.
            media_url = report.variants[0].uri
            if playlist.drm:
                report.status = "drm"
                raise RuntimeError("DRM-protected session keys declared")
            response = self._fetch(media_url, candidate, report, "HLS media playlist", max_bytes=4 * 1024 * 1024)
            playlist = manifest.parse_hls(response.text(), response.final_url)
        if playlist.drm:
            report.status = "drm"
            raise RuntimeError("DRM-protected (" + ", ".join(sorted(playlist.key_methods | playlist.key_formats)) + ")")
        report.encrypted = bool(playlist.key_methods)
        report.duration = round(playlist.duration, 3) if playlist.duration else None
        report.evidence.update(vod=playlist.vod, segments=len(playlist.segments),
                               key_methods=sorted(playlist.key_methods))
        if not playlist.vod:
            report.evidence.setdefault("warnings", []).append("live/event playlist: duration is a window")
        if not playlist.segments:
            raise RuntimeError("media playlist has no segments")
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:60", "#EXT-X-PLAYLIST-TYPE:VOD"]
        if playlist.map_uri:
            init = self._fetch(playlist.map_uri, candidate, report, "HLS init segment", max_bytes=MAX_SEGMENT)
            (work / "init.mp4").write_bytes(init.body)
            lines.append('#EXT-X-MAP:URI="init.mp4"')
        if playlist.key_uris:
            key = self._fetch(playlist.key_uris[0], candidate, report, "HLS clear key", max_bytes=64)
            (work / "key.bin").write_bytes(key.body)
            lines.append('#EXT-X-KEY:METHOD=AES-128,URI="key.bin"')
        segment = self._fetch(playlist.segments[0], candidate, report, "HLS first segment", max_bytes=MAX_SEGMENT)
        report.fingerprint["head_sha256"] = hashlib.sha256(segment.body[:FINGERPRINT_BYTES]).hexdigest()
        (work / "segment0").write_bytes(segment.body)
        lines += ["#EXTINF:10.0,", "segment0", "#EXT-X-ENDLIST"]
        local = work / "local.m3u8"
        local.write_text("\n".join(lines) + "\n")
        self._finish(report, local, ["-allowed_extensions", "ALL"])
        # The probed sample is one rendition; keep the master's best resolution when known.
        if report.variants:
            report.width, report.height = report.variants[-1].width or report.width, \
                report.variants[-1].height or report.height
            report.duration = round(playlist.duration, 3) if playlist.duration else report.duration

    # --- DASH ---------------------------------------------------------------------------------
    def _dash(self, candidate: MediaCandidate, report: MediaReport, work: Path) -> None:
        response = self._fetch(candidate.url, candidate, report, "DASH manifest", max_bytes=4 * 1024 * 1024)
        mpd = manifest.parse_dash(response.text(), response.final_url)
        report.evidence["final_url_sha256"] = hashlib.sha256(response.final_url.encode()).hexdigest()
        report.container = "dash"
        report.variants = sorted(mpd.variants(), key=lambda v: (v.height, v.bandwidth))
        report.duration = mpd.duration
        report.evidence.update(dynamic=mpd.dynamic, representations=len(mpd.representations))
        if mpd.drm:
            report.status = "drm"
            raise RuntimeError("DRM-protected (ContentProtection declared)")
        if mpd.dynamic:
            report.evidence.setdefault("warnings", []).append("dynamic (live) manifest")
        videos = sorted((r for r in mpd.representations if r.mime.startswith("video")), key=lambda r: r.bandwidth)
        if not videos:
            raise RuntimeError("manifest has no video representation")
        rep = videos[0]
        sample = work / "dash.mp4"
        if rep.init_url and rep.first_segment_url:
            init = self._fetch(rep.init_url, candidate, report, "DASH init segment", max_bytes=MAX_SEGMENT)
            first = self._fetch(rep.first_segment_url, candidate, report, "DASH first segment", max_bytes=MAX_SEGMENT)
            sample.write_bytes(init.body + first.body)
            report.fingerprint["head_sha256"] = hashlib.sha256(first.body[:FINGERPRINT_BYTES]).hexdigest()
        elif rep.base_url:
            head = self._fetch(rep.base_url, candidate, report, "DASH representation", byte_range=(0, HEAD_BYTES - 1),
                               max_bytes=HEAD_BYTES)
            sample.write_bytes(head.body)
            report.fingerprint["head_sha256"] = hashlib.sha256(head.body[:FINGERPRINT_BYTES]).hexdigest()
        else:
            raise RuntimeError("unsupported DASH segment addressing")
        duration = report.duration
        self._finish(report, sample)
        report.duration = duration or report.duration
        best = report.variants[-1] if report.variants else None
        if best and best.height:
            report.width, report.height = best.width, best.height


def signature(report: MediaReport) -> str:
    """Technical signature of an encode (duration to the second, picture size and codecs)."""
    if report.duration is None or not (report.video_codec or report.audio_codec):
        return ""
    return f"{round(report.duration)}s:{report.width}x{report.height}:{report.video_codec}:{report.audio_codec}"
