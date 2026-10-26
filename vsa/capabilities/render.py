"""Optional JavaScript rendering with a headless browser (Playwright).

Every browser request is checked against the same access policy as ordinary fetches; anything
else is aborted. Media and manifest requests the page makes are recorded (not downloaded in full).
The browser profile is temporary; no cookies, logins or extensions are used.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from ..http import EvidenceLog, redact
from ..contracts import Evidence
from ..policy import AccessPolicy

MEDIA_TYPES = ("video/", "audio/", "application/vnd.apple.mpegurl", "application/x-mpegurl",
               "application/dash+xml")
MEDIA_SUFFIXES = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".m3u8", ".mpd")


@dataclass
class Rendered:
    url: str
    html: str
    network_media: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def browser_executable() -> str | None:
    """A Chrome/Chromium binary: VSA_CHROME, Playwright's own download, or a system install."""
    configured = os.environ.get("VSA_CHROME")
    if configured:
        return configured
    for candidate in ("/opt/google/chrome/chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(candidate) or (candidate if os.path.exists(candidate) else None)
        if found:
            return found
    return None


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def render(url: str, policy: AccessPolicy, log: EvidenceLog, timeout_ms: int = 20000,
           settle_ms: int = 1500) -> Rendered:
    from playwright.sync_api import sync_playwright

    policy.require(url)
    result = Rendered(url=url, html="")
    import time

    def route(route_obj):
        request_url = route_obj.request.url
        if request_url.startswith(("data:", "blob:")):
            return route_obj.continue_()
        decision = policy.check(request_url)
        if not decision.allowed:
            result.blocked.append(redact(request_url))
            log.add(Evidence(time.time(), route_obj.request.method, redact(request_url), 0,
                             error="access_denied", note="browser: " + decision.reason))
            return route_obj.abort()
        return route_obj.continue_()

    def on_response(response):
        content_type = (response.headers or {}).get("content-type", "").lower()
        path = response.url.split("?", 1)[0].lower()
        if content_type.startswith(MEDIA_TYPES) or path.endswith(MEDIA_SUFFIXES):
            result.network_media.append(response.url)
        log.add(Evidence(time.time(), response.request.method, redact(response.url), response.status,
                         content_type, note="browser"))

    with sync_playwright() as playwright:
        launch = dict(headless=True, args=["--no-sandbox", "--disable-extensions", "--mute-audio"])
        executable = browser_executable()
        if executable:
            launch["executable_path"] = executable
        browser = playwright.chromium.launch(**launch)
        try:
            context = browser.new_context(user_agent=policy.user_agent, accept_downloads=False,
                                          service_workers="block")
            context.route("**/*", route)
            page = context.new_page()
            page.on("response", on_response)
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass  # long-polling pages never go idle; the DOM so far is still useful
            page.wait_for_timeout(settle_ms)
            result.html = page.content()
            result.url = page.url
        finally:
            browser.close()
    result.network_media = list(dict.fromkeys(result.network_media))
    return result
