"""Motif generators drawn from the user's published figures: tissue, annotation, plots, box genes.

Colours come from the style tokens (cell, panel, callout, plot, gene_box), so the same motif
renders in the "reference" palette (the published figures) or the muted "nature" one.
"""

from __future__ import annotations

import math
import random
from typing import Any
from xml.sax.saxutils import escape

from . import common as c
from .registry import generator

Point = tuple[float, float]


def _text(tid: str, label: str, x: float, y: float, text: str, css: str, anchor: str = "middle") -> str:
    return (
        f'<text id="{tid}" inkscape:label="{escape(label)}" x="{c.f(x)}" y="{c.f(y)}" text-anchor="{anchor}" '
        f'style="{css}">{escape(text)}</text>'
    )


def _rounded_hexagon(cx: float, cy: float, r: float, rounding: float) -> str:
    """Flat-topped hexagon (points left/right), corners optionally rounded (0..0.5 of a side)."""
    corners = [(cx + r * math.cos(math.radians(60 * i)), cy + r * math.sin(math.radians(60 * i))) for i in range(6)]
    if rounding <= 0:
        return "M " + " L ".join(f"{c.f(x)},{c.f(y)}" for x, y in corners) + " Z"
    k = rounding
    d = ""
    for i in range(6):
        prev, cur, nxt = corners[i - 1], corners[i], corners[(i + 1) % 6]
        a = (cur[0] + (prev[0] - cur[0]) * k, cur[1] + (prev[1] - cur[1]) * k)
        b = (cur[0] + (nxt[0] - cur[0]) * k, cur[1] + (nxt[1] - cur[1]) * k)
        d += f"{'M' if i == 0 else 'L'} {c.f(a[0])},{c.f(a[1])} Q {c.f(cur[0])},{c.f(cur[1])} {c.f(b[0])},{c.f(b[1])} "
    return d + "Z"


# ---- cells -----------------------------------------------------------------------------


@generator(
    "cells",
    ranges={"size": (0.5, 200), "gap": (0, 50), "rounding": (0, 0.5), "highlight": (0, 1), "nucleus": (0, 0.8)},
    docs={
        "size": "cell radius (centre to corner)",
        "gap": "space between neighbouring cells (0 = touching tissue)",
        "rounding": "corner rounding, 0-0.5 of a side",
        "highlight": "fraction of cells in one seeded contiguous highlighted patch",
        "nucleus": "nucleus size relative to the cell (0 = no nuclei)",
        "fade": "cells fade out towards the edge of the region",
    },
)
def hex_tissue(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    x: float,
    y: float,
    width: float,
    height: float,
    size: float = 4.0,
    gap: float = 0.0,
    rounding: float = 0.0,
    nucleus: float = 0.32,
    highlight: float = 0.0,
    fade: bool = False,
    weight: float = 0.3,
) -> str:
    """Hexagonal tissue of cells with nuclei; optional seeded highlighted patch and faded edge."""
    cell = style["cell"]
    pitch = size + gap / math.sqrt(3)  # centre spacing grows with the gap
    dx, dy = 1.5 * pitch, math.sqrt(3) * pitch
    cells: dict[tuple[int, int], Point] = {}
    for col in range(int(width / dx) + 2):
        for row in range(int(height / dy) + 2):
            cx = x + size + col * dx
            cy = y + size * math.sqrt(3) / 2 + row * dy + (dy / 2 if col % 2 else 0)
            if cx + size <= x + width + 1e-6 and cy + size * math.sqrt(3) / 2 <= y + height + 1e-6:
                cells[(col, row)] = (cx, cy)

    def neighbours(col: int, row: int) -> list[tuple[int, int]]:
        odd = col % 2
        steps = [(0, -1), (0, 1), (-1, 0), (1, 0), (-1, 1 if odd else -1), (1, 1 if odd else -1)]
        return [(col + a, row + b) for a, b in steps if (col + a, row + b) in cells]

    lit: set[tuple[int, int]] = set()
    if highlight > 0 and cells:
        target = max(1, round(highlight * len(cells)))
        keys = sorted(cells)
        mid = (x + width / 2, y + height / 2)
        start = min(keys, key=lambda k: c.dist(cells[k], mid) + rng.uniform(0, 2 * size))
        frontier = [start]
        while frontier and len(lit) < target:
            k = frontier.pop(rng.randrange(len(frontier)))
            if k in lit:
                continue
            lit.add(k)
            frontier.extend(n for n in neighbours(*k) if n not in lit)

    centre = (x + width / 2, y + height / 2)
    reach = math.hypot(width, height) / 2
    parts: list[str] = []
    for (col, row), (cx, cy) in sorted(cells.items()):
        on = (col, row) in lit
        fill, nuc = (cell["highlight"], cell["highlight_nucleus"]) if on else (cell["fill"], cell["nucleus"])
        opacity = f";opacity:{c.f(max(0.25, 1 - 0.85 * (c.dist((cx, cy), centre) / reach) ** 1.6))}" if fade else ""
        body = c.path(
            f"{gid}-cell-{col}-{row}",
            f"cell {col},{row}" + (" (highlighted)" if on else ""),
            _rounded_hexagon(cx, cy, size, rounding),
            f"fill:{fill};stroke:{cell['stroke']};stroke-width:{c.f(weight)};stroke-linejoin:round",
        )
        if nucleus > 0:
            nx, ny = cx + rng.uniform(-0.08, 0.08) * size, cy + rng.uniform(-0.08, 0.08) * size
            r = size * nucleus
            body += (
                f'<ellipse id="{gid}-nucleus-{col}-{row}" inkscape:label="nucleus" cx="{c.f(nx)}" cy="{c.f(ny)}" '
                f'rx="{c.f(r * 1.12)}" ry="{c.f(r * 0.9)}" style="fill:{nuc};stroke:none"/>'
            )
        parts.append(c.group(f"{gid}-c-{col}-{row}", f"cell {col},{row}", body, f' style="opacity:1{opacity}"'))
    return c.group(gid, "hexagonal tissue", "".join(parts))


