"""Build a reviewable MovieHub change for a generated adapter, on a local branch only.

MovieHub's tests pin its provider inventory (number of websites, adapters and modules), so adding
a provider legitimately changes those numbers. Each count is updated only where the exact
expected assertion is found once; anything else is reported for manual review. Nothing is pushed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .contract import inspect_checkout
from .generate import add_record_to_providers, provider_record

COUNT_ASSERTIONS = [
    # (pattern with one integer group, increment when the site is new, increment when it already exists)
    (r"self\.assertEqual\(len\(everything\), (\d+)\)", 1, 0),
    (r"self\.assertEqual\(len\(\{r\['site'\] for r in everything\}\), (\d+)\)", 1, 0),
    (r"self\.assertEqual\(sum\(len\(r\['adapters'\]\) for r in everything\), (\d+)\)", 1, 1),
    (r"self\.assertEqual\(len\(rows\), (\d+)\)", 1, 0),
    (r"self\.assertEqual\(sum\(len\(r\['adapters'\]\) for r in rows\), (\d+)\)", 1, 1),
    (r"self\.assertEqual\(vod\.validate\(\), (\d+)\)", 1, 1),
    # test_all_bundled_provider_constructors_import counts every module record.
    (r"self\.assertEqual\(len\(rows\),(\d+)\)", 1, 1),
    (r"self\.assertEqual\(len\(\{r\['id'\] for r in rows\}\),(\d+)\)", 1, 1),
]
JAVA_VALIDATE = r"assertEquals\((\d+), FreeVodProviders\.validate\(context\)\);"


@dataclass
class InventoryUpdate:
    text: str
    applied: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)


def existing_sites(python_dir: Path, native_records: list[dict]) -> set[str]:
    records = json.loads((python_dir / "moviehub_providers.json").read_text())
    return {r["site"] for r in records} | {r["site"] for r in native_records}


def update_python_inventory(text: str, recipe: dict, site_exists: bool, import_test: bool = True) -> InventoryUpdate:
    update = InventoryUpdate(text)
    for pattern, new_site, old_site in COUNT_ASSERTIONS:
        step = old_site if site_exists else new_site
        matches = list(re.finditer(pattern, update.text))
        if len(matches) != 1:
            update.manual.append(f"expected one match for {pattern!r}, found {len(matches)}")
            continue
        if step == 0:
            continue
        match = matches[0]
        value = int(match.group(1))
        update.text = update.text[:match.start(1)] + str(value + step) + update.text[match.end(1):]
        update.applied.append(f"{match.group(0)} -> {value + step}")
    cls = "Generated" + "".join(p.title() for p in recipe["id"].split("_")) + "Tests"
    anchor = "from test_native_vod_sites import NativeSiteTests\n"
    line = f"from test_native_{recipe['id']} import {cls}\n"
    if line in update.text or not import_test:
        pass
    elif anchor in update.text:
        update.text = update.text.replace(anchor, anchor + line, 1)
        update.applied.append("imported the generated test into test_free_vod.py (CI runs only that file)")
    else:
        update.manual.append("could not find where test_free_vod.py imports the native site tests")
    return update


JAVA_IDS = r"assertEquals\((\d+), FreeVodProviders\.ids\(context\)\.size\(\)\);"


def update_java_inventory(text: str, site_exists: bool = False) -> InventoryUpdate:
    update = InventoryUpdate(text)
    for pattern, step, label in ((JAVA_VALIDATE, 1, "validate()"), (JAVA_IDS, 0 if site_exists else 1, "ids()")):
        matches = list(re.finditer(pattern, update.text))
        if len(matches) != 1:
            update.manual.append(f"FreeVodProvidersTest {label} count not found exactly once")
            continue
        if step == 0:
            continue
        value = int(matches[0].group(1))
        update.text = update.text[:matches[0].start(1)] + str(value + step) + update.text[matches[0].end(1):]
        update.applied.append(f"FreeVodProvidersTest {label} {value} -> {value + step}")
    return update


@dataclass
class IntegrationResult:
    branch: str
    worktree: Path
    patch: Path
    applied: list[str]
    manual: list[str]


def build_branch(checkout: Path, package_dir: Path, recipe: dict, out_patch: Path,
                 workdir: Path) -> IntegrationResult:
    """Clone the checkout locally, add the adapter on a new branch, and write the diff as a patch."""
    contract = inspect_checkout(checkout)
    branch = f"vsa/add-{recipe['id'].replace('_', '-')}"
    worktree = Path(workdir) / "moviehub-integration"
    if worktree.exists():
        shutil.rmtree(worktree)
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(Path(checkout).resolve()), str(worktree)],
                   check=True)
    subprocess.run(["git", "-C", str(worktree), "checkout", "--quiet", "-b", branch], check=True)
    generated = Path(package_dir) / "android-tv-launcher"
    for path in generated.rglob("*"):
        if path.is_file():
            target = worktree / "android-tv-launcher" / path.relative_to(generated)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    python_dir = worktree / "android-tv-launcher/app/src/main/python"
    site_exists = recipe["site"] in existing_sites(contract.python_dir, contract.native_records)
    init = python_dir / "moviehub_native/__init__.py"
    init.write_text(add_record_to_providers(init.read_text(), provider_record(recipe)))
    applied, manual = ["added moviehub_native." + recipe["id"] + " and its PROVIDERS record"], []
    tests = worktree / "android-tv-launcher/tests/test_free_vod.py"
    has_test = (Path(package_dir) / f"android-tv-launcher/tests/test_native_{recipe['id']}.py").exists()
    py_update = update_python_inventory(tests.read_text(), recipe, site_exists, import_test=has_test)
    tests.write_text(py_update.text)
    java = worktree / "android-tv-launcher/app/src/androidTest/java/local/tv/home/FreeVodProvidersTest.java"
    if java.exists():
        java_update = update_java_inventory(java.read_text(), site_exists)
        java.write_text(java_update.text)
        applied += java_update.applied
        manual += java_update.manual
    applied += py_update.applied
    manual += py_update.manual
    manual.append("documentation counts (TESTING.md, ARCHITECTURE.md, docs/FREE_VOD_AUDIT.md, CHANGELOG.md) "
                  "are left for the owner to word")
    subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True)
    diff = subprocess.run(["git", "-C", str(worktree), "diff", "--cached", "--binary"], check=True,
                          capture_output=True, text=True).stdout
    out_patch.parent.mkdir(parents=True, exist_ok=True)
    out_patch.write_text(diff)
    return IntegrationResult(branch, worktree, out_patch, applied, manual)
