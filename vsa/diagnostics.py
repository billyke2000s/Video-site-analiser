"""Failure diagnosis, targeted repairs and versioned recipes.

`diagnose()` reads a run's evidence (HTTP log, media reports, steps, MovieHub's result) and
explains likely causes. Some causes have a targeted repair: a small recipe change or a settings
change. `RecipeVersions.try_repair()` applies it to a *new* version, reruns the affected steps,
and promotes the version only if the rerun is at least as good. Earlier working versions are
kept, never overwritten.

Restrictions are never "repaired": robots.txt refusals, authentication, paywalls and DRM are
reported with no repair.
"""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .contracts import Evidence, MediaReport
from .store import Store


@dataclass
class Repair:
    description: str
    recipe_patch: dict = field(default_factory=dict)  # deep-merged into the recipe
    settings: dict = field(default_factory=dict)  # engine settings to change for the rerun
    rerun: list[str] = field(default_factory=list)  # steps to repeat: search, discover, verify


@dataclass
class Finding:
    code: str
    cause: str
    evidence: list[str] = field(default_factory=list)
    repair: Repair | None = None
    severity: str = "error"  # "error" | "warning" | "info"


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        out[key] = _merge(out.get(key, {}), value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def diagnose(reports: list[MediaReport], evidence: list[Evidence], steps: list[dict] | None = None,
             recipe: dict | None = None, moviehub: dict | None = None, timeout: float = 20.0) -> list[Finding]:
    findings: list[Finding] = []
    steps = steps or []

    denied = [e for e in evidence if e.error == "access_denied"]
    robots = [e for e in denied if "robots" in e.note]
    if robots:
        findings.append(Finding("robots", "The site's robots.txt disallows these paths; they were not fetched.",
                                [f"{e.url}: {e.note}" for e in robots[:5]]))
    others = [e for e in denied if e not in robots]
    if others:
        findings.append(Finding("not_permitted", "Requests went to hosts or addresses outside the permission list "
                                "(add the media host to the site's permission entry if it is legitimately part of the site).",
                                [f"{e.url}: {e.note}" for e in others[:5]]))

    timeouts = [e for e in evidence if e.error in ("ConnectTimeout", "ReadTimeout", "Timeout")]
    if timeouts:
        findings.append(Finding("timeout", "Requests timed out; the site or its media host is slow or unreachable.",
                                [e.url for e in timeouts[:5]],
                                Repair(f"Retry with a longer timeout ({int(timeout * 2)} s) and one more retry.",
                                       settings=dict(timeout=timeout * 2, retries=3), rerun=["verify"]),
                                severity="warning"))
    dns = [e for e in evidence if e.error in ("ConnectionError", "NameResolutionError")]
    if dns:
        findings.append(Finding("connection", "Could not connect (DNS, refused connection or network block).",
                                [e.url for e in dns[:5]]))

    for report in reports:
        url = report.evidence.get("url", report.candidate.url)
        text = " ".join(report.errors)
        if report.status == "drm":
            findings.append(Finding("drm", "The stream is DRM-protected. It is not decrypted or bypassed.", [url]))
        elif report.status == "auth_required":
            findings.append(Finding("auth", "The media requires signing in. Authentication is not attempted.", [url]))
        elif report.status == "blocked":
            findings.append(Finding("blocked", "The access policy refused this media URL.", [url, text]))
        elif "HTTP 403" in text:
            findings.append(Finding("forbidden", "The host refused the request (HTTP 403): hotlink protection, a "
                                    "regional restriction or an expired signed link. Not bypassed.", [url]))
        elif "HTTP 404" in text or "HTTP 410" in text:
            findings.append(Finding("missing", "The media link is dead (404/410). The site lists a file it no "
                                    "longer serves; the adapter will skip it at runtime.", [url], severity="warning"))
        elif "HTML page instead of media" in text:
            findings.append(Finding("html_not_media", "The link leads to a web page, not media: a landing page, "
                                    "an error page or an expired link.", [url]))
        elif "byte ranges" in text:
            findings.append(Finding("no_ranges", "The MP4 index is at the end of the file and the server ignores "
                                    "byte ranges, so players cannot seek or start quickly.", [url]))
        elif report.status == "failed":
            findings.append(Finding("decode", "The file was fetched but did not decode as video.", [url, text]))
        if report.duration is not None and 0 < report.duration < 120:
            findings.append(Finding("short", "Shorter than 120 s: MovieHub rejects it as a trailer or clip "
                                    "(intended behaviour).", [url, f"{report.duration:.1f} s"], severity="info"))

    search = next((s for s in steps if s.get("step") == "search"), None)
    if search is not None and not search.get("results") and recipe:
        inferred = recipe.get("inferred", {})
        if inferred.get("broad_matches"):
            prefix = recipe.get("search", {}).get("link_prefix", "/")
            findings.append(Finding(
                "search_strict", "The search page lists links containing the title words, but none matched the exact "
                "title and year.", [f"{inferred['broad_matches']} broad matches; link prefix {prefix!r}"],
                Repair("Accept results anywhere on the site and allow a two-year difference in release year.",
                       recipe_patch={"search": {"link_prefix": "/"}, "match": {"year_tolerance": 2},
                                     "inferred": {"locked": True}},
                       rerun=["search", "discover", "verify"])))
        else:
            findings.append(Finding("search_empty", "The site returned no results for this title.", []))

    if moviehub is not None:
        playable = [r for r in reports if r.playable]
        accepted = len(moviehub.get("sources", []))
        if accepted < len(playable):
            statuses = [f"{h.get('provider')}: {h.get('status')}" for h in moviehub.get("hostAttempts", [])]
            findings.append(Finding("moviehub_rejected", f"MovieHub accepted {accepted} of {len(playable)} sources the "
                                    "analyser verified. MovieHub only plays HTTPS, video/* or known media paths, clear "
                                    "(non-DRM) streams of at least 120 s.", statuses[:10]))
    return findings


class RecipeVersions:
    """Versioned recipes in the store. Working versions are kept when a repair fails."""

    def __init__(self, store: Store):
        self.store = store

    def save(self, recipe: dict, status: str, note: str = "") -> int:
        rows = self.store.query("SELECT MAX(version) AS v FROM recipes WHERE recipe_id = ?", (recipe["id"],))
        version = (rows[0]["v"] or 0) + 1
        self.store.execute("INSERT INTO recipes (created, recipe_id, version, status, body, note) VALUES (?, ?, ?, ?, ?, ?)",
                           (time.time(), recipe["id"], version, status, json.dumps(recipe), note[:2000]))
        return version

    def set_status(self, recipe_id: str, version: int, status: str) -> None:
        self.store.execute("UPDATE recipes SET status = ? WHERE recipe_id = ? AND version = ?", (status, recipe_id, version))

    def working(self, recipe_id: str) -> tuple[int, dict] | None:
        rows = self.store.query("SELECT version, body FROM recipes WHERE recipe_id = ? AND status = 'working' "
                                "ORDER BY version DESC LIMIT 1", (recipe_id,))
        return (rows[0]["version"], json.loads(rows[0]["body"])) if rows else None

    def history(self, recipe_id: str) -> list[dict]:
        return self.store.query("SELECT version, status, note, created FROM recipes WHERE recipe_id = ? ORDER BY version",
                                (recipe_id,))

    def promote(self, recipe: dict, note: str = "") -> int:
        previous = self.working(recipe["id"])
        version = self.save(recipe, "working", note)
        if previous:
            self.set_status(recipe["id"], previous[0], "superseded")
        return version

    def try_repair(self, recipe: dict, repair: Repair, rerun: Callable[[dict, dict], dict],
                   baseline_score: int) -> dict[str, Any]:
        """Apply a repair to a new version, rerun, and promote only if the rerun scores at least as well.

        `rerun(recipe, settings)` must return {"score": int, ...}: the number of playable sources.
        """
        candidate = _merge(recipe, repair.recipe_patch) if repair.recipe_patch else copy.deepcopy(recipe)
        version = self.save(candidate, "candidate", repair.description)
        try:
            outcome = rerun(candidate, repair.settings)
        except Exception as error:
            self.set_status(candidate["id"], version, "failed")
            return dict(promoted=False, version=version, error=f"{type(error).__name__}: {error}")
        better = outcome.get("score", 0) >= max(1, baseline_score)
        if better:
            previous = self.working(candidate["id"])
            self.set_status(candidate["id"], version, "working")
            if previous and previous[0] != version:
                self.set_status(candidate["id"], previous[0], "superseded")
        else:
            self.set_status(candidate["id"], version, "failed")
        return dict(promoted=better, version=version, outcome=outcome)