# ---- annotation ------------------------------------------------------------------------


@generator(
    "annotation",
    ranges={"radius": (0, 100), "title_size": (1, 50)},
    docs={"radius": "corner radius (default from the style)", "title": "bold title centred at the top; empty for none"},
)
def panel(
    gid: str,
    style: dict[str, Any],
    x: float,
    y: float,
    width: float,
    height: float,
    title: str = "",
    radius: float = 0.0,
    title_size: float = 4.2,
    fill: str = "panel.fill",
) -> str:
    """Rounded panel with an outline and an optional bold centred title (groups a sub-model)."""
    tok = style["panel"]
    r = radius or min(tok["radius"], width / 4, height / 4)
    css = f"fill:{c.color(style, fill)};stroke:{tok['stroke']};stroke-width:{c.f(tok['weight'])}"
    body = (
        f'<rect id="{gid}-box" inkscape:label="panel" x="{c.f(x)}" y="{c.f(y)}" width="{c.f(width)}" '
        f'height="{c.f(height)}" rx="{c.f(r)}" style="{css}"/>'
    )
    if title:
        font = style["font"]["label"]
        body += _text(
            f"{gid}-title",
            "panel title",
            x + width / 2,
            y + title_size * 1.5,
            title,
            f"font-family:{font};font-weight:bold;font-size:{c.f(title_size)}px;fill:{tok['title']}",
        )
    return c.group(gid, f"panel {title}".strip(), body)


