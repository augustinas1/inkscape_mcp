"""Figure-level live operations: selection, save, labels, organise, parked objects, renders.

These build on tools/live.py (export snapshots, edit_xml batches, the Inkscape CLI) and keep
the agent's round trips down: one call answers "what is selected?" with ids, outlines and a
picture; edits can come back with a before/after render; a figure can be saved, labelled and
organised into layers without a window or a pile of hand-written XPath.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from lxml import etree

from . import live

SVG = "http://www.w3.org/2000/svg"
INK = "http://www.inkscape.org/namespaces/inkscape"
SODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
LABEL = f"{{{INK}}}label"
GROUPMODE = f"{{{INK}}}groupmode"
NOT_DRAWN = frozenset({"defs", "metadata", "namedview", "title", "desc", "style", "script"})
BACKUP_DIR = Path(os.path.expanduser("~/.cache/inkscape_mcp/backups"))
Box = tuple[float, float, float, float]  # left, top, right, bottom (user units)


def _local(el: Any) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _parse(data: bytes) -> Any:
    return etree.fromstring(data, parser=etree.XMLParser(huge_tree=True))


async def snapshot() -> tuple[Path, bytes]:
    path = live._scratch_path()
    await live._export_document_async(live._get_bus(), path)
    return path, path.read_bytes()


# ---- measuring and rendering ---------------------------------------------------------------


def _scale(svg_bytes: bytes) -> tuple[float, float, float]:
    """(CSS px per user unit, viewBox min-x, min-y)."""
    import inkex

    svg = inkex.load_svg(svg_bytes).getroot()
    vb = [float(v) for v in re.split(r"[\s,]+", (svg.get("viewBox") or "0 0").strip())[:2]]
    return svg.uutounit(1, "px"), vb[0], vb[1]


async def query_all(exe: str, svg_path: Path, svg_bytes: bytes) -> dict[str, Box]:
    """Inkscape's visual box of every element, in user units ({} if the CLI fails)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "--query-all", str(svg_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except (OSError, TimeoutError):
        return {}
    scale, x0, y0 = _scale(svg_bytes)
    boxes: dict[str, Box] = {}
    for line in out.decode(errors="replace").splitlines():
        parts = line.split(",")
        if len(parts) != 5:
            continue
        try:
            x, y, w, h = (float(v) for v in parts[1:])
        except ValueError:
            continue
        left, top = x0 + x / scale, y0 + y / scale
        boxes[parts[0]] = (left, top, left + w / scale, top + h / scale)
    return boxes


def union(boxes: list[Box]) -> Box | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


async def render_area(exe: str, svg_path: Path, svg_bytes: bytes, box: Box, png: Path, height_px: int = 500) -> str:
    """Render `box` (user units) of `svg_path` to `png`; '' on success, else the failure."""
    from ..figure_outline import user_area_to_px

    h = max(box[3] - box[1], 1e-6)
    scale, _, _ = _scale(svg_bytes)
    dpi = max(30.0, min(1200.0, height_px / (h * scale) * 96))
    cmd = [
        exe,
        "--app-id-tag=mcp",
        f"--export-filename={png}",
        "--export-type=png",
        f"--export-area={user_area_to_px(svg_bytes, box)}",
        f"--export-dpi={dpi:.1f}",
        "--export-background=white",
        "--export-background-opacity=1",
        str(svg_path),
    ]
    return str(await live._run_png_export(cmd, png))


def stitch(pngs: list[Path], out: Path, gap: int = 12) -> Path:
    """Side by side, top-aligned, on white."""
    from PIL import Image

    images = [Image.open(p).convert("RGB") for p in pngs]
    width = sum(i.width for i in images) + gap * (len(images) - 1)
    sheet = Image.new("RGB", (width, max(i.height for i in images)), "white")
    x = 0
    for image in images:
        sheet.paste(image, (x, 0))
        x += image.width + gap
    sheet.save(out)
    return out


