"""Command line interface. Run `vsa --help`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .contracts import Evidence, MediaCandidate, MediaReport, Variant, to_dict
from .diagnostics import diagnose
from .pipeline import PipelineRequest
from .policy import SitePermission


def _workspace(args):
    from .workspace import Workspace

    return Workspace(Path(args.data_dir), render=getattr(args, "render", False))


def cmd_permit(args):
    ws = _workspace(args)
    ws.permit(SitePermission(domain=args.domain.lower(), reason=args.reason, rate_per_second=args.rate,
                             extra_hosts=args.extra_host or []))
    print(json.dumps(ws.permissions(), indent=2))


def cmd_analyse(args):
    ws = _workspace(args)
    run = ws.new_run(label=args.url)
    profile = run.analyse(args.url)
    print(json.dumps(to_dict(profile), indent=2))
    print(f"\nEvidence: {run.dir}", file=sys.stderr)


def cmd_search(args):
    ws = _workspace(args)
    run = ws.new_run(label=args.url)
    run.analyse(args.url)
    for index, item in enumerate(run.search(args.query, args.year)):
        print(f"[{index}] {item.title} ({item.year or '?'})  {item.url}")
    if not run.results:
        print("No exactly matching results.", file=sys.stderr)
        return 1


def cmd_run(args):
    ws = _workspace(args)
    request = PipelineRequest(url=args.url, query=args.query, year=args.year, imdb=args.imdb, tmdb=args.tmdb,
                              pick=[int(x) for x in args.pick.split(",")] if args.pick else None,
                              moviehub=Path(args.moviehub) if args.moviehub else None, moviehub_mode=args.mode,
                              python=args.python, integration_patch=not args.no_patch, adapter_id=args.adapter_id)
    run = ws.pipeline(request)
    print((run.dir / "summary.md").read_text())
    print(f"Evidence bundle: {run.dir}")
    if not run.ok:
        for finding in ws.diagnose(run):
            print(f"- [{finding.code}] {finding.cause}" + (f" Repair: {finding.repair.description}" if finding.repair else ""))
    return 0 if run.ok else 1


def cmd_contract(args):
    from .moviehub.contract import inspect_checkout

    contract = inspect_checkout(Path(args.moviehub))
    print(json.dumps(dict(python_dir=str(contract.python_dir), native_providers=contract.native_records,
                          adapter_methods=contract.calls, direct_args=contract.direct_args), indent=2))


def cmd_generate(args):
    from .moviehub.generate import write_package

    recipe = json.loads(Path(args.recipe).read_text())
    paths = write_package(recipe, Path(args.out), None)
    for name, path in paths.items():
        print(f"{name}: {path}")


def _report(data: dict) -> MediaReport:
    candidate = MediaCandidate(**data["candidate"])
    fields = {k: v for k, v in data.items() if k not in ("candidate", "variants")}
    return MediaReport(candidate=candidate, variants=[Variant(**v) for v in data.get("variants", [])], **fields)


def cmd_diagnose(args):
    folder = Path(args.run_dir)
    load = lambda name, default: json.loads((folder / name).read_text()) if (folder / name).exists() else default
    reports = [_report(r) for r in load("media.json", {}).get("reports", [])]
    evidence = [Evidence(**json.loads(line)) for line in (folder / "evidence.jsonl").read_text().splitlines() if line] \
        if (folder / "evidence.jsonl").exists() else []
    profile = load("profile.json", {})
    findings = diagnose(reports, evidence, load("steps.json", []), profile.get("recipe"), load("moviehub_result.json", None))
    print(json.dumps(to_dict(findings), indent=2))


def cmd_batch(args):
    from .batch import BatchSettings

    ws = _workspace(args)
    queue = ws.batch(BatchSettings(concurrency=args.concurrency, max_attempts=args.attempts))
    if args.action == "add":
        ids = queue.add_lines(Path(args.file).read_text().splitlines())
        print(f"queued {len(ids)}")
    elif args.action == "run":
        work = ws.batch_work(Path(args.moviehub) if args.moviehub else None, args.python, args.query)
        final = queue.run(work, on_progress=lambda p: print(json.dumps(p), file=sys.stderr))
        print(json.dumps(final))
        return 0 if final["failed"] == 0 else 1
    else:
        print(json.dumps(dict(progress=queue.progress(), items=queue.items(50)), indent=2, default=str))


def cmd_serve(args):
    from .web.server import serve

    ws = _workspace(args)
    server, app = serve(ws, args.port, Path(args.moviehub) if args.moviehub else None, args.python)
    print(f"Review UI: http://127.0.0.1:{server.server_address[1]}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vsa", description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("VSA_DATA", "vsa-data"),
                        help="permissions, database, cache, recipes and run evidence (default: ./vsa-data)")
    parser.add_argument("--render", action="store_true", help="also render pages with a headless browser")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("permit", help="allow a site to be analysed")
    p.add_argument("domain")
    p.add_argument("--reason", required=True)
    p.add_argument("--rate", type=float, default=1.0, help="requests per second (default 1)")
    p.add_argument("--extra-host", action="append", help="additional host glob, e.g. '*.cdn.example'")
    p.set_defaults(func=cmd_permit)

    p = sub.add_parser("analyse", help="inspect a URL")
    p.add_argument("url")
    p.set_defaults(func=cmd_analyse)

    p = sub.add_parser("search", help="analyse a URL and search it")
    p.add_argument("url")
    p.add_argument("query")
    p.add_argument("--year", default="")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("run", help="full workflow: analyse, search, verify, generate and test against MovieHub")
    p.add_argument("url")
    p.add_argument("--query", required=True)
    p.add_argument("--year", default="")
    p.add_argument("--imdb", default="")
    p.add_argument("--tmdb", default="")
    p.add_argument("--pick", default="", help="comma-separated result indices (default: all matches)")
    p.add_argument("--moviehub", default="", help="path to a MovieHub checkout")
    p.add_argument("--mode", choices=("replay", "live"), default="replay",
                   help="run MovieHub's query on recorded exchanges (replay) or the real network (live)")
    p.add_argument("--python", default=sys.executable, help="interpreter with requests and six for MovieHub")
    p.add_argument("--adapter-id", default="")
    p.add_argument("--no-patch", action="store_true", help="skip the local MovieHub integration branch/patch")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("contract", help="show the provider contract read from a MovieHub checkout")
    p.add_argument("moviehub")
    p.set_defaults(func=cmd_contract)

    p = sub.add_parser("generate", help="generate an adapter from a recipe file (no tests)")
    p.add_argument("recipe")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("diagnose", help="diagnose a saved run folder")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("batch", help="persistent batch queue")
    p.add_argument("action", choices=("add", "run", "status"))
    p.add_argument("file", nargs="?", help="for add: lines of URL<TAB>title<TAB>year")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--query", default="", help="default title for lines without one")
    p.add_argument("--moviehub", default="")
    p.add_argument("--python", default=sys.executable)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("serve", help="start the local review UI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--moviehub", default="")
    p.add_argument("--python", default=sys.executable)
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
