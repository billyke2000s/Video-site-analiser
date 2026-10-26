"""Local review UI: a small JSON API over the workspace, served on loopback only.

Mutating requests need the per-process token embedded in the page, and the Host header must be a
loopback name, so other websites open in the same browser cannot drive the tool.
"""
from __future__ import annotations

import json
import mimetypes
import secrets
import sys
import threading
import traceback
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..batch import BatchSettings
from ..contracts import to_dict
from ..pipeline import PipelineRequest, group_dict
from ..policy import AccessDenied, SitePermission
from ..workspace import Workspace

RUN_FILES = {"evidence.jsonl", "steps.json", "profile.json", "media.json", "moviehub_result.json", "suites.txt",
             "summary.md", "diagnosis.json", "reviews.jsonl"}


class App:
    def __init__(self, workspace: Workspace, moviehub: Path | None = None, python: str = sys.executable):
        self.ws = workspace
        self.moviehub = moviehub
        self.python = python
        self.token = secrets.token_urlsafe(24)
        self.batch_thread: threading.Thread | None = None
        self.findings: dict[str, list] = {}

    # --- API handlers (each returns a JSON-serialisable value) -----------------------------
    def state(self, _):
        runs = self.ws.store.query("SELECT id, created, kind, site, status, evidence_path FROM runs "
                                   "ORDER BY id DESC LIMIT 20")
        return dict(permissions=self.ws.permissions(), batch=self.ws.batch().progress(),
                    batch_running=bool(self.batch_thread and self.batch_thread.is_alive()), runs=runs,
                    moviehub=str(self.moviehub) if self.moviehub else "")

    def analyse(self, body):
        run = self.ws.new_run(label=body["url"])
        profile = run.analyse(body["url"])
        return dict(run=run.id, profile=to_dict(profile))

    def search(self, body):
        run = self.ws.run(body["run"])
        return dict(results=to_dict(run.search(body["query"], body.get("year", ""))), recipe=run.profile.recipe)

    def discover(self, body):
        run = self.ws.run(body["run"])
        run.select([int(i) for i in body.get("indices", [])] or None)
        return dict(candidates=to_dict(run.discover()))

    def verify(self, body):
        run = self.ws.run(body["run"])
        reports = run.verify()
        return dict(reports=to_dict(reports), groups=[group_dict(g) for g in run.groups])

    def generate(self, body):
        run = self.ws.run(body["run"])
        moviehub = Path(body["moviehub"]) if body.get("moviehub") else self.moviehub
        request = PipelineRequest(url=run.label, query=run.query, year=run.year, moviehub=moviehub,
                                  python=self.python, adapter_id=body.get("adapter_id", ""),
                                  integration_patch=bool(body.get("patch", True)))
        run.ok = run.generate(request)
        self.ws.record(run, request)
        (run.dir / "summary.md").write_text(run.summary(request))
        return dict(ok=run.ok, steps=run.steps, dir=str(run.dir))

    def review(self, body):
        run = self.ws.run(body["run"])
        if body["verdict"] not in ("working", "wrong"):
            raise ValueError("verdict must be working or wrong")
        outcome = self.ws.review(run, body.get("target", "run"), body["verdict"], body.get("note", ""))
        if body["verdict"] == "wrong":
            self.findings[run.id] = self.ws.diagnose(run)
        return outcome

    def diagnose(self, body):
        run = self.ws.run(body["run"])
        self.findings[run.id] = self.ws.diagnose(run)
        return dict(findings=to_dict(self.findings[run.id]))

    def repair(self, body):
        run = self.ws.run(body["run"])
        findings = self.findings.get(run.id) or self.ws.diagnose(run)
        return self.ws.repair(run, findings[int(body["finding"])])

    def permit(self, body):
        permission = SitePermission(domain=body["domain"].strip().lower(), reason=body["reason"].strip(),
                                    rate_per_second=float(body.get("rate_per_second", 1.0)))
        if not permission.reason:
            raise ValueError("a reason is required for every permitted site")
        self.ws.permit(permission)
        return dict(permissions=self.ws.permissions())

    def batch_add(self, body):
        ids = self.ws.batch().add_lines(str(body.get("lines", "")).splitlines())
        return dict(added=len(ids), progress=self.ws.batch().progress())

    def batch_start(self, body):
        if self.batch_thread and self.batch_thread.is_alive():
            return dict(started=False, reason="already running")
        settings = BatchSettings(concurrency=max(1, min(8, int(body.get("concurrency", 2)))),
                                 max_attempts=max(1, int(body.get("attempts", 3))))
        queue = self.ws.batch(settings)
        work = self.ws.batch_work(self.moviehub, self.python, body.get("query", ""))
        self.batch_thread = threading.Thread(target=queue.run, args=(work,), daemon=True)
        self.batch_thread.start()
        return dict(started=True)

    def batch_state(self, _):
        queue = self.ws.batch()
        return dict(progress=queue.progress(), items=queue.items(100),
                    running=bool(self.batch_thread and self.batch_thread.is_alive()))

    POST = {"analyse": analyse, "search": search, "discover": discover, "verify": verify, "generate": generate,
            "review": review, "diagnose": diagnose, "repair": repair, "permit": permit,
            "batch/add": batch_add, "batch/start": batch_start}
    GET = {"state": state, "batch": batch_state}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "vsa"

        def log_message(self, fmt, *args):
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost", "[::1]")

        def _send(self, status: int, body: bytes, content_type: str = "application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value):
            self._send(status, json.dumps(value, default=str).encode())

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, dict(error="bad host"))
            parts = urlsplit(self.path)
            path = parts.path
            if path in ("/", "/index.html"):
                page = resources.files("vsa").joinpath("web", "static", "index.html").read_text()
                return self._send(200, page.replace("{{TOKEN}}", app.token).encode(), "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = path.removeprefix("/static/")
                if "/" in name or name.startswith("."):
                    return self._json(404, dict(error="not found"))
                item = resources.files("vsa").joinpath("web", "static", name)
                if not item.is_file():
                    return self._json(404, dict(error="not found"))
                return self._send(200, item.read_bytes(), mimetypes.guess_type(name)[0] or "application/octet-stream")
            if path.startswith("/api/run/"):
                run_id = path.removeprefix("/api/run/")
                name = parse_qs(parts.query).get("file", [""])[0]
                try:
                    run = app.ws.run(run_id)
                except LookupError as error:
                    return self._json(404, dict(error=str(error)))
                if name in RUN_FILES and (run.dir / name).exists():
                    return self._send(200, (run.dir / name).read_bytes(), "text/plain; charset=utf-8")
                return self._json(200, dict(steps=run.steps, dir=str(run.dir),
                                            files=sorted(p.name for p in run.dir.iterdir() if p.name in RUN_FILES)))
            handler = App.GET.get(path.removeprefix("/api/"))
            if handler is None:
                return self._json(404, dict(error="not found"))
            return self._json(200, handler(app, None))

        def do_POST(self):
            if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-VSA-Token", ""), app.token):
                return self._json(403, dict(error="forbidden"))
            handler = App.POST.get(urlsplit(self.path).path.removeprefix("/api/"))
            if handler is None:
                return self._json(404, dict(error="not found"))
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1024 * 1024:
                return self._json(413, dict(error="request too large"))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                return self._json(200, handler(app, body))
            except AccessDenied as denied:
                return self._json(403, dict(error=f"Access policy: {denied.reason}", url=denied.url))
            except (KeyError, ValueError, LookupError) as error:
                return self._json(400, dict(error=f"{type(error).__name__}: {error}"))
            except Exception as error:
                traceback.print_exc()
                return self._json(500, dict(error=f"{type(error).__name__}: {str(error)[:500]}"))

    return Handler


def serve(workspace: Workspace, port: int = 8765, moviehub: Path | None = None, python: str = sys.executable,
          host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, App]:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the review UI only listens on loopback")
    app = App(workspace, moviehub, python)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server, app
