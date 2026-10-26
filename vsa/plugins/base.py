from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import metadata, resources
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from ..contracts import MediaCandidate, SearchResult, SiteProfile
from ..http import EvidenceLog, Fetcher
from ..policy import AccessPolicy


@dataclass
class Context:
    fetcher: Fetcher
    policy: AccessPolicy
    log: EvidenceLog
    render: bool = False
    notes: list[str] = field(default_factory=list)


@runtime_checkable
class SitePlugin(Protocol):
    name: str

    def score(self, url: str) -> int:
        """0 = cannot handle; higher wins. The generic plugin scores 1."""

    def analyse(self, ctx: Context, url: str, profile: SiteProfile) -> SiteProfile:
        """Add plugin knowledge (search interfaces, recipe) to the common page analysis."""

    def search(self, ctx: Context, profile: SiteProfile, query: str, year: str = "") -> list[SearchResult]:
        ...

    def discover(self, ctx: Context, profile: SiteProfile, result: SearchResult) -> list[MediaCandidate]:
        ...


def kind_for(url: str) -> str:
    path = urlsplit(url).path.lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    return "progressive"


def builtin_recipes() -> list[dict]:
    folder = resources.files("vsa") / "recipes"
    return [json.loads(item.read_text()) for item in folder.iterdir() if item.name.endswith(".json")]


def load_recipes(extra_dirs: list[Path] | None = None) -> list[dict]:
    recipes = {r["id"]: r for r in builtin_recipes()}
    for folder in extra_dirs or []:
        for path in sorted(Path(folder).glob("*.json")):
            recipe = json.loads(path.read_text())
            recipes[recipe["id"]] = recipe  # user recipes override built-in ones with the same id
    return list(recipes.values())


def load_plugins(recipe_dirs: list[Path] | None = None) -> list[SitePlugin]:
    from .generic import GenericPlugin
    from .recipe import RecipePlugin

    plugins: list[SitePlugin] = [RecipePlugin(r) for r in load_recipes(recipe_dirs)]
    for entry in metadata.entry_points(group="vsa.plugins"):
        plugin = entry.load()()
        if isinstance(plugin, SitePlugin):
            plugins.append(plugin)
    plugins.append(GenericPlugin())
    return plugins


def choose(plugins: list[SitePlugin], url: str) -> SitePlugin:
    scored = sorted(((p.score(url), i, p) for i, p in enumerate(plugins)), key=lambda x: (-x[0], x[1]))
    if not scored or scored[0][0] <= 0:
        raise LookupError("no plugin can handle " + url)
    return scored[0][2]
