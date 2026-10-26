"""Static HTML inspection: metadata, navigation, search forms, embedded media and players.

Uses the standard-library parser. Page scripts are never executed here (see render.py for the
optional, sandboxed browser rendering capability).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

from ..contracts import PageAnalysis, SearchInterface

MEDIA_EXTENSIONS = ("mp4", "m4v", "webm", "mkv", "mov", "m3u8", "mpd")
MEDIA_URL = re.compile(r"""(?:https?:)?//[^\s"'<>\\()]+?\.(?:mp4|m4v|webm|mkv|mov|m3u8|mpd)(?:\?[^\s"'<>\\()]*)?""", re.I)
RELATIVE_MEDIA = re.compile(r"""["'](/[^\s"'<>\\()]+?\.(?:mp4|m4v|webm|mkv|mov|m3u8|mpd)(?:\?[^\s"'<>\\()]*)?)["']""", re.I)
SEARCH_NAMES = {"q", "s", "query", "search", "keyword", "keywords", "term", "k", "search_query"}


@dataclass
class Form:
    action: str
    method: str
    inputs: list[dict] = field(default_factory=list)
    role: str = ""
    css: str = ""


@dataclass
class Anchor:
    href: str
    text: str
    in_nav: bool


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self._in_title = "", False
        self.meta: dict[str, str] = {}
        self.json_ld: list[str] = []
        self._script: list[str] | None = None
        self._script_type = ""
        self.scripts = 0
        self.inline_scripts: list[str] = []
        self.links: list[dict] = []
        self.anchors: list[Anchor] = []
        self._anchor: dict | None = None
        self._nav_depth = 0
        self.forms: list[Form] = []
        self._form: Form | None = None
        self.media: list[dict] = []
        self.iframes: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = a.get("property") or a.get("name") or a.get("itemprop")
            if key and "content" in a:
                self.meta[key.lower()] = a["content"]
        elif tag == "script":
            self.scripts += 1
            self._script, self._script_type = [], a.get("type", "").lower()
        elif tag == "link":
            self.links.append(a)
        elif tag in ("nav", "header") or a.get("role") == "navigation":
            self._nav_depth += 1
        elif tag == "a" and a.get("href"):
            self._anchor = dict(href=a["href"], text=[], nav=self._nav_depth > 0)
        elif tag == "form":
            self._form = Form(action=a.get("action", ""), method=(a.get("method") or "get").upper(),
                              role=a.get("role", ""), css=" ".join([a.get("class", ""), a.get("id", "")]))
        elif tag in ("input", "select", "textarea") and self._form is not None:
            self._form.inputs.append(dict(name=a.get("name", ""), type=a.get("type", "text").lower(),
                                          value=a.get("value", ""), placeholder=a.get("placeholder", "")))
        elif tag in ("video", "audio", "source", "track") and a.get("src"):
            self.media.append(dict(tag=tag, src=a["src"], type=a.get("type", ""), label=a.get("label", "") or
                                   a.get("res", "") or a.get("size", "") or a.get("data-quality", "")))
        elif tag == "iframe" and a.get("src"):
            self.iframes.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._script is not None:
            body = "".join(self._script)
            if self._script_type == "application/ld+json":
                self.json_ld.append(body)
            elif body.strip():
                self.inline_scripts.append(body[:200000])
            self._script = None
        elif tag in ("nav", "header") and self._nav_depth:
            self._nav_depth -= 1
        elif tag == "a" and self._anchor is not None:
            self.anchors.append(Anchor(self._anchor["href"], " ".join("".join(self._anchor["text"]).split()),
                                       self._anchor["nav"]))
            self._anchor = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._script is not None:
            self._script.append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)


@dataclass
class ParsedPage:
    url: str
    title: str
    meta: dict[str, str]
    structured: list[dict]
    anchors: list[Anchor]
    forms: list[Form]
    media: list[dict]
    iframes: list[str]
    links: list[dict]
    script_count: int
    inline_scripts: list[str]
    html: str


def parse(html: str, url: str) -> ParsedPage:
    collector = _Collector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # malformed markup from an untrusted site must not stop analysis
        pass
    structured: list[dict] = []
    for block in collector.json_ld:
        try:
            value = json.loads(block.strip())
        except ValueError:
            continue
        stack = value if isinstance(value, list) else [value]
        while stack:
            item = stack.pop(0)
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    stack.extend(item["@graph"])
                structured.append(item)
    return ParsedPage(url=url, title=" ".join(collector.title.split()), meta=collector.meta,
                      structured=structured, anchors=collector.anchors, forms=collector.forms,
                      media=collector.media, iframes=[urljoin(url, f) for f in collector.iframes],
                      links=collector.links, script_count=collector.scripts,
                      inline_scripts=collector.inline_scripts, html=html)