def _pad(box: Box, margin: float) -> Box:
    return (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)


def _exe(config: Any) -> str:
    return str(getattr(config, "inkscape_executable", None) or live._resolve_inkscape_exe(config))


async def render_ids(
    exe: str, svg_path: Path, svg_bytes: bytes, ids: list[str], stem: str, boxes: dict[str, Box] | None = None
) -> tuple[Path | None, dict[str, Box]]:
    """One picture of `ids`: their common area if they sit together, else one panel each, side by side."""
    boxes = boxes if boxes is not None else await query_all(exe, svg_path, svg_bytes)
    found = {i: boxes[i] for i in ids if i in boxes}
    if not found:
        return None, {}
    scratch = svg_path.parent
    whole = union(list(found.values()))
    if whole is None:
        return None, {}
    parts_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in found.values())
    whole_area = (whole[2] - whole[0]) * (whole[3] - whole[1])
    if len(found) == 1 or parts_area >= 0.25 * whole_area:  # close together: one frame
        png = scratch / f"{stem}.png"
        margin = 0.04 * max(whole[2] - whole[0], whole[3] - whole[1])
        failure = await render_area(exe, svg_path, svg_bytes, _pad(whole, margin), png)
        return (None if failure else png), found
    pngs = []
    for k, b in enumerate(found.values()):
        png = scratch / f"{stem}-{k}.png"
        margin = 0.04 * max(b[2] - b[0], b[3] - b[1])
        if not await render_area(exe, svg_path, svg_bytes, _pad(b, margin), png, height_px=400):
            pngs.append(png)
    return (stitch(pngs, scratch / f"{stem}.png") if pngs else None), found


# ---- selection -----------------------------------------------------------------------------


async def selected_ids(bus: Any) -> tuple[list[str], str]:
    """(ids, how) — via select-list when Inkscape's output is captured (no undo entry), else the
    inspect extension (which adds an "[unchanged]" undo step)."""
    from .. import selection_probe

    ids = await asyncio.to_thread(selection_probe.selected_ids, bus)
    if ids is not None:
        return ids, "select-list"
    res = await live._do_inspect(bus, "selection", "selection", time.perf_counter())
    return list(res.get("data", {}).get("ids", [])), "inspect"


async def op_selection(bus: Any, operation: str, payload: str, config: Any, start: float) -> dict[str, Any]:
    """What is selected: ids, an outline of each, and one picture of them.

    payload: {"render": false} skips the picture; {"lines": N} sets outline lines per object.
    """
    from ..figure_outline import outline

    args = json.loads(payload) if payload else {}
    ids, how = await selected_ids(bus)
    if not ids:
        return live._result(operation, True, "nothing selected", data={"ids": [], "via": how}, start=start)
    path, data = await snapshot()
    lines = int(args.get("lines", 12))
    outlines = []
    for i in ids:
        try:
            outlines.append(outline(data, i, lines).split("\n", 1)[1])
        except (KeyError, IndexError):
            outlines.append(f"{i}  (not found in the snapshot)")
    result: dict[str, Any] = {"ids": ids, "via": how, "outline": "\n".join(outlines)}
    if args.get("render", True):
        png, boxes = await render_ids(_exe(config), path, data, ids, f"selection-{uuid.uuid4().hex[:6]}")
        result["png"] = str(png) if png else None
        result["boxes"] = {k: [round(v, 1) for v in b] for k, b in boxes.items()}
    message = f"{len(ids)} selected ({', '.join(ids[:6])}{', …' if len(ids) > 6 else ''})\n{result['outline']}"
    if result.get("png"):
        message += f"\nrender: {result['png']}"
    return live._result(operation, True, message, data=result, start=start)


# ---- save ----------------------------------------------------------------------------------


