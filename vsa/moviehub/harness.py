"""Execute generated adapters through MovieHub's own Python code.

The MovieHub checkout is never modified: its Python tree is copied to a temporary staging
directory, the generated module and record are added there, and MovieHub's real
`moviehub_vod.query_site()` runs in a separate interpreter. In `live` mode nothing is patched;
in `replay` mode only the HTTP transport and DNS are replaced by recorded exchanges.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .contract import Contract, inspect_checkout
from .generate import add_record_to_providers, provider_record
from .integrate import existing_sites, update_python_inventory

RUNNER = r'''
import hashlib, json, sys, time
from urllib.parse import urlsplit
sys.path.insert(0, sys.argv[1])
args = json.loads(sys.argv[2])
patches = []
if args.get("replay"):
    import vsa_replay
    replay, patches = vsa_replay.install(json.loads(open(args["replay"]).read())["entries"])
import moviehub_vod as vod
started = time.monotonic()
budget = args.get("budget", 300)
result = json.loads(vod.query_site(args["provider"], json.dumps(args["info"]), lambda: time.monotonic() - started > budget))
for item in result.get("sources", []):
    item["url_sha256"] = hashlib.sha256(item["url"].encode()).hexdigest()
    item["url_tail"] = "/".join(urlsplit(item["url"]).path.split("/")[-2:])  # stable across mirror nodes
    item["url"] = urlsplit(item["url"]).hostname  # host only: signed query strings stay out of evidence
    item["headers"] = sorted(item.get("headers", {}))  # header names only: values can be cookies
print("VSA-RESULT " + json.dumps(result))
'''


@dataclass
class StagedTree:
    root: Path
    python_dir: Path
    tests_dir: Path
    contract: Contract

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def stage(checkout: Path, package_dir: Path, recipe: dict) -> StagedTree:
    contract = inspect_checkout(checkout)
    root = Path(tempfile.mkdtemp(prefix="vsa-moviehub-"))
    python_dir, tests_dir = root / "python", root / "tests"
    shutil.copytree(contract.python_dir, python_dir)
    shutil.copytree(contract.tests_dir, tests_dir, ignore=shutil.ignore_patterns("*.java"))
    generated = Path(package_dir) / "android-tv-launcher"
    shutil.copy2(generated / "app/src/main/python/moviehub_native" / f"{recipe['id']}.py",
                 python_dir / "moviehub_native" / f"{recipe['id']}.py")
    init = python_dir / "moviehub_native" / "__init__.py"
    init.write_text(add_record_to_providers(init.read_text(), provider_record(recipe)))
    for path in (generated / "tests").rglob("*"):
        if path.is_file():
            target = tests_dir / path.relative_to(generated / "tests")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    # MovieHub pins its provider inventory in test_free_vod.py; apply the same update the patch makes.
    tests = tests_dir / "test_free_vod.py"
    site_exists = recipe["site"] in existing_sites(contract.python_dir, contract.native_records)
    has_test = (tests_dir / f"test_native_{recipe['id']}.py").exists()
    tests.write_text(update_python_inventory(tests.read_text(), recipe, site_exists, import_test=has_test).text)
    (python_dir / "vsa_replay.py").write_text(resources.files("vsa").joinpath("moviehub", "replay.py").read_text())
    return StagedTree(root=root, python_dir=python_dir, tests_dir=tests_dir, contract=contract)


def run_query(tree: StagedTree, provider_id: str, info: dict, python: str = sys.executable,
              replay: Path | None = None, budget: int = 300) -> dict:
    """Run MovieHub's moviehub_vod.query_site() for one provider in a fresh interpreter."""
    args = dict(provider=provider_id, info=info, budget=budget, replay=str(replay) if replay else None)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    done = subprocess.run([python, "-I", "-c", RUNNER, str(tree.python_dir), json.dumps(args)],
                          capture_output=True, text=True, timeout=budget + 60, cwd=tree.root, env=env)
    line = next((l for l in done.stdout.splitlines() if l.startswith("VSA-RESULT ")), None)
    if done.returncode != 0 or line is None:
        raise RuntimeError("MovieHub runner failed:\n" + (done.stderr or done.stdout)[-3000:])
    return json.loads(line[len("VSA-RESULT "):])


@dataclass
class SuiteResult:
    name: str
    passed: bool
    summary: str
    output: str


def run_suites(tree: StagedTree, recipe: dict, python: str = sys.executable) -> list[SuiteResult]:
    """MovieHub's own Python suite (as CI runs it) and the generated adapter test, separately."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME",)}
    env["PYTHONPATH"] = str(tree.python_dir)
    results = []
    commands = [("moviehub test_free_vod.py", [python, "test_free_vod.py"])]
    generated = tree.tests_dir / f"test_native_{recipe['id']}.py"
    if generated.exists():
        commands.append((f"generated test_native_{recipe['id']}", [python, "-m", "unittest", "-v",
                                                                    f"test_native_{recipe['id']}"]))
    for name, command in commands:
        done = subprocess.run(command, capture_output=True, text=True, timeout=900, cwd=tree.tests_dir, env=env)
        output = done.stdout + done.stderr
        summary = next((l for l in reversed(output.splitlines()) if l.startswith(("OK", "FAILED", "Ran "))), "")
        ran = re.search(r"Ran (\d+) tests?", output)
        results.append(SuiteResult(name, done.returncode == 0,
                                   (f"{ran.group(1)} tests, " if ran else "") + summary, output[-6000:]))
    return results
