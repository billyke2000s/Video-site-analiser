"""HLS and DASH manifest parsing (metadata only; nothing is decrypted or circumvented)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..contracts import Variant

ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attributes(line: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in ATTRIBUTE.findall(line.split(":", 1)[1] if ":" in line else "")}


@dataclass
class HlsPlaylist:
    master: bool
    variants: list[Variant] = field(default_factory=list)
    audio_uris: list[str] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    duration: float = 0.0
    ended: bool = False
    playlist_type: str = ""
    map_uri: str = ""
    key_methods: set[str] = field(default_factory=set)
    key_formats: set[str] = field(default_factory=set)
    key_uris: list[str] = field(default_factory=list)

    @property
    def drm(self) -> bool:
        """Protected by a DRM system (as opposed to clear AES-128 with a fetchable key)."""
        return any(m.startswith("SAMPLE-AES") for m in self.key_methods) or any(
            f.lower() != "identity" for f in self.key_formats)

    @property
    def vod(self) -> bool:
        return self.ended or self.playlist_type == "VOD"


def parse_hls(text: str, base_url: str) -> HlsPlaylist:
    lines = [line.strip() for line in text.lstrip("﻿").splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("not an HLS playlist")
    playlist = HlsPlaylist(master=any(line.startswith("#EXT-X-STREAM-INF") for line in lines))
    pending: dict[str, str] | None = None
    for line in lines[1:]:
        if line.startswith("#EXT-X-STREAM-INF"):
            pending = _attributes(line)
        elif line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line:
            uri = _attributes(line).get("URI")
            if uri:
                playlist.audio_uris.append(urljoin(base_url, uri))
        elif line.startswith("#EXTINF"):
            try:
                playlist.duration += float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-ENDLIST"):
            playlist.ended = True
        elif line.startswith("#EXT-X-PLAYLIST-TYPE"):
            playlist.playlist_type = line.split(":", 1)[1].strip()
        elif line.startswith("#EXT-X-MAP"):
            uri = _attributes(line).get("URI")
            if uri and not playlist.map_uri:
                playlist.map_uri = urljoin(base_url, uri)
        elif line.startswith(("#EXT-X-KEY", "#EXT-X-SESSION-KEY")):
            attrs = _attributes(line)
            method = attrs.get("METHOD", "")
            if method and method != "NONE":
                playlist.key_methods.add(method)
                if "KEYFORMAT" in attrs:
                    playlist.key_formats.add(attrs["KEYFORMAT"])
                if attrs.get("URI"):
                    playlist.key_uris.append(urljoin(base_url, attrs["URI"]))
        elif not line.startswith("#"):
            uri = urljoin(base_url, line)
            if pending is not None:
                width, height = 0, 0
                if "x" in pending.get("RESOLUTION", ""):
                    w, h = pending["RESOLUTION"].split("x", 1)
                    width, height = int(w or 0), int(h or 0)
                bandwidth = pending.get("BANDWIDTH", "0")
                playlist.variants.append(Variant(bandwidth=int(bandwidth) if bandwidth.isdigit() else 0,
                                                 width=width, height=height, codecs=pending.get("CODECS", ""),
                                                 uri=uri))
                pending = None
            else:
                playlist.segments.append(uri)
    return playlist


# --- DASH -----------------------------------------------------------------------------------
@dataclass
class DashRepresentation:
    id: str
    mime: str
    codecs: str
    bandwidth: int
    width: int
    height: int
    init_url: str = ""
    first_segment_url: str = ""
    base_url: str = ""  # SegmentBase / single-file representation


@dataclass
class DashManifest:
    duration: float | None
    dynamic: bool
    drm: bool
    representations: list[DashRepresentation]

    def variants(self) -> list[Variant]:
        return [Variant(bandwidth=r.bandwidth, width=r.width, height=r.height, codecs=r.codecs, uri=r.id)
                for r in self.representations if r.mime.startswith("video")]


def iso_duration(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?",
                         value.strip())
    if not match:
        return None
    days, hours, minutes, seconds = (float(x) if x else 0.0 for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element, name):
    return next((c for c in element if _local(c.tag) == name), None)


def _expand(template: str, rep_id: str, bandwidth: int, number: int, time_value: int) -> str:
    def replace(match):
        name, fmt = match.group(1), match.group(2)
        value = {"RepresentationID": rep_id, "Bandwidth": bandwidth, "Number": number, "Time": time_value}.get(name)
        if value is None:
            return match.group(0)
        if fmt and name != "RepresentationID":
            return ("%" + fmt[1:]) % value if fmt.startswith("%") else str(value)
        return str(value)

    return re.sub(r"\$(RepresentationID|Bandwidth|Number|Time)(%0\d+d)?\$", replace, template).replace("$$", "$")


def parse_dash(text: str, base_url: str) -> DashManifest:
    if len(text) > 8 * 1024 * 1024 or "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("refusing oversized or DTD-bearing MPD")
    root = ET.fromstring(text)
    if _local(root.tag) != "MPD":
        raise ValueError("not a DASH manifest")
    drm = any(_local(e.tag) == "ContentProtection" for e in root.iter())
    base = urljoin(base_url, (_child(root, "BaseURL").text or "").strip()) if _child(root, "BaseURL") is not None else base_url
    representations = []
    period = _child(root, "Period")
    if period is not None:
        if _child(period, "BaseURL") is not None:
            base = urljoin(base, (_child(period, "BaseURL").text or "").strip())
        for adaptation in (c for c in period if _local(c.tag) == "AdaptationSet"):
            a_base = urljoin(base, (_child(adaptation, "BaseURL").text or "").strip()) \
                if _child(adaptation, "BaseURL") is not None else base
            a_template = _child(adaptation, "SegmentTemplate")
            for rep in (c for c in adaptation if _local(c.tag) == "Representation"):
                mime = rep.get("mimeType") or adaptation.get("mimeType") or ""
                content = adaptation.get("contentType", "")
                if not mime and content:
                    mime = content + "/mp4"
                bandwidth = int(rep.get("bandwidth", "0") or 0)
                r_base_el = _child(rep, "BaseURL")
                r_base = urljoin(a_base, (r_base_el.text or "").strip()) if r_base_el is not None else a_base
                item = DashRepresentation(id=rep.get("id", ""), mime=mime,
                                          codecs=rep.get("codecs") or adaptation.get("codecs") or "",
                                          bandwidth=bandwidth, width=int(rep.get("width") or adaptation.get("width") or 0),
                                          height=int(rep.get("height") or adaptation.get("height") or 0))
                # Elements without children are falsy, so compare with None explicitly.
                template = _child(rep, "SegmentTemplate")
                if template is None:
                    template = a_template
                if template is not None:
                    start = int(template.get("startNumber", "1"))
                    timeline = _child(template, "SegmentTimeline")
                    first_time = 0
                    if timeline is not None and len(timeline):
                        first_time = int(timeline[0].get("t", "0"))
                    if template.get("initialization"):
                        item.init_url = urljoin(r_base, _expand(template.get("initialization"), item.id, bandwidth,
                                                                start, first_time))
                    if template.get("media"):
                        item.first_segment_url = urljoin(r_base, _expand(template.get("media"), item.id, bandwidth,
                                                                         start, first_time))
                elif r_base_el is not None:
                    item.base_url = r_base
                representations.append(item)
    return DashManifest(duration=iso_duration(root.get("mediaPresentationDuration")),
                        dynamic=root.get("type") == "dynamic", drm=drm, representations=representations)
