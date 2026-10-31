"""Organic generators: seeded blobs, folded chains with over/under crossings, wobbly lines."""

from __future__ import annotations

import math
import random
from typing import Any

from . import common as c
from .registry import generator

Point = tuple[float, float]


def blob_points(cx: float, cy: float, rx: float, ry: float, lumpiness: float, rng: random.Random) -> list[Point]:
    """Points of a smooth closed outline: an ellipse perturbed by a few random low harmonics."""
    harmonics = [(k, rng.uniform(-1, 1) * lumpiness / k**0.6, rng.uniform(0, 2 * math.pi)) for k in range(2, 6)]
    n = 20
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        r = 1 + sum(a * math.sin(k * t + ph) for k, a, ph in harmonics)
        pts.append((cx + rx * r * math.cos(t), cy + ry * r * math.sin(t)))
    return pts


@generator(
    "organic",
    ranges={"rx": (0.1, 1000), "ry": (0.1, 1000), "lumpiness": (0, 0.4)},
    docs={"lumpiness": "0 = ellipse; 0.05-0.15 = cell-like; 0.3 = very irregular"},
)
def blob(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    cx: float,
    cy: float,
    rx: float = 10.0,
    ry: float = 7.0,
    lumpiness: float = 0.08,
    fill: str = "species.protein.surface",
    stroke: str = "none",
    weight: float = 0.35,
) -> str:
    """Smooth, seeded organic outline (cell, compartment, surface): an editable closed path."""
    d = c.smooth_path(blob_points(cx, cy, rx, ry, lumpiness, rng), closed=True)
    css = f"fill:{c.color(style, fill)};stroke:{c.color(style, stroke)};stroke-width:{c.f(weight)}"
    return c.group(gid, "blob", c.path(f"{gid}-outline", "outline", d, css))


def _fold_points(cx: float, cy: float, rx: float, ry: float, variation: float, rng: random.Random) -> list[Point]:
    """A seeded variant of the hand-designed two-loop fold (see figure_kit._FOLD).

    The template is randomly rotated and mirrored, then bent by a smooth random displacement
    field of strength `variation`, so every seed differs but keeps the fold's good structure:
    two loops, a crossing, short termini.
    """
    from ..figure_kit import _FOLD

    angle = rng.uniform(0, 2 * math.pi)
    mirror = -1 if rng.random() < 0.5 else 1
    ca, sa = math.cos(angle), math.sin(angle)
    k = 2 * math.pi / 9.0  # displacement wavelength ~ template size
    waves = [(rng.uniform(-k, k), rng.uniform(-k, k), rng.uniform(0, 2 * math.pi)) for _ in range(3)]
    pts = []
    for tx, ty in _FOLD:
        tx *= mirror
        ux, uy = tx * ca - ty * sa, tx * sa + ty * ca  # rotate in template units
        dx = sum(math.sin(wx * ux + wy * uy + ph) for wx, wy, ph in waves) * variation * 0.9
        dy = sum(math.cos(wy * ux - wx * uy + ph) for wx, wy, ph in waves) * variation * 0.9
        pts.append((cx + (ux + dx) * rx / 7.0, cy + (uy + dy) * ry / 5.0))
    return pts


def _segment_hit(a: Point, b: Point, p: Point, q: Point) -> bool:
    def orient(u: Point, v: Point, w: Point) -> float:
        return (v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0])

    return orient(a, b, p) * orient(a, b, q) < 0 and orient(p, q, a) * orient(p, q, b) < 0


def crossings(samples: list[Point], skip: int = 6) -> list[tuple[int, int]]:
    """(earlier, later) sample indices where the polyline crosses itself."""
    hits = []
    for j in range(len(samples) - 1):
        for i in range(j - skip):
            if _segment_hit(samples[i], samples[i + 1], samples[j], samples[j + 1]):
                hits.append((i, j))
    return hits


