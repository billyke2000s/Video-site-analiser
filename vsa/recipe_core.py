"""Recipe interpreter shared by the analyser and by generated MovieHub adapters.

This file is copied verbatim into every generated adapter, so the code that the analyser
verifies is the code MovieHub runs. Constraints: Python 3.11 standard library only, no I/O
(callers pass fetched text in), no evaluation of anything taken from a website.

A recipe is JSON data describing how a site is searched and where its media is listed:
  kind "json_api": search endpoint -> result list -> detail endpoint -> file list
  kind "html":     search page -> result links -> detail page -> <video>/<source>/JSON-LD/literal URLs
"""
import json
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlsplit

RECIPE_SCHEMA = 1
MEDIA_PATH = re.compile(r"\.(?:mp4|m4v|webm|mkv|mov|m3u8|mpd)$", re.I)
LITERAL_MEDIA = re.compile(r"""(?:https?:)?//[^\s"'<>\\()]+?\.(?:mp4|m4v|webm|mkv|mov|m3u8|mpd)(?:\?[^\s"'<>\\()]*)?""", re.I)
TEMPLATE_SLOT = re.compile(r"\{([a-zA-Z0-9_.-]+)(?:\|([a-z]+))?\}")


def get_path(value, path):
    """Follow a dotted path ("response.docs", "files.0.name") through dicts and lists."""
    if not path:
        return value
    for part in str(path).split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def text_value(value):
    if isinstance(value, list):
        value = value[0] if value else ""
    return "" if value is None else str(value)


def fill(template, values):
    """Substitute {name|filter} slots. Filters: query, path, terms, raw (default raw)."""
    def replace(match):
        value = text_value(values.get(match.group(1), ""))
        kind = match.group(2) or "raw"
        if kind == "query":
            return quote_plus(value)
        if kind == "path":
            return quote(value, safe="/")
        if kind == "terms":
            return " ".join(re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE).split())
        return value
    return TEMPLATE_SLOT.sub(replace, template)


def normal(title):
    title = unicodedata.normalize("NFKD", str(title)).encode("ascii", "ignore").decode("ascii").casefold()
    title = title.replace("&", " and ")
    title = re.sub(r"\(\s*(19|20)\d{2}\s*\)\s*$", " ", title)
    title = " ".join(re.sub(r"[^a-z0-9]+", " ", title).split())
    return title[4:] if title.startswith("the ") else title


def title_ok(found, aliases):
    wanted = {normal(a) for a in aliases if a}
    return bool(wanted) and normal(found) in wanted


def year_ok(found, wanted, tolerance):
    found_match = re.search(r"\b(1[89]\d{2}|20\d{2})\b", text_value(found))
    if not found_match or not str(wanted).isdigit():
        return True  # yearless results are allowed; titles must still match exactly
    return abs(int(found_match.group(1)) - int(wanted)) <= tolerance


def quality(height):
    try:
        height = int(float(height))
    except (TypeError, ValueError):
        return "SD"
    for limit, label in ((2000, "2160p"), (1000, "1080p"), (700, "720p"), (460, "480p")):
        if height >= limit:
            return label
    return "SD"


def search_request(recipe, info):
    """URL for the search request (query parameters already encoded)."""
    search = recipe["search"]
    values = dict(title=info["title"], year=info.get("year", ""))
    url = fill(search["url"], values)
    params = [(k, fill(v, values)) for k, v in search.get("params", [])]
    return url + (("&" if "?" in url else "?") + urlencode(params) if params else "")


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors, self.media, self.json_ld = [], [], []
        self._anchor, self._script = None, None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self._anchor = [a["href"], []]
        elif tag in ("video", "source") and a.get("src"):
            self.media.append((a["src"], a.get("label") or a.get("res") or a.get("size") or a.get("data-quality") or ""))
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._script = []

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self.anchors.append((self._anchor[0], " ".join("".join(self._anchor[1]).split())))
            self._anchor = None
        elif tag == "script" and self._script is not None:
            self.json_ld.append("".join(self._script))
            self._script = None

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor[1].append(data)
        if self._script is not None:
            self._script.append(data)


