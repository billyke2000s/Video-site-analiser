"""A data directory and the services built on it, shared by the CLI and the web UI."""
from __future__ import annotations

import dataclasses
import json
import shutil
import threading
from dataclasses import asdict
from importlib import resources
from pathlib import Path

from .batch import BatchQueue, BatchSettings
from .contracts import to_dict
from .diagnostics import Finding, RecipeVersions, diagnose
from .engine import EngineSettings
from .pipeline import Pipeline, PipelineRequest, Run
from .policy import AccessPolicy, SitePermission
from .store import SqliteHttpCache, Store


class Workspace:
    def __init__(self, data_dir: Path, render: bool = False, cache_ttl: float = 3600):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.permissions_path = self.data_dir / "permissions.json"
        if not self.permissions_path.exists():
            self.permissions_path.write_text(
                resources.files("vsa").joinpath("permissions.default.json").read_text())
        self.policy = AccessPolicy.load(self.permissions_path)
        self.store = Store(self.data_dir / "vsa.sqlite3")
        self.cache = SqliteHttpCache(self.store, cache_ttl)
        self.recipe_dir = self.data_dir / "recipes"
        self.recipe_dir.mkdir(exist_ok=True)
        self.runs_dir = self.data_dir / "runs"
        self.settings = EngineSettings(render=render, recipe_dirs=[self.recipe_dir])
        self.versions = RecipeVersions(self.store)
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    # --- permissions --------------------------------------------------------------------------
    def permit(self, permission: SitePermission) -> None:
        self.policy.permit(permission)
        self.policy.save(self.permissions_path)

    def permissions(self) -> list[dict]:
        return [asdict(p) for p in self.policy.permissions]

    # --- runs -----------------------------------------------------------------------------------
    def new_run(self, label: str = "") -> Run:
        run = Run(self.policy, self.runs_dir, self.settings, self.cache, label=label)
        with self._lock:
            self.runs[run.id] = run
        return run

    def run(self, run_id: str) -> Run:
        try:
            return self.runs[run_id]
        except KeyError:
            raise LookupError(f"unknown or expired run {run_id}") from None

    def pipeline(self, request: PipelineRequest) -> Run:
        run = Pipeline(self.policy, self.runs_dir, self.settings, self.cache).run(request)
        self.record(run, request)
        return run

    def record(self, run: Run, request: PipelineRequest | None = None) -> None:
        site = run.profile.site if run.profile else ""
        self.store.add_run("pipeline", site, dict(url=run.label, run=run.id, query=run.query, year=run.year),
                           "complete" if run.ok else "incomplete", run.steps, str(run.dir))
        if run.ok and run.recipe:
            self.versions.promote(run.recipe, note=f"verified by run {run.id}")
            (self.recipe_dir / f"{run.recipe['id']}.json").write_text(json.dumps(
                {k: v for k, v in run.recipe.items() if k != "allow_http_for_tests"}, indent=2))

    # --- review and diagnosis -----------------------------------------------------------------
    def review(self, run: Run, target: str, verdict: str, note: str = "") -> dict:
        row = self.store.add_run("review", run.profile.site if run.profile else "", dict(run=run.id, target=target),
                                 verdict, dict(note=note), str(run.dir))
        self.store.add_review(row, target, verdict, note)
        with (run.dir / "reviews.jsonl").open("a") as handle:
            handle.write(json.dumps(dict(target=target, verdict=verdict, note=note)) + "\n")
        findings = self.diagnose(run) if verdict == "wrong" else []
        return dict(review=row, findings=[to_dict(f) for f in findings])

    def diagnose(self, run: Run) -> list[Finding]:
        recipe = run.profile.recipe if run.profile else None
        findings = diagnose(run.reports, run.log.rows, run.steps, recipe, run.moviehub_result,
                            timeout=self.settings.timeout)
        run.write("diagnosis.json", [to_dict(f) for f in findings])
        return findings

    def repair(self, run: Run, finding: Finding) -> dict:
        """Apply a finding's repair to a new recipe version and rerun the affected steps."""
        if finding.repair is None or run.profile is None or run.profile.recipe is None:
            raise ValueError("this finding has no automatic repair")
        baseline = sum(1 for r in run.reports if r.playable)
        original = run.profile.recipe
        if self.versions.working(original["id"]) is None and baseline:
            self.versions.promote(original, note=f"baseline from run {run.id}")

        def rerun(recipe: dict, settings: dict) -> dict:
            known = {k: v for k, v in settings.items() if hasattr(self.settings, k)}
            attempt = Run(self.policy, self.runs_dir, dataclasses.replace(self.settings, **known), self.cache,
                          label=run.label)
            with self._lock:
                self.runs[attempt.id] = attempt
            attempt.analyse(run.profile.url)
            attempt.profile.recipe = recipe
            if "search" in finding.repair.rerun:
                attempt.search(run.query, run.year)
            else:
                attempt.query, attempt.year, attempt.results = run.query, run.year, run.results
            attempt.select(None)
            attempt.discover()
            attempt.verify()
            return dict(score=sum(1 for r in attempt.reports if r.playable), run=attempt.id)

        outcome = self.versions.try_repair(original, finding.repair, rerun, baseline)
        return dict(outcome, history=self.versions.history(original["id"]))

    # --- batch ------------------------------------------------------------------------------------
    def batch(self, settings: BatchSettings | None = None) -> BatchQueue:
        return BatchQueue(self.store, settings)

    def batch_work(self, moviehub: Path | None, python: str, default_query: str = "") -> callable:
        def work(item: dict) -> dict:
            query = item["query"] or default_query
            if not query:
                run = self.new_run(label=item["url"])
                run.analyse(item["url"])
                self.record(run)
                return dict(ok=True, run=run.id, analysed_only=True)
            run = self.pipeline(PipelineRequest(url=item["url"], query=query, year=item["year"], moviehub=moviehub,
                                                python=python, integration_patch=False))
            failed = next((s for s in reversed(run.steps) if s["status"] == "failed"), None)
            return dict(ok=run.ok, run=run.id, error=json.dumps(failed, default=str)[:500] if failed else "")
        return work

    def clear_cache(self) -> None:
        self.store.execute("DELETE FROM cache")

    def remove(self) -> None:  # used by tests
        shutil.rmtree(self.data_dir, ignore_errors=True)
