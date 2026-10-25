"""Read the frame inventory (outer frame sizes and counts) from the Excel sheet."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import openpyxl

# IKEA RIBBA: the outer frame size is the picture size plus 2.5 cm in both directions.
# Only used to derive the picture-size labels ("13x18"); the layout uses the outer sizes.
PICTURE_OFFSET = 2.5

_FORMAT_RE = re.compile(r"\s*([\d.,]+)\s*[x×X*]\s*([\d.,]+)\s*")


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def fmt(v: float) -> str:
    """Format a cm value without trailing '.0'."""
    return f"{v:g}"


@dataclass(frozen=True)
class FrameType:
    idx: int
    w: float  # outer width in cm (as listed: first number)
    h: float  # outer height in cm (as listed: second number)
    count: int

    @property
    def picture(self) -> tuple[float, float]:
        return (self.w - PICTURE_OFFSET, self.h - PICTURE_OFFSET)

    @property
    def picture_key(self) -> str:
        """Orientation independent picture size, e.g. '13x18'."""
        a, b = sorted(self.picture)
        return f"{fmt(a)}x{fmt(b)}"

    @property
    def orientation(self) -> str:
        if self.w > self.h:
            return "L"
        if self.h > self.w:
            return "P"
        return ""

    @property
    def name(self) -> str:
        return f"{self.picture_key} {self.orientation}".strip()

    @property
    def outer(self) -> str:
        return f"{fmt(self.w)}x{fmt(self.h)}"


@dataclass(frozen=True)
class Frame:
    """One physical frame (an instance of a FrameType)."""

    id: int
    type: int
    w: float
    h: float


def parse_format(text: str) -> tuple[float, float]:
    m = _FORMAT_RE.fullmatch(str(text))
    if not m:
        raise ValueError(f"cannot parse frame format {text!r} (expected 'W x H')")
    return _num(m.group(1)), _num(m.group(2))


def load_inventory(path: str | Path) -> list[FrameType]:
    """Read rows of 'Format' (W x H in cm) and count from the first sheet."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    types: list[FrameType] = []
    rows = ws.iter_rows(values_only=True)
    next(rows)  # header row: Format / Mänge
    for row in rows:
        if not row or row[0] is None:
            continue
        w, h = parse_format(row[0])
        count = int(row[1] or 0)
        if count <= 0:
            continue
        types.append(FrameType(len(types), w, h, count))
    wb.close()
    if not types:
        raise ValueError(f"no frames found in {path}")
    return types


def expand(types: list[FrameType]) -> list[Frame]:
    frames: list[Frame] = []
    for t in types:
        for _ in range(t.count):
            frames.append(Frame(len(frames), t.idx, t.w, t.h))
    return frames
