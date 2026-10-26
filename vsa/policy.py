"""Site-access permissions. Every request the analyser makes passes through `AccessPolicy.check`.

Rules:
- A host is reachable only if an explicit permission entry covers it (deny by default).
- HTTPS only, unless the entry allows plain HTTP.
- Hosts must resolve to public addresses, unless the entry is a local test fixture.
- robots.txt is honoured for every permitted host (unless the entry opts out, which should be
  reserved for the operator's own sites).
- Credentials in URLs are refused. The analyser never logs in, solves challenges or removes DRM.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import json
import socket
import threading
import time
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests

DEFAULT_USER_AGENT = "VideoSiteAnalyser/0.1 (+https://github.com/billyke2000s/Video-site-analiser)"


@dataclass
class SitePermission:
    domain: str
    reason: str
    include_subdomains: bool = True
    extra_hosts: list[str] = field(default_factory=list)  # glob patterns, e.g. "*.us.archive.org"
    rate_per_second: float = 1.0
    respect_robots: bool = True
    allow_http: bool = False
    allow_private_network: bool = False  # local test fixtures only

    def covers(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        domain = self.domain.lower()
        if host == domain or (self.include_subdomains and host.endswith("." + domain)):
            return True
        return any(fnmatch.fnmatch(host, pattern.lower()) for pattern in self.extra_hosts)


@dataclass
class AccessDecision:
    allowed: bool
    reason: str
    permission: SitePermission | None = None


class AccessDenied(Exception):
    def __init__(self, url: str, reason: str):
        super().__init__(reason)
        self.url = url
        self.reason = reason


class AccessPolicy:
    def __init__(self, permissions: list[SitePermission], user_agent: str = DEFAULT_USER_AGENT,
                 robots_timeout: float = 10.0, resolver=socket.getaddrinfo):
        self.permissions = list(permissions)
        self.user_agent = user_agent
        self.robots_timeout = robots_timeout
        self._resolver = resolver
        self._robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None, str]] = {}
        self._lock = threading.Lock()
        self._last_request: dict[str, float] = {}
        self._rate_lock = threading.Lock()

    # --- loading -------------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path, **kwargs) -> "AccessPolicy":
        data = json.loads(Path(path).read_text()) if Path(path).exists() else {"sites": []}
        return cls([SitePermission(**row) for row in data.get("sites", [])], **kwargs)

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"sites": [asdict(p) for p in self.permissions]}, indent=2) + "\n")

    def permit(self, permission: SitePermission) -> None:
        self.permissions = [p for p in self.permissions if p.domain != permission.domain] + [permission]

    # --- checks --------------------------------------------------------------------------
    def permission_for(self, host: str) -> SitePermission | None:
        matches = [p for p in self.permissions if p.covers(host)]
        # The most specific domain wins.
        return max(matches, key=lambda p: len(p.domain)) if matches else None

    def check(self, url: str, robots: bool = True) -> AccessDecision:
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            return AccessDecision(False, "malformed URL")
        if parts.scheme not in ("http", "https") or not host:
            return AccessDecision(False, "unsupported scheme")
        if parts.username or parts.password:
            return AccessDecision(False, "credentials in URL")
        if len(url) > 16384 or "\r" in url or "\n" in url:
            return AccessDecision(False, "URL too long or contains line breaks")
        permission = self.permission_for(host)
        if permission is None:
            return AccessDecision(False, f"no permission entry for {host}")
        if parts.scheme == "http" and not permission.allow_http:
            return AccessDecision(False, "plain HTTP not permitted for this site", permission)
        if not permission.allow_private_network:
            try:
                port = parts.port or (443 if parts.scheme == "https" else 80)
                addresses = {item[4][0] for item in self._resolver(host, port, type=socket.SOCK_STREAM)}
            except (OSError, ValueError) as error:
                return AccessDecision(False, f"DNS lookup failed: {type(error).__name__}", permission)
            for address in addresses:
                if not ipaddress.ip_address(address.split("%")[0]).is_global:
                    return AccessDecision(False, "host resolves to a non-public address", permission)
        if robots and permission.respect_robots:
            allowed, why = self._robots_allows(url, parts)
            if not allowed:
                return AccessDecision(False, why, permission)
        return AccessDecision(True, "permitted", permission)

    def require(self, url: str, robots: bool = True) -> SitePermission:
        decision = self.check(url, robots=robots)
        if not decision.allowed:
            raise AccessDenied(url, decision.reason)
        return decision.permission  # type: ignore[return-value]

    def wait_turn(self, url: str) -> None:
        """Simple per-permission rate limit: at most `rate_per_second` requests per site."""
        host = urlsplit(url).hostname or ""
        permission = self.permission_for(host)
        if permission is None or permission.rate_per_second <= 0:
            return
        interval = 1.0 / permission.rate_per_second
        with self._rate_lock:
            now = time.monotonic()
            ready = self._last_request.get(permission.domain, 0.0) + interval
            self._last_request[permission.domain] = max(now, ready)
        if ready > now:
            time.sleep(ready - now)

    # --- robots.txt -------------------------------------------------------------------------
    def _robots_allows(self, url: str, parts) -> tuple[bool, str]:
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            cached = self._robots.get(origin)
        if cached is None or time.monotonic() - cached[0] > 3600:
            parser, note = self._fetch_robots(origin)
            cached = (time.monotonic(), parser, note)
            with self._lock:
                self._robots[origin] = cached
        parser, note = cached[1], cached[2]
        if parser is None:
            return (note == "no robots.txt", "robots.txt " + note)
        if parser.can_fetch(self.user_agent, url):
            return True, "robots.txt allows"
        return False, "disallowed by robots.txt"

    def _fetch_robots(self, origin: str) -> tuple[urllib.robotparser.RobotFileParser | None, str]:
        url = origin + "/robots.txt"
        # robots.txt itself is fetched under the same host/address rules, without recursion.
        decision = self.check(url, robots=False)
        if not decision.allowed:
            return None, "not checkable: " + decision.reason
        try:
            with requests.Session() as session:
                response = session.get(url, headers={"User-Agent": self.user_agent},
                                       timeout=self.robots_timeout, allow_redirects=True, stream=True)
                body = response.raw.read(512 * 1024, decode_content=True)
                final_host = urlsplit(response.url).hostname or ""
                if self.permission_for(final_host) is None:
                    return None, "redirected to an unpermitted host"
        except requests.RequestException as error:
            # Unreachable robots.txt: be conservative and treat the site as disallowed.
            return None, f"unreachable ({type(error).__name__})"
        if response.status_code in (401, 403):
            return None, f"returned HTTP {response.status_code}"
        if 400 <= response.status_code < 500:
            return None, "no robots.txt"
        if response.status_code >= 500:
            return None, f"returned HTTP {response.status_code}"
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(body.decode("utf-8", "replace").splitlines())
        return parser, "parsed"
