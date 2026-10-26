"""Generic plugin: works from page analysis alone, with no site-specific knowledge."""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import quote_plus, urljoin, urlsplit

from .. import recipe_core
from ..capabilities import html as html_cap
from ..capabilities import render as render_cap
from ..contracts import MediaCandidate, SearchResult, SiteProfile
from .base import Context, kind_for

PREFERRED = ("schema_search_action", "opensearch", "html_form")


def _tokens(text: str) -> set[str]:
    return set(recipe_core.normal(text).split())


class GenericPlugin:
    name = "generic"

    def score(self, url: str) -> int:
        return 1

    def analyse(self, ctx: Context, url: str, profile: SiteProfile) -> SiteProfile:
        if not profile.search_interfaces:
            profile.page.notes.append("no search interface declared on this page")
        return profile

    def _interface(self, profile: SiteProfile):
        ranked = sorted(profile.search_interfaces,
                        key=lambda s: PREFERRED.index(s.kind) if s.kind in PREFERRED else len(PREFERRED))
        if not ranked:
            raise LookupError("no search interface found; try the site's home page, or add a recipe")
        return ranked[0]

    def _search_page(self, ctx: Context, url: str) -> tuple[str, str, bool]:
        response = ctx.fetcher.get(url, note="search")
        if response.status != 200:
            raise RuntimeError(f"search returned HTTP {response.status}")
        return response.text(), response.final_url, False

    def search(self, ctx: Context, profile: SiteProfile, query: str, year: str = "") -> list[SearchResult]:
        interface = self._interface(profile)
        url = interface.url_template.replace("{query}", quote_plus(query))
        body, final_url, rendered = self._search_page(ctx, url)
        broad = self._results(body, final_url, query)
        if not broad and ctx.render and render_cap.available():
            page = render_cap.render(url, ctx.policy, ctx.log)
            body, final_url, rendered = page.html, page.url, True
            broad = self._results(body, final_url, query)
        prefix = self._link_prefix([urlsplit(r.url).path for r in broad])
        info = {"title": query, "year": year, "aliases": []}
        if profile.recipe and profile.recipe.get("inferred", {}).get("locked"):
            # A reviewed or repaired recipe is used as it is, not inferred again.
            profile.recipe["inferred"]["broad_matches"] = len(broad)
            return [SearchResult(site=profile.site, item_id=i["id"], title=i["title"], url=i["url"], year=i["year"])
                    for i in recipe_core.pick_items(profile.recipe, body, info, final_url)]
        profile.recipe = {
            "schema": recipe_core.RECIPE_SCHEMA, "id": self._identifier(profile.site),
            "name": profile.site, "site": profile.site, "base": f"{urlsplit(final_url).scheme}://{urlsplit(final_url).netloc}",
            "kind": "html", "movies": True, "series": False, "match": {"year_tolerance": 1, "max_items": 5},
            "search": {"url": interface.url_template.replace("{query}", "{title|query}"), "link_prefix": prefix},
            "detail": {"literal_urls": True, "server": ""},
            "inferred": {"search_interface": interface.kind, "results_needed_rendering": rendered,
                         "broad_matches": len(broad)},
        }
        # Return exactly what the generated adapter would select (exact title, year when known).
        return [SearchResult(site=profile.site, item_id=i["id"], title=i["title"], url=i["url"], year=i["year"])
                for i in recipe_core.pick_items(profile.recipe, body, info, final_url)]

    @staticmethod
    def _identifier(site: str) -> str:
        value = re.sub(r"\W+", "_", site.removeprefix("www.")).strip("_").lower()
        return value if value[:1].isalpha() else "site_" + value

    @staticmethod
    def _results(body: str, page_url: str, query: str) -> list[SearchResult]:
        wanted = _tokens(query)
        host = urlsplit(page_url).hostname
        found: dict[str, SearchResult] = {}
        for anchor_href, text in recipe_core.parse_html(body).anchors:
            target = urljoin(page_url, anchor_href)
            parts = urlsplit(target)
            if parts.hostname != host or not text or parts.path in ("", "/"):
                continue
            if wanted and wanted <= _tokens(text):
                year = re.search(r"\b(1[89]\d{2}|20\d{2})\b", text)
                found.setdefault(target, SearchResult(site=host or "", item_id=parts.path, title=text[:200],
                                                      url=target, year=year.group(1) if year else ""))
        return list(found.values())

    @staticmethod
    def _link_prefix(paths: list[str]) -> str:
        """The first path segment shared by most result links ("/watch/"), else "/"."""
        firsts = Counter("/" + p.strip("/").split("/", 1)[0] + "/" for p in paths if p.strip("/").count("/") >= 1)
        if firsts:
            value, count = firsts.most_common(1)[0]
            if count >= max(1, len(paths) // 2):
                return value
        return "/"

    def discover(self, ctx: Context, profile: SiteProfile, result: SearchResult) -> list[MediaCandidate]:
        """Media from the result page, using the same interpreter the generated adapter runs."""
        response = ctx.fetcher.get(result.url, note="detail")
        if response.status != 200:
            raise RuntimeError(f"detail returned HTTP {response.status}")
        recipe = profile.recipe or {"kind": "html", "detail": {"literal_urls": True, "server": ""}}
        entries = recipe_core.media_entries(recipe, {"id": result.item_id}, response.text(), response.final_url)
        how = "static"
        if not entries and ctx.render and render_cap.available():
            rendered = render_cap.render(result.url, ctx.policy, ctx.log)
            dom = html_cap.parse(rendered.html, rendered.url)
            urls = list(dict.fromkeys(html_cap.media_urls(dom) + rendered.network_media))
            entries = [dict(url=u, label="", quality="SD", server=urlsplit(u).hostname or "", declared={})
                       for u in urls]
            how = "rendered"
            if entries and profile.recipe is not None:
                # MovieHub adapters never run website scripts, so this recipe cannot be generated.
                profile.recipe.setdefault("inferred", {})["media_needed_rendering"] = True
        return [MediaCandidate(url=e["url"], kind=kind_for(e["url"]), site=profile.site, item_id=result.item_id,
                               label=e["label"], server=e["server"], headers={"Referer": response.final_url},
                               declared=dict(e["declared"], quality=e["quality"], discovered_by=how))
                for e in entries]
