"""Exact design measurements of a figure at print size, and checks against the style rules.

Typography (pt), colours (near-duplicates, hue families), stroke widths (pt) and
near-miss alignments (mm), all from the SVG source: user units are scaled by the root
width/height against the viewBox, and every element's composed transform. The rules are
the user's confirmed figure style (P02 alignment, P03 colour, P04 type, P07 strokes);
`rules` overrides any default.

Pure and offline. The walk reads lxml attributes directly and parses styles with a cached
plain split: inkex's `el.style` and `specified_style()` re-parse through tinycss2 on every
access, which made a 500 KB figure take many seconds.

    python -m inkscape_mcp.figure_measure FILE [FILE...] [--json]
"""

from __future__ import annotations

import functools
import itertools
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lxml import etree

_SVG = "http://www.w3.org/2000/svg"
_SKIP = frozenset(
    f"{{{_SVG}}}{n}" for n in ("defs", "metadata", "title", "desc", "style", "script", "clipPath", "mask")
)
_SKIP |= {"{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}namedview"}
_TEXT = f"{{{_SVG}}}text"
_SHAPES = frozenset(f"{{{_SVG}}}{n}" for n in ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon"))
_MM_PER = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6, "px": 25.4 / 96, "": 25.4 / 96}
_PX_PER = {"px": 1.0, "": 1.0, "pt": 96 / 72, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "pc": 16.0}
_INHERITED = (
    "fill",
    "stroke",
    "stroke-width",
    "font-family",
    "font-weight",
    "font-style",
    "text-anchor",
    "fill-opacity",
    "stroke-opacity",
)
_OWN = ("opacity", "display", "filter")
_NAMES = {"black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000", "blue": "#0000ff"}

DEFAULT_RULES: dict[str, Any] = {
    "family": "Calibri",
    "sizes": [10, 8, 7, 6],
    "size_tolerance": 0.15,
    "min_size": 6,
    "max_hue_families": 3,
    # P07, weight by role: arrows in this band (big connecting arrows may exceed it), panel
    # outlines at least this heavy; other strokes are reported, not checked.
    "arrow_pt": (0.25, 1.0),
    "panel_min_pt": 1.0,
}
_NEAR_DE = 3.0  # CIE76 distance under which two colours are copy drift, not design
_CHROMA = 15.0  # Lab chroma above which a colour has a hue
_HUE_MERGE = 25.0  # degrees
_NEAR_MIN, _NEAR_MAX = 0.02, 0.5  # mm: a misalignment, not a match and not a design choice
_ROW_REACH = 20.0  # mm: texts further apart vertically are not compared
_BIG_RECT = 0.15  # fraction of the page width
_MAX_IDS = 5  # ids per finding; the full lists are in the dict

Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_raw_get = etree.ElementBase.get


@functools.lru_cache(maxsize=4096)
def _declarations(style: str) -> dict[str, str]:
    pairs = (part.split(":", 1) for part in style.split(";") if ":" in part)
    return {k.strip(): v.strip() for k, v in pairs}


