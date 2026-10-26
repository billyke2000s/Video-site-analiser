"""Typed data passed between the engine, capabilities, plugins, the store and the UI.

Everything here is plain data (dataclasses) so it can be stored as JSON and shown in the UI.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


def to_dict(value: Any) -> Any:
    """Recursively convert dataclasses to JSON-compatible dicts."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_dict(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [to_dict(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_dict(v) for k, v in value.items()}
    return value


@dataclass
class SearchInterface:
    """A way to search a site, found by analysis or declared by a plugin."""

    kind: str  # "json_api" | "html_form" | "opensearch" | "schema_search_action" | "plugin"
    url_template: str  # contains {query}
    method: str = "GET"
    evidence: str = ""


@dataclass
class PageAnalysis:
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    title: str = ""
    rendered: bool = False
    robots_allowed: bool = True
    structured_data: list[dict] = field(default_factory=list)
    opengraph: dict[str, str] = field(default_factory=dict)
    navigation: list[dict] = field(default_factory=list)
    search_interfaces: list[SearchInterface] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    iframes: list[str] = field(default_factory=list)
    script_count: int = 0
    network_media: list[str] = field(default_factory=list)  # seen only while rendering
    notes: list[str] = field(default_factory=list)


@dataclass
class SiteProfile:
    """Result of analysing a URL: which plugin handles it and what it found."""

    url: str
    site: str
    plugin: str
    page: PageAnalysis
    search_interfaces: list[SearchInterface] = field(default_factory=list)
    recipe: dict | None = None


@dataclass
class SearchResult:
    site: str
    item_id: str
    title: str
    url: str
    year: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class MediaCandidate:
    """One accessible media resource. `declared` holds what the site claims about it."""

    url: str
    kind: str  # "progressive" | "hls" | "dash"
    site: str
    item_id: str = ""
    label: str = ""
    server: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    declared: dict[str, Any] = field(default_factory=dict)


@dataclass
class Variant:
    """One rendition inside an HLS master playlist or DASH manifest."""

    bandwidth: int = 0
    width: int = 0
    height: int = 0
    codecs: str = ""
    uri: str = ""


@dataclass
class MediaReport:
    candidate: MediaCandidate
    status: str = "unknown"  # "playable" | "failed" | "drm" | "auth_required" | "blocked"
    container: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    duration: float | None = None
    bitrate: int = 0
    size: int | None = None
    variants: list[Variant] = field(default_factory=list)
    decoded_frames: int = 0
    encrypted: bool = False
    errors: list[str] = field(default_factory=list)
    fingerprint: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def playable(self) -> bool:
        return self.status == "playable"


@dataclass
class Evidence:
    """One HTTP exchange, with secrets redacted. Stored for every fetch."""

    time: float
    method: str
    url: str
    status: int
    content_type: str = ""
    bytes: int = 0
    ms: int = 0
    error: str = ""
    note: str = ""