def remember_path(path: str) -> None:
    """Note which file a document name came from (Inkscape does not report it over D-Bus)."""
    from ..selection_probe import DOC_PATHS, known_paths

    known = known_paths()
    known[Path(path).name] = str(Path(path).resolve())
    DOC_PATHS.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATHS.write_text(json.dumps(known, indent=1))


def known_path(docname: str) -> str | None:
    from ..selection_probe import known_paths

    return known_paths().get(docname)


async def op_save(bus: Any, operation: str, payload: str, start: float) -> dict[str, Any]:
    """Write the active document to its file, without a window (works with several open).

    payload: {"path": "/abs/file.svg"} when the document's file is not known (it is for files
    opened through open_file). The previous file is copied to the backups folder first.
    Inkscape still shows the document as modified; Cmd+S afterwards is harmless.
    """
    args = json.loads(payload) if payload else {}
    _, data = await snapshot()
    root = _parse(data)
    docname = root.get(f"{{{SODI}}}docname", "")
    target = args.get("path") or known_path(docname)
    if not target:
        return live._result(
            operation,
            False,
            f'don\'t know which file {docname!r} came from — pass payload {{"path": "/abs/{docname}"}}',
            error="unknown path",
            start=start,
        )
    target_path = Path(str(target))
    if target_path.name != docname and not args.get("path"):
        return live._result(operation, False, f"{target} is not {docname!r}", error="path mismatch", start=start)
    backup = None
    if target_path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{target_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}{target_path.suffix}"
        shutil.copy2(target_path, backup)
    tmp = target_path.with_name(f".{target_path.name}.saving")
    tmp.write_bytes(data)
    tmp.replace(target_path)
    remember_path(str(target_path))
    return live._result(
        operation,
        True,
        f"saved {docname!r} to {target_path} ({len(data) // 1024} KB)"
        + (f"; previous copy in {backup}" if backup else ""),
        data={"path": str(target_path), "backup": str(backup) if backup else None, "bytes": len(data)},
        start=start,
    )


# ---- labels --------------------------------------------------------------------------------


def _text_of(el: Any) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _is_glyph_run(el: Any) -> bool:
    kids = [c for c in el if isinstance(c.tag, str)]
    return _local(el) == "g" and bool(kids) and all(_local(c) == "use" for c in kids)


def _endpoints(el: Any) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """First and last point of a path, in document coordinates."""
    import inkex

    try:
        path = inkex.Path(el.get("d", ""))
        points = [p for p in path.end_points]
        if not points:
            return None
        t = _composed(el)
        a, b = t.apply_to_point(points[0]), t.apply_to_point(points[-1])
        return (a.x, a.y), (b.x, b.y)
    except Exception:
        return None


def _composed(el: Any) -> Any:
    import inkex

    t = inkex.Transform()
    for node in [el, *el.iterancestors()]:
        if isinstance(node.tag, str) and node.get("transform"):
            t = inkex.Transform(node.get("transform")) @ t
    return t


def _nearest(
    point: tuple[float, float], texts: list[tuple[str, Box]], reach: float, skip: str | None = None
) -> str | None:
    best, best_d = None, reach
    for text, b in texts:
        if text == skip:
            continue
        dx = max(b[0] - point[0], 0.0, point[0] - b[2])
        dy = max(b[1] - point[1], 0.0, point[1] - b[3])
        d = math.hypot(dx, dy)
        if d < best_d:
            best, best_d = text, d
    return best


def _arrow_name(ends: Any, texts: list[tuple[str, Box]], reach: float) -> str:
    """'arrow: Translation → Protein', with a text beside the middle (a rate) in brackets."""
    if not ends:
        return "arrow"
    (x0, y0), (x1, y1) = ends
    rate = _nearest(((x0 + x1) / 2, (y0 + y1) / 2), texts, reach * 0.5)
    src = _nearest(ends[0], texts, reach, skip=rate)
    dst = _nearest(ends[1], texts, reach, skip=rate)
    if src == dst:
        src = None  # one text near both ends names where it points, not where it starts
    name = "arrow"
    if src or dst:
        name += ": " + (f"{src} " if src else "") + (f"→ {dst}" if dst else "→")
    return name + (f" ({rate})" if rate else "")