@generator(
    "annotation",
    choices={"side": ["auto", "left", "right"]},
    ranges={"size": (1, 50)},
    docs={
        "x": "pill centre",
        "target_x": "point the leader ends at (a dot is drawn there)",
        "side": "pill edge the leader leaves from",
    },
)
def label_pill(
    gid: str,
    style: dict[str, Any],
    x: float,
    y: float,
    text: str,
    target_x: float,
    target_y: float,
    size: float = 4.0,
    side: str = "auto",
) -> str:
    """Rounded 'pill' label with an elbowed leader line ending in a dot at the target."""
    tok = style["callout"]
    w = max(size * 2.5, len(text) * size * 0.6 + size * 1.8)  # Arial averages ~0.55 em per character
    h = size * 1.9
    left = side == "left" or (side == "auto" and target_x < x)
    ex = x - w / 2 if left else x + w / 2
    corner = min(abs(target_y - y), abs(target_x - ex), size) * 0.8
    sx = -1 if target_x < ex else 1
    sy = -1 if target_y < y else 1
    leader = (
        f"M {c.f(ex)},{c.f(y)} H {c.f(target_x - sx * corner)} "
        f"Q {c.f(target_x)},{c.f(y)} {c.f(target_x)},{c.f(y + sy * corner)} V {c.f(target_y)}"
    )
    weight = tok["weight"]
    body = (
        c.path(f"{gid}-leader", "leader", leader, c.stroke_style(tok["color"], weight))
        + f'<circle id="{gid}-dot" inkscape:label="target dot" cx="{c.f(target_x)}" cy="{c.f(target_y)}" '
        f'r="{c.f(weight * 1.8)}" style="fill:{tok["color"]};stroke:none"/>'
        + f'<rect id="{gid}-pill" inkscape:label="pill" x="{c.f(x - w / 2)}" y="{c.f(y - h / 2)}" width="{c.f(w)}" '
        f'height="{c.f(h)}" rx="{c.f(h * 0.32)}" '
        f'style="fill:#ffffff;stroke:{tok["color"]};stroke-width:{c.f(weight)}"/>'
        + _text(
            f"{gid}-text",
            "label",
            x,
            y + size * 0.35,
            text,
            f"font-family:{style['font']['label']};font-size:{c.f(size)}px;fill:{tok['text']}",
        )
    )
    return c.group(gid, f"label: {text}", body)


def _outer_tangents(a: Point, ra: float, b: Point, rb: float) -> list[tuple[Point, Point]] | None:
    """The two external tangent segments between circles A and B (None if one contains the other)."""
    d = c.dist(a, b)
    if d <= abs(ra - rb) or d == 0:
        return None
    base = math.atan2(b[1] - a[1], b[0] - a[0])
    off = math.acos((ra - rb) / d)
    out = []
    for sign in (1, -1):
        t = base + sign * off
        out.append(
            ((a[0] + ra * math.cos(t), a[1] + ra * math.sin(t)), (b[0] + rb * math.cos(t), b[1] + rb * math.sin(t)))
        )
    return out


@generator(
    "annotation",
    ranges={"r1": (0.1, 500), "r2": (0.1, 500)},
    docs={
        "x1": "centre of the region being magnified",
        "r1": "its radius",
        "x2": "centre of the lens",
        "r2": "lens radius",
    },
)
def zoom_callout(
    gid: str,
    style: dict[str, Any],
    x1: float,
    y1: float,
    r1: float,
    x2: float,
    y2: float,
    r2: float,
    lens_fill: str = "#ffffff",
) -> str:
    """Magnifier: a circle on the source, a translucent cone, and a larger lens circle for the detail.

    The lens is filled (white by default) so the cone stays behind it; put the magnified detail
    inside with component apply {"mode": "clip", "to": "<id>-lens"}.
    """
    tok = style["callout"]
    tangents = _outer_tangents((x1, y1), r1, (x2, y2), r2)
    parts: list[str] = []
    if tangents:
        (p1, q1), (p2, q2) = tangents
        cone = "M " + " L ".join(f"{c.f(px)},{c.f(py)}" for px, py in (p1, q1, q2, p2)) + " Z"
        parts.append(
            c.path(
                f"{gid}-cone", "cone", cone, f"fill:{tok['fill']};fill-opacity:{c.f(tok['fill_opacity'])};stroke:none"
            )
        )
    weight = c.f(tok["weight"])
    circles = (("source", (x1, y1, r1), "none"), ("lens", (x2, y2, r2), c.color(style, lens_fill)))
    for name, (cx, cy, r), fill in circles:
        parts.append(
            f'<circle id="{gid}-{name}" inkscape:label="{name} circle" cx="{c.f(cx)}" cy="{c.f(cy)}" r="{c.f(r)}" '
            f'style="fill:{fill};stroke:{tok["color"]};stroke-width:{weight}"/>'
        )
    return c.group(gid, "zoom callout", "".join(parts))


