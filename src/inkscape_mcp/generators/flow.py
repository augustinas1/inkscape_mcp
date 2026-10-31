"""Flow generators: evenly spaced streamlines over a vector field (Jobard & Lefer, 1997).

Lines are traced through the field in both directions from a seed and stop at the region
edge, on approaching another line closer than `spacing * test_ratio`, or when they close on
themselves. New seeds are taken `spacing` to either side of every accepted line, so the
field fills evenly. All lines are drawn as one compound path.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable
from typing import Any

from . import common as c
from .registry import generator

Point = tuple[float, float]
Field = Callable[[float, float], Point]
FIELDS = ["swirl", "vortex", "waves", "parallel", "source"]


def make_field(kind: str, x: float, y: float, width: float, height: float, scale: float, angle: float,
               rng: random.Random) -> Field:  # fmt: skip
    """Unit-length direction field of `kind` over the region."""
    cx, cy = x + width / 2, y + height / 2
    if kind == "swirl":
        # Divergence-free: velocity = curl of a seeded smooth stream function.
        k = 2 * math.pi / scale
        terms = []
        for _ in range(4):
            a = rng.uniform(0, 2 * math.pi)
            terms.append((k * rng.uniform(0.6, 1.4) * math.cos(a), k * rng.uniform(0.6, 1.4) * math.sin(a),
                          rng.uniform(0, 2 * math.pi), rng.uniform(0.5, 1.0)))  # fmt: skip

        def raw(px: float, py: float) -> Point:
            vx = vy = 0.0
            for kx, ky, ph, amp in terms:
                g = amp * math.cos(kx * px + ky * py + ph)
                vx += g * ky
                vy -= g * kx
            return vx, vy
    elif kind == "vortex":

        def raw(px: float, py: float) -> Point:
            return -(py - cy), px - cx
    elif kind == "source":

        def raw(px: float, py: float) -> Point:
            return px - cx, py - cy
    elif kind == "waves":
        k = 2 * math.pi / scale
        ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))

        def raw(px: float, py: float) -> Point:
            s = (px - x) * ca + (py - y) * sa
            bend = 0.8 * math.cos(k * s)
            return ca - sa * bend, sa + ca * bend
    else:  # parallel
        ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))

        def raw(px: float, py: float) -> Point:
            return ca, sa

    def unit(px: float, py: float) -> Point:
        vx, vy = raw(px, py)
        n = math.hypot(vx, vy)
        return (vx / n, vy / n) if n > 1e-9 else (0.0, 0.0)

    return unit


def streamlines(
    field: Field,
    x: float,
    y: float,
    width: float,
    height: float,
    spacing: float,
    test_ratio: float,
    min_length: float,
    rng: random.Random,
    max_lines: int = 2000,
) -> list[list[Point]]:
    """Jobard-Lefer evenly spaced streamline placement."""
    step = spacing / 5
    dtest = spacing * test_ratio
    cell = spacing
    grid: dict[tuple[int, int], list[tuple[Point, int]]] = {}

    def inside(p: Point) -> bool:
        return x <= p[0] <= x + width and y <= p[1] <= y + height

    def near(p: Point, radius: float, line_id: int | None = None) -> bool:
        gx, gy = int((p[0] - x) / cell), int((p[1] - y) / cell)
        reach = int(radius / cell) + 1
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for q, lid in grid.get((gx + dx, gy + dy), ()):
                    if lid != line_id and (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 < radius * radius:
                        return True
        return False

    def trace(seed: Point, line_id: int) -> list[Point]:
        halves: list[list[Point]] = []
        close_gap = int(3 * spacing / step)
        for direction in (1, -1):
            pts = [seed]
            p = seed
            for _ in range(int(4 * (width + height) / step)):
                v1 = field(*p)
                mid = (p[0] + direction * v1[0] * step / 2, p[1] + direction * v1[1] * step / 2)
                v = field(*mid)  # RK2 (midpoint)
                if v == (0.0, 0.0):
                    break
                nxt = (p[0] + direction * v[0] * step, p[1] + direction * v[1] * step)
                if not inside(nxt) or near(nxt, dtest, line_id):
                    break
                # Closed orbits (vortex): stop when the line returns to its own start.
                own = halves[0] if halves else pts
                if len(own) > close_gap and c.dist(nxt, seed) < step * 1.5 and len(pts) > close_gap:
                    break
                pts.append(nxt)
                p = nxt
            halves.append(pts)
        return list(reversed(halves[1]))[:-1] + halves[0]

    def length(line: list[Point]) -> float:
        return sum(c.dist(a, b) for a, b in itertools.pairwise(line))

    lines: list[list[Point]] = []

    def accept(line: list[Point]) -> None:
        lid = len(lines)
        lines.append(line)
        for p in line:
            grid.setdefault((int((p[0] - x) / cell), int((p[1] - y) / cell)), []).append((p, lid))

    def try_seed(seed: Point) -> list[Point] | None:
        if not inside(seed) or near(seed, spacing):
            return None
        line = trace(seed, len(lines))
        if len(line) < 2 or length(line) < min_length:
            return None
        xs, ys = [q[0] for q in line], [q[1] for q in line]
        if max(max(xs) - min(xs), max(ys) - min(ys)) < 1.5 * spacing:
            return None  # a tiny loop round a singularity reads as a blot, not a line
        accept(line)
        return line

    queue: list[list[Point]] = []
    first = try_seed((x + width / 2 + rng.uniform(-spacing, spacing), y + height / 2 + rng.uniform(-spacing, spacing)))
    if first:
        queue.append(first)
    stride = max(1, int(spacing / step))
    misses = 0
    while len(lines) < max_lines:
        while queue and len(lines) < max_lines:
            line = queue.pop(0)
            for i in range(0, len(line), stride):
                p = line[min(i, len(line) - 1)]
                vx, vy = field(*p)
                for side in (1, -1):
                    new = try_seed((p[0] - side * vy * spacing, p[1] + side * vx * spacing))
                    if new:
                        queue.append(new)
        # Fill disconnected regions (around sources, sinks, separate vortices) from random seeds.
        new = try_seed((rng.uniform(x, x + width), rng.uniform(y, y + height)))
        if new:
            queue.append(new)
            misses = 0
        else:
            misses += 1
            if misses > 150:
                break
    return lines


@generator(
    "flow",
    choices={"field": FIELDS},
    ranges={"spacing": (0.2, 100), "scale": (1, 2000), "test_ratio": (0.2, 0.95), "weight": (0.02, 5)},
    docs={
        "field": "swirl (seeded, smooth) | vortex | waves | parallel | source",
        "spacing": "distance between neighbouring lines",
        "scale": "feature size of swirl/waves",
        "angle": "direction of parallel/waves, degrees",
        "test_ratio": "lines stop when nearer than spacing*test_ratio to another",
    },
)
def flow_lines(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    x: float,
    y: float,
    width: float,
    height: float,
    field: str = "swirl",
    spacing: float = 2.0,
    scale: float = 40.0,
    angle: float = 0.0,
    test_ratio: float = 0.5,
    min_length: float = 4.0,
    color: str = "ink",
    weight: float = 0.3,
) -> str:
    """Evenly spaced streamlines over a vector field: fibres, currents, calm textured backgrounds."""
    vec = make_field(field, x, y, width, height, scale, angle, rng)
    lines = streamlines(vec, x, y, width, height, spacing, test_ratio, min_length, rng)
    d = " ".join(c.smooth_path(line[::2] + ([line[-1]] if len(line) % 2 == 0 else [])) for line in lines)
    return c.group(
        gid,
        f"flow lines ({field})",
        c.path(f"{gid}-lines", "streamlines", d, c.stroke_style(c.color(style, color), weight)),
    )
