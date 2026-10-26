"""Plugin driven entirely by a recipe (data). Adding a JSON-API or HTML site needs no code."""
from __future__ import annotations

from urllib.parse import urlsplit

from .. import recipe_core
from ..contracts import MediaCandidate, SearchInterface, SearchResult, SiteProfile
from .base import Context, kind_for


class RecipePlugin:
    def __init__(self, recipe: dict):
        if recipe.get("schema") != recipe_core.RECIPE_SCHEMA:
            raise ValueError(f"recipe {recipe.get('id')}: unsupported schema {recipe.get('schema')}")
        self.recipe = recipe
        self.name = "recipe:" + recipe["id"]

    def score(self, url: str) -> int:
        host = (urlsplit(url).hostname or "").lower()
        site = self.recipe["site"].lower()
        return 100 if host == site or host.endswith("." + site) else 0

    def analyse(self, ctx: Context, url: str, profile: SiteProfile) -> SiteProfile:
        template = recipe_core.search_request(self.recipe, {"title": "QUERYSLOT", "year": ""})
        profile.search_interfaces.insert(0, SearchInterface(
            "json_api" if self.recipe["kind"] == "json_api" else "html_form",
            template.replace("QUERYSLOT", "{query}"), evidence=f"recipe {self.recipe['id']}"))
        profile.recipe = self.recipe
        return profile

    def search(self, ctx: Context, profile: SiteProfile, query: str, year: str = "") -> list[SearchResult]:
        recipe = profile.recipe or self.recipe
        info = {"title": query, "year": year, "aliases": []}
        url = recipe_core.search_request(recipe, info)
        response = ctx.fetcher.get(url, note="search")
        if response.status != 200:
            raise RuntimeError(f"search returned HTTP {response.status}")
        items = recipe_core.pick_items(recipe, response.text(), info, response.final_url)
        return [SearchResult(site=recipe["site"], item_id=i["id"], title=i["title"], url=i["url"], year=i["year"])
                for i in items]

    def discover(self, ctx: Context, profile: SiteProfile, result: SearchResult) -> list[MediaCandidate]:
        recipe = profile.recipe or self.recipe
        response = ctx.fetcher.get(result.url, note="detail")
        if response.status != 200:
            raise RuntimeError(f"detail returned HTTP {response.status}")
        entries = recipe_core.media_entries(recipe, {"id": result.item_id}, response.text(), response.final_url)
        return [MediaCandidate(url=e["url"], kind=kind_for(e["url"]), site=recipe["site"], item_id=result.item_id,
                               label=e["label"], server=e["server"], headers={"Referer": self._referer(recipe, response)},
                               declared=dict(e["declared"], quality=e["quality"]))
                for e in entries]

    @staticmethod
    def _referer(recipe: dict, detail_response) -> str:
        # Must match the generated adapter: HTML sites refer from the detail page, APIs from the base.
        return detail_response.final_url if recipe["kind"] == "html" else recipe["base"] + "/"
