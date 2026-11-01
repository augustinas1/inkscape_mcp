"""Tiling generators: line grids, Truchet tiles, radial figures, isometric grids and blocks.

Line work is emitted as ONE compound path per family (compact; Path ▸ Break Apart splits it).
Grids are clipped exactly to their region, and Truchet tiles repeat seamlessly when the
region is a whole number of tiles — so both work as pattern fills.
"""

from __future__ import annotations

import math
import random
from typing import Any

from . import common as c
from .registry import generator

Point = tuple[float, float]
Segment = tuple[Point, Point]


def clip_segment(p: Point, q: Point, x0: float, y0: float, x1: float, y1: float) -> Segment | None:
    """Liang-Barsky: the part of segment p-q inside the rectangle, or None."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for edge_p, edge_q in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if edge_p == 0:
            if edge_q < 0:
                return None
            continue
        t = edge_q / edge_p
        if edge_p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return (p[0] + t0 * dx, p[1] + t0 * dy), (p[0] + t1 * dx, p[1] + t1 * dy)


def _line_family(x: float, y: float, w: float, h: float, angle: float, spacing: float) -> list[Segment]:
    """Parallel lines at `angle` (degrees), `spacing` apart, clipped to the region."""
    a = math.radians(angle)
    ux, uy = math.cos(a), math.sin(a)  # along the lines
    nx, ny = -uy, ux  # across them
    cx, cy = x + w / 2, y + h / 2
    reach = math.hypot(w, h) / 2 + spacing
    segs = []
    k = -math.floor(reach / spacing)
    while k * spacing <= reach:
        ox, oy = cx + nx * k * spacing, cy + ny * k * spacing
        seg = clip_segment((ox - ux * reach, oy - uy * reach), (ox + ux * reach, oy + uy * reach), x, y, x + w, y + h)
        if seg and c.dist(*seg) > 1e-6:
            segs.append(seg)
        k += 1
    return segs


def _segments_path(segs: list[Segment]) -> str:
    return " ".join(f"M {c.f(p[0])},{c.f(p[1])} L {c.f(q[0])},{c.f(q[1])}" for p, q in segs)


@generator(
    "tiling",
    choices={"lattice": ["square", "triangle", "hex"]},
    ranges={"spacing": (0.2, 500), "weight": (0.02, 10)},
    docs={"lattice": "square | triangle (3 directions) | hex (honeycomb cells)", "angle": "rotation, degrees"},
)
def grid_lines(
    gid: str,
    style: dict[str, Any],
    x: float,
    y: float,
    width: float,
    height: float,
    lattice: str = "square",
    spacing: float = 5.0,
    angle: float = 0.0,
    color: str = "ink",
    weight: float = 0.25,
) -> str:
    """A line grid (square, triangular or honeycomb), clipped exactly to the region."""
    if lattice == "hex":
        segs = _honeycomb(x, y, width, height, spacing, angle)
    else:
        angles = [angle, angle + 90] if lattice == "square" else [angle, angle + 60, angle + 120]
        step = spacing if lattice == "square" else spacing * math.sqrt(3) / 2
        segs = [s for a in angles for s in _line_family(x, y, width, height, a, step)]
    style_css = c.stroke_style(c.color(style, color), weight, "butt")
    return c.group(gid, f"{lattice} grid", c.path(f"{gid}-lines", "grid lines", _segments_path(segs), style_css))


def _honeycomb(x: float, y: float, w: float, h: float, side: float, angle: float) -> list[Segment]:
    """Edges of a hexagon tiling with side `side` (pointy-top), clipped to the region."""
    a = math.radians(angle)
    cx, cy = x + w / 2, y + h / 2

    def rot(p: Point) -> Point:
        dx, dy = p[0] - cx, p[1] - cy
        return cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a)

    col_w, row_h = side * math.sqrt(3), side * 1.5
    reach = math.hypot(w, h) / 2 + 2 * side
    seen: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    segs = []
    for r in range(-int(reach / row_h) - 1, int(reach / row_h) + 2):
        for k in range(-int(reach / col_w) - 1, int(reach / col_w) + 2):
            hx = cx + k * col_w + (col_w / 2 if r % 2 else 0)
            hy = cy + r * row_h
            corners = [
                rot((hx + side * math.cos(math.radians(60 * i - 30)), hy + side * math.sin(math.radians(60 * i - 30))))
                for i in range(6)
            ]
            for i in range(6):
                p, q = corners[i], corners[(i + 1) % 6]
                key = tuple(sorted(((round(p[0], 4), round(p[1], 4)), (round(q[0], 4), round(q[1], 4)))))
                if key in seen:
                    continue
                seen.add(key)  # type: ignore[arg-type]
                seg = clip_segment(p, q, x, y, x + w, y + h)
                if seg and c.dist(*seg) > 1e-6:
                    segs.append(seg)
    return segs


@generator(
    "tiling",
    choices={"motif": ["arcs", "diagonals", "triangles"]},
    ranges={"size": (0.5, 500), "weight": (0.02, 20)},
    docs={
        "motif": "arcs (quarter circles; join into continuous curves) | diagonals (maze) | triangles (filled)",
        "size": "tile size; make width/height multiples of it for a seamless pattern",
    },
)
def truchet(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    x: float,
    y: float,
    width: float,
    height: float,
    size: float = 5.0,
    motif: str = "arcs",
    color: str = "ink",
    weight: float = 0.4,
) -> str:
    """Truchet tiles: each tile's motif randomly flipped, so simple tiles form flowing paths."""
    col = c.color(style, color)
    cols, rows = max(1, int(width / size + 1e-9)), max(1, int(height / size + 1e-9))  # never past the region
    s = size
    r = s / 2
    parts = []
    for j in range(rows):
        for i in range(cols):
            tx, ty = x + i * s, y + j * s
            flip = rng.random() < 0.5
            if motif == "arcs":
                if flip:  # arcs about the top-left and bottom-right corners
                    parts.append(f"M {c.f(tx + r)},{c.f(ty)} A {c.f(r)},{c.f(r)} 0 0 1 {c.f(tx)},{c.f(ty + r)}")
                    parts.append(
                        f"M {c.f(tx + s - r)},{c.f(ty + s)} A {c.f(r)},{c.f(r)} 0 0 1 {c.f(tx + s)},{c.f(ty + s - r)}"
                    )
                else:  # about the top-right and bottom-left corners
                    parts.append(f"M {c.f(tx + s - r)},{c.f(ty)} A {c.f(r)},{c.f(r)} 0 0 0 {c.f(tx + s)},{c.f(ty + r)}")
                    parts.append(f"M {c.f(tx + r)},{c.f(ty + s)} A {c.f(r)},{c.f(r)} 0 0 0 {c.f(tx)},{c.f(ty + s - r)}")
            elif motif == "diagonals":
                if flip:
                    parts.append(f"M {c.f(tx)},{c.f(ty)} L {c.f(tx + s)},{c.f(ty + s)}")
                else:
                    parts.append(f"M {c.f(tx + s)},{c.f(ty)} L {c.f(tx)},{c.f(ty + s)}")
            else:  # triangles: fill one half of the tile, choosing one of four corners
                corner = rng.randrange(4)
                pts = [(tx, ty), (tx + s, ty), (tx + s, ty + s), (tx, ty + s)]
                del pts[corner]
                parts.append("M " + " L ".join(f"{c.f(px)},{c.f(py)}" for px, py in pts) + " Z")
    d = " ".join(parts)
    if motif == "triangles":
        css = f"fill:{col};stroke:none"
    else:
        css = c.stroke_style(col, weight, "butt")
    return c.group(gid, f"truchet ({motif})", c.path(f"{gid}-tiles", "tiles", d, css))