def propose_labels(
    root: Any, boxes: dict[str, Box], reach: float = 25.0, relabel: frozenset[str] = frozenset()
) -> dict[str, str]:
    """{id: label} for unlabelled elements (and those labelled with one of `relabel`'s generic
    names): texts by content, outlined text, arrows by what they connect ("arrow: Translation →
    Protein"), groups by their main text."""
    texts = [
        (_text_of(t)[:30], boxes[t.get("id")])
        for t in root.iter(f"{{{SVG}}}text")
        if t.get("id") in boxes and _text_of(t)
    ]
    out: dict[str, str] = {}
    for el in root.iter():
        if not isinstance(el.tag, str) or not el.get("id") or _local(el) in NOT_DRAWN | {"tspan"}:
            continue
        if el.get(LABEL) and el.get(LABEL) not in relabel:
            continue
        if any(_local(a) in ("defs", "text") for a in el.iterancestors()):
            continue
        kind = _local(el)
        name = None
        if kind == "text":
            name = _text_of(el)[:40] or None
        elif _is_glyph_run(el):
            name = "outlined text"
        elif kind in ("path", "line", "polyline") and "marker-end" in (el.get("style") or "") + (
            el.get("marker-end") or ""
        ):
            name = _arrow_name(_endpoints(el), texts, reach)
        elif kind == "g":
            inner = [_text_of(t) for t in el.iter(f"{{{SVG}}}text") if _text_of(t)]
            if inner and len(inner) <= 4:
                name = max(inner, key=len)[:40]
        if name and name != el.get(LABEL):
            out[el.get("id")] = name
    return out


async def op_label(bus: Any, operation: str, payload: str, window_id: int, config: Any, start: float) -> dict[str, Any]:
    """Give unlabelled elements plain-word labels (one undo step).

    payload: {"check": true} lists them; {"relabel": ["arrow", "outlined text"]} also renames
    elements whose label is one of those generic names; {"reach": 25} is how far (user units) an
    arrow's end may be from a text to be named after it.
    """
    args = json.loads(payload) if payload else {}
    path, data = await snapshot()
    boxes = await query_all(_exe(config), path, data)
    labels = propose_labels(_parse(data), boxes, float(args.get("reach", 25)), frozenset(args.get("relabel", [])))
    shown = "\n".join(f"  {k}: {v}" for k, v in list(labels.items())[:40])
    if args.get("check") or not labels:
        verb = "would label" if labels else "nothing to label"
        return live._result(
            operation, True, f"{verb} {len(labels)} element(s)\n{shown}", data={"labels": labels}, start=start
        )
    ops = [
        {"xpath": f"//*[@id='{k}']", "action": "set_attr", "name": "inkscape:label", "value": v}
        for k, v in labels.items()
    ]
    res = await live._op_edit_xml(bus, operation, "", json.dumps({"ops": ops}), window_id, start)
    if res["success"]:
        res["message"] = f"labelled {len(labels)} element(s), one undo step\n{shown}"
    return res


# ---- organise: flatten wrappers, layers by area -----------------------------------------------

_NON_INHERITED = {"opacity", "clip-path", "mask", "filter", "display", "isolation", "mix-blend-mode"}
_INHERITED = {
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "font-family",
    "font-size", "font-style", "font-weight", "font-variant", "font-stretch", "-inkscape-font-specification",
    "text-anchor", "text-align", "writing-mode", "direction", "letter-spacing", "word-spacing", "line-height",
    "visibility", "color", "paint-order", "marker", "marker-start", "marker-mid", "marker-end",
    "font-variant-ligatures", "font-variant-caps", "font-variant-numeric", "font-variant-east-asian",
    "font-feature-settings", "text-transform", "baseline-shift", "dominant-baseline", "white-space",
}  # fmt: skip


