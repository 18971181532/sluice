"""Text output that stays legible in a terminal and in a README.

Nothing here knows about TCP.  The only opinion is that numbers should carry
their unit, alignment should follow the type, and a curve is worth more than a
table of cwnd values when the point is the shape.
"""

from __future__ import annotations

import unicodedata
from typing import Iterable, List, Optional, Sequence, Tuple

BAR = "▁▂▃▄▅▆▇█"


def width(text: str) -> int:
    """Display width: CJK characters count two, combining marks count zero."""
    total = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in "WF" else 1
    return total


def pad(text: str, size: int, align: str = "left") -> str:
    gap = max(0, size - width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def plural(count: int, word: str) -> str:
    return word if count == 1 else word + "s"


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    aligns: Optional[Sequence[str]] = None,
) -> str:
    body = [list(map(str, row)) for row in rows]
    columns = len(headers)
    if aligns is None:
        aligns = ["left"] * columns
    widths = [width(header) for header in headers]
    for row in body:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], width(cell))
    out = ["  ".join(pad(h, widths[i], aligns[i]) for i, h in enumerate(headers))]
    out.append("  ".join("─" * widths[i] for i in range(columns)))
    for row in body:
        out.append(
            "  ".join(pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(out)


def pairs(rows: Iterable[Sequence[str]]) -> str:
    """Key/value block with no header rule -- a label is not a column."""
    body = [list(map(str, row)) for row in rows]
    left = max((width(row[0]) for row in body), default=0)
    right = max((width(row[1]) for row in body if len(row) > 1), default=0)
    return "\n".join(
        f"{pad(row[0], left)}  {pad(row[1], right)}".rstrip() for row in body
    )


def number(value: float, digits: int = 1) -> str:
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.{digits}f}"


def size(count: float) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if abs(count) >= scale:
            return f"{count / scale:.1f} {unit}"
    return f"{count:.0f} B"


def rate(mbps: float) -> str:
    return f"{mbps:.2f} Mbit/s"


def millis(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.1f} ms"
    return f"{seconds:.2f} s"


def percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def bar(value: float, maximum: float, width_of: int = 24) -> str:
    if maximum <= 0:
        return ""
    filled = int(round(max(0.0, min(1.0, value / maximum)) * width_of))
    return "█" * filled + "·" * (width_of - filled)


def sparkline(values: Sequence[float], width_of: int = 60) -> str:
    """One line, no axes.  Good for a shape you already know how to read."""
    if not values:
        return ""
    if len(values) > width_of:
        step = len(values) / width_of
        values = [values[min(int(i * step), len(values) - 1)] for i in range(width_of)]
    low = min(values)
    high = max(values)
    if high == low:
        return BAR[0] * len(values)
    return "".join(BAR[int((v - low) / (high - low) * (len(BAR) - 1))] for v in values)


def _ticks(low: float, high: float, count: int) -> List[float]:
    if count <= 1:
        return [high]
    span = high - low
    return [low + span * index / (count - 1) for index in range(count)]


def chart(
    points: Sequence[Tuple[float, float]],
    width_of: int = 66,
    height: int = 12,
    title: str = "",
    unit: str = "",
    x_unit: str = "s",
    scale: float = 1.0,
) -> str:
    """A line chart in monospace.

    Columns are time buckets; each bucket keeps the *peak* value, because a
    congestion window that spikes for one millisecond is the interesting part,
    and averaging it away would hide the thing being measured.
    """
    if not points:
        return "(no samples)"
    end = max(x for x, _ in points) or 1.0
    peak = max(y for _, y in points) * scale
    ceiling = peak if peak > 0 else 1.0
    buckets: List[Optional[float]] = [None] * width_of
    for x, y in points:
        column = min(width_of - 1, max(0, int(x / end * width_of)))
        value = y * scale
        held = buckets[column]
        buckets[column] = value if held is None else max(held, value)

    # A bucket with no sample keeps the previous value.  Dropping it to zero
    # would turn a sparse series into a comb of spikes: the samples are event
    # times, so a gap means "nothing changed", not "it fell to zero".
    columns: List[float] = []
    carried = 0.0
    for value in buckets:
        if value is not None:
            carried = value
        columns.append(carried)

    def row_of(value: float) -> int:
        return height - 1 - int(round(min(max(value, 0.0) / ceiling, 1.0) * (height - 1)))

    rows = [row_of(value) for value in columns]
    grid = [[" "] * width_of for _ in range(height)]
    for column, row in enumerate(rows):
        previous = rows[column - 1] if column else row
        if row == previous:
            grid[row][column] = "─"
            continue
        if row < previous:  # the line climbs: up from the left, out to the right
            grid[previous][column] = "╯"
            grid[row][column] = "╭"
        else:  # and here it falls
            grid[previous][column] = "╮"
            grid[row][column] = "╰"
        for between in range(min(row, previous) + 1, max(row, previous)):
            grid[between][column] = "│"

    label_rows = sorted({0, (height - 1) // 3, 2 * (height - 1) // 3, height - 1})
    labels = {row: ceiling * (height - 1 - row) / max(height - 1, 1)
              for row in label_rows}
    label_width = max(len(number(round(value))) for value in labels.values())
    lines = [title] if title else []
    for row in range(height):
        text = number(round(labels[row])) if row in labels else ""
        lines.append(f"{pad(text, label_width, 'right')} ┤" + "".join(grid[row]).rstrip())
    lines.append(" " * label_width + " └" + "─" * width_of)
    note = f" 0{' ' * max(width_of - 16, 1)}{end:.1f} {x_unit}"
    if unit:
        note += f"     ({unit})"
    lines.append(" " * (label_width + 1) + note)
    return "\n".join(lines)


def wrap(text: str, limit: int = 78, indent: str = "") -> str:
    words = text.split()
    lines: List[str] = []
    current = indent
    for word in words:
        candidate = current + word if current.strip() == "" else current + " " + word
        if width(candidate) > limit and current.strip():
            lines.append(current)
            current = indent + word
        else:
            current = candidate
    if current.strip():
        lines.append(current)
    return "\n".join(lines)