@generator(
    "tiling",
    choices={"figure": ["rings", "rays", "spiral", "polygons"]},
    ranges={"count": (1, 500), "r_outer": (0.1, 1000), "r_inner": (0, 1000), "sides": (3, 24), "turns": (0.2, 50)},
    docs={
        "figure": "rings | rays | spiral (Archimedean) | polygons (nested)",
        "count": "number of rings / rays / polygons",
        "turns": "spiral turns",
    },
)
def radial(
    gid: str,
    style: dict[str, Any],
    cx: float,
    cy: float,
    figure: str = "rings",
    r_outer: float = 15.0,
    r_inner: float = 2.0,
    count: int = 8,
    sides: int = 6,
    turns: float = 5.0,
    rotation: float = 0.0,
    color: str = "ink",
    weight: float = 0.3,
) -> str:
    """Radial figures about a centre: concentric rings, rays, an Archimedean spiral, nested polygons."""
    parts: list[str] = []
    radii = [r_inner + (r_outer - r_inner) * (k + 1) / count for k in range(count)] if count > 0 else []
    rot = math.radians(rotation)
    if figure == "rings":
        parts = [c.dot_path([(cx, cy)], r) for r in radii]
    elif figure == "rays":
        for k in range(count):
            a = rot + 2 * math.pi * k / count
            parts.append(
                f"M {c.f(cx + r_inner * math.cos(a))},{c.f(cy + r_inner * math.sin(a))} "
                f"L {c.f(cx + r_outer * math.cos(a))},{c.f(cy + r_outer * math.sin(a))}"
            )
    elif figure == "spiral":
        n = max(16, int(turns * 24))
        pts = []
        for k in range(n + 1):
            t = turns * 2 * math.pi * k / n
            r = r_inner + (r_outer - r_inner) * k / n
            pts.append((cx + r * math.cos(t + rot), cy + r * math.sin(t + rot)))
        parts = [c.smooth_path(pts)]
    else:  # polygons
        for r in radii:
            corners = [
                (cx + r * math.cos(rot + 2 * math.pi * i / sides), cy + r * math.sin(rot + 2 * math.pi * i / sides))
                for i in range(sides)
            ]
            parts.append("M " + " L ".join(f"{c.f(px)},{c.f(py)}" for px, py in corners) + " Z")
    css = c.stroke_style(c.color(style, color), weight)
    return c.group(gid, f"radial {figure}", c.path(f"{gid}-lines", figure, " ".join(parts), css))


