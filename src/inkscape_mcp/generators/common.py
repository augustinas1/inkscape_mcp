"""Shared helpers for generators: number formatting, splines, colours from style tokens."""

from __future__ import annotations

import math
from typing import Any
from xml.sax.saxutils import escape

Point = tuple[float, float]


def f(v: float) -> str:
    """Compact coordinate formatting (3 decimals, no trailing zeros)."""
    text = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def catmull_rom(pts: list[Point], closed: bool = False) -> list[tuple[Point, Point, Point, Point]]:
    """Cubic Bézier segments (p0, c1, c2, p1) of the Catmull-Rom spline through `pts`."""
    n = len(pts)
    segs = []
    for i in range(n) if closed else range(n - 1):
        p0 = pts[i - 1] if (closed or i > 0) else pts[i]
        p1, p2 = pts[i], pts[(i + 1) % n]
        p3 = pts[(i + 2) % n] if (closed or i + 2 < n) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        segs.append((p1, c1, c2, p2))
    return segs


def smooth_path(pts: list[Point], closed: bool = False) -> str:
    """Path data for a smooth curve through `pts`."""
    if len(pts) < 2:
        return ""
    d = f"M {f(pts[0][0])},{f(pts[0][1])}"
    for _, c1, c2, p in catmull_rom(pts, closed):
        d += f" C {f(c1[0])},{f(c1[1])} {f(c2[0])},{f(c2[1])} {f(p[0])},{f(p[1])}"
    return d + (" Z" if closed else "")


def sample_spline(pts: list[Point], per_segment: int = 12, closed: bool = False) -> list[Point]:
    """Dense points ON the spline that smooth_path draws (for hit-testing and overlays)."""
    out: list[Point] = []
    for p0, c1, c2, p1 in catmull_rom(pts, closed):
        for k in range(per_segment):
            t = k / per_segment
            a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t**3
            out.append((a * p0[0] + b * c1[0] + c * c2[0] + d * p1[0], a * p0[1] + b * c1[1] + c * c2[1] + d * p1[1]))
    if pts and not closed:
        out.append(pts[-1])
    return out


def polyline(pts: list[Point]) -> str:
    return "M " + " L ".join(f"{f(x)},{f(y)}" for x, y in pts) if pts else ""


def color(style: dict[str, Any], value: str) -> str:
    """A colour: '#rrggbb' as is, 'none', or a dotted style-token path ('ink', 'species.rna.color')."""
    if value.startswith("#") or value == "none":
        return value
    node: Any = style
    for part in value.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"unknown colour token {value!r}")
        node = node[part]
    if not isinstance(node, str):
        raise ValueError(f"colour token {value!r} is not a colour")
    return node


def tint(hex_color: str, amount: float) -> str:
    """Opaque colour `hex_color` shows at opacity `amount` over white (paler for smaller amount)."""
    return "#" + "".join(f"{round(255 - (255 - int(hex_color[i : i + 2], 16)) * amount):02x}" for i in (1, 3, 5))


def group(gid: str, label: str, body: str, extra: str = "") -> str:
    return f'<g id="{gid}" inkscape:label="{escape(label)}"{extra}>{body}</g>'


def path(pid: str, label: str, d: str, style: str) -> str:
    return f'<path id="{pid}" inkscape:label="{escape(label)}" d="{d}" style="{style}"/>'


def stroke_style(col: str, width: float, cap: str = "round") -> str:
    return f"fill:none;stroke:{col};stroke-width:{f(width)};stroke-linecap:{cap};stroke-linejoin:round"


def dot_path(points: list[Point], r: float) -> str:
    """Many filled dots as ONE path (two arcs each) — compact, still one editable object."""
    return " ".join(
        f"M {f(x - r)},{f(y)} a {f(r)},{f(r)} 0 1 0 {f(2 * r)},0 a {f(r)},{f(r)} 0 1 0 {f(-2 * r)},0" for x, y in points
    )


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