def _style(el: Any) -> dict[str, str]:
    out = {}
    for decl in (el.get("style") or "").split(";"):
        k, _, v = decl.partition(":")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def flatten(root: Any) -> int:
    """Fold single-child wrapper groups (no clip, mask, filter, opacity, label, layer, reference)
    into their child, merging inherited style and composing transforms at full precision."""
    import inkex

    from .. import svg_cleanup
    from ..transplant import transform_str

    referenced: set[str] = set()
    for el in root.iter():
        if isinstance(el.tag, str):
            referenced |= svg_cleanup.references(el)
    removed, changed = 0, True
    while changed:
        changed = False
        for g in list(root.iter(f"{{{SVG}}}g")):
            parent = g.getparent()
            if parent is None or any(_local(a) == "defs" for a in g.iterancestors()):
                continue
            kids = [c for c in g if isinstance(c.tag, str)]
            if len(kids) != 1 or _local(kids[0]) in NOT_DRAWN:
                continue
            if g.get("id") in referenced or g.get(GROUPMODE) or g.get(LABEL):
                continue
            attrs = {etree.QName(k).localname: v for k, v in g.attrib.items()}
            plain = {k: v for k, v in attrs.items() if k not in ("id", "style", "transform")}
            gstyle = _style(g)
            if set(plain) - _INHERITED or set(gstyle) - _INHERITED:
                continue
            child = kids[0]
            cstyle = _style(child)
            for k, v in {**plain, **gstyle}.items():
                if k not in cstyle and child.get(k) is None:
                    cstyle[k] = v
            if cstyle:
                child.set("style", ";".join(f"{k}:{v}" for k, v in cstyle.items()))
            t = inkex.Transform(g.get("transform", "")) @ inkex.Transform(child.get("transform", ""))
            if t == inkex.Transform():
                child.attrib.pop("transform", None)
            else:
                child.set("transform", transform_str(t))
            child.tail = g.tail
            parent.replace(g, child)
            removed += 1
            changed = True
    return removed