# ---- plots -----------------------------------------------------------------------------


def _axes(gid: str, style: dict[str, Any], x: float, y: float, w: float, h: float, xl: str, yl: str) -> str:
    tok = style["plot"]
    d = f"M {c.f(x)},{c.f(y)} V {c.f(y + h)} H {c.f(x + w)}"
    out = c.path(f"{gid}-axes", "axes", d, c.stroke_style(tok["axis"], 0.35, "square"))
    font = f"font-family:{style['font']['label']};font-size:3px;fill:{tok['axis']}"
    if xl:
        out += _text(f"{gid}-xlabel", "x label", x + w / 2, y + h + 4, xl, font)
    if yl:
        out += (
            f'<text id="{gid}-ylabel" inkscape:label="y label" transform="rotate(-90 {c.f(x - 2.5)} {c.f(y + h / 2)})" '
            f'x="{c.f(x - 2.5)}" y="{c.f(y + h / 2)}" text-anchor="middle" style="{font}">{escape(yl)}</text>'
        )
    return out


@generator(
    "plot",
    ranges={"count": (1, 100), "volatility": (0, 5), "weight": (0.05, 5)},
    docs={
        "count": "number of trajectories",
        "volatility": "how strongly paths wander",
        "fade": "fade out to the right",
    },
)
def trajectory_fan(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    x: float,
    y: float,
    width: float,
    height: float,
    count: int = 8,
    volatility: float = 1.0,
    fade: bool = True,
    weight: float = 0.4,
) -> str:
    """Stochastic trajectories fanning out from a common start (simulation output), fading to the right."""
    series = style["plot"]["series"]
    steps = 90
    parts = []
    defs = []
    walks = []
    for _ in range(count):
        level, levels = 0.0, []
        for _k in range(steps + 1):
            level += rng.gauss(0, 1) * volatility * 0.11
            levels.append(level)
        walks.append(levels)
    # Scale the whole fan to fit (clamping would flatten paths along the edges).
    spread = max((abs(v) for w in walks for v in w), default=1.0) or 1.0
    for i, levels in enumerate(walks):
        col = series[i % len(series)]
        pts = [(x + width * k / steps, y + height / 2 - v / spread * height * 0.48) for k, v in enumerate(levels)]
        stroke = col
        if fade:
            defs.append(
                f'<linearGradient id="{gid}-fade{i}" gradientUnits="userSpaceOnUse" x1="{c.f(x)}" y1="0" '
                f'x2="{c.f(x + width)}" y2="0"><stop offset="0.35" stop-color="{col}" stop-opacity="1"/>'
                f'<stop offset="1" stop-color="{col}" stop-opacity="0.05"/></linearGradient>'
            )
            stroke = f"url(#{gid}-fade{i})"
        parts.append(c.path(f"{gid}-path{i}", f"trajectory {i + 1}", c.polyline(pts), c.stroke_style(stroke, weight)))
    defs_xml = f'<defs id="{gid}-defs">{"".join(defs)}</defs>' if defs else ""
    return c.group(gid, "trajectory fan", defs_xml + "".join(parts))


def _sample(dist: str, rng: random.Random, mean: float) -> float:
    if dist == "poisson":
        k, p, limit = 0, 1.0, math.exp(-mean)
        while True:
            p *= rng.random()
            if p <= limit:
                return float(k)
            k += 1
    if dist == "bimodal":
        return max(
            0.0, rng.gauss(mean * 0.45, mean * 0.15) if rng.random() < 0.45 else rng.gauss(mean * 1.3, mean * 0.2)
        )
    # negative binomial (bursty): gamma-Poisson mixture
    lam = rng.gammavariate(2.0, mean / 2.0)
    return _sample("poisson", rng, lam) if lam < 500 else round(rng.gauss(lam, math.sqrt(lam)))


