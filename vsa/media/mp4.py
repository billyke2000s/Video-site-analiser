"""Minimal ISO-BMFF top-level box scanning, to find the `moov` index without downloading a film."""
from __future__ import annotations

import struct


def top_level_boxes(data: bytes, total_size: int | None = None) -> list[tuple[str, int, int]]:
    """(type, offset, size) for every top-level box whose header lies inside `data`.

    Box sizes let us step past `mdat` without reading it, so a box listed here may extend past
    the end of `data`. Scanning stops at the first header outside `data` or a malformed box.
    """
    boxes = []
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[offset:offset + 8])
        try:
            name = kind.decode("ascii")
        except UnicodeDecodeError:
            break
        if not name.isprintable():
            break
        if size == 1:
            if offset + 16 > len(data):
                break
            size = struct.unpack(">Q", data[offset + 8:offset + 16])[0]
        elif size == 0:
            size = (total_size - offset) if total_size else len(data) - offset
        if size < 8:
            break
        boxes.append((name, offset, size))
        offset += size
    return boxes


def next_box_offset(data: bytes) -> int | None:
    """Offset of the first top-level box that starts beyond `data`, if the chain reaches it."""
    boxes = top_level_boxes(data)
    if not boxes:
        return None
    name, offset, size = boxes[-1]
    end = offset + size
    return end if end >= len(data) else None


def is_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] in (b"ftyp", b"styp", b"moov", b"free", b"skip", b"wide", b"mdat")


def read_box_header(data: bytes) -> tuple[str, int] | None:
    if len(data) < 8:
        return None
    size, kind = struct.unpack(">I4s", data[:8])
    if size == 1 and len(data) >= 16:
        size = struct.unpack(">Q", data[8:16])[0]
    try:
        return kind.decode("ascii"), size
    except UnicodeDecodeError:
        return None
