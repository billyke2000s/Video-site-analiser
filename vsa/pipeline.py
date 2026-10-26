"""The end-to-end workflow, as resumable steps with a preserved evidence bundle.

URL -> analyse -> search -> select -> discover -> verify -> deduplicate -> generate adapter
    -> execute through MovieHub's real moviehub_vod.query_site() -> MovieHub test suites
    -> local integration branch and patch (never pushed).

`Run` holds one workflow's state. The CLI runs every step at once (`Pipeline.run`); the web UI
calls the steps one at a time so a person can choose results and review media in between.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .contracts import MediaCandidate, MediaReport, SearchResult, SiteProfile, to_dict
from .dedup import MediaGroup
from .engine import Engine, EngineSettings
from .http import EvidenceLog, Recorder
from .moviehub import generate, harness, integrate
from .policy import AccessPolicy


@dataclass
class PipelineRequest:
    url: str
    query: str
    year: str = ""
    imdb: str = ""
    tmdb: str = ""
    pick: list[int] | None = None  # indices of search results to use; default: every matching result
    moviehub: Path | None = None
    moviehub_mode: str = "replay"  # "replay" (recorded exchanges) or "live" (real network)
    python: str = sys.executable  # interpreter with MovieHub's requirements (requests, six)
    integration_patch: bool = True
    adapter_id: str = ""  # override the recipe/module id


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def group_dict(item: MediaGroup) -> dict:
    return dict(media_id=item.media_id, sites=item.sites, cross_site=item.cross_site, evidence=item.evidence,
                related=item.related, members=[m.candidate.url for m in item.members])


class Run:
    def __init__(self, policy: AccessPolicy, out_dir: Path, settings: EngineSettings | None = None, cache=None,
                 label: str = ""):
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.dir = Path(out_dir) / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = EvidenceLog(self.dir / "evidence.jsonl")
        self.recorder = Recorder()
        self.engine = Engine(policy, settings, cache=cache, log=self.log, recorder=self.recorder)
        self.label = label
        self.steps: list[dict] = []
        self.profile: SiteProfile | None = None
        self.results: list[SearchResult] = []
        self.chosen: list[SearchResult] = []
        self.candidates: list[MediaCandidate] = []
        self.reports: list[MediaReport] = []
        self.groups: list[MediaGroup] = []
        self.query, self.year = "", ""
        self.moviehub_result: dict | None = None
        self.recipe: dict | None = None
        self.ok = False

    # --- bookkeeping ------------------------------------------------------------------------
    def step(self, name: str, status: str, **details) -> dict:
        row = dict(step=name, status=status, time=round(time.time(), 3), **details)
        self.steps.append(row)
        (self.dir / "steps.json").write_text(json.dumps(self.steps, indent=2, default=str))
        return row

    def write(self, name: str, value) -> None:
        (self.dir / name).write_text(json.dumps(value, indent=2, default=str))

    # --- steps ------------------------------------------------------------------------------
    def analyse(self, url: str) -> SiteProfile:
        self.profile = self.engine.analyse(url)
        self.write("profile.json", to_dict(self.profile))
        self.step("analyse", "ok", plugin=self.profile.plugin, search_interfaces=len(self.profile.search_interfaces),
                  media_on_page=len(self.profile.page.media_urls))
        return self.profile

    def search(self, query: str, year: str = "") -> list[SearchResult]:
        self.query, self.year = query, year
        self.results = self.engine.search(self.profile, query, year)
        self.step("search", "ok" if self.results else "failed", results=[to_dict(r) for r in self.results])
        return self.results

    def select(self, indices: list[int] | None = None) -> list[SearchResult]:
        self.chosen = [self.results[i] for i in indices] if indices else list(self.results)
        self.step("select", "ok" if self.chosen else "failed", selected=[r.item_id for r in self.chosen])
        return self.chosen

    def discover(self) -> list[MediaCandidate]:
        self.candidates = []
        for item in self.chosen:
            self.candidates += self.engine.discover(self.profile, item)
        self.step("discover", "ok" if self.candidates else "failed", candidates=len(self.candidates))
        return self.candidates

    def verify(self) -> list[MediaReport]:
        self.reports = self.engine.verify(self.candidates)
        self.groups = self.engine.deduplicate(self.reports)
        self.write("media.json", dict(reports=to_dict(self.reports), groups=[group_dict(g) for g in self.groups]))
        playable = [r for r in self.reports if r.playable]
        self.step("verify", "ok" if playable else "failed", playable=len(playable), total=len(self.reports),
                  distinct_media=len(self.groups), statuses=sorted({r.status for r in self.reports}))
        return self.reports

    def info(self, request: PipelineRequest | None = None) -> dict:
        http = bool(self.recipe and self.recipe["base"].startswith("http://"))
        tmdb = request.tmdb if request else ""
        return dict(imdb=request.imdb if request else "", tmdb=int(tmdb) if str(tmdb).isdigit() else 0,
                    title=self.query, year=self.year, season=-1, episode=-1, http=http)

    def generate(self, request: PipelineRequest) -> bool:
        """Generate the adapter; with a MovieHub checkout, run it through MovieHub and its tests."""
        if self.profile is None or self.profile.recipe is None:
            self.step("generate", "failed", reason="the plugin produced no recipe")
            return False
        if not any(r.playable for r in self.reports):
            self.step("generate", "failed", reason="no verified playable media")
            return False
        recipe = json.loads(json.dumps(self.profile.recipe))
        if request.adapter_id:
            recipe["id"] = request.adapter_id
        if recipe["base"].startswith("http://"):
            recipe["allow_http_for_tests"] = True  # local fixtures only; MovieHub refuses http by default
        self.recipe = recipe
        package = self.dir / "adapter"
        paths = generate.write_package(recipe, package, None)
        self.step("generate", "ok", files={k: str(v.relative_to(self.dir)) for k, v in paths.items()})
        if request.moviehub is None:
            self.step("moviehub", "skipped", reason="no MovieHub checkout given")
            return True

        info = self.info(request)
        entries = generate.trim_entries(self.recorder.entries, {c.url for c in self.candidates})
        replay_path = self.dir / "replay.json"
        replay_path.write_text(json.dumps(dict(entries=entries)))
        tree = harness.stage(request.moviehub, package, recipe)
        try:
            movie = harness.run_query(tree, "native:" + recipe["id"], info, python=request.python,
                                      replay=replay_path if request.moviehub_mode == "replay" else None)
        finally:
            tree.cleanup()
        self.moviehub_result = movie
        self.write("moviehub_result.json", movie)
        playable = [r for r in self.reports if r.playable]
        ours = {r.evidence.get("final_url_sha256") for r in playable} | {_sha(r.candidate.url) for r in playable}
        matched = sum(1 for s in movie.get("sources", []) if s.get("url_sha256") in ours)
        self.step("moviehub_query", "ok" if movie.get("status") == "complete" else "failed",
                  mode=request.moviehub_mode, moviehub_status=movie.get("status"),
                  comparison=dict(analyser_playable=len(playable), moviehub_sources=len(movie.get("sources", [])),
                                  matched=matched))
        if movie.get("status") != "complete":
            return False

        fixture = generate.Fixture(info=info, entries=entries, expected_urls=sorted({c.url for c in self.candidates}),
                                   expected_playable=len(movie["sources"]), http=info["http"])
        generate.write_package(recipe, package, fixture)
        tree = harness.stage(request.moviehub, package, recipe)
        try:
            suites = harness.run_suites(tree, recipe, python=request.python)
        finally:
            tree.cleanup()
        (self.dir / "suites.txt").write_text("\n\n".join(f"== {s.name}: {s.summary}\n{s.output}" for s in suites))
        self.step("moviehub_tests", "ok" if all(s.passed for s in suites) else "failed",
                  suites=[dict(name=s.name, passed=s.passed, summary=s.summary) for s in suites])
        if not all(s.passed for s in suites):
            return False
        if request.integration_patch:
            outcome = integrate.build_branch(request.moviehub, package, recipe,
                                             self.dir / f"moviehub-add-{recipe['id']}.patch", self.dir / "work")
            self.step("integration_patch", "ok", branch=outcome.branch, patch=str(outcome.patch.relative_to(self.dir)),
                      applied=outcome.applied, manual=outcome.manual)
        return True

    def summary(self, request: PipelineRequest | None = None) -> str:
        lines = [f"# Run {self.id}", ""]
        if request:
            lines += [f"- URL: {request.url}", f"- Query: {request.query} {request.year}".rstrip()]
        lines += [f"- Result: {'complete' if self.ok else 'incomplete'}", "", "| Step | Status | Details |",
                  "| --- | --- | --- |"]
        for row in self.steps:
            details = {k: v for k, v in row.items() if k not in ("step", "status", "time", "results", "suites")}
            lines.append(f"| {row['step']} | {row['status']} | {json.dumps(details, default=str)[:300]} |")
        return "\n".join(lines) + "\n"


class Pipeline:
    def __init__(self, policy: AccessPolicy, out_dir: Path, settings: EngineSettings | None = None, cache=None):
        self.policy = policy
        self.out_dir = Path(out_dir)
        self.settings = settings or EngineSettings()
        self.cache = cache

    def run(self, request: PipelineRequest) -> Run:
        run = Run(self.policy, self.out_dir, self.settings, self.cache, label=request.url)
        try:
            run.analyse(request.url)
            if run.search(request.query, request.year) and run.select(request.pick) and run.discover():
                run.verify()
                run.ok = run.generate(request)
        except Exception as error:
            run.step("error", "failed", error=f"{type(error).__name__}: {str(error)[:2000]}")
        finally:
            (run.dir / "summary.md").write_text(run.summary(request))
        return run