@generator(
    "plot",
    choices={"distribution": ["negbin", "poisson", "bimodal"]},
    ranges={"bins": (3, 200), "samples": (10, 20000), "mean": (0.5, 1000)},
    docs={
        "distribution": "what the bars sample: negbin (bursty), poisson or bimodal",
        "fit": "draw a smooth fitted curve over the bars",
        "x_label": "axis label; empty for none",
    },
)
def histogram_fit(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    x: float,
    y: float,
    width: float,
    height: float,
    distribution: str = "negbin",
    mean: float = 20.0,
    bins: int = 24,
    samples: int = 600,
    fit: bool = True,
    axes: bool = True,
    x_label: str = "",
    y_label: str = "Probability",
) -> str:
    """Histogram of seeded counts with a fitted curve over it (data vs model), optional axes."""
    tok = style["plot"]
    data = [_sample(distribution, rng, mean) for _ in range(samples)]
    hi = max(data) * 1.05 or 1.0
    counts = [0] * bins
    for v in data:
        counts[min(bins - 1, int(v / hi * bins))] += 1
    top = max(counts) or 1
    bw = width / bins
    bars = " ".join(
        f"M {c.f(x + i * bw)},{c.f(y + height)} V {c.f(y + height - n / top * height * 0.88)} "
        f"H {c.f(x + (i + 1) * bw)} V {c.f(y + height)} Z"
        for i, n in enumerate(counts)
        if n
    )
    parts = [c.path(f"{gid}-bars", "histogram bars", bars, f"fill:{tok['bars']};stroke:none")]
    if fit:
        # A smooth density estimate of the same sample stands in for the fitted model.
        bw_k = hi / bins * 1.4
        xs = [hi * k / 80 for k in range(81)]
        dens = [sum(math.exp(-0.5 * ((xv - v) / bw_k) ** 2) for v in data) for xv in xs]
        peak = max(dens) or 1.0
        scale = max(counts) / top * 0.88
        pts = [(x + xv / hi * width, y + height - dv / peak * height * scale) for xv, dv in zip(xs, dens, strict=True)]
        parts.append(c.path(f"{gid}-fit", "fitted curve", c.smooth_path(pts[::2]), c.stroke_style(tok["fit"], 0.6)))
    if axes:
        parts.append(_axes(gid, style, x, y, width, height, x_label, y_label))
    return c.group(gid, f"histogram ({distribution})", "".join(parts))


@generator(
    "plot",
    ranges={"count": (2, 30), "overlap": (0, 3)},
    docs={"count": "number of ridges (time points)", "overlap": "how much ridges overlap vertically"},
)
def ridge_plot(
    gid: str,
    style: dict[str, Any],
    x: float,
    y: float,
    width: float,
    height: float,
    count: int = 5,
    overlap: float = 1.2,
    color: str = "plot.ridge",
    axes: bool = True,
) -> str:
    """Distributions evolving over time as staggered, translucent ridges (back to front)."""
    col = c.color(style, color)
    step_x, step_y = width * 0.32 / count, height * 0.55 / count
    peak_h = height * 0.45 * overlap / 1.2
    rw = width * 0.62
    parts: list[str] = []
    for k in range(count):
        ox, oy = x + k * step_x, y + height - (count - 1 - k) * step_y
        mu = 0.25 + 0.5 * k / max(1, count - 1)
        sd = 0.07 + 0.05 * k / max(1, count - 1)
        pts = []
        for i in range(61):
            t = i / 60
            g = math.exp(-0.5 * ((t - mu) / sd) ** 2)
            pts.append((ox + t * rw, oy - g * peak_h * (1 - 0.25 * k / max(1, count))))
        d = c.smooth_path(pts) + f" L {c.f(ox + rw)},{c.f(oy)} L {c.f(ox)},{c.f(oy)} Z"
        css = f"fill:{col};fill-opacity:0.28;stroke:{col};stroke-width:0.4;stroke-linejoin:round"
        parts.insert(0, c.path(f"{gid}-ridge{k}", f"ridge {k + 1}", d, css))  # later ridges drawn behind
    if axes:
        tok = style["plot"]
        back_y = y + height - (count - 1) * step_y  # baseline of the back ridge
        front = (x + count * step_x, y + height + step_y)  # just past the front ridge's baseline
        d = f"M {c.f(x)},{c.f(back_y - peak_h * 1.1)} V {c.f(back_y)} L {c.f(front[0])},{c.f(front[1])}"
        parts.append(c.path(f"{gid}-axes", "axes", d, c.stroke_style(tok["axis"], 0.35)))
    return c.group(gid, "ridge plot", "".join(parts))