def schema_types(item: dict) -> set[str]:
    value = item.get("@type", [])
    return {str(v) for v in (value if isinstance(value, list) else [value])}


def search_interfaces(page: ParsedPage) -> list[SearchInterface]:
    """Find search interfaces declared by the page, most reliable first."""
    found: list[SearchInterface] = []
    # 1. schema.org SearchAction (used by search engines' sitelinks search box).
    for item in page.structured:
        actions = item.get("potentialAction", [])
        for action in actions if isinstance(actions, list) else [actions]:
            if not isinstance(action, dict) or "SearchAction" not in schema_types(action):
                continue
            target = action.get("target")
            template = target.get("urlTemplate") if isinstance(target, dict) else target
            if isinstance(template, str):
                slot = re.search(r"\{([^}]+)\}", template)
                if slot:
                    found.append(SearchInterface("schema_search_action",
                                                 urljoin(page.url, template.replace(slot.group(0), "{query}")),
                                                 evidence="JSON-LD potentialAction SearchAction"))
    # 2. OpenSearch description documents (resolved later by the caller: they need a fetch).
    for link in page.links:
        if "search" in link.get("rel", "").lower() and "opensearch" in link.get("type", "").lower():
            found.append(SearchInterface("opensearch", urljoin(page.url, link.get("href", "")),
                                         evidence="<link rel=search type=opensearchdescription>"))
    # 3. HTML forms that look like search forms.
    for form in page.forms:
        text_inputs = [i for i in form.inputs if i["type"] in ("search", "text", "") and i["name"]]
        query_input = next((i for i in text_inputs if i["type"] == "search" or i["name"].lower() in SEARCH_NAMES),
                           None)
        looks_like_search = form.role == "search" or "search" in form.css.lower() or query_input is not None
        if form.method != "GET" or not text_inputs or not looks_like_search:
            continue
        query_input = query_input or text_inputs[0]
        hidden = {i["name"]: i["value"] for i in form.inputs if i["type"] == "hidden" and i["name"]}
        action = urljoin(page.url, form.action or page.url)
        params = urlencode(dict(hidden, **{query_input["name"]: "QUERYSLOT"}))
        template = action + ("&" if "?" in action else "?") + params.replace("QUERYSLOT", "{query}")
        found.append(SearchInterface("html_form", template, "GET",
                                     evidence=f"<form> input name={query_input['name']!r}"))
    return found


def media_urls(page: ParsedPage) -> list[str]:
    """Media resources referenced by tags, structured data, metadata or literal URLs in scripts."""
    urls: list[str] = []
    for item in page.media:
        if item["tag"] != "track":
            urls.append(urljoin(page.url, item["src"]))
    for item in page.structured:
        if schema_types(item) & {"VideoObject", "MediaObject", "Movie", "Episode"}:
            for key in ("contentUrl", "embedUrl"):
                value = item.get(key)
                if isinstance(value, str) and is_media_url(value):
                    urls.append(urljoin(page.url, value))
    for key in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        if key in page.meta and is_media_url(page.meta[key]):
            urls.append(urljoin(page.url, page.meta[key]))
    # Inline JSON often escapes slashes ("https:\/\/host\/file.mp4").
    unescaped = page.html.replace("\\/", "/")
    for match in MEDIA_URL.findall(unescaped):
        urls.append(urljoin(page.url, match))
    for match in RELATIVE_MEDIA.findall(unescaped):
        urls.append(urljoin(page.url, match))
    return list(dict.fromkeys(u for u in urls if urlsplit(u).scheme in ("http", "https")))


def is_media_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.rsplit(".", 1)[-1] in MEDIA_EXTENSIONS if "." in path else False


def navigation(page: ParsedPage, limit: int = 40) -> list[dict]:
    same_site = urlsplit(page.url).hostname
    rows = []
    for anchor in page.anchors:
        target = urljoin(page.url, anchor.href)
        if anchor.in_nav and urlsplit(target).hostname == same_site and anchor.text:
            rows.append(dict(text=anchor.text[:80], url=target))
    return rows[:limit]


def analyse(html: str, url: str) -> tuple[ParsedPage, PageAnalysis]:
    page = parse(html, url)
    og = {k: v for k, v in page.meta.items() if k.startswith(("og:", "twitter:"))}
    analysis = PageAnalysis(url=url, final_url=url, title=page.title, structured_data=page.structured[:20],
                            opengraph=og, navigation=navigation(page), search_interfaces=search_interfaces(page),
                            media_urls=media_urls(page), iframes=page.iframes[:20], script_count=page.script_count)
    return page, analysis
