"""The core engine: URL -> analyse -> search -> select -> discover -> verify -> deduplicate.

The engine knows nothing about particular websites; plugins and recipes supply that.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .capabilities import html as html_cap
from .capabilities import opensearch
from .capabilities import render as render_cap
from .contracts import MediaCandidate, MediaReport, SearchResult, SiteProfile
from .dedup import MediaGroup, group
from .http import EvidenceLog, Fetcher, Recorder
from .media.verify import Verifier, VerifySettings
from .plugins.base import Context, SitePlugin, choose, load_plugins
from .policy import AccessDenied, AccessPolicy


@dataclass
class EngineSettings:
    render: bool = False
    verify_workers: int = 3
    retries: int = 2
    timeout: float = 20.0
    recipe_dirs: list[Path] = field(default_factory=list)
    verify: VerifySettings = field(default_factory=VerifySettings)


class Engine:
    def __init__(self, policy: AccessPolicy, settings: EngineSettings | None = None, cache=None,
                 log: EvidenceLog | None = None, recorder: Recorder | None = None,
                 plugins: list[SitePlugin] | None = None):
        self.policy = policy
        self.settings = settings or EngineSettings()
        self.log = log or EvidenceLog()
        self.fetcher = Fetcher(policy, self.log, cache=cache, recorder=recorder, retries=self.settings.retries,
                               timeout=self.settings.timeout)
        self.plugins = plugins if plugins is not None else load_plugins(self.settings.recipe_dirs)
        self.ctx = Context(self.fetcher, policy, self.log, render=self.settings.render)
        self.verifier = Verifier(self.fetcher, self.settings.verify)

    def plugin(self, profile: SiteProfile) -> SitePlugin:
        return next(p for p in self.plugins if p.name == profile.plugin)

    # --- 1. analyse -------------------------------------------------------------------------
    def analyse(self, url: str) -> SiteProfile:
        plugin = choose(self.plugins, url)
        site = (urlsplit(url).hostname or "").removeprefix("www.")
        decision = self.policy.check(url)
        if not decision.allowed:
            raise AccessDenied(url, decision.reason)
        response = self.fetcher.get(url, note="analyse")
        page_html = response.text() if "html" in response.content_type or response.body[:200].lstrip().startswith(b"<") else ""
        _, page = html_cap.analyse(page_html, response.final_url)
        page.url, page.status, page.content_type = url, response.status, response.content_type
        if response.auth_required:
            page.notes.append("page requires authentication; not attempted")
        if self.settings.render and render_cap.available():
            try:
                rendered = render_cap.render(url, self.policy, self.log)
                _, dom = html_cap.analyse(rendered.html, rendered.url)
                page.rendered = True
                page.network_media = rendered.network_media
                page.media_urls = list(dict.fromkeys(page.media_urls + dom.media_urls))
                known = {s.url_template for s in page.search_interfaces}
                page.search_interfaces += [s for s in dom.search_interfaces if s.url_template not in known]
                if rendered.blocked:
                    page.notes.append(f"browser requests blocked by policy: {len(rendered.blocked)}")
            except Exception as error:
                page.notes.append(f"rendering failed: {type(error).__name__}: {str(error)[:200]}")
        interfaces = []
        for interface in page.search_interfaces:
            if interface.kind == "opensearch" and "{query}" not in interface.url_template:
                try:
                    description = self.fetcher.get(interface.url_template, note="opensearch description")
                    interfaces += opensearch.parse_description(description.text(), description.final_url)
                except (AccessDenied, Exception) as error:  # noqa: B014 - record and continue
                    page.notes.append(f"OpenSearch description unusable: {type(error).__name__}")
            else:
                interfaces.append(interface)
        page.search_interfaces = interfaces
        profile = SiteProfile(url=url, site=site, plugin=plugin.name, page=page, search_interfaces=list(interfaces))
        return plugin.analyse(self.ctx, url, profile)

    # --- 2. search ----------------------------------------------------------------------------
    def search(self, profile: SiteProfile, query: str, year: str = "") -> list[SearchResult]:
        return self.plugin(profile).search(self.ctx, profile, query, year)

    # --- 3. discover --------------------------------------------------------------------------
    def discover(self, profile: SiteProfile, result: SearchResult) -> list[MediaCandidate]:
        return self.plugin(profile).discover(self.ctx, profile, result)

    # --- 4. verify ----------------------------------------------------------------------------
    def verify(self, candidates: list[MediaCandidate]) -> list[MediaReport]:
        workers = max(1, self.settings.verify_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self.verifier.verify, candidates))

    # --- 5. deduplicate -------------------------------------------------------------------------
    @staticmethod
    def deduplicate(reports: list[MediaReport]) -> list[MediaGroup]:
        return group(reports)
