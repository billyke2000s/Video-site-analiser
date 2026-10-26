"""Shared test helpers."""
import os
import shutil
import sys
from pathlib import Path

from vsa.policy import AccessPolicy, SitePermission

ROOT = Path(__file__).resolve().parents[2]
MOVIEHUB = Path(os.environ.get("MOVIEHUB_CHECKOUT", ROOT.parent / "billyke2000s" / "moviehub"))
MOVIEHUB_PYTHON = os.environ.get("MOVIEHUB_PYTHON", sys.executable)


def moviehub_available() -> bool:
    if not (MOVIEHUB / "android-tv-launcher/app/src/main/python/moviehub_vod.py").exists():
        return False
    import subprocess
    done = subprocess.run([MOVIEHUB_PYTHON, "-c", "import requests, six"], capture_output=True)
    return done.returncode == 0


def media_tools() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def fixture_policy(*hosts: str, robots: bool = True) -> AccessPolicy:
    return AccessPolicy([SitePermission(domain=h, reason="local test fixture", rate_per_second=0, allow_http=True,
                                        allow_private_network=True, respect_robots=robots) for h in hosts])
