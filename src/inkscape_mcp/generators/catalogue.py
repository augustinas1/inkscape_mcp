"""Visual catalogue of every registered generator: a contact sheet built from the registry.

One row per generator, three cells per row — three seeds for seeded generators, three
parameter variants for the others — each captioned with the exact call that reproduces it,
so "flow_lines, seed 2" can be pointed at and asked for. New generators appear automatically
(with a generic demo layout unless they need a custom one in DEMOS).

    python -m inkscape_mcp.generators.catalogue out.html [category] [--style reference|nature|FILE] [--assets]
    (--assets adds the local asset library: keep such pages out of git, e.g. inside the library folder)
    python -m inkscape_mcp.generators.catalogue out.svg [category]    # contact sheet
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
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
    "gene_box": lambda b: {"x": b[0] + b[2] / 2 - 10, "y": b[1] + b[3] / 2 + 3},
    "panel": lambda b: {"x": b[0] + 4, "y": b[1] + 4, "width": b[2] - 8, "height": b[3] - 8},
    "label_pill": lambda b: {
        "x": b[0] + b[2] * 0.62,
        "y": b[1] + b[3] * 0.68,
        "target_x": b[0] + b[2] * 0.25,
        "target_y": b[1] + b[3] * 0.3,
        "text": "Burst",
    },
    "zoom_callout": lambda b: {
        "x1": b[0] + 12,
        "y1": b[1] + b[3] * 0.65,
        "r1": 4,
        "x2": b[0] + b[2] - 15,
        "y2": b[1] + b[3] / 2,
        "r2": 11,
    },
    "hex_tissue": lambda b: {"x": b[0] + 3, "y": b[1] + 3, "width": b[2] - 6, "height": b[3] - 6, "highlight": 0.3},
    "histogram_fit": lambda b: {"x": b[0] + 8, "y": b[1] + 4, "width": b[2] - 12, "height": b[3] - 12},
    "ridge_plot": lambda b: {"x": b[0] + 6, "y": b[1] + 4, "width": b[2] - 12, "height": b[3] - 12},
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
    "gene_box": [{"state": "on"}, {"state": "off", "sub": "off"}, {"state": "on", "promoter": False}],
    "panel": [{"title": "Telegraph model"}, {"title": ""}, {"title": "Poisson", "fill": "#f4f5ff"}],
    "label_pill": [{}, {"text": "Cell-cell interactions", "size": 3}, {"text": "Burst size", "side": "left"}],
    "zoom_callout": [{}, {"r2": 8}, {"lens_fill": "#f7f7f7"}],
    "ridge_plot": [{}, {"count": 8, "overlap": 1.6}, {"color": "plot.fit", "axes": False}],
    "hex_cells": [{"pick": "alternate", "spacing": 3}, {"pick": "spaced", "spacing": 3}, {"pick": "all", "spacing": 3}],
    "iso_box": [
        {},
        {"sx": 14, "sy": 8, "sz": 4, "color": "species.rna.color"},
        {"sz": 14, "color": "species.protein.color"},
    ],
}
SEEDS = (1, 2, 3)
# Categories that need inputs a catalogue cell cannot invent (a file, a library entry); the asset
# library gets its own section when requested.
_NOT_SHOWN = {"import", "assets"}


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
        (g for g in registry.REGISTRY.values() if category in (None, g.category) and g.category not in _NOT_SHOWN),
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


def _cell_svg(gen: registry.Generator, col: int, seed: int | None, extra: dict[str, Any], style: dict[str, Any],
              marker: str) -> str:  # fmt: skip
    """One catalogue cell as a small standalone inline <svg> (cell box at the origin)."""
    base = _demo_params(gen, (0.0, 0.0, CELL_W, CELL_H))
    try:
        body = registry.build(gen.name, {"id": f"cat-{gen.name}-{col}", **(base or {}), **extra}, style, seed)
    except ValueError as exc:
        body = f'<text x="2" y="6" style="font-size:2.4px;fill:#c00">{escape(str(exc))}</text>'
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {CELL_W:g} {CELL_H:g}" role="img" '
        f'aria-label="{_attr(gen.name)} example {col + 1}"><defs>{marker}</defs>{body}</svg>'
    )


def _attr(text: str) -> str:
    """Escape for a double-quoted HTML attribute (saxutils.escape leaves quotes alone)."""
    return escape(text, {'"': "&quot;"})


def _request(gen: registry.Generator, seed: int | None, extra: dict[str, Any]) -> str:
    """What to type in chat to get this cell."""
    if seed is not None:
        return f"{gen.name} seed {seed}"
    if not extra:
        return f"{gen.name} (defaults)"
    return f"{gen.name} with " + ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in extra.items())


def _param_rows(gen: registry.Generator) -> str:
    rows = []
    for p in gen.params.values():
        if p.kind == "choice" and p.choices:
            kind = " | ".join(p.choices)
        else:
            kind = p.kind
        if p.minimum is not None or p.maximum is not None:
            kind += f" [{p.minimum:g}..{p.maximum:g}]" if p.minimum is not None and p.maximum is not None else ""
        default = "required" if p.required else json.dumps(p.default, ensure_ascii=False)
        rows.append(
            f"<tr><td><code>{escape(p.name)}</code></td><td>{escape(kind)}</td>"
            f"<td><code>{escape(default)}</code></td><td>{escape(p.doc)}</td></tr>"
        )
    return "".join(rows)


_PAGE_CSS = """
:root{--bg:#f6f6f4;--card:#ffffff;--ink:#222;--muted:#6b6b6b;--line:#e2e2de;--accent:#2b8a69}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
header{padding:28px 16px 8px;max-width:1080px;margin:0 auto}
h1{font-size:24px;margin:0 0 6px}
header p{margin:4px 0;color:var(--muted);max-width:760px}
nav{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line)}
nav .in{max-width:1080px;margin:0 auto;padding:10px 16px;display:flex;gap:8px;flex-wrap:wrap;
  align-items:center}
