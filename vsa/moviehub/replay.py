"""Replay recorded HTTP exchanges into `requests`, the transport MovieHub's Python code uses.

This file is also copied verbatim into generated MovieHub tests, so it uses only the standard
library and `requests`. It follows MovieHub's own offline-test technique (patching
`requests.Session.request`). DNS is answered with a fixed public placeholder address because the
transport is replayed: no socket is ever opened.
"""
import base64
import io
import re
import socket
from urllib.parse import urlsplit

import requests
from requests.structures import CaseInsensitiveDict

PLACEHOLDER_ADDRESS = "93.184.215.14"


class Replay:
    def __init__(self, entries):
        self.entries = entries
        self.requests = []

    def _find(self, method, url, start):
        best = None
        for entry in self.entries:
            if entry["method"] != method or entry["url"] != url:
                continue
            first = 0
            if entry.get("range"):
                first = int(re.match(r"bytes=(\d+)-", entry["range"]).group(1))
            body_length = len(base64.b64decode(entry["body"]))
            if first <= start < first + max(body_length, 1) or (start == 0 and first == 0):
                if best is None or first > best[1]:
                    best = (entry, first)
        return best

    def request(self, session, method, url, **kwargs):
        headers = CaseInsensitiveDict(kwargs.get("headers") or {})
        wanted = re.match(r"bytes=(\d+)-(\d*)", headers.get("Range", "") or "")
        start = int(wanted.group(1)) if wanted else 0
        self.requests.append((method, url, headers.get("Range")))
        for entry in self.entries:
            # A recorded request that was redirected: answer the original URL with the redirect.
            if entry["method"] == method and entry["url"] == url and entry.get("final_url", url) != url:
                return self._response(url, 302, {"Location": entry["final_url"]}, b"")
        found = self._find(method, url, start) or self._find_final(method, url, start)
        if found is None:
            raise AssertionError("Unrecorded request: " + method + " " + url)
        entry, first = found
        body = base64.b64decode(entry["body"])
        out_headers = dict(entry.get("headers", {}))
        status = entry["status"]
        if wanted:
            end = int(wanted.group(2)) if wanted.group(2) else None
            offset = start - first
            body = body[offset:(end - first + 1) if end is not None else None]
            total = re.search(r"/(\d+)$", out_headers.get("content-range", ""))
            size = total.group(1) if total else "*"
            out_headers["content-range"] = "bytes %d-%d/%s" % (start, start + max(len(body), 1) - 1, size)
            out_headers["content-length"] = str(len(body))
            status = 206 if status in (200, 206) else status
        return self._response(url, status, out_headers, body)

    def _find_final(self, method, url, start):
        aliases = [dict(e, url=e["final_url"]) for e in self.entries if e.get("final_url") == url]
        return Replay(aliases)._find(method, url, start) if aliases else None

    @staticmethod
    def _response(url, status, headers, body):
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict(headers)
        response.raw = io.BytesIO(body)
        response.url = url
        response.reason = "Replayed"
        response.encoding = "utf-8"
        return response


def install(entries):
    """Start patches for `requests` and DNS; returns (replay, patches). Stop the patches after use."""
    from unittest import mock

    replay = Replay(entries)

    def transport(session, method, url, **kwargs):
        return replay.request(session, method, url, **kwargs)

    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PLACEHOLDER_ADDRESS, port or 443))]

    patches = [mock.patch.object(requests.Session, "request", new=transport),
               mock.patch.object(socket, "getaddrinfo", new=resolve)]
    for item in patches:
        item.start()
    return replay, patches


def hosts(entries):
    return sorted({urlsplit(e["url"]).hostname for e in entries})