@generator(
    "organic",
    ranges={"rx": (0.5, 500), "ry": (0.5, 500), "variation": (0, 1), "weight": (0.05, 10)},
    docs={
        "variation": "0 = the template fold (rotated/mirrored); 1 = strongly bent",
        "fill": "faint envelope behind the chain, or 'none'",
        "gap": "draw over/under gaps where the chain crosses itself",
    },
)
def chain(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    cx: float,
    cy: float,
    rx: float = 7.0,
    ry: float = 5.0,
    variation: float = 0.35,
    color: str = "species.protein.color",
    fill: str = "species.protein.surface",
    weight: float = 0.9,
    gap: bool = True,
) -> str:
    """A continuous chain folded into a compact globule (polymer, protein, fibre), crossings drawn over/under."""
    col = c.color(style, color)
    fill_col = c.color(style, fill)
    pts = _fold_points(cx, cy, rx, ry, variation, rng)
    parts = []
    if fill_col != "none":
        # Fit the surface to the fold as drawn (rotation and bending move it), ignoring the termini.
        body = c.sample_spline(pts[2:-2], per_segment=6)
        xs, ys = [q[0] for q in body], [q[1] for q in body]
        ecx, ecy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        erx, ery = (max(xs) - min(xs)) / 2 + 1.6 * weight, (max(ys) - min(ys)) / 2 + 1.6 * weight
        env = blob_points(ecx, ecy, erx * 1.08, ery * 1.08, 0.05, rng)
        parts.append(
            c.path(f"{gid}-surface", "surface", c.smooth_path(env, closed=True), f"fill:{fill_col};stroke:none")
        )
    line = c.stroke_style(col, weight)
    parts.append(c.path(f"{gid}-chain", "chain", c.smooth_path(pts), line))
    if gap:
        samples = c.sample_spline(pts, per_segment=12)
        seg = 2.2 * min(rx / 7.0, ry / 5.0) / 12  # sample spacing along the chain (template step ~2.2)
        half = max(3, round(weight * 2.2 / seg))  # samples covering ~2.2 stroke widths
        for n, (i, j) in enumerate(crossings(samples), 1):
            over = j if n % 2 else i  # alternate which strand passes over, like a real fold
            window = samples[max(0, over - half) : over + half + 2]
            gap_col = fill_col if fill_col != "none" else "#ffffff"
            parts.append(
                c.path(
                    f"{gid}-gap{n}", "crossing gap", c.polyline(window), c.stroke_style(gap_col, weight * 2.2, "butt")
                )
            )
            parts.append(c.path(f"{gid}-over{n}", "chain (over-crossing)", c.polyline(window), line))
    return c.group(gid, "folded chain", "".join(parts))


@generator(
    "organic",
    ranges={"amplitude": (0, 50), "wavelength": (0.5, 500)},
    docs={"amplitude": "max sideways wander", "wavelength": "distance between wiggles"},
)
def wobble_line(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    amplitude: float = 0.6,
    wavelength: float = 8.0,
    color: str = "ink",
    weight: float = 0.6,
) -> str:
    """A gently hand-drawn line between two points (fibres, membranes, informal connectors)."""
    length = math.hypot(x2 - x1, y2 - y1) or 1.0
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    waves = [(rng.uniform(0.6, 1.4) / wavelength, rng.uniform(0, 2 * math.pi), rng.uniform(0.5, 1)) for _ in range(2)]
    n = max(4, int(length / (wavelength / 4)))
    pts = []
    for k in range(n + 1):
        s = length * k / n
        taper = math.sin(math.pi * k / n)  # ends stay on the given points
        off = amplitude * taper * sum(a * math.sin(2 * math.pi * fr * s + ph) for fr, ph, a in waves) / 1.5
        pts.append((x1 + ux * s - uy * off, y1 + uy * s + ux * off))
    return c.group(
        gid,
        "wobbly line",
        c.path(f"{gid}-line", "line", c.smooth_path(pts), c.stroke_style(c.color(style, color), weight)),
    )