_ISO_X = (math.cos(math.radians(30)), -math.sin(math.radians(30)))  # +x: up-right on screen
_ISO_Y = (-math.cos(math.radians(30)), -math.sin(math.radians(30)))  # +y: up-left on screen


@generator("tiling", ranges={"spacing": (0.2, 500), "weight": (0.02, 10)})
def iso_grid(
    gid: str,
    style: dict[str, Any],
    x: float,
    y: float,
    width: float,
    height: float,
    spacing: float = 5.0,
    verticals: bool = True,
    color: str = "ink",
    weight: float = 0.2,
) -> str:
    """Isometric grid: lines at 30° and 150° (and optionally vertical), clipped to the region."""
    step = spacing * math.sqrt(3) / 2
    segs = _line_family(x, y, width, height, 30, step) + _line_family(x, y, width, height, 150, step)
    if verticals:
        segs += _line_family(x, y, width, height, 90, step)
    css = c.stroke_style(c.color(style, color), weight, "butt")
    return c.group(gid, "isometric grid", c.path(f"{gid}-lines", "grid lines", _segments_path(segs), css))


@generator("tiling", ranges={"sx": (0.1, 500), "sy": (0.1, 500), "sz": (0, 500)})
def iso_box(
    gid: str,
    style: dict[str, Any],
    cx: float,
    cy: float,
    sx: float = 10.0,
    sy: float = 10.0,
    sz: float = 6.0,
    color: str = "gene.on.promoter",
    edge: str = "ink",
    weight: float = 0.3,
) -> str:
    """Isometric block (sx by sy footprint, sz tall) with three faces in tints of one colour.

    (cx, cy) is the front-bottom corner. Faces: top lightest, left mid, right darkest — the
    flat 'three-tone' shading of technical diagrams.
    """
    base = c.color(style, color)

    def at(px: float, py: float, pz: float) -> Point:
        return cx + px * _ISO_X[0] + py * _ISO_Y[0], cy + px * _ISO_X[1] + py * _ISO_Y[1] - pz

    def face(points: list[Point]) -> str:
        return "M " + " L ".join(f"{c.f(px)},{c.f(py)}" for px, py in points) + " Z"

    top = face([at(0, 0, sz), at(sx, 0, sz), at(sx, sy, sz), at(0, sy, sz)])
    left = face([at(0, 0, 0), at(0, sy, 0), at(0, sy, sz), at(0, 0, sz)])
    right = face([at(0, 0, 0), at(sx, 0, 0), at(sx, 0, sz), at(0, 0, sz)])
    edge_css = f"stroke:{c.color(style, edge)};stroke-width:{c.f(weight)};stroke-linejoin:round"
    body = (
        c.path(f"{gid}-top", "top face", top, f"fill:{c.tint(base, 0.35)};{edge_css}")
        + c.path(f"{gid}-left", "left face", left, f"fill:{c.tint(base, 0.65)};{edge_css}")
        + c.path(f"{gid}-right", "right face", right, f"fill:{base};{edge_css}")
    )
    return c.group(gid, "isometric block", body)


