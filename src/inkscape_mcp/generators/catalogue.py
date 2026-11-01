"""Visual catalogue of every registered generator: a contact sheet built from the registry.

One row per generator, three cells per row — three seeds for seeded generators, three
parameter variants for the others — each captioned with the exact call that reproduces it,
so "flow_lines, seed 2" can be pointed at and asked for. New generators appear automatically
(with a generic demo layout unless they need a custom one in DEMOS).

    python -m inkscape_mcp.generators.catalogue out.svg [category]
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any
from xml.sax.saxutils import escape

from . import registry

CELL_W, CELL_H, GAP, HEAD = 52.0, 38.0, 6.0, 9.0
Box = tuple[float, float, float, float]
Demo = Callable[[Box], dict[str, Any]]

# Per-generator demo params for a cell (x, y, w, h). Generators not listed get generic ones.
DEMOS: dict[str, Demo] = {
    "arrow": lambda b: {
        "x1": b[0] + 8,
        "y1": b[1] + b[3] * 0.6,
        "x2": b[0] + b[2] - 8,
        "y2": b[1] + b[3] * 0.6,
        "symbol": "k",
    },
    "gene": lambda b: {"x": b[0] + 9, "y": b[1] + b[3] / 2 + 2, "width": b[2] - 11},
    "rna_strand": lambda b: {"x": b[0] + 8, "y": b[1] + b[3] / 2 - 4, "length": b[2] - 16},
    "protein": lambda b: {"cx": b[0] + b[2] / 2, "cy": b[1] + b[3] / 2 - 4},
    "decay": lambda b: {"x": b[0] + b[2] / 2 - 5, "y": b[1] + b[3] / 2 - 3},
    "wobble_line": lambda b: {"x1": b[0] + 4, "y1": b[1] + b[3] / 2, "x2": b[0] + b[2] - 4, "y2": b[1] + b[3] / 2},
    "radial": lambda b: {"cx": b[0] + b[2] / 2, "cy": b[1] + b[3] / 2, "r_outer": b[3] / 2 - 2},
    "iso_box": lambda b: {"cx": b[0] + b[2] / 2, "cy": b[1] + b[3] - 7},
}
# Unseeded generators show parameter variants instead of seeds.
VARIANTS: dict[str, list[dict[str, Any]]] = {
    "arrow": [{"symbol": "k"}, {"symbol": "\u03c3", "sub": "on"}, {"symbol": "\u03b3", "label_side": "right"}],
    "gene": [{"state": "on"}, {"state": "off"}, {"state": "on", "text": "GENE"}],
    "rna_strand": [{}, {"waves": 3, "amplitude": 1.0}, {"waves": 0.5, "label": None}],
    "protein": [{}, {"mirror": True}, {"size": 1.3, "label": None}],
    "decay": [{"species": "rna"}, {"species": "protein"}, {"species": "rna", "label": None}],
    "grid_lines": [{"lattice": "square"}, {"lattice": "triangle", "spacing": 6}, {"lattice": "hex", "spacing": 3}],
    "radial": [{"figure": "rings"}, {"figure": "spiral"}, {"figure": "polygons", "count": 5}],
    "iso_grid": [{}, {"spacing": 3}, {"verticals": False}],
    "iso_box": [
        {},
        {"sx": 14, "sy": 8, "sz": 4, "color": "species.rna.color"},
        {"sz": 14, "color": "species.protein.color"},
    ],
}
SEEDS = (1, 2, 3)


def _demo_params(gen: registry.Generator, box: Box) -> dict[str, Any] | None:
    if gen.name in DEMOS:
        return DEMOS[gen.name](box)
    names = set(gen.params)
    x, y, w, h = box
    if {"x", "y", "width", "height"} <= names:
        return {"x": x + 1, "y": y + 1, "width": w - 2, "height": h - 2}
    if {"cx", "cy"} <= names:
        return {"cx": x + w / 2, "cy": y + h / 2}
    return None


def cells(gen: registry.Generator) -> list[tuple[int | None, dict[str, Any], str]]:
    """(seed, extra params, caption) for the generator's three catalogue cells."""
    if gen.seeded:
        return [(s, {}, f"seed {s}") for s in SEEDS]
    variants = VARIANTS.get(gen.name, [{}])
    return [(None, v, _caption(json.dumps(v, ensure_ascii=False)) if v else "defaults") for v in variants[:3]]


def _caption(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def render(style: dict[str, Any], category: str | None = None) -> str:
    """The catalogue as a standalone SVG document (mm units)."""
    registry._load_all()
    gens = sorted(
        (g for g in registry.REGISTRY.values() if category in (None, g.category) and g.category != "import"),
        key=lambda g: (g.category, g.name),
    )
    width = GAP + 3 * (CELL_W + GAP)
    row_h = HEAD + CELL_H + 7
    parts: list[str] = []
    y = GAP
    for gen in gens:
        parts.append(
            f'<text x="{GAP}" y="{y + 4}" style="font-family:sans-serif;font-size:3.6px;font-weight:bold;fill:#333">'
            f"{escape(gen.name)}"
            f'<tspan dx="2.5" style="font-weight:normal;fill:#999">{escape(gen.category)}</tspan></text>'
            f'<text x="{GAP}" y="{y + 7.6}" style="font-family:sans-serif;font-size:2.6px;fill:#777">'
            f"{escape(gen.doc[:120])}</text>"
        )
        for col, (seed, extra, caption) in enumerate(cells(gen)):
            box = (GAP + col * (CELL_W + GAP), y + HEAD, CELL_W, CELL_H)
            base = _demo_params(gen, box)
            parts.append(
                f'<rect x="{box[0]}" y="{box[1]}" width="{CELL_W}" height="{CELL_H}" '
                'style="fill:none;stroke:#e2e2e2;stroke-width:0.2"/>'
            )
            if base is None:
                body = '<text style="font-size:3px;fill:#c00">no demo layout</text>'
            else:
                try:
                    body = registry.build(gen.name, {"id": f"cat-{gen.name}-{col}", **base, **extra}, style, seed)
                except ValueError as exc:
                    body = (
                        f'<text x="{box[0] + 2}" y="{box[1] + 5}" style="font-size:2.4px;fill:#c00">'
                        f"{escape(str(exc))}</text>"
                    )
            parts.append(body)
            parts.append(
                f'<text x="{box[0]}" y="{box[1] + CELL_H + 3.6}" '
                f'style="font-family:monospace;font-size:2.4px;fill:#888">{escape(caption)}</text>'
            )
        y += row_h
    height = y + GAP
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width:g}mm" height="{height:g}mm" viewBox="0 0 {width:g} {height:g}">'
        f'<rect width="{width:g}" height="{height:g}" fill="#ffffff"/>' + "".join(parts) + "</svg>\n"
    )


def main(argv: list[str]) -> int:
    from ..figure_kit import TRIANGLE_MARKER, load_style

    if not argv:
        print(__doc__)
        return 2
    svg = render(load_style(), argv[1] if len(argv) > 1 else None)
    svg = svg.replace("</svg>\n", f"<defs>{TRIANGLE_MARKER}</defs></svg>\n")
    with open(argv[0], "w", encoding="utf-8") as fh:
        fh.write(svg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
