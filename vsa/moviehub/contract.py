"""Read MovieHub's actual provider contract from a checkout, without importing or running it.

Generation is refused when the checkout no longer matches the contract the generator targets,
so a MovieHub refactor produces a clear error instead of an adapter that silently breaks.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

PYTHON_DIR = Path("android-tv-launcher/app/src/main/python")
TESTS_DIR = Path("android-tv-launcher/tests")

# The method signatures moviehub_vod.query() calls on a website adapter's `source` object.
EXPECTED_METHODS = {
    "movie": ["self", "imdb", "tmdb", "title", "localtitle", "aliases", "year"],
    "tvshow": ["self", "imdb", "tmdb", "tvdb", "title", "localtitle", "aliases", "year"],
    "episode": ["self", "key", "imdb", "tmdb", "tvdb", "title", "premiered", "season", "episode"],
    "sources": ["self", "key", "hosts"],
    "resolve": ["self", "url"],
}
RECORD_KEYS = {"id", "module", "family", "name", "site", "movies", "series"}


class ContractMismatch(RuntimeError):
    pass


@dataclass
class Contract:
    root: Path
    python_dir: Path
    tests_dir: Path
    native_records: list[dict]
    calls: dict[str, list[str]] = field(default_factory=dict)
    direct_args: list[str] = field(default_factory=list)

    @property
    def native_ids(self) -> set[str]:
        return {r["id"] for r in self.native_records}


def _function_args(node: ast.FunctionDef) -> list[str]:
    return [a.arg for a in node.args.args]


def _parse(path: Path) -> ast.Module:
    if not path.exists():
        raise ContractMismatch(f"missing {path}")
    return ast.parse(path.read_text(), str(path))


def inspect_checkout(root: Path) -> Contract:
    root = Path(root)
    python_dir, tests_dir = root / PYTHON_DIR, root / TESTS_DIR
    problems: list[str] = []

    # 1. moviehub_vod exposes query_site/query and calls the adapter methods we generate.
    vod = _parse(python_dir / "moviehub_vod.py")
    functions = {n.name: n for n in vod.body if isinstance(n, ast.FunctionDef)}
    for name, args in (("query_site", ["provider_id", "raw", "cancelled", "emit"]),
                       ("query", ["provider_id", "raw", "cancelled", "emit", "deadline"]),
                       ("playable", ["raw", "preferred"]), ("search_providers", ["include_retired"])):
        if name not in functions:
            problems.append(f"moviehub_vod.{name} is missing")
        elif _function_args(functions[name]) != args:
            problems.append(f"moviehub_vod.{name}{tuple(_function_args(functions[name]))} != {tuple(args)}")
    called = {n.func.attr for n in ast.walk(functions.get("query", vod)) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "provider"}
    for method in ("movie", "tvshow", "episode", "sources", "resolve"):
        if method not in called:
            problems.append(f"moviehub_vod.query no longer calls provider.{method}()")
    if "moviehub_native" not in (python_dir / "moviehub_vod.py").read_text():
        problems.append("moviehub_vod no longer imports moviehub_native providers")

    # 2. moviehub_native.PROVIDERS is a literal list of records with the expected keys.
    native = _parse(python_dir / "moviehub_native" / "__init__.py")
    records: list[dict] = []
    for node in native.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PROVIDERS" for t in node.targets):
            if not isinstance(node.value, ast.List):
                problems.append("moviehub_native.PROVIDERS is not a list literal")
                continue
            for element in node.value.elts:
                if not (isinstance(element, ast.Call) and getattr(element.func, "id", "") == "dict"):
                    problems.append("a PROVIDERS entry is not dict(...)")
                    continue
                record = {k.arg: ast.literal_eval(k.value) for k in element.keywords}
                if set(record) != RECORD_KEYS:
                    problems.append(f"PROVIDERS record keys changed: {sorted(record)}")
                records.append(record)
    if not records:
        problems.append("moviehub_native.PROVIDERS not found")

    # 3. Existing native adapters still use the method signatures we generate.
    for record in records:
        module = python_dir / Path(*record["module"].split("."))
        tree = _parse(module.with_suffix(".py"))
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "source"), None)
        if cls is None:
            problems.append(f"{record['module']} has no class source")
            continue
        for item in cls.body:
            if isinstance(item, ast.FunctionDef) and item.name in EXPECTED_METHODS:
                if _function_args(item) != EXPECTED_METHODS[item.name]:
                    problems.append(f"{record['module']}.source.{item.name} signature changed")

    # 4. The helpers generated code imports.
    hosts = _parse(python_dir / "moviehub_compat" / "current_hosts.py")
    direct = next((n for n in hosts.body if isinstance(n, ast.FunctionDef) and n.name == "direct"), None)
    if direct is None or _function_args(direct)[:3] != ["url", "referer", "provider"]:
        problems.append("moviehub_compat.current_hosts.direct(url, referer, provider, quality) changed")
    client = _parse(python_dir / "moviehub_compat" / "client.py")
    client_functions = {n.name for n in client.body if isinstance(n, ast.FunctionDef)}
    for name in ("request", "safe", "check", "host_result", "scope"):
        if name not in client_functions:
            problems.append(f"moviehub_compat.client.{name} is missing")

    if problems:
        raise ContractMismatch("MovieHub checkout does not match the generator's contract:\n- " + "\n- ".join(problems))
    return Contract(root=root, python_dir=python_dir, tests_dir=tests_dir, native_records=records,
                    calls={m: EXPECTED_METHODS[m] for m in EXPECTED_METHODS},
                    direct_args=_function_args(direct))