def hex_centres(x: float, y: float, w: float, h: float, side: float) -> list[tuple[int, int, Point]]:
    """(row, column, centre) of the pointy-top hexagons that grid_lines(lattice="hex") draws."""
    cx, cy = x + w / 2, y + h / 2
    col_w, row_h = side * math.sqrt(3), side * 1.5
    out = []
    for r in range(-int(h / 2 / row_h) - 2, int(h / 2 / row_h) + 3):
        for k in range(-int(w / 2 / col_w) - 2, int(w / 2 / col_w) + 3):
            out.append((r, k, (cx + k * col_w + (col_w / 2 if r % 2 else 0), cy + r * row_h)))
    return out


@generator(
    "tiling",
    choices={"pick": ["alternate", "spaced", "all"]},
    ranges={"spacing": (0.2, 500)},
    docs={
        "spacing": "hexagon side — use the same value as the grid_lines(hex) it should fill",
        "pick": "alternate = every other cell in each row (cells meet diagonally) | "
        "spaced = one in three, no two filled cells touch | all",
        "offset": "shift which cells are picked (0, 1 or 2)",
    },
)
def hex_cells(
    gid: str,
    style: dict[str, Any],
    x: float,
    y: float,
    width: float,
    height: float,
    spacing: float = 5.0,
    pick: str = "alternate",
    offset: int = 0,
    fill: str = "species.protein.surface",
) -> str:
    """Filled hexagons on the honeycomb grid_lines(lattice="hex") draws, clipped to the region."""
    side = spacing
    cells = []
    for r, k, (hx, hy) in hex_centres(x, y, width, height, side):
        if not (x - side <= hx <= x + width + side and y - side <= hy <= y + height + side):
            continue
        if pick == "alternate" and (k + offset) % 2:
            continue
        if pick == "spaced":
            q = k - (r - (r & 1)) // 2  # offset rows -> axial coordinates
            if (q - r + offset) % 3:
                continue
        corners = [
            (hx + side * math.cos(math.radians(60 * i - 30)), hy + side * math.sin(math.radians(60 * i - 30)))
            for i in range(6)
        ]
        cells.append("M " + " L ".join(f"{c.f(px)},{c.f(py)}" for px, py in corners) + " Z")
    clip = (
        f'<clipPath id="{gid}-clip" inkscape:label="region"><rect id="{gid}-clip-rect" inkscape:label="region" '
        f'x="{c.f(x)}" y="{c.f(y)}" width="{c.f(width)}" height="{c.f(height)}"/></clipPath>'
    )
    body = clip + c.path(f"{gid}-cells", f"{pick} cells", " ".join(cells), f"fill:{c.color(style, fill)};stroke:none")
    return c.group(gid, f"hexagon fill ({pick})", body, f' clip-path="url(#{gid}-clip)"')