# ---- schematic: the compact box gene ---------------------------------------------------


@generator(
    "schematic",
    choices={"state": ["on", "off"]},
    ranges={"width": (2, 200), "height": (1, 100), "line": (0, 400)},
    docs={
        "x": "left end of the DNA line",
        "y": "DNA line height (box centre)",
        "line": "DNA line length (default: box + margins)",
        "promoter": "draw the bent promoter arrow",
        "symbol": "italic gene symbol inside the box; sub = upright subscript",
    },
)
def gene_box(
    gid: str,
    style: dict[str, Any],
    x: float,
    y: float,
    state: str = "on",
    width: float = 12.0,
    height: float = 4.5,
    line: float = 0.0,
    symbol: str = "G",
    sub: str = "",
    promoter: bool = True,
) -> str:
    """Compact gene: a coloured box on a thin DNA line with a bent promoter arrow (cyan on / coral off)."""
    tok = style["gene_box"]
    total = line or width + 8
    bx = x + (total - width) / 2
    ink = tok["line"]
    parts = [
        c.path(f"{gid}-dna", "DNA", f"M {c.f(x)},{c.f(y)} H {c.f(x + total)}", c.stroke_style(ink, 0.35, "butt")),
        f'<rect id="{gid}-box" inkscape:label="gene" x="{c.f(bx)}" y="{c.f(y - height / 2)}" width="{c.f(width)}" '
        f'height="{c.f(height)}" style="fill:{tok[state]};stroke:none"/>',
    ]
    if promoter:
        top = y - height / 2 - height * 0.75
        d = f"M {c.f(bx)},{c.f(y - height / 2)} V {c.f(top)} H {c.f(bx + width * 0.95)}"
        parts.append(
            c.path(
                f"{gid}-promoter",
                "promoter arrow",
                d,
                f"fill:none;stroke:{ink};stroke-width:0.35;stroke-linejoin:miter;marker-end:url(#{style['marker']})",
            )
        )
    font = style["font"]
    size = height * 0.72
    sub_xml = (
        f'<tspan baseline-shift="sub" style="font-style:normal;font-size:{c.f(size * 0.68)}px">{escape(sub)}</tspan>'
        if sub
        else ""
    )
    parts.append(
        f'<text id="{gid}-symbol" inkscape:label="gene symbol" x="{c.f(bx + width / 2)}" y="{c.f(y + size * 0.36)}" '
        f'text-anchor="middle" style="font-family:{font["math"]};font-style:italic;font-size:{c.f(size)}px;'
        f'fill:{tok["text"]}">{escape(symbol)}{sub_xml}</text>'
    )
    return c.group(gid, f"gene ({state})", "".join(parts))


# ---- organic: space-filling protein ----------------------------------------------------


def _shade(hex_color: str, factor: float) -> str:
    """Darker (factor < 1) or lighter towards white (factor > 1) version of a colour."""
    rgb = [int(hex_color[i : i + 2], 16) for i in (1, 3, 5)]
    if factor <= 1:
        out = [round(v * factor) for v in rgb]
    else:
        out = [round(v + (255 - v) * (factor - 1)) for v in rgb]
    return "#" + "".join(f"{max(0, min(255, v)):02x}" for v in out)


