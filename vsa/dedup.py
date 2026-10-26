"""Cross-site deduplication of verified media.

Two sources are the *same media* only on content evidence:
  - an identical checksum declared by the sites (SHA-1 / MD5), or
  - identical leading bytes (SHA-256 of the first 64 KiB) **and** identical total size, or, for
    segmented streams, an identical first segment.
Host names, paths, titles and labels are never used to merge, so different encodes on one host
stay separate while one file mirrored on several sites or servers is recognised as one.

Sources with the same rounded duration but different content are reported as `related`
(probably the same work in a different encode), never merged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import MediaReport


def keys(report: MediaReport) -> list[str]:
    declared = report.candidate.declared
    found = []
    for name in ("sha1", "md5"):
        value = str(declared.get(name) or "").strip().lower()
        if value and all(c in "0123456789abcdef" for c in value):
            found.append(f"{name}:{value}")
    head = report.fingerprint.get("head_sha256")
    if head:
        if report.container in ("hls", "dash"):
            found.append(f"segment:{head}")
        elif report.fingerprint.get("size"):
            found.append(f"head:{head}:{report.fingerprint['size']}")
    return found


@dataclass
class MediaGroup:
    media_id: str
    members: list[MediaReport] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)

    @property
    def sites(self) -> list[str]:
        return sorted({m.candidate.site for m in self.members})

    @property
    def cross_site(self) -> bool:
        return len(self.sites) > 1


def group(reports: list[MediaReport]) -> list[MediaGroup]:
    parent = list(range(len(reports)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    links: dict[int, set[str]] = {i: set() for i in range(len(reports))}
    for index, report in enumerate(reports):
        for key in keys(report):
            links[index].add(key)
            if key in owner:
                parent[find(index)] = find(owner[key])
            else:
                owner[key] = index
    groups: dict[int, MediaGroup] = {}
    for index, report in enumerate(reports):
        root = find(index)
        item = groups.get(root)
        if item is None:
            item = groups[root] = MediaGroup(media_id=f"unidentified:{index}")
        item.members.append(report)
        for key in sorted(links[index]):
            if key not in item.evidence:
                item.evidence.append(key)
    result = list(groups.values())
    for item in result:
        if item.evidence:  # name each group by its strongest evidence, from any member
            item.media_id = min(item.evidence, key=_strength)
    # Related encodes: same duration (to 2 s) but different content.
    for a in result:
        da = _duration(a)
        for b in result:
            db = _duration(b)
            if a is not b and da and db and abs(da - db) <= 2.0:
                a.related.append(b.media_id)
    return result


def _strength(key: str) -> tuple[int, str]:
    """Declared checksums name a group before byte fingerprints."""
    order = ("sha1:", "md5:", "head:", "segment:")
    return next((i for i, prefix in enumerate(order) if key.startswith(prefix)), len(order)), key


def _duration(item: MediaGroup) -> float | None:
    values = [m.duration for m in item.members if m.duration]
    return values[0] if values else None
