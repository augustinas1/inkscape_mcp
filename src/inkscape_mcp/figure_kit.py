"""Pre-styled, parametric figure components in a restrained Nature-Reviews-like schematic style.

Every generator returns an SVG fragment (a ``<g>`` with stable ids and ``inkscape:label``s)
built from one set of style tokens, so components drawn on different days still match:
the same stroke weights, hues, paler-tint-for-"less" convention and maths typography.

Tokens come from ``DEFAULT_STYLE``, optionally overridden by a JSON file (e.g. the figure's
``style.json``) — change a colour there and every later component follows.

Coordinates are document user units (mm for a mm document); all components are drawn
untransformed at the position given, so the user can move and edit them like hand-drawn
objects.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .generators import registry

DEFAULT_STYLE: dict[str, Any] = {
    "ink": "#333333",  # arrows, maths
    "muted": "#777777",  # secondary labels ("decay")
    "dna": "#4d4d4d",
    "species": {
        # one hue per species; "pale" = the opaque tint used for "less" (degraded, inactive)
        "rna": {"color": "#d1495b", "pale": "#df808c"},
        "protein": {"color": "#2b8a69", "pale": "#5cb295", "surface": "#e4f4ed"},
    },
    "gene": {
        "on": {
            "promoter": "#2a7ab0",
            "promoter_edge": "#1d5f8c",
            "body": "#b3d4ea",
            "body_edge": "#7fb0d3",
            "text": "#10456b",
        },
        "off": {
            "promoter": "#9e9e9e",
            "promoter_edge": "#7a7a7a",
            "body": "#e3e3e3",
            "body_edge": "#b3b3b3",
            "text": "#5c5c5c",
        },
    },
    "stroke": {"strand": 0.9, "base": 0.45, "arrow": 0.6, "dna": 1.0, "hairline": 0.35},
    "font": {
        "math": "Arial",
        "math_size": 7.0,
        "sub_size": 4.5,
        "label": "sans-serif",
        "label_size": 3.8,
    },
    "marker": "Triangle",
    # Tokens for the motif generators (cells, annotation, plots, box genes).
    "gene_box": {"on": "#2a7ab0", "off": "#9e9e9e", "line": "#333333", "text": "#ffffff"},
    "cell": {
        "fill": "#f5f5f5",
        "stroke": "#b9b9b9",
        "nucleus": "#e1e1e1",
        "highlight": "#d8e6ef",
        "highlight_nucleus": "#2a7ab0",
    },
    "panel": {"stroke": "#9aa5b1", "fill": "#ffffff", "title": "#222222", "weight": 0.5, "radius": 4.0},
    "callout": {"color": "#9aa5b1", "fill": "#9aa5b1", "fill_opacity": 0.18, "weight": 0.5, "text": "#222222"},
    "plot": {
        "bars": "#b3d4ea",
        "fit": "#d1495b",
        "axis": "#333333",
        "ridge": "#7a5aa8",
        "series": ["#2a7ab0", "#d1495b", "#2b8a69", "#e0a030", "#7a5aa8", "#5a9fc0"],
    },
}

# Named presets: overrides of DEFAULT_STYLE. "nature" is the default (muted, Nature-Reviews-like);
# "reference" is the palette of the user's published figures (sky/cyan, coral, periwinkle).
STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "nature": {},
    "reference": {
        "ink": "#1a1a1a",
        "muted": "#808080",
        "dna": "#1a1a1a",
        "species": {
            "rna": {"color": "#0098d1", "pale": "#8fd3ef"},
            "protein": {"color": "#d6589f", "pale": "#eba9d0", "surface": "#fbe6f2"},
        },
        "gene": {
            "on": {
                "promoter": "#57c6ff",
                "promoter_edge": "#2d98c3",
                "body": "#c3ebfb",
                "body_edge": "#7fd0f2",
                "text": "#0a516e",
            },
            "off": {
                "promoter": "#fd7b74",
                "promoter_edge": "#e0575a",
                "body": "#ffd6d3",
                "body_edge": "#f5a19c",
                "text": "#a93a45",
            },
        },
        "gene_box": {"on": "#57c6ff", "off": "#fd7b74", "line": "#1a1a1a", "text": "#1a1a1a"},
        "cell": {
            "fill": "#f7f7f7",
            "stroke": "#b2b2b2",
            "nucleus": "#e3e3e3",
            "highlight": "#f7cbe8",
            "highlight_nucleus": "#e05fb0",
        },
        "panel": {"stroke": "#8f9bf5", "fill": "#ffffff", "title": "#000000", "weight": 0.7, "radius": 6.0},
        "callout": {"color": "#8f9bf5", "fill": "#c9d1fb", "fill_opacity": 0.55, "weight": 0.7, "text": "#000000"},
        "plot": {
            "bars": "#a8dcf0",
            "fit": "#fd6b6c",
            "axis": "#000000",
            "ridge": "#9b85c9",
            "series": ["#3eafdd", "#fd6b6c", "#96b1fa", "#f9be73", "#6fd1d5", "#7550a6"],
        },
        "font": {"label": "Arial"},
    },
}

# The stock "Triangle arrow" marker exactly as Inkscape writes it. Append it to <defs> AFTER
# the paths that use it (inkscape:collect="always" drops unreferenced markers).
TRIANGLE_MARKER = (
    '<marker style="overflow:visible" id="Triangle" refX="0" refY="0" orient="auto-start-reverse" '
    'inkscape:stockid="Triangle arrow" markerWidth="1" markerHeight="1" viewBox="0 0 1 1" '
    'inkscape:isstock="true" inkscape:collect="always" preserveAspectRatio="xMidYMid">'
    '<path transform="scale(0.5)" style="fill:context-stroke;fill-rule:evenodd;stroke:context-stroke;'
    'stroke-width:1pt" d="M 5.77,0 -2.88,5 V -5 Z" id="TrianglePath"/></marker>'
)


def load_style(path: str | Path | None = None) -> dict[str, Any]:
    """The effective style: DEFAULT_STYLE, then a preset, then a JSON file's overrides.

    `path` is a preset name ("nature", "reference") or a JSON file. A file may name a preset to
    build on with "extends": {"extends": "reference", "species": {"rna": {"color": "#e05"}}}.
    """
    style = copy.deepcopy(DEFAULT_STYLE)
    if path is None:
        return style
    if str(path) in STYLE_PRESETS:
        _merge(style, copy.deepcopy(STYLE_PRESETS[str(path)]))
        return style
    if not Path(path).is_file():
        raise ValueError(f"style {str(path)!r} is neither a preset {sorted(STYLE_PRESETS)} nor a file")
    overrides = json.loads(Path(path).read_text())
    base = overrides.pop("extends", None)
    if base is not None:
        if base not in STYLE_PRESETS:
            raise ValueError(f"{path}: extends {base!r}, not a preset {sorted(STYLE_PRESETS)}")
        _merge(style, copy.deepcopy(STYLE_PRESETS[base]))
    _merge(style, overrides)
    return style


def _merge(base: dict[str, Any], over: dict[str, Any]) -> None:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


# ---- geometry helpers ----------------------------------------------------------------


def _f(v: float) -> str:
    text = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _smooth(pts: list[tuple[float, float]], closed: bool = False) -> str:
    """Catmull-Rom spline through `pts` as cubic Bézier path data."""
    n = len(pts)
    d = f"M {_f(pts[0][0])},{_f(pts[0][1])}"
    for i in range(n) if closed else range(n - 1):
        p0 = pts[i - 1] if (closed or i > 0) else pts[i]
        p1, p2 = pts[i], pts[(i + 1) % n]
        p3 = pts[(i + 2) % n] if (closed or i + 2 < n) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f" C {_f(c1[0])},{_f(c1[1])} {_f(c2[0])},{_f(c2[1])} {_f(p2[0])},{_f(p2[1])}"
    return d + (" Z" if closed else "")


def _wave(
    x: float, y: float, length: float, waves: float, amplitude: float, samples: int = 24
) -> tuple[list[tuple[float, float]], Callable[[float], float]]:
    """Points and a y(x) function for a gentle sine strand starting at (x, y), first lobe up."""

    def y_at(px: float) -> float:
        return y - amplitude * math.sin(2 * math.pi * waves * (px - x) / length)

    count = max(samples, int(waves * 16))
    return [(x + length * k / count, y_at(x + length * k / count)) for k in range(count + 1)], y_at


def _bases(xs: list[float], y_at: Callable[[float], float], length: float) -> str:
    return " ".join(f"M {_f(px)},{_f(y_at(px))} v {_f(length)}" for px in xs)


def _blob(cx: float, cy: float, rx: float, ry: float, seed: float = 0.8) -> str:
    pts = []
    for i in range(18):
        t = 2 * math.pi * i / 18
        r = 1 + 0.05 * math.sin(3 * t + seed) + 0.03 * math.cos(5 * t)
        pts.append((cx + rx * r * math.cos(t), cy + ry * r * math.sin(t)))
    return _smooth(pts, closed=True)


def _g(gid: str, label: str, body: str, transform: str = "") -> str:
    tr = f' transform="{transform}"' if transform else ""
    return f'<g id="{gid}" inkscape:label="{escape(label)}"{tr}>{body}</g>'


def _text(tid: str, label: str, x: float, y: float, content: str, style: str, anchor: str = "middle") -> str:
    return (
        f'<text id="{tid}" inkscape:label="{escape(label)}" x="{_f(x)}" y="{_f(y)}" '
        f'text-anchor="{anchor}" style="{style}">{content}</text>'
    )


def _math(
    symbol: str,
    sub: str | None,
    style: dict[str, Any],
    size: float | None = None,
    sub_size: float | None = None,
) -> tuple[str, str]:
    """(style, content) for an italic maths symbol with an upright subscript."""
    font = style["font"]
    size = size or font["math_size"]
    sub_size = sub_size or font["sub_size"]
    css = f"font-family:{font['math']};font-style:italic;font-size:{_f(size)}px;fill:{style['ink']}"
    content = escape(symbol)
    if sub:
        content += (
            f'<tspan baseline-shift="sub" style="font-style:normal;font-size:{_f(sub_size)}px">{escape(sub)}</tspan>'
        )
    return css, content


# ---- components ------------------------------------------------------------------------


def arrow(
    gid: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    style: dict[str, Any],
    symbol: str | None = None,
    sub: str | None = None,
    label_offset: float = 3.0,
    label_side: str = "left",
) -> str:
    """Reaction arrow (stock Triangle marker) with an optional rate label beside its middle.

    The label sits `label_offset` from the arrow on `label_side` (left/right of the direction
    of travel; for a rightward arrow, "left" is above).
    """
    path = (
        f'<path id="{gid}-arrow" inkscape:label="arrow" d="M {_f(x1)},{_f(y1)} L {_f(x2)},{_f(y2)}" '
        f'style="fill:none;stroke:{style["ink"]};stroke-width:{_f(style["stroke"]["arrow"])};'
        f'stroke-linecap:butt;marker-end:url(#{style["marker"]})"/>'
    )
    body = path
    if symbol:
        length = math.hypot(x2 - x1, y2 - y1) or 1.0
        nx, ny = (y2 - y1) / length, -(x2 - x1) / length  # left normal (y down)
        if label_side == "right":
            nx, ny = -nx, -ny
        mx, my = (x1 + x2) / 2 + nx * label_offset, (y1 + y2) / 2 + ny * label_offset
        anchor = "middle" if abs(nx) < 0.5 else ("end" if nx < 0 else "start")
        if abs(nx) >= 0.5:
            my += style["font"]["math_size"] * 0.35  # vertical centring for side labels
        css, content = _math(symbol, sub, style)
        body += _text(f"{gid}-label", f"{symbol}{('_' + sub) if sub else ''} label", mx, my, content, css, anchor)
    return _g(gid, f"arrow {symbol or ''}".strip(), body)


def gene(
    gid: str,
    x: float,
    y: float,
    style: dict[str, Any],
    state: str = "on",
    width: float = 54.0,
    height: float = 8.0,
    promoter_width: float = 9.0,
    text: str | None = None,
    symbol: str = "G",
    tss: bool | None = None,
) -> str:
    """Gene on a DNA line: promoter + body, ON (blue) or OFF (grey), maths label to the left.

    (x, y) is the left end of the DNA line, on its centre line.
    """
    c = style["gene"][state]
    hair = _f(style["stroke"]["hairline"])
    top = y - height / 2
    p_x = x + 6
    b_x = p_x + promoter_width + 2
    b_w = width - (b_x - x) - 5
    parts = [
        f'<path id="{gid}-dna" inkscape:label="DNA" d="M {_f(x)},{_f(y)} H {_f(x + width)}" '
        f'style="fill:none;stroke:{style["dna"]};stroke-width:{_f(style["stroke"]["dna"])};stroke-linecap:round"/>',
        f'<rect id="{gid}-promoter" inkscape:label="promoter" x="{_f(p_x)}" y="{_f(top)}" width="{_f(promoter_width)}" '
        f'height="{_f(height)}" rx="1.2" '
        f'style="fill:{c["promoter"]};stroke:{c["promoter_edge"]};stroke-width:{hair}"/>',
        f'<rect id="{gid}-body" inkscape:label="gene body" x="{_f(b_x)}" y="{_f(top)}" width="{_f(b_w)}" '
        f'height="{_f(height)}" rx="1.2" style="fill:{c["body"]};stroke:{c["body_edge"]};stroke-width:{hair}"/>',
    ]
    font = style["font"]
    parts.append(
        _text(
            f"{gid}-text",
            "state label",
            b_x + b_w / 2,
            y + font["label_size"] * 0.35,
            escape(text if text is not None else state.upper()),
            f"font-family:{font['label']};font-weight:bold;font-size:{_f(font['label_size'])}px;fill:{c['text']}",
        )
    )
    css, content = _math(symbol, state, style, size=5.0, sub_size=3.2)
    parts.append(_text(f"{gid}-symbol", f"{symbol}_{state} label", x - 4, y + 1.8, content, css, "end"))
    if tss if tss is not None else state == "on":
        cx = p_x + promoter_width / 2
        parts.append(
            f'<path id="{gid}-tss" inkscape:label="TSS arrow" '
            f'd="M {_f(cx)},{_f(top)} V {_f(top - 4.5)} H {_f(cx + 5)}" '
            f'style="fill:none;stroke:{c["promoter"]};stroke-width:0.7;stroke-linecap:butt;'
            f'marker-end:url(#{style["marker"]})"/>'
        )
    return _g(gid, f"{state.upper()} gene", "".join(parts))


def rna_strand(
    gid: str,
    x: float,
    y: float,
    style: dict[str, Any],
    length: float = 27.0,
    waves: float = 1.5,
    amplitude: float = 1.3,
    base_spacing: float = 1.1,
    base_length: float = 1.8,
    label: str | None = "mRNA",
    symbol: str | None = "M",
    color: str | None = None,
) -> str:
    """Gentle RNA strand with a comb of vertical bases; (x, y) is its left end on the centre line."""
    col = color or style["species"]["rna"]["color"]
    pts, y_at = _wave(x, y, length, waves, amplitude)
    count = int((length - 0.6) / base_spacing)
    xs = [x + 0.6 + base_spacing * i for i in range(count + 1) if 0.6 + base_spacing * i <= length - 0.4]
    parts = [
        f'<path id="{gid}-bases" inkscape:label="bases" d="{_bases(xs, y_at, base_length)}" '
        f'style="fill:none;stroke:{col};stroke-width:{_f(style["stroke"]["base"])};stroke-linecap:round"/>',
        f'<path id="{gid}-strand" inkscape:label="strand" d="{_smooth(pts)}" '
        f'style="fill:none;stroke:{col};stroke-width:{_f(style["stroke"]["strand"])};'
        f'stroke-linecap:round;stroke-linejoin:round"/>',
    ]
    if label:
        parts.append(_species_label(gid, x + length / 2, y + amplitude + base_length + 4.6, label, symbol, col, style))
    return _g(gid, label or "RNA", "".join(parts))


def _species_label(gid: str, x: float, y: float, name: str, symbol: str | None, color: str, style: dict) -> str:
    font = style["font"]
    content = escape(name)
    if symbol:
        content += f' (<tspan style="font-family:{font["math"]};font-style:italic">{escape(symbol)}</tspan>)'
    return _text(
        f"{gid}-label",
        f"{name} label",
        x,
        y,
        content,
        f"font-family:{font['label']};font-size:{_f(font['label_size'])}px;fill:{color}",
    )


# A hand-tuned compact fold (unit coordinates around the centre): the chain enters at the
# N-terminus, loops twice and crosses itself; points 13..17 pass OVER earlier strands.
_FOLD = [
    (-7.3, 4.7),
    (-5.4, 3.7),
    (-4.2, 1.7),
    (-5.2, -0.5),
    (-4.6, -2.7),
    (-2.4, -3.7),
    (-0.8, -2.5),
    (-1.2, -0.3),
    (-0.2, 2.1),
    (2.0, 2.9),
    (4.0, 1.9),
    (4.4, -0.3),
    (3.2, -2.5),
    (1.2, -3.3),
    (-0.6, -1.5),
    (-2.6, 0.1),
    (-3.6, 2.1),
    (-2.4, 3.9),
    (0.0, 4.5),
    (2.8, 4.3),
    (5.0, 3.3),
    (6.2, 1.3),
]
_FOLD_OVER = (13, 17)


def protein(
    gid: str,
    cx: float,
    cy: float,
    style: dict[str, Any],
    size: float = 1.0,
    mirror: bool = False,
    label: str | None = "protein",
    symbol: str | None = "P",
    color: str | None = None,
) -> str:
    """Abstract folded protein: one continuous chain with over/under gaps, on a faint surface.

    Drawn about (cx, cy); ~15 x 11 units at size 1.
    """
    sp = style["species"]["protein"]
    col = color or sp["color"]
    sx = -size if mirror else size
    pts = [(cx + px * sx, cy + py * size) for px, py in _FOLD]
    chain = _smooth(pts)
    segs = chain.split(" C ")
    a, b = _FOLD_OVER
    over = f"M {_f(pts[a][0])},{_f(pts[a][1])} C " + " C ".join(segs[a + 1 : b])
    w = style["stroke"]["strand"]
    line = f"fill:none;stroke:{col};stroke-width:{_f(w)};stroke-linecap:round;stroke-linejoin:round"
    parts = [
        f'<path id="{gid}-surface" inkscape:label="surface" d="{_blob(cx, cy, 7.0 * size, 5.1 * size)}" '
        f'style="fill:{sp["surface"]};stroke:none"/>',
        f'<path id="{gid}-chain" inkscape:label="polypeptide chain" d="{chain}" style="{line}"/>',
        f'<path id="{gid}-gap" inkscape:label="crossing gap" d="{over}" '
        f'style="fill:none;stroke:{sp["surface"]};stroke-width:{_f(w * 2.1)};stroke-linecap:butt"/>',
        f'<path id="{gid}-over" inkscape:label="chain (over-crossing)" d="{over}" style="{line}"/>',
    ]
    if label:
        parts.append(_species_label(gid, cx, cy + 5.6 * size + 4.1, label, symbol, col, style))
    return _g(gid, label or "protein", "".join(parts))


def _tint(hex_color: str, opacity: float) -> str:
    """The opaque colour `hex_color` shows at `opacity` over white."""
    return "#" + "".join(f"{round(255 - (255 - int(hex_color[i : i + 2], 16)) * opacity):02x}" for i in (1, 3, 5))


def decay(
    gid: str,
    x: float,
    y: float,
    style: dict[str, Any],
    species: str = "rna",
    label: str | None = "decay",
) -> str:
    """Degradation products: two short broken pieces + loose units fading to paler, opaque tints.

    (x, y) is the left edge, vertically centred; ~10 x 10 units.
    """
    col = style["species"][species]["color"]
    parts: list[str] = []
    for i, (fx, fy, angle, fade) in enumerate(((0.0, -3.2, -20, 0.8), (0.4, 2.6, 25, 0.7)), 1):
        tint = _tint(col, fade)
        transform = f"rotate({angle} {_f(x + fx + 2)} {_f(y + fy)})"
        if species == "rna":
            pts, y_at = _wave(x + fx, y + fy, 4.0, 1.0, 0.5, samples=12)
            parts.append(
                f'<path id="{gid}-piece{i}-bases" inkscape:label="bases" transform="{transform}" '
                f'd="{_bases([x + fx + 0.5 + 1.1 * k for k in range(3)], y_at, 1.8)}" '
                f'style="fill:none;stroke:{tint};stroke-width:{_f(style["stroke"]["base"])};stroke-linecap:round"/>'
                f'<path id="{gid}-piece{i}" inkscape:label="fragment {i}" transform="{transform}" d="{_smooth(pts)}" '
                f'style="fill:none;stroke:{tint};stroke-width:{_f(style["stroke"]["strand"])};stroke-linecap:round"/>'
            )
        else:
            beads = [(x + fx, y + fy + 0.4), (x + fx + 1.4, y + fy - 0.4), (x + fx + 2.8, y + fy + 0.3)]
            parts.append(
                f'<g id="{gid}-piece{i}" inkscape:label="fragment {i}" transform="{transform}">'
                f'<path id="{gid}-piece{i}-bonds" inkscape:label="bonds" '
                f'd="M {" L ".join(f"{_f(px)},{_f(py)}" for px, py in beads)}" '
                f'style="fill:none;stroke:{tint};stroke-width:{_f(style["stroke"]["base"])};stroke-linecap:round"/>'
                + "".join(
                    f'<circle id="{gid}-piece{i}-unit{j}" inkscape:label="unit" cx="{_f(px)}" cy="{_f(py)}" r="0.7" '
                    f'style="fill:{tint};stroke:none"/>'
                    for j, (px, py) in enumerate(beads, 1)
                )
                + "</g>"
            )
    loose = [(5.6, -1.6, 0.75), (7.2, -4.4, 0.6), (6.4, 0.4, 0.55), (8.6, -1.8, 0.45), (7.8, 3.2, 0.35)]
    for j, (dx, dy, fade) in enumerate(loose, 1):
        parts.append(
            f'<circle id="{gid}-unit{j}" inkscape:label="unit" cx="{_f(x + dx)}" cy="{_f(y + dy)}" r="0.6" '
            f'style="fill:{_tint(col, fade)};stroke:none"/>'
        )
    if label:
        font = style["font"]
        parts.append(
            _text(
                f"{gid}-label",
                "decay label",
                x + 5,
                y + 8.8,
                escape(label),
                f"font-family:{font['label']};font-size:{_f(font['label_size'])}px;fill:{style['muted']}",
            )
        )
    return _g(gid, f"{species} decay", "".join(parts))


COMPONENTS: dict[str, Callable[..., str]] = {
    "arrow": arrow,
    "gene": gene,
    "rna_strand": rna_strand,
    "protein": protein,
    "decay": decay,
}

_CHOICES = {
    "arrow": {"label_side": ["left", "right"]},
    "gene": {"state": ["on", "off"]},
    "decay": {"species": ["rna", "protein"]},
}
_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "rna_strand": {"length": (2, 500), "waves": (0, 20), "amplitude": (0, 20)},
    "protein": {"size": (0.1, 10)},
    "gene": {"width": (10, 500), "height": (1, 100)},
}
for _name, _fn in COMPONENTS.items():
    registry.register(_fn, "schematic", ranges=_RANGES.get(_name), choices=_CHOICES.get(_name))


def build(kind: str, params: dict[str, Any], style: dict[str, Any]) -> str:
    """Build component `kind` from JSON-ish `params` (which must include `id`)."""
    return registry.build(kind, params, style)