def parse_html(html):
    parser = _Anchors()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser


def pick_items(recipe, body, info, page_url):
    """Search results that match the wanted title (and year when both sides have one)."""
    aliases = [info["title"]] + [a.get("title", "") if isinstance(a, dict) else str(a) for a in info.get("aliases", [])]
    tolerance = int(recipe.get("match", {}).get("year_tolerance", 1))
    limit = int(recipe.get("match", {}).get("max_items", 5))
    items = []
    if recipe["kind"] == "json_api":
        fields = recipe["search"]["fields"]
        rows = get_path(json.loads(body) if isinstance(body, str) else body, recipe["search"]["results"]) or []
        for row in rows if isinstance(rows, list) else []:
            title = text_value(get_path(row, fields["title"]))
            year = text_value(get_path(row, fields.get("year", "")))
            item_id = text_value(get_path(row, fields["id"]))
            if item_id and title_ok(title, aliases) and year_ok(year, info.get("year", ""), tolerance):
                items.append(dict(id=item_id, title=title, year=year, url=fill(recipe["detail"]["url"], dict(id=item_id))))
    else:
        prefix = recipe["search"].get("link_prefix", "")
        host = urlsplit(page_url).hostname
        for href, text in parse_html(body).anchors:
            target = urljoin(page_url, href)
            parts = urlsplit(target)
            if parts.hostname != host or not parts.path.startswith(prefix):
                continue
            if title_ok(text, aliases) and year_ok(text, info.get("year", ""), tolerance):
                year = re.search(r"\b(1[89]\d{2}|20\d{2})\b", text)
                items.append(dict(id=parts.path, title=text, year=year.group(1) if year else "", url=target))
    unique = {}
    for item in items:
        unique.setdefault(item["url"], item)
    return list(unique.values())[:limit]


def _rejected(recipe_detail, payload):
    for rule in recipe_detail.get("reject", []):
        if get_path(payload, rule["path"]) in rule["values"]:
            return True
    return False


def media_entries(recipe, item, body, page_url):
    """Distinct media entries for one result: url, label, quality, server and declared facts."""
    detail = recipe["detail"]
    entries = []
    if recipe["kind"] == "json_api":
        payload = json.loads(body) if isinstance(body, str) else body
        if not isinstance(payload, dict) or _rejected(detail, payload):
            return []
        fields = detail["fields"]
        for row in get_path(payload, detail["files"]) or []:
            if not isinstance(row, dict):
                continue
            facts = {key: text_value(get_path(row, path)) for key, path in fields.items()}
            if any(facts.get(k, "") not in allowed for k, allowed in detail.get("include", {}).items()):
                continue
            if any(facts.get(k, "") in blocked for k, blocked in detail.get("exclude", {}).items()):
                continue
            values = dict(facts, id=item["id"])
            url = fill(detail["media_url"], values)
            entries.append(dict(url=url, label=fill(detail.get("label", "{name}"), values),
                                quality=quality(facts.get("height")), server=detail.get("server", ""),
                                declared={k: v for k, v in facts.items() if v}))
    else:
        page = parse_html(body)
        found = [(urljoin(page_url, src), label) for src, label in page.media]
        for block in page.json_ld:
            try:
                data = json.loads(block)
            except ValueError:
                continue
            for node in data if isinstance(data, list) else [data]:
                if isinstance(node, dict):
                    value = node.get("contentUrl")
                    if isinstance(value, str):
                        found.append((urljoin(page_url, value), node.get("name", "")))
        if recipe["detail"].get("literal_urls", True):
            for match in LITERAL_MEDIA.findall(body.replace("\\/", "/")):
                found.append((urljoin(page_url, match), ""))
        seen = set()
        for url, label in found:
            if url in seen or urlsplit(url).scheme not in ("http", "https"):
                continue
            if not MEDIA_PATH.search(urlsplit(url).path):
                continue
            seen.add(url)
            height = re.search(r"(\d{3,4})p?\b", str(label))
            entries.append(dict(url=url, label=str(label)[:100], quality=quality(height.group(1) if height else None),
                                server=detail.get("server", "") or (urlsplit(url).hostname or ""), declared={}))
    return entries