def _layer_for(box: Box | None, layers: list[dict[str, Any]]) -> int | None:
    if box is None:
        return None
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for k, layer in enumerate(layers):
        x0, y0, x1, y1 = layer["area"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return k
    return None


async def _identical(exe: str, a: tuple[Path, bytes], b: tuple[Path, bytes]) -> int | None:
    """Pixels that differ between two renders of the page (None if rendering failed)."""
    from PIL import Image, ImageChops

    pngs = []
    for svg_path, data in (a, b):
        svg_path.write_bytes(data)
        png = svg_path.with_suffix(".png")
        cmd = [exe, str(svg_path), "--export-type=png", "--export-width=1600", "--export-area-page",
               "--export-background=white", "--export-background-opacity=1", f"--export-filename={png}"]  # fmt: skip
        if await live._run_png_export(cmd, png):
            return None
        pngs.append(png)
    diff = ImageChops.difference(Image.open(pngs[0]).convert("RGB"), Image.open(pngs[1]).convert("RGB")).convert("L")
    return int(sum(diff.histogram()[1:]))


async def op_organize(
    bus: Any, operation: str, payload: str, window_id: int, config: Any, start: float
) -> dict[str, Any]:
    """Restructure without changing a pixel: flatten wrapper chains, sort top-level objects into
    layers by area, label what is unlabelled. Verified by rendering before applying.

    payload: {"layers": [{"label": "Panel A", "area": [x0, y0, x1, y1]}, ...]  (user units; objects go
              to the layer containing their centre, in document order; others stay where they are)
              "flatten": true, "labels": true, "check": false, "force": false}
    """
    args = json.loads(payload) if payload else {}
    exe = _exe(config)
    path, data = await snapshot()
    root = _parse(data)
    old_tops = [c for c in root if isinstance(c.tag, str)]
    new = copy.deepcopy(root)
    report: dict[str, Any] = {}

    if args.get("flatten", True):
        report["flattened"] = flatten(new)
    layers = list(args.get("layers", []))
    moved: dict[int, list[Any]] = {}
    if layers:
        measure = path.with_name(f"{path.stem}-measure.svg")
        measure.write_bytes(etree.tostring(new))
        boxes = await query_all(exe, measure, etree.tostring(new))
        for el in [c for c in new if isinstance(c.tag, str) and _local(c) not in NOT_DRAWN and not c.get(GROUPMODE)]:
            k = _layer_for(boxes.get(el.get("id", "")), layers)
            if k is not None:
                moved.setdefault(k, []).append(el)
        made = []
        for k, layer in enumerate(layers):
            node = etree.SubElement(
                new,
                f"{{{SVG}}}g",
                {"id": f"layer-{uuid.uuid4().hex[:6]}", GROUPMODE: "layer", LABEL: str(layer["label"])},
            )
            for el in moved.get(k, []):
                node.append(el)
            made.append(node)
        report["layers"] = {str(layer["label"]): len(moved.get(k, [])) for k, layer in enumerate(layers)}
    if args.get("labels", True):
        measure = path.with_name(f"{path.stem}-measure.svg")
        measure.write_bytes(etree.tostring(new))
        names = propose_labels(new, await query_all(exe, measure, etree.tostring(new)))
        for el in new.iter():
            if isinstance(el.tag, str) and el.get("id") in names:
                el.set(LABEL, names[el.get("id")])
        report["labelled"] = len(names)

    differing = await _identical(
        exe, (path.with_name(f"{path.stem}-a.svg"), data), (path.with_name(f"{path.stem}-b.svg"), etree.tostring(new))
    )
    report["differing_pixels"] = differing
    summary = ", ".join(f"{k} {v}" for k, v in report.items())
    if differing is None or (differing and not args.get("force")):
        why = "could not render to verify" if differing is None else f"{differing} pixel(s) would change"
        return live._result(operation, False, f"organize not applied: {why} ({summary}); pass force to apply anyway",
                            data=report, error="not identical", start=start)  # fmt: skip
    if args.get("check"):
        return live._result(operation, True, f"organize check (nothing changed): {summary}", data=report, start=start)

    # Replace the drawing: old top-level drawn children out, new ones (incl. layers) in.
    ops: list[dict[str, Any]] = []
    for el in old_tops:
        if _local(el) in NOT_DRAWN:
            continue
        if not el.get("id"):
            return live._result(operation, False, f"a top-level <{_local(el)}> has no id", error="no id", start=start)
        ops.append({"xpath": f"/svg:svg/*[@id='{el.get('id')}']", "action": "remove"})
    for el in [c for c in new if isinstance(c.tag, str) and _local(c) not in NOT_DRAWN]:
        ops.append({"xpath": "/svg:svg", "action": "append", "xml": etree.tostring(el, encoding="unicode")})
    ops_file = path.with_name(f"{path.stem}-organize.json")
    ops_file.write_text(json.dumps({"ops": ops}))
    res = await live._op_edit_xml(bus, operation, "", json.dumps({"ops_path": str(ops_file)}), window_id, start)
    if res["success"]:
        res["message"] = f"organized, one undo step, render identical: {summary}"
        res["data"] = {**res.get("data", {}), **report}
    return res


# ---- parked (off-page) objects ---------------------------------------------------------------


async def op_parked(
    bus: Any, operation: str, payload: str, window_id: int, config: Any, start: float
) -> dict[str, Any]:
    """Objects entirely off the page (parked old versions, drafts). payload {"delete": true} removes
    them (one undo step); {"delete": ["id", ...]} removes just those."""
    import inkex

    args = json.loads(payload) if payload else {}
    path, data = await snapshot()
    root = _parse(data)
    boxes = await query_all(_exe(config), path, data)
    svg = inkex.load_svg(data).getroot()
    vb = [float(v) for v in re.split(r"[\s,]+", (svg.get("viewBox") or "").strip())] if svg.get("viewBox") else None
    page = (vb[0], vb[1], vb[0] + vb[2], vb[1] + vb[3]) if vb else (0.0, 0.0, svg.viewport_width, svg.viewport_height)

    def candidates() -> list[Any]:
        out = []
        for el in root:
            if not isinstance(el.tag, str) or _local(el) in NOT_DRAWN:
                continue
            out.extend([c for c in el if isinstance(c.tag, str)] if el.get(GROUPMODE) == "layer" else [el])
        return out

    parked = []
    for el in candidates():
        b = boxes.get(el.get("id", ""))
        if b and (b[2] < page[0] or b[0] > page[2] or b[3] < page[1] or b[1] > page[3]):
            parked.append((el.get("id"), el.get(LABEL) or "", [round(v, 1) for v in b]))
    listing = "\n".join(f"  {i}  {lab!r}  x {b[0]}..{b[2]}  y {b[1]}..{b[3]}" for i, lab, b in parked)
    delete = args.get("delete")
    if not delete:
        return live._result(operation, True, f"{len(parked)} object(s) off the page\n{listing}",
                            data={"parked": parked}, start=start)  # fmt: skip
    ids = [i for i, _, _ in parked] if delete is True else [i for i in delete if i in {p[0] for p in parked}]
    if not ids:
        return live._result(operation, True, "nothing off the page to delete", data={"parked": parked}, start=start)
    ops = [{"xpath": f"//*[@id='{i}']", "action": "remove"} for i in ids]
    res = await live._op_edit_xml(bus, operation, "", json.dumps({"ops": ops}), window_id, start)
    if res["success"]:
        res["message"] = f"deleted {len(ids)} off-page object(s), one undo step: {', '.join(ids)}"
    return res


# ---- before/after render for edits -----------------------------------------------------------

_ID_IN_XPATH = re.compile(r"@id\s*=\s*['\"]([^'\"]+)['\"]")
_ID_IN_XML = re.compile(r"\bid=['\"]([^'\"]+)['\"]")


def ids_touched(ops: list[dict[str, Any]], limit: int = 3) -> list[str]:
    """The ids an edit batch names: targets of its xpaths and the roots of fragments it adds."""
    ids: list[str] = []
    for op in ops:
        for m in _ID_IN_XPATH.findall(str(op.get("xpath", ""))):
            ids.append(m)
        xml = op.get("xml")
        if xml:
            first = _ID_IN_XML.search(str(xml))
            if first:
                ids.append(first.group(1))
    seen: list[str] = []
    for i in ids:
        if i not in seen:
            seen.append(i)
    return seen


async def before_after(config: Any, before: tuple[Path, bytes], ids: list[str]) -> dict[str, Any]:
    """Render the area the edit touched, before and after, side by side."""
    exe = _exe(config)
    after_path, after = await snapshot()
    boxes_after = await query_all(exe, after_path, after)
    boxes_before = await query_all(exe, before[0], before[1])
    box = union([boxes_after.get(i) or boxes_before.get(i) for i in ids])  # type: ignore[misc]
    if box is None:
        return {"render_note": "nothing with a visible extent to frame"}
    box = _pad(box, 0.06 * max(box[2] - box[0], box[3] - box[1]))
    stem = f"edit-{uuid.uuid4().hex[:6]}"
    pngs = []
    for name, (svg_path, data) in (("before", before), ("after", (after_path, after))):
        png = svg_path.with_name(f"{stem}-{name}.png")
        if await render_area(exe, svg_path, data, box, png, height_px=420):
            return {"render_note": "render failed"}
        pngs.append(png)
    sheet = stitch(pngs, after_path.with_name(f"{stem}.png"))
    return {"render": str(sheet), "render_box": [round(v, 1) for v in box]}
