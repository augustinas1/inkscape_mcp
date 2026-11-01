"""A local asset library: reusable illustrations taken from the user's own figures.

The library is a folder of SVGs (by default ``reference_figures/`` in the working directory or the
project; override with $INKSCAPE_MCP_ASSETS) plus an ``assets.json`` index naming the assets:

    {"assets": {
        "microscope": {"file": "microscope.svg"},                        # a whole figure
        "pipeline": {"file": "overview_figure.svg", "element": "g42",     # one part of a figure
                      "label": "data pipeline strip"}}}

The folder and its index stay local (never committed). An asset is inserted through the same
pipeline as import_svg — cleaned, ids prefixed, recoloured on request — and fitted by the
bounding box of what is actually drawn, not the page.

    python -m inkscape_mcp.generators.assets index [DIR]   # (re)build the index; keeps curated entries
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import inkex
from lxml import etree

from .importer import bring_in, load_svg
from .registry import generator

ENV = "INKSCAPE_MCP_ASSETS"
INDEX = "assets.json"
_SKIP = {"defs", "metadata", "namedview", "title", "desc", "style", "script"}


def library_dir() -> Path | None:
    """The asset folder: $INKSCAPE_MCP_ASSETS, else reference_figures/ in the cwd or the project."""
    env = os.environ.get(ENV)
    candidates = [Path(env).expanduser()] if env else []
    candidates += [Path.cwd() / "reference_figures", Path(__file__).resolve().parents[3] / "reference_figures"]
    return next((p for p in candidates if p.is_dir()), None)


def load_index(lib: Path) -> dict[str, dict[str, Any]]:
    """Named assets: the index file if present, otherwise one entry per SVG in the folder."""
    path = lib / INDEX
    if path.is_file():
        assets = json.loads(path.read_text(encoding="utf-8")).get("assets", {})
        return {k: v for k, v in assets.items() if isinstance(v, dict) and v.get("file")}
    return {p.stem: {"file": p.name} for p in sorted(lib.glob("*.svg"))}


def build_index(lib: Path) -> dict[str, dict[str, Any]]:
    """One entry per SVG, merged with the existing index so curated entries survive; boxes measured."""
    entries: dict[str, dict[str, Any]] = {p.stem: {"file": p.name} for p in sorted(lib.glob("*.svg"))}
    path = lib / INDEX
    if path.is_file():
        entries.update(json.loads(path.read_text(encoding="utf-8")).get("assets", {}))
    for entry in entries.values():
        box = visible_box(lib / entry["file"], entry.get("element"))
        if box:
            entry["box"] = [round(v, 3) for v in box]
    path.write_text(json.dumps({"assets": entries}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return entries


def _drawn_box(el: Any) -> tuple[float, float, float, float] | None:
    from ..figure_outline import _Outliner

    return _Outliner()._box_only(el)


def _inkscape() -> str | None:
    exe = os.environ.get("INKSCAPE_BIN") or shutil.which("inkscape")
    mac = "/Applications/Inkscape.app/Contents/MacOS/inkscape"
    return exe or (mac if Path(mac).is_file() else None)


def visible_box(src: Path, element_id: str | None) -> tuple[float, float, float, float] | None:
    """Inkscape's own visual bounding box (honours clipping), in user units: (x, y, w, h).

    Geometric boxes overshoot when content is clipped (plots, cropped drawings), which would
    shrink the asset; this asks Inkscape what it actually draws. None without the CLI.
    """
    exe = _inkscape()
    if exe is None:
        return None
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [exe, "--query-all", str(src)], capture_output=True, text=True, timeout=120, check=False
    )
    rows = {}
    for line in proc.stdout.splitlines():
        parts = line.split(",")
        if len(parts) == 5:
            try:
                rows[parts[0]] = tuple(float(v) for v in parts[1:])
            except ValueError:
                continue
    doc = inkex.load_svg(str(src)).getroot()
    if element_id:
        wanted = [element_id]
    else:
        wanted = [ch.get("id") for ch in doc if isinstance(ch, inkex.ShapeElement) and ch.get("id")]
    boxes = [rows[i] for i in wanted if i in rows and rows[i][2] > 0 and rows[i][3] > 0]
    if not boxes:
        return None
    scale = doc.uutounit(1, "px")  # CSS px per user unit
    vb = [float(v) for v in (doc.get("viewBox") or "0 0 0 0").replace(",", " ").split()[:2]] or [0.0, 0.0]
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (vb[0] + x0 / scale, vb[1] + y0 / scale, (x1 - x0) / scale, (y1 - y0) / scale)


def extract(lib: Path, entry: dict[str, Any]) -> tuple[Any, tuple[float, float, float, float]]:
    """The asset as a standalone <svg> root holding only its content (+ defs), and its drawn box."""
    src = lib / entry["file"]
    root = load_svg(src)
    doc = inkex.load_svg(str(src)).getroot()
    element_id = entry.get("element")
    if element_id:
        target = doc.getElementById(element_id)
        if target is None:
            raise ValueError(f"asset {entry['file']}: no element {element_id!r}")
        parts = [target]
    else:
        layers = [ch for ch in doc if isinstance(ch, inkex.ShapeElement)]
        parts = layers or []
    cached = entry.get("box")
    if isinstance(cached, list) and len(cached) == 4:
        x0, y0, x1, y1 = cached[0], cached[1], cached[0] + cached[2], cached[1] + cached[3]
    else:
        boxes = [b for b in (_drawn_box(p) for p in parts) if b]
        if not boxes:
            raise ValueError(f"asset {entry['file']}: nothing drawn")
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)

    if element_id:
        # Rebuild: defs + the element under its ancestors' combined transform (so it looks as drawn).
        src_el = root.find(f".//*[@id='{element_id}']")
        out = etree.Element(root.tag, nsmap=root.nsmap)
        for defs in root.iter("{http://www.w3.org/2000/svg}defs"):
            out.append(copy.deepcopy(defs))
        parent = target.getparent()
        wrapper = etree.SubElement(out, "{http://www.w3.org/2000/svg}g")
        transform = parent.composed_transform() if parent is not None else inkex.Transform()
        if transform != inkex.Transform():
            wrapper.set("transform", str(transform))
        wrapper.append(copy.deepcopy(src_el))
        root = out
    return root, (x0, y0, x1 - x0, y1 - y0)


@generator(
    "assets",
    choices={"recolor": ["none", "palette", "mono"]},
    ranges={"width": (0.1, 5000)},
    docs={
        "name": "asset name from the library index (see catalogue), or a file stem",
        "element": "optional element id inside the asset's file, to take just that part",
        "recolor": "none (as drawn) | palette | mono",
        "height": "optional: fit inside width x height, keeping proportions",
    },
)
def asset(
    gid: str,
    style: dict[str, Any],
    name: str,
    x: float,
    y: float,
    width: float,
    height: float = 0.0,
    element: str = "",
    recolor: str = "none",
    color: str = "ink",
) -> str:
    """A reusable illustration from the local asset library, fitted to (x, y, width[, height])."""
    lib = library_dir()
    if lib is None:
        raise ValueError(f"no asset library: create reference_figures/ or set ${ENV}")
    index = load_index(lib)
    entry = dict(index.get(name) or ({"file": f"{name}.svg"} if (lib / f"{name}.svg").is_file() else {}))
    if not entry:
        raise ValueError(f"unknown asset {name!r}; available: {sorted(index)}")
    if element:
        entry["element"] = element
    root, view = extract(lib, entry)
    if height > 0 and view[3] * width / view[2] > height:  # too tall for the box: fit the height instead
        width = view[2] * height / view[3]
    label = entry.get("label") or name
    return bring_in(root, view, gid, style, x, y, width, recolor, color, f"asset: {label}")


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "index":
        print(__doc__)
        return 2
    lib = Path(argv[1]) if len(argv) > 1 else library_dir()
    if lib is None or not lib.is_dir():
        print("no asset library folder found")
        return 1
    entries = build_index(lib)
    print(f"{len(entries)} assets indexed in {lib / INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
