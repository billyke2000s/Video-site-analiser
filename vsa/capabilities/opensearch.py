"""OpenSearch description documents -> search URL templates."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from ..contracts import SearchInterface


def parse_description(xml: str, base_url: str) -> list[SearchInterface]:
    if len(xml) > 256 * 1024 or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        return []  # untrusted XML: refuse document type declarations outright
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    found = []
    for element in root.iter():
        if not element.tag.endswith("Url"):
            continue
        template = element.get("template", "")
        kind = element.get("type", "")
        method = (element.get("method") or "GET").upper()
        if "{searchTerms}" not in template or method != "GET" or "suggestions" in kind:
            continue
        # Optional OpenSearch parameters ({startPage?}) are dropped; required others are unsupported.
        template = re.sub(r"[&?]?[\w.-]+=\{[\w:]+\?\}", "", template)
        if re.search(r"\{(?!searchTerms\})[\w:]+\}", template):
            continue
        found.append(SearchInterface("opensearch", urljoin(base_url, template.replace("{searchTerms}", "{query}")),
                                     evidence=f"OpenSearch Url type={kind or 'unspecified'}"))
    # Prefer HTML result pages over feeds for human review; both are recorded.
    return sorted(found, key=lambda s: "html" not in s.evidence)
