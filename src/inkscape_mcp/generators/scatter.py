"""Scatter generators: evenly spread, jittered and clustered point fields drawn as marks.

All draw their marks as ONE compound path (compact, still one editable object; Path ▸ Break
Apart splits it). Region = (x, y, width, height) in document user units.
"""

from __future__ import annotations

import math
import random
from typing import Any

from . import common as c
from .registry import generator

_MARKS = ["dot", "ring", "cross", "tick"]
Point = tuple[float, float]


def poisson_disc(
    width: float, height: float, spacing: float, rng: random.Random, periodic: bool = False, tries: int = 30
) -> list[Point]:
    """Bridson's Poisson-disc sampling: no two points closer than `spacing`, otherwise random.

    With `periodic`, distances wrap around the edges so the field tiles seamlessly.
    """
    cell = spacing / math.sqrt(2)
    cols, rows = max(1, math.ceil(width / cell)), max(1, math.ceil(height / cell))
    grid: dict[tuple[int, int], Point] = {}
    points: list[Point] = []

    def too_close(p: Point) -> bool:
        gx, gy = int(p[0] / cell), int(p[1] / cell)
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                nx, ny = gx + dx, gy + dy
                if periodic:
                    nx, ny = nx % cols, ny % rows
                q = grid.get((nx, ny))
                if q is None:
                    continue
                ddx, ddy = abs(p[0] - q[0]), abs(p[1] - q[1])
                if periodic:
                    ddx, ddy = min(ddx, width - ddx), min(ddy, height - ddy)
                if ddx * ddx + ddy * ddy < spacing * spacing:
                    return True
        return False

    def add(p: Point) -> None:
        points.append(p)
        grid[(int(p[0] / cell), int(p[1] / cell))] = p

    add((rng.uniform(0, width), rng.uniform(0, height)))
    active = [0]
    while active:
        i = rng.randrange(len(active))
        base = points[active[i]]
        for _ in range(tries):
            a, r = rng.uniform(0, 2 * math.pi), spacing * (1 + rng.random())
            p = (base[0] + r * math.cos(a), base[1] + r * math.sin(a))
            if periodic:
                p = (p[0] % width, p[1] % height)
            elif not (0 <= p[0] < width and 0 <= p[1] < height):
                continue
            if not too_close(p):
                add(p)
                active.append(len(points) - 1)
                break
        else:
            active.pop(i)
    return points


def _marks(gid: str, label: str, pts: list[Point], mark: str, size: float, col: str, weight: float) -> str:
    if mark == "dot":
        return c.path(f"{gid}-marks", label, c.dot_path(pts, size / 2), f"fill:{col};stroke:none")
    if mark == "ring":
        return c.path(f"{gid}-marks", label, c.dot_path(pts, size / 2), c.stroke_style(col, weight))
    h = size / 2
    if mark == "cross":
        d = " ".join(f"M {c.f(x - h)},{c.f(y)} H {c.f(x + h)} M {c.f(x)},{c.f(y - h)} V {c.f(y + h)}" for x, y in pts)
    else:  # tick
        d = " ".join(f"M {c.f(x)},{c.f(y - h)} V {c.f(y + h)}" for x, y in pts)
    return c.path(f"{gid}-marks", label, d, c.stroke_style(col, weight))


def _offset(pts: list[Point], x: float, y: float) -> list[Point]:
    return [(px + x, py + y) for px, py in pts]


_DOCS = {
    "spacing": "minimum distance between points",
    "mark": "dot | ring | cross | tick",
    "size": "mark diameter / length",
    "color": "'#rrggbb' or a style token path, e.g. 'ink' or 'species.rna.color'",
    "periodic": "wrap distances at the edges so the field tiles seamlessly (pattern fills)",
}


@generator("scatter", choices={"mark": _MARKS}, ranges={"spacing": (0.2, 200), "size": (0.05, 50)}, docs=_DOCS)
def scatter_even(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    x: float,
    y: float,
    width: float,
    height: float,
    spacing: float = 3.0,
    mark: str = "dot",
    size: float = 0.8,
    color: str = "ink",
    weight: float = 0.3,
    periodic: bool = False,
) -> str:
    """Evenly spread random points (Poisson-disc): no clumps, no gaps, no visible grid."""
    inset = 0.0 if periodic else size / 2  # keep marks inside the region (a tile must not be inset)
    pts = _offset(poisson_disc(width - 2 * inset, height - 2 * inset, spacing, rng, periodic), x + inset, y + inset)
    return c.group(gid, "even scatter", _marks(gid, "marks", pts, mark, size, c.color(style, color), weight))


@generator("scatter", choices={"mark": _MARKS}, ranges={"spacing": (0.2, 200), "jitter": (0, 1)}, docs=_DOCS)
def scatter_grid(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    x: float,
    y: float,
    width: float,
    height: float,
    spacing: float = 3.0,
    jitter: float = 0.3,
    hexagonal: bool = True,
    mark: str = "dot",
    size: float = 0.8,
    color: str = "ink",
    weight: float = 0.3,
) -> str:
    """Grid points nudged by `jitter` (fraction of spacing); `hexagonal` offsets alternate rows."""
    pts: list[Point] = []
    row_h = spacing * (math.sqrt(3) / 2 if hexagonal else 1)
    rows, cols = int(height / row_h) + 1, int(width / spacing) + 1
    for r in range(rows):
        shift = spacing / 2 if hexagonal and r % 2 else 0
        for k in range(cols):
            px = k * spacing + shift + rng.uniform(-jitter, jitter) * spacing / 2
            py = r * row_h + rng.uniform(-jitter, jitter) * spacing / 2
            if size / 2 <= px <= width - size / 2 and size / 2 <= py <= height - size / 2:
                pts.append((x + px, y + py))
    return c.group(gid, "jittered grid", _marks(gid, "marks", pts, mark, size, c.color(style, color), weight))


@generator(
    "scatter",
    choices={"mark": _MARKS},
    ranges={"clusters": (1, 200), "per_cluster": (1, 500), "spread": (0.1, 200)},
    docs={**_DOCS, "spread": "cluster radius (standard deviation)"},
)
def scatter_clusters(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    x: float,
    y: float,
    width: float,
    height: float,
    clusters: int = 5,
    per_cluster: int = 12,
    spread: float = 3.0,
    mark: str = "dot",
    size: float = 0.8,
    color: str = "ink",
    weight: float = 0.3,
) -> str:
    """Clustered points (Thomas process): evenly spaced cluster centres, Gaussian members."""
    centres = poisson_disc(width, height, max(spread * 3, min(width, height) / (clusters**0.5 + 1)), rng)
    rng.shuffle(centres)
    pts: list[Point] = []
    for cx, cy in centres[:clusters]:
        for _ in range(per_cluster):
            px, py = rng.gauss(cx, spread), rng.gauss(cy, spread)
            if size / 2 <= px <= width - size / 2 and size / 2 <= py <= height - size / 2:
                pts.append((x + px, y + py))
    return c.group(gid, "clustered scatter", _marks(gid, "marks", pts, mark, size, c.color(style, color), weight))
