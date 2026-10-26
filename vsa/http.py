"""Bounded HTTP access for the analyser. Every request is policy-checked, logged and size-limited.

External websites are untrusted: responses are capped, redirects are followed manually so each
hop is re-checked against the access policy, and credentials never cross hosts.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from .contracts import Evidence, to_dict
from .policy import AccessDenied, AccessPolicy

MAX_BODY = 8 * 1024 * 1024
RETRY_STATUS = {429, 500, 502, 503, 504}
SENSITIVE = re.compile(r"(token|key|sig|signature|auth|session|secret|password|expires|hash|^h$|policy|cred)", re.I)


def redact(url: str) -> str:
    """Keep URL structure for evidence; hide query values that look like credentials or tokens."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<malformed>"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        hidden = SENSITIVE.search(key) or len(value) > 48
        query.append((key, "<redacted>" if hidden and value else value))
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))


class EvidenceLog:
    """Append-only, thread-safe log of HTTP exchanges (JSON lines when a path is given)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.rows: list[Evidence] = []
        self._lock = threading.Lock()
        self.listeners: list[Callable[[Evidence], None]] = []

    def add(self, row: Evidence) -> None:
        with self._lock:
            self.rows.append(row)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as handle:
                    handle.write(json.dumps(to_dict(row)) + "\n")
        for listener in list(self.listeners):
            listener(row)


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    truncated: bool = False
    from_cache: bool = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").lower()

    def text(self) -> str:
        charset = re.search(r"charset=([\w-]+)", self.content_type)
        try:
            return self.body.decode(charset.group(1) if charset else "utf-8", "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text())

    @property
    def auth_required(self) -> bool:
        return self.status == 401 or "www-authenticate" in self.headers


class Recorder:
    """Records exchanges so a run can be replayed offline (used for generated adapter tests)."""

    def __init__(self, max_body: int = 256 * 1024):
        self.max_body = max_body
        self.entries: list[dict] = []
        self._lock = threading.Lock()

    def add(self, method: str, url: str, request_range: str | None, response: Response) -> None:
        body = response.body[: self.max_body]
        keep = {k: v for k, v in response.headers.items()
                if k in ("content-type", "content-range", "content-length", "location", "accept-ranges")}
        with self._lock:
            self.entries.append(dict(method=method, url=url, final_url=response.final_url, range=request_range,
                                     status=response.status,
                                     headers=keep, body=base64.b64encode(body).decode()))

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.entries, indent=1))


class Fetcher:
    def __init__(self, policy: AccessPolicy, log: EvidenceLog | None = None, cache=None,
                 recorder: Recorder | None = None, retries: int = 2, timeout: float = 20.0,
                 session_factory: Callable[[], requests.Session] = requests.Session):
        self.policy = policy
        self.log = log or EvidenceLog()
        self.cache = cache
        self.recorder = recorder
        self.retries = retries
        self.timeout = timeout
        self._session_factory = session_factory
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            self._local.session = session
        return session

    def get(self, url: str, headers: dict[str, str] | None = None, max_bytes: int = MAX_BODY,
            byte_range: tuple[int, int | None] | None = None, note: str = "", use_cache: bool = True,
            method: str = "GET", data: Any = None) -> Response:
        request_headers = {"User-Agent": self.policy.user_agent, "Accept": "*/*"}
        request_headers.update(headers or {})
        range_header = None
        if byte_range is not None:
            start, end = byte_range
            range_header = f"bytes={start}-{'' if end is None else end}"
            request_headers["Range"] = range_header
        cacheable = use_cache and self.cache is not None and method == "GET" and byte_range is None
        if cacheable:
            cached = self.cache.get(url)
            if cached is not None:
                self.log.add(Evidence(time.time(), method, redact(url), cached.status, cached.content_type,
                                      len(cached.body), 0, note=(note + " (cache)").strip()))
                if self.recorder:
                    self.recorder.add(method, url, range_header, cached)
                return cached
        attempt = 0
        while True:
            try:
                response = self._request(method, url, request_headers, max_bytes, data, note)
            except AccessDenied:
                raise
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt >= self.retries:
                    raise
                self.log.add(Evidence(time.time(), method, redact(url), 0, error=type(error).__name__,
                                      note="retrying"))
                attempt += 1
                time.sleep(min(8.0, 0.5 * 2 ** attempt))
                continue
            if response.status in RETRY_STATUS and attempt < self.retries:
                attempt += 1
                retry_after = response.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.isdigit() else 0.5 * 2 ** attempt
                time.sleep(min(30.0, delay))
                continue
            break
        if self.recorder:
            self.recorder.add(method, url, range_header, response)
        if cacheable and response.status == 200 and not response.truncated and len(response.body) < 2 * 1024 * 1024:
            if not response.content_type.startswith(("video/", "audio/")):
                self.cache.put(url, response)
        return response

    def _request(self, method: str, url: str, headers: dict[str, str], max_bytes: int, data: Any,
                 note: str) -> Response:
        current = url
        started = time.monotonic()
        for _ in range(6):
            try:
                self.policy.require(current)
            except AccessDenied as denied:
                self.log.add(Evidence(time.time(), method, redact(current), 0, error="access_denied",
                                      note=denied.reason))
                raise
            self.policy.wait_turn(current)
            hop_started = time.monotonic()
            try:
                raw = self._session().request(method, current, headers=headers, data=data, stream=True,
                                              allow_redirects=False, timeout=(min(10.0, self.timeout), self.timeout))
            except requests.RequestException as error:
                self.log.add(Evidence(time.time(), method, redact(current), 0, error=type(error).__name__, note=note))
                raise
            if raw.is_redirect and "location" in raw.headers:
                target = urljoin(current, raw.headers["location"])
                self.log.add(Evidence(time.time(), method, redact(current), raw.status_code,
                                      ms=int((time.monotonic() - hop_started) * 1000), note="redirect"))
                raw.close()
                if urlsplit(target).netloc != urlsplit(current).netloc:
                    headers = {k: v for k, v in headers.items() if k.lower() not in ("cookie", "authorization")}
                if raw.status_code == 303 or (raw.status_code in (301, 302) and method == "POST"):
                    method, data = "GET", None
                current = target
                continue
            chunks, size, truncated = [], 0, False
            try:
                for chunk in raw.iter_content(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        chunks.append(chunk[: len(chunk) - (size - max_bytes)])
                        truncated = True
                        break
                    chunks.append(chunk)
            finally:
                raw.close()
            body = b"".join(chunks)
            response = Response(url=url, final_url=current, status=raw.status_code,
                                headers={k.lower(): v for k, v in raw.headers.items()}, body=body,
                                elapsed_ms=int((time.monotonic() - started) * 1000), truncated=truncated)
            self.log.add(Evidence(time.time(), method, redact(current), response.status, response.content_type,
                                  len(body), response.elapsed_ms, note=note))
            return response
        raise AccessDenied(url, "too many redirects")

    def text(self, url: str, **kwargs) -> str:
        return self.get(url, **kwargs).text()

    def json(self, url: str, **kwargs) -> Any:
        return self.get(url, **kwargs).json()