nav a{color:var(--ink);text-decoration:none;padding:3px 10px;border:1px solid var(--line);border-radius:14px;
  background:var(--card)}
nav input{margin-left:auto;padding:5px 10px;border:1px solid var(--line);border-radius:14px;min-width:200px;
  font:inherit}
main{max-width:1080px;margin:0 auto;padding:8px 16px 48px}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:30px 0 8px}
.gen{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
.gen h3{margin:0;font-size:17px}.gen h3 code{font-size:17px}
.gen .doc{margin:4px 0 12px;color:var(--muted)}
.cells{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
figure{margin:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}
figure svg{display:block;width:100%;height:auto}
figcaption{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 8px;
  border-top:1px solid var(--line);font:12px ui-monospace,Menlo,monospace;color:var(--muted)}
button{font:12px -apple-system,Arial,sans-serif;border:1px solid var(--line);background:#fafafa;
  border-radius:6px;padding:2px 8px;cursor:pointer;white-space:nowrap}
button.done{background:var(--accent);color:#fff;border-color:var(--accent)}
details{margin-top:12px}summary{cursor:pointer;color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%;margin-top:8px;font-size:13px}
td{border-top:1px solid var(--line);padding:5px 8px;vertical-align:top}td:first-child{white-space:nowrap}
.hidden{display:none}
@media (max-width:720px){.cells{grid-template-columns:1fr}nav input{margin-left:0;width:100%}}
"""

_PAGE_JS = """
document.querySelectorAll('button[data-copy]').forEach(b=>b.addEventListener('click',()=>{
  navigator.clipboard.writeText(b.dataset.copy).then(()=>{b.textContent='copied';b.classList.add('done');
  setTimeout(()=>{b.textContent='copy';b.classList.remove('done')},1200)})}));
const q=document.getElementById('filter');
q.addEventListener('input',()=>{const t=q.value.toLowerCase();
  document.querySelectorAll('.gen').forEach(g=>g.classList.toggle('hidden',!g.dataset.search.includes(t)))});
"""


def _asset_section(style: dict[str, Any], marker: str) -> str:
    """HTML for the local asset library (empty when there is none). Never part of committed docs."""
    from . import assets

    lib = assets.library_dir()
    if lib is None:
        return ""
    figs = []
    for name, entry in sorted(assets.load_index(lib).items()):
        try:
            body = registry.build(
                "asset",
                {"id": f"cat-asset-{name}", "name": name, "x": 2, "y": 2, "width": CELL_W - 4, "height": CELL_H - 4},
                style,
            )
        except ValueError as exc:
            body = f'<text x="2" y="6" style="font-size:2.4px;fill:#c00">{escape(str(exc))}</text>'
        request = f"asset {name}"
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {CELL_W:g} {CELL_H:g}" role="img" '
            f'aria-label="asset {_attr(name)}"><defs>{marker}</defs>{body}</svg>'
        )
        figs.append(
            f'<figure>{svg}<figcaption><span title="{_attr(entry.get("label") or entry["file"])}">'
            f'{escape(name)}</span><button data-copy="{_attr(request)}" '
            'title="copy a request to paste into chat">copy</button></figcaption></figure>'
        )
    search = "assets " + " ".join(sorted(assets.load_index(lib)))
    return (
        '<h2 id="cat-assets">assets</h2>'
        f'<section class="gen" id="assets" data-search="{_attr(search)}"><h3><code>asset</code></h3>'
        f'<p class="doc">Illustrations from your own figures ({escape(str(lib))}; local, not in git). '
        'Insert with e.g. <i>"asset NAME at the top left, 40 mm wide"</i>.</p>'
        f'<div class="cells">{"".join(figs)}</div></section>'
    )


def render_html(style: dict[str, Any], category: str | None = None, include_assets: bool = False) -> str:
    """The catalogue as one self-contained HTML page (inline SVG, no external resources)."""
    from ..figure_kit import TRIANGLE_MARKER

    registry._load_all()
    gens = sorted(
        (g for g in registry.REGISTRY.values() if category in (None, g.category) and g.category not in _NOT_SHOWN),
        key=lambda g: (g.category, g.name),
    )
    categories = sorted({g.category for g in gens})
    sections = []
    for cat in categories:
        blocks = []
        for gen in (g for g in gens if g.category == cat):
            figs = []
            for col, (seed, extra, caption) in enumerate(cells(gen)):
                request = _request(gen, seed, extra)
                figs.append(
                    f"<figure>{_cell_svg(gen, col, seed, extra, style, TRIANGLE_MARKER)}"
                    f'<figcaption><span title="{_attr(request)}">{escape(caption)}</span>'
                    f'<button data-copy="{_attr(request)}" title="copy a request to paste into chat">copy</button>'
                    "</figcaption></figure>"
                )
            search = f"{gen.name} {gen.category} {gen.doc}".lower()
            blocks.append(
                f'<section class="gen" id="{_attr(gen.name)}" data-search="{_attr(search)}">'
                f'<h3><code>{escape(gen.name)}</code></h3><p class="doc">{escape(gen.doc)}</p>'
                f'<div class="cells">{"".join(figs)}</div>'
                f"<details><summary>parameters</summary><table>{_param_rows(gen)}</table></details></section>"
            )
        sections.append(f'<h2 id="cat-{escape(cat)}">{escape(cat)}</h2>' + "".join(blocks))
    asset_html = _asset_section(style, TRIANGLE_MARKER) if include_assets and category in (None, "assets") else ""
    if asset_html:
        sections.append(asset_html)
        categories = [*categories, "assets"]
    nav = "".join(f'<a href="#cat-{escape(c)}">{escape(c)}</a>' for c in categories)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Generator catalogue</title><style>{_PAGE_CSS}</style></head><body>
<header><h1>Generator catalogue</h1>
<p>Every generator the Inkscape MCP can draw, three seeds or variants each. To use one, press
<b>copy</b> under an example and paste it into Claude with where it should go — e.g.
<i>"chain seed 2 as a second protein at the bottom right"</i>. Add words like denser, paler or
thinner to adjust it, or "clip it into the cell" / "use it as a pattern fill on the box".</p>
<p>Colours and line weights come from your style tokens (<code>figures/style.json</code>);
regenerate this page after changing them:
<code>python -m inkscape_mcp.generators.catalogue docs/catalogue.html</code></p>
</header>
<nav><div class="in">{nav}
<input id="filter" type="search" placeholder="filter generators" aria-label="filter generators"></div></nav>
<main>{"".join(sections)}</main><script>{_PAGE_JS}</script></body></html>
"""


def main(argv: list[str]) -> int:
    from ..figure_kit import TRIANGLE_MARKER, load_style

    if not argv:
        print(__doc__)
        return 2
    style_arg = None
    if "--style" in argv:
        i = argv.index("--style")
        style_arg = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    include_assets = "--assets" in argv
    argv = [a for a in argv if a != "--assets"]
    out, category = argv[0], (argv[1] if len(argv) > 1 else None)
    style_arg = style_arg or next((p for p in ("figures/style.json",) if Path(p).is_file()), None)
    style = load_style(style_arg)
    if out.lower().endswith((".html", ".htm")):
        text = render_html(style, category, include_assets)
    else:
        text = render(style, category).replace("</svg>\n", f"<defs>{TRIANGLE_MARKER}</defs></svg>\n")
    Path(out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