def _mul(m: Matrix, n: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


@functools.lru_cache(maxsize=4096)
def _transform(text: str) -> Matrix:
    result = _IDENTITY
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = [float(x) for x in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", args)]
        if name == "matrix" and len(v) == 6:
            m: Matrix = (v[0], v[1], v[2], v[3], v[4], v[5])
        elif name == "translate" and v:
            m = (1.0, 0.0, 0.0, 1.0, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale" and v:
            m = (v[0], 0.0, 0.0, v[1] if len(v) > 1 else v[0], 0.0, 0.0)
        elif name == "rotate" and v:
            c, s = math.cos(math.radians(v[0])), math.sin(math.radians(v[0]))
            m = (c, s, -s, c, 0.0, 0.0)
            if len(v) == 3:
                m = _mul(_mul((1.0, 0.0, 0.0, 1.0, v[1], v[2]), m), (1.0, 0.0, 0.0, 1.0, -v[1], -v[2]))
        elif name == "skewX" and v:
            m = (1.0, 0.0, math.tan(math.radians(v[0])), 1.0, 0.0, 0.0)
        elif name == "skewY" and v:
            m = (1.0, math.tan(math.radians(v[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        result = _mul(result, m)
    return result


def _scales(m: Matrix) -> tuple[float, float, float]:
    """(uniform scale from the determinant, x scale, y scale) of a matrix."""
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])), math.hypot(m[0], m[1]), math.hypot(m[2], m[3])


def _length(value: str) -> tuple[float, str] | None:
    match = re.match(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-z%]*)", value or "")
    return (float(match.group(1)), match.group(2)) if match else None


def _page(svg: Any) -> tuple[float, float, float]:
    """(pt per user unit, page width mm, page height mm)."""
    box = [float(v) for v in re.findall(r"[-+]?[\d.]+(?:[eE][-+]?\d+)?", svg.get("viewBox") or "")]
    dims = []
    for i, name in enumerate(("width", "height")):
        parsed = _length(svg.get(name) or "")
        if parsed and parsed[1] != "%":
            dims.append(parsed[0] * _MM_PER.get(parsed[1], _MM_PER["px"]))
        else:
            dims.append(box[2 + i] * _MM_PER["px"] if len(box) == 4 else 0.0)
    mm_per_uu = dims[0] / box[2] if len(box) == 4 and box[2] and dims[0] else _MM_PER["px"]
    pages = (
        dims[0] or (box[2] * mm_per_uu if len(box) == 4 else 0.0),
        dims[1] or (box[3] * mm_per_uu if len(box) == 4 else 0.0),
    )
    return mm_per_uu * 72 / 25.4, pages[0], pages[1]


# ---- colour ---------------------------------------------------------------------------------


def _hex(value: str) -> str | None:
    """`#rrggbb` for a hex, rgb() or basic named colour; None for none/url/context-*/other."""
    v = (value or "").strip().lower()
    if v in _NAMES:
        return _NAMES[v]
    if re.fullmatch(r"#[0-9a-f]{3}", v):
        return "#" + "".join(c * 2 for c in v[1:])
    if re.fullmatch(r"#[0-9a-f]{6}([0-9a-f]{2})?", v):
        return v[:7]
    match = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", v)
    return "#{:02x}{:02x}{:02x}".format(*(min(255, int(g)) for g in match.groups())) if match else None


def _lab(color: str) -> tuple[float, float, float]:
    """CIE L*a*b* (D65) of `#rrggbb`."""
    chans = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    r, g, b = lin
    xyz = (
        (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047,
        0.2126729 * r + 0.7151522 * g + 0.0721750 * b,
        (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883,
    )
    f = [t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116 for t in xyz]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def _delta_e(a: str, b: str) -> float:
    return math.dist(_lab(a), _lab(b))


def _near_duplicates(counts: Counter[str]) -> list[list[tuple[str, int]]]:
    """Groups of colours within ΔE < 3 of the group's most used colour, groups of two or more."""
    groups: list[list[str]] = []
    for color, _ in counts.most_common():
        for group in groups:
            if _delta_e(group[0], color) < _NEAR_DE:
                group.append(color)
                break
        else:
            groups.append([color])
    return [[(c, counts[c]) for c in g] for g in groups if len(g) > 1]


def _hue_families(counts: Counter[str]) -> tuple[list[dict[str, Any]], list[tuple[str, int]]]:
    """(hue families, greys): chromatic colours chained by hue angle within 25 degrees."""
    hued, greys = [], []
    for color, n in counts.most_common():
        _, a, b = _lab(color)
        if math.hypot(a, b) > _CHROMA:
            hued.append((math.degrees(math.atan2(b, a)) % 360, color, n))
        else:
            greys.append((color, n))
    hued.sort()
    if not hued:
        return [], greys
    count = len(hued)
    splits = [i for i in range(count) if (hued[(i + 1) % count][0] - hued[i][0]) % 360 > _HUE_MERGE or count == 1]
    if not splits:
        runs = [hued]
    else:
        start = (splits[0] + 1) % count
        ordered = hued[start:] + hued[:start]
        runs, run = [], [ordered[0]]
        for prev, item in itertools.pairwise(ordered):
            if (item[0] - prev[0]) % 360 > _HUE_MERGE:
                runs.append(run)
                run = []
            run.append(item)
        runs.append(run)
    families = [
        {
            "hue": [round(r[0][0]), round(r[-1][0])],
            "colors": [(c, n) for _, c, n in sorted(r, key=lambda t: -t[2])],
            "uses": sum(n for _, _, n in r),
        }
        for r in runs
    ]
    return sorted(families, key=lambda f: -f["uses"]), greys


# ---- walk -----------------------------------------------------------------------------------


def _apply(inh: dict[str, Any], el: Any) -> dict[str, Any]:
    """`inh` updated with the inherited presentation properties `el` sets (copied only if it sets any)."""
    style = _raw_get(el, "style")
    decl = _declarations(style) if style else {}
    out = inh
    for name in (*_INHERITED, "font-size"):
        value = decl.get(name) or _raw_get(el, name)
        if not value:
            continue
        if out is inh:
            out = dict(inh)
        if name == "font-size":
            parsed = _length(value)
            if parsed:
                num, unit = parsed
                out[name] = (
                    inh["font-size"] * num / 100
                    if unit == "%"
                    else inh["font-size"] * num
                    if unit in ("em", "ex")
                    else num * _PX_PER.get(unit, 1.0)
                )
        else:
            out[name] = value.strip()
    return out


def _own(el: Any, name: str) -> str:
    style = _raw_get(el, "style")
    value = _declarations(style).get(name) if style else None
    return value if value is not None else (_raw_get(el, name) or "")


def _weight(value: str) -> str:
    v = (value or "normal").strip().lower()
    return "bold" if v in ("700", "bold") else "normal" if v in ("400", "normal") else v


def _first_number(value: str | None) -> float:
    match = re.match(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)", value or "")
    return float(match.group(1)) if match else 0.0


class _Collector:
    def __init__(self, pt_uu: float, page_mm: tuple[float, float]) -> None:
        self.pt_uu = pt_uu
        self.mm_uu = pt_uu * 25.4 / 72
        self.page_mm = page_mm
        self.texts: list[dict[str, Any]] = []
        self.fills: Counter[str] = Counter()
        self.strokes: Counter[str] = Counter()
        self.widths: Counter[float] = Counter()
        self.width_ids: dict[float, list[str]] = {}
        self.arrows: list[tuple[str, float]] = []  # (id, stroke pt) of paths with an end marker
        self.panels: list[tuple[str, float]] = []  # (id, stroke pt) of large outlined rects
        self.opacities: Counter[float] = Counter()
        self.gradients = 0
        self.filters = 0
        self.rects: list[dict[str, Any]] = []
        self.gradient_ids: set[str] = set()

    def visit(self, el: Any, m: Matrix, inh: dict[str, Any], active: bool, root_id: str) -> None:
        if not isinstance(el.tag, str) or el.tag in _SKIP or _own(el, "display") == "none":
            return
        tr = _raw_get(el, "transform")
        if tr:
            m = _mul(m, _transform(tr))
        inh = _apply(inh, el)
        active = active or (el.get("id") == root_id)
        if active:
            self.element(el, m, inh)
        if el.tag == _TEXT:
            return
        for child in el:
            self.visit(child, m, inh, active, root_id)

    def element(self, el: Any, m: Matrix, inh: dict[str, Any]) -> None:
        opacity = _own(el, "opacity")
        if _own(el, "filter").startswith("url("):
            self.filters += 1
        if el.tag == _TEXT:
            self.text(el, m, inh)
            return
        fill, stroke = inh.get("fill", ""), inh.get("stroke", "")
        if el.tag in _SHAPES:
            self.paint(el, m, inh, fill, stroke)
        if opacity:
            self.opacity(opacity, el, inh, fill if el.tag in _SHAPES else "")

    def opacity(self, opacity: str, el: Any, inh: dict[str, Any], fill: str) -> None:
        try:
            o = float(opacity) * (float(inh.get("fill-opacity", 1)) if fill and fill != "none" else 1.0)
        except ValueError:
            return
        if o < 0.999:
            self.opacities[round(o, 2)] += 1

    def paint(self, el: Any, m: Matrix, inh: dict[str, Any], fill: str, stroke: str) -> None:
        if fill.startswith("url("):
            self.gradients += 1
        if stroke.startswith("url("):
            self.gradients += 1
        if (c := _hex(fill)) is not None:
            self.fills[c] += 1
        width = 0.0
        if (c := _hex(stroke)) is not None:
            self.strokes[c] += 1
            parsed = _length(str(inh.get("stroke-width", "1")))
            if parsed and parsed[0] > 0:
                width = round(parsed[0] * _PX_PER.get(parsed[1], 1.0) * _scales(m)[0] * self.pt_uu, 2)
                self.widths[width] += 1
                self.width_ids.setdefault(width, []).append(el.get("id") or "")
                if (_own(el, "marker-end") or _own(el, "marker-start")).startswith("url("):
                    self.arrows.append((el.get("id") or "", width))
        if el.tag == f"{{{_SVG}}}rect":
            _, sx, sy = _scales(m)
            w = _first_number(el.get("width")) * sx * self.mm_uu
            h = _first_number(el.get("height")) * sy * self.mm_uu
            if w > _BIG_RECT * self.page_mm[0]:
                self.rects.append({"id": el.get("id") or "", "w": w, "h": h})
                if width:
                    self.panels.append((el.get("id") or "", width))

    def text(self, el: Any, m: Matrix, inh: dict[str, Any]) -> None:
        nodes = [n for n in el.iter() if isinstance(n.tag, str)]
        content = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
        if not content:
            return
        node = next((n for n in nodes if (n.text or "").strip()), el)
        chain = []
        walk = node
        while walk is not el:
            chain.append(walk)
            walk = walk.getparent()
        style = inh
        for item in reversed(chain):
            style = _apply(style, item)
        first = next((n for n in nodes if n.get("x") is not None or n.get("y") is not None), el)
        x, y = _first_number(first.get("x")), _first_number(first.get("y"))
        px, py = m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]
        family = str(style.get("font-family", "")).split(",")[0].strip().strip("'\"") or "?"
        raw = str(style.get("fill", "")) or "#000000"
        fill = _hex(raw) or raw
        if _hex(raw) is not None:
            self.fills[fill] += 1
        self.texts.append(
            {
                "id": el.get("id") or "",
                "text": content,
                "family": family,
                "weight": _weight(str(style.get("font-weight", ""))),
                "style": str(style.get("font-style", "normal")),
                "pt": round(style.get("font-size", 16.0) * _scales(m)[0] * self.pt_uu, 1),
                "fill": fill,
                "anchor": str(style.get("text-anchor", "start")),
                "x_mm": px * self.mm_uu,
                "y_mm": py * self.mm_uu,
            }
        )


# ---- analysis -------------------------------------------------------------------------------


def _near_misses(values: list[float], near: Callable[[int, int], bool]) -> list[tuple[int, int]]:
    """(outlier, reference) index pairs: values 0.02-0.5 apart where `near`; the outlier is the
    less shared of the two (later in the list on a tie), once, against its closest reference."""
    shared = [
        sum(1 for k, w in enumerate(values) if abs(w - v) <= _NEAR_MIN and near(i, k)) for i, v in enumerate(values)
    ]
    best: dict[int, tuple[float, int]] = {}
    for i, v in enumerate(values):
        for j, w in enumerate(values):
            d = abs(v - w)
            if i == j or not (_NEAR_MIN < d < _NEAR_MAX) or not near(i, j):
                continue
            if (shared[i], -i) < (shared[j], -j) and (i not in best or d < best[i][0]):
                best[i] = (d, j)
    return [(i, j) for i, (_, j) in sorted(best.items())]


def _alignment(col: _Collector) -> dict[str, list[dict[str, Any]]]:
    left = [t for t in col.texts if t["anchor"] == "start"]
    xs = [t["x_mm"] for t in left]
    pairs = _near_misses(xs, lambda i, k: abs(left[i]["y_mm"] - left[k]["y_mm"]) <= _ROW_REACH)
    texts = [
        {
            "id": left[i]["id"],
            "text": left[i]["text"][:30],
            "x_mm": round(xs[i], 2),
            "ref_x_mm": round(xs[j], 2),
            "ref": left[j]["text"][:30],
            "dx_mm": round(xs[i] - xs[j], 2),
        }
        for i, j in pairs
    ]
    rects = []
    for dim in ("w", "h"):
        vals = [r[dim] for r in col.rects]
        for i, j in _near_misses(vals, lambda i, k: True):
            rects.append(
                {
                    "id": col.rects[i]["id"],
                    "ref": col.rects[j]["id"],
                    "dim": "width" if dim == "w" else "height",
                    "mm": round(vals[i], 2),
                    "ref_mm": round(vals[j], 2),
                    "d_mm": round(vals[i] - vals[j], 2),
                }
            )
    return {"texts": texts, "rects": rects}


def _ids(items: list[str]) -> str:
    shown = [i for i in items if i][:_MAX_IDS]
    return ", ".join(shown) + (f", … +{len(items) - _MAX_IDS}" if len(items) > _MAX_IDS else "")


def _num(v: float) -> str:
    return f"{v:g}"


def _checks(result: dict[str, Any], col: _Collector, rules: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(code: str, ok: bool, text: str, warn: bool = False) -> None:
        out.append({"code": code, "ok": ok, "text": text, **({"warn": True} if warn else {})})

    al = result["alignment"]
    if al["texts"] or al["rects"]:
        parts = [
            f'{a["id"] or a["text"]} "{a["text"]}" {a["dx_mm"]:+g} mm ({a["x_mm"]:g} vs {a["ref_x_mm"]:g})'
            for a in al["texts"][:_MAX_IDS]
        ]
        parts += [
            f"{a['id']} {a['dim']} {a['d_mm']:+g} mm ({a['mm']:g} vs {a['ref_mm']:g})" for a in al["rects"][:_MAX_IDS]
        ]
        add("P02", False, f"{len(al['texts'])} text and {len(al['rects'])} rect near-misses: " + "; ".join(parts))
    else:
        add("P02", True, "no near-miss alignments")

    colors = result["colors"]
    fams, dups = colors["hue_families"], colors["near_duplicates"]
    if len(fams) > rules["max_hue_families"]:  # a category set (clusters, steps) may add hues
        add("P03", False, f"{len(fams)} hue families (role colours max {rules['max_hue_families']})", warn=True)
    else:
        add("P03", True, f"{len(fams)} hue families")
    if dups:
        add(
            "P03",
            False,
            "near-duplicate colours: "
            + "; ".join(
                " ~ ".join(f"{c} ({n})" for c, n in g[:4]) + (f" +{len(g) - 4}" if len(g) > 4 else "")
                for g in dups[:_MAX_IDS]
            ),
        )
    else:
        add("P03", True, "no near-duplicate colours")

    texts = col.texts
    families = Counter(t["family"] for t in texts)
    wanted = rules["family"]
    stray = [t["id"] for t in texts if t["family"] != wanted]
    listing = ", ".join(f"{f} ({n})" for f, n in families.most_common())
    if stray:
        add("P04", False, f"{len(families)} fonts: {listing} — ids {_ids(stray)}")
    else:
        add("P04", True, f"font {wanted} only ({len(texts)})")
    tol = rules["size_tolerance"]
    off = [t for t in texts if not any(abs(t["pt"] - s) <= tol for s in rules["sizes"])]
    sizes_txt = "/".join(_num(s) for s in rules["sizes"])
    if off:
        by_size = Counter(t["pt"] for t in off)
        add(
            "P04",
            False,
            f"sizes off {sizes_txt} pt: "
            + ", ".join(f"{_num(s)} x{n}" for s, n in sorted(by_size.items()))
            + f" — ids {_ids([t['id'] for t in off])}",
        )
    else:
        add("P04", True, f"sizes {sizes_txt} pt only")
    small = [t for t in texts if t["pt"] < rules["min_size"] - tol]
    if small:
        add(
            "P04",
            False,
            f"{len(small)} below {_num(rules['min_size'])} pt, smallest {_num(min(t['pt'] for t in small))}"
            f" — ids {_ids([t['id'] for t in small])}",
        )
    else:
        add("P04", True, f"none below {_num(rules['min_size'])} pt")

    lo, hi = rules["arrow_pt"]
    if col.arrows:
        weights = [w for _, w in col.arrows]
        spread = f"{len(weights)} arrows {_num(min(weights))}-{_num(max(weights))} pt"
        odd = [(i, w) for i, w in col.arrows if not lo <= w <= hi]
        if odd:  # big connecting arrows may be heavier: worth a look, not a fault
            listing = ", ".join(f"{i or '?'} {_num(w)}" for i, w in odd[:_MAX_IDS])
            more = f", … +{len(odd) - _MAX_IDS}" if len(odd) > _MAX_IDS else ""
            add("P07", False, f"{spread}; {len(odd)} outside {_num(lo)}-{_num(hi)} pt: {listing}{more}", warn=True)
        else:
            add("P07", True, f"{spread}, all within {_num(lo)}-{_num(hi)} pt")
    thin = [(i, w) for i, w in col.panels if w < rules["panel_min_pt"]]
    if thin:
        listing = ", ".join(f"{i or '?'} {_num(w)}" for i, w in thin[:_MAX_IDS])
        add("P07", False, f"{len(thin)} panel outlines under {_num(rules['panel_min_pt'])} pt: {listing}")
    elif col.panels:
        add("P07", True, f"{len(col.panels)} panel outlines, all at least {_num(rules['panel_min_pt'])} pt")
    return out


def measure(svg_bytes: bytes, root_id: str = "", rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """Measurements and rule checks for the document, or the subtree with id `root_id`."""
    rule = {**DEFAULT_RULES, **(rules or {})}
    svg = etree.fromstring(svg_bytes, parser=etree.XMLParser(huge_tree=True))
    if root_id and not svg.xpath("//*[@id=$i]", i=root_id):
        raise KeyError(f"no element with id {root_id!r}")
    pt_uu, w_mm, h_mm = _page(svg)
    col = _Collector(pt_uu, (w_mm, h_mm))
    inh: dict[str, Any] = {"font-size": 16.0}
    root_m = _transform(_raw_get(svg, "transform") or "")
    inh = _apply(inh, svg)
    for child in svg:
        col.visit(child, root_m, inh, not root_id or svg.get("id") == root_id, root_id)

    styles = Counter((t["family"], t["weight"], t["style"], t["pt"], t["fill"]) for t in col.texts)
    all_colors = col.fills + col.strokes
    families, greys = _hue_families(all_colors)
    result: dict[str, Any] = {
        "page_mm": [round(w_mm, 2), round(h_mm, 2)],
        "pt_per_unit": round(pt_uu, 4),
        "texts": {
            "count": len(col.texts),
            "styles": [
                {"family": k[0], "weight": k[1], "style": k[2], "pt": k[3], "fill": k[4], "count": n}
                for k, n in sorted(styles.items(), key=lambda kv: (-kv[0][3], -kv[1]))
            ],
        },
        "colors": {
            "fills": dict(col.fills.most_common()),
            "strokes": dict(col.strokes.most_common()),
            "near_duplicates": _near_duplicates(all_colors),
            "hue_families": families,
            "greys": greys,
            "opacities": {str(k): v for k, v in sorted(col.opacities.items())},
            "gradients": col.gradients,
            "filters": col.filters,
        },
        "strokes": {"widths_pt": {str(w): n for w, n in sorted(col.widths.items())}},
        "alignment": _alignment(col),
    }
    result["checks"] = _checks(result, col, rule)
    return result


def report(result: dict[str, Any]) -> str:
    """The result as compact text: a header, the type styles, colours, strokes, then the checks."""
    t, c = result["texts"], result["colors"]
    lines = [f"{result['page_mm'][0]:g} x {result['page_mm'][1]:g} mm, 1 user unit = {result['pt_per_unit']:g} pt"]
    styles = t["styles"]
    shown = [
        f"{_num(s['pt'])}pt {s['family']}{' ' + s['weight'] if s['weight'] != 'normal' else ''}"
        f"{' ' + s['style'] if s['style'] != 'normal' else ''} {s['fill']} x{s['count']}"
        for s in styles[:14]
    ]
    lines.append(
        f"type, {t['count']} texts: " + "; ".join(shown) + (f"; … +{len(styles) - 14}" if len(styles) > 14 else "")
    )
    fams = "; ".join(f"{f['hue'][0]}-{f['hue'][1]}° x{f['uses']}" for f in c["hue_families"][:6])
    greys = ", ".join(f"{g} x{n}" for g, n in c["greys"][:6])
    lines.append(
        f"colours: {len(c['fills'])} fills, {len(c['strokes'])} strokes;"
        f" hue families {len(c['hue_families'])} ({fams}); greys {greys}"
    )
    extras = []
    if c["opacities"]:
        levels = sorted(float(k) for k in c["opacities"])
        extras.append(f"opacity<1 x{sum(c['opacities'].values())} ({levels[0]:g}-{levels[-1]:g})")
    if c["gradients"]:
        extras.append(f"gradients x{c['gradients']}")
    if c["filters"]:
        extras.append(f"filters x{c['filters']}")
    if extras:
        lines.append("effects: " + "; ".join(extras))
    widths = result["strokes"]["widths_pt"]
    top = sorted(widths.items(), key=lambda kv: -kv[1])[:8]
    more = f", … {len(widths) - len(top)} more widths" if len(widths) > len(top) else ""
    lines.append(
        "stroke pt: "
        + (", ".join(f"{float(w):g} x{n}" for w, n in sorted(top, key=lambda kv: float(kv[0]))) + more or "none")
    )
    lines += [f"{k['code']} {'✓' if k['ok'] else '⚠' if k.get('warn') else '✗'} {k['text']}" for k in result["checks"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    files = [a for a in args if a != "--json"]
    if not files:
        print("usage: python -m inkscape_mcp.figure_measure FILE [FILE...] [--json]", file=sys.stderr)
        return 2
    for name in files:
        result = measure(Path(name).read_bytes())
        print(f"== {Path(name).name}")
        print(json.dumps(result, indent=1, default=str) if as_json else report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