def _deepen(hex_color: str, amount: float) -> str:
    """A deeper version of a colour: darker AND more saturated (shading that does not turn grey)."""
    import colorsys

    rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    h, lightness, sat = colorsys.rgb_to_hls(*rgb)
    out = colorsys.hls_to_rgb(h, lightness * (1 - 0.22 * amount), min(1.0, sat * (1 + 0.45 * amount)))
    return "#" + "".join(f"{round(v * 255):02x}" for v in out)


def _inside(p: Point, poly: list[Point]) -> bool:
    x, y = p
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


@generator(
    "organic",
    ranges={"size": (0.5, 500), "bump": (0.08, 0.6), "lumpiness": (0, 0.4), "texture": (0, 1), "notches": (0, 6)},
    docs={
        "size": "overall radius",
        "bump": "sphere size relative to the protein (smaller = finer, more atoms)",
        "lumpiness": "how irregular the overall outline is",
        "texture": "contrast of the surface: lighter domains and wider crevices between them",
        "notches": "number of inward bays bitten out of the outline (0 = a rounder protein)",
        "color": "fill colour (hex or token); the outline and texture are shades of it",
    },
)
def protein_globule(
    gid: str,
    style: dict[str, Any],
    rng: random.Random,
    cx: float,
    cy: float,
    size: float = 7.0,
    bump: float = 0.13,
    lumpiness: float = 0.22,
    texture: float = 0.6,
    notches: int = 2,
    color: str = "species.protein.globule",
    outline: float = 0.25,
) -> str:
    """Space-filling protein: a lumpy globule of spheres with soft lighter domains, darker crevices, one outline."""
    from . import common, organic, scatter

    base = c.color(style, color)
    dark = _deepen(_shade(base, 0.62), 0.3)  # outline: a deep shade of the same hue, not grey
    region = organic.blob_points(cx, cy, size, size * 0.86, lumpiness, rng)
    r = size * bump
    xs, ys = [p[0] for p in region], [p[1] for p in region]
    pts = scatter.poisson_disc(max(xs) - min(xs), max(ys) - min(ys), r * 0.95, rng)
    spheres = [
        ((min(xs) + px, min(ys) + py), r * rng.uniform(0.8, 1.2))
        for px, py in pts
        if _inside((min(xs) + px, min(ys) + py), region)
    ] or [((cx, cy), size * 0.9)]
    # Bays: bite a few seeded notches out of the outline, so the silhouette is not a rounded blob.
    for _ in range(notches):
        edge = region[rng.randrange(len(region))]
        bite_r = size * rng.uniform(0.22, 0.38)
        bite = (edge[0] + (edge[0] - cx) * 0.05, edge[1] + (edge[1] - cy) * 0.05)
        kept = [sph for sph in spheres if common.dist(sph[0], bite) > bite_r]
        if len(kept) > len(spheres) * 0.75:
            spheres = kept
            region = [
                q if common.dist(q, bite) > bite_r else (cx + (q[0] - cx) * 0.7, cy + (q[1] - cy) * 0.7) for q in region
            ]

    def circles(items: list[tuple[Point, float]], grow: float = 0.0) -> str:
        return " ".join(common.dot_path([p], rad + grow) for p, rad in items)

    core = [(cx + (x - cx) * 0.78, cy + (y - cy) * 0.78) for x, y in region]

    # Domains: spheres grouped around a few evenly spread lobe centres. Each lobe is drawn as one
    # merged, slightly shrunk, soft-edged shape in a light tone; the darker base shows between
    # lobes as crevices, and an omitted lobe reads as a recessed pocket.
    centres = [
        (min(xs) + px, min(ys) + py)
        for px, py in scatter.poisson_disc(max(xs) - min(xs), max(ys) - min(ys), size * 0.42, rng)
        if _inside((min(xs) + px, min(ys) + py), core)
    ] or [(cx, cy)]
    # Weighted assignment (a power diagram): heavier centres claim more spheres, so domains come
    # out in varied sizes and some elongated, as in a real surface, not as equal cells.
    weights = [rng.uniform(0.0, 0.3 * size) for _ in centres]
    lobes: dict[int, list[tuple[Point, float]]] = {}
    for sphere in spheres:
        k = min(range(len(centres)), key=lambda i: common.dist(sphere[0], centres[i]) - weights[i])
        lobes.setdefault(k, []).append(sphere)
    occupied = sorted(lobes)
    n_pockets = min(2, len(occupied) // 6) if len(occupied) > 3 else 0
    if len(occupied) > 3 and rng.random() < 0.7:
        n_pockets = max(1, n_pockets)
    # Pockets are small recesses: chosen among the smaller domains, never a large one.
    # Prefer the shaded (lower-right) side, where a recess reads naturally under top-left light.
    small = sorted(occupied, key=lambda k: len(lobes[k]))[: max(1, len(occupied) // 2)]
    shaded = [k for k in small if (centres[k][0] - cx) + (centres[k][1] - cy) > 0]
    small = shaded or small
    pockets = set(rng.sample(small, min(n_pockets, len(small)))) if n_pockets else set()
    rim = r * (0.08 + 0.12 * texture)  # width of the darker rim where a domain overlaps its neighbours
    lobe_paths = []
    # Back to front: shaded (lower-right) domains first, so lit domains overlap them like a surface.
    order = sorted(
        (k for k in lobes if k not in pockets),
        key=lambda k: (centres[k][0] - cx) + (centres[k][1] - cy),
        reverse=True,
    )
    for k in order:
        members = [(p, rad * 0.92) for p, rad in lobes[k]]  # still overlapping: a solid lobe
        # Light from the upper left: domains there are lighter, towards the lower right deeper.
        ux, uy = (centres[k][0] - cx) / size, (centres[k][1] - cy) / size
        lit = max(-1.0, min(1.0, -(ux + uy) / 1.4))
        tone = _shade(base, 1.1 + 0.4 * texture * lit + rng.uniform(-0.05, 0.05))
        lobe_paths.append(
            c.path(
                f"{gid}-lobe{k}-rim",
                f"domain {k + 1} rim",
                circles(members, rim),
                f"fill:{_deepen(base, 0.2)};fill-opacity:{c.f(0.35 + 0.4 * texture)};stroke:none;"
                f"filter:url(#{gid}-soft)",
            )
        )
        lobe_paths.append(
            c.path(
                f"{gid}-lobe{k}",
                f"domain {k + 1}",
                circles(members),  # edges are the spheres' own arcs: bulgy, like the reference
                f"fill:{tone};stroke:none;filter:url(#{gid}-soft)",
            )
        )
    xlink = "http://www.w3.org/1999/xlink"
    defs = (
        f'<defs id="{gid}-defs">'
        f'<filter id="{gid}-soft" inkscape:label="domain edge: soften" '
        'x="-0.25" y="-0.25" width="1.5" height="1.5" '
        f'inkscape:auto-region="false" style="color-interpolation-filters:sRGB">'
        f'<feGaussianBlur id="{gid}-soft-blur" stdDeviation="{c.f(r * 0.07)}"/></filter>'
        f'<clipPath id="{gid}-clip" inkscape:label="body clip"><use xmlns:xlink="{xlink}" xlink:href="#{gid}-core"/>'
        f'<use xmlns:xlink="{xlink}" xlink:href="#{gid}-body"/></clipPath></defs>'
    )
    parts = [
        defs,
        # Outline: every sphere, a little larger, in the dark shade — only the union's edge stays visible.
        c.path(f"{gid}-outline", "outline", circles(spheres, outline), f"fill:{dark};stroke:none"),
        # Base (the crevice tone): a smooth core plus the spheres; the core is its own element, as in
        # one compound path its opposite winding would cancel the overlap and open holes.
        c.path(f"{gid}-core", "core", c.smooth_path(core, closed=True), f"fill:{_deepen(base, 0.25)};stroke:none"),
        c.path(f"{gid}-body", "body", circles(spheres), f"fill:{_deepen(base, 0.35)};stroke:none"),
        c.group(f"{gid}-domains", "domains", "".join(lobe_paths), f' clip-path="url(#{gid}-clip)"'),
    ]
    return c.group(gid, "protein (space-filling)", "".join(parts))
