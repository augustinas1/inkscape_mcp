"""A compact, human-readable outline of a figure: what is in it and where.

One line per meaningful element — id, Inkscape label, kind, colours, short text and the
bounding box in document user units — nested like the Objects dialog. It is what an agent
needs to map "the mRNA" to `path67` without reading kilobytes of XML, and is small enough
(~1-3 KB) to inject into the agent's context on every change (see change_tracker).

Bounding boxes of shapes come from inkex's geometry. inkex's text boxes are unusable (they
include the origin), so text extents are estimated from font size, character count and
anchor; group boxes are the union of their children's, so a text estimate never inflates
them by more than its own error.
"""

from __future__ import annotations

import math
import re
from typing import Any

import inkex

_SVG = "http://www.w3.org/2000/svg"
_INK = "http://www.inkscape.org/namespaces/inkscape"
_SODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"

_SKIP_TAGS = frozenset(
    {
        f"{{{_SVG}}}defs",
        f"{{{_SVG}}}metadata",
        f"{{{_SVG}}}title",
        f"{{{_SVG}}}desc",
        f"{{{_SVG}}}style",
        f"{{{_SVG}}}script",
        f"{{{_SODI}}}namedview",
    }
)
_MAX_CHILDREN = 10  # a group lists this many children, then summarises the rest
_MAX_LINES = 150
_MAX_TEXT = 40
_CHAR_WIDTH = 0.55  # average glyph advance as a fraction of font size (sans-serif)
Box = tuple[float, float, float, float]  # left, top, right, bottom


def _num(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else ("0" if text == "-0" else text)


def _span(a: float, b: float) -> str:
    return _num(a) if abs(b - a) < 0.05 else f"{_num(a)}..{_num(b)}"


def _union(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _prop(el: Any, name: str) -> str:
    """A presentation property from the style attribute, falling back to the XML attribute."""
    value = el.style.get(name) if el.get("style") else None
    return str(value if value is not None else el.get(name) or "")


def _length(value: str, default: float) -> float:
    match = re.match(r"\s*([-+]?[\d.]+(?:e[-+]?\d+)?)", value or "")
    return float(match.group(1)) if match else default


def _inherited(el: Any, name: str, default: str = "") -> str:
    node = el
    while node is not None and isinstance(node.tag, str):
        value = _prop(node, name)
        if value:
            return value
        node = node.getparent()
    return default


def _font_size(el: Any) -> float:
    """The text's font size: the first one set inside it (tspans often carry it), else inherited."""
    for node in el.iter():
        if isinstance(node.tag, str):
            size = _prop(node, "font-size")
            if size and not size.endswith("%"):
                return _length(size, 4.0)
    return _length(_inherited(el, "font-size"), 4.0)


def _text_box(el: Any, transform: Any) -> Box | None:
    first = next((t for t in el.iter() if isinstance(t.tag, str) and t.get("x") is not None), el)
    try:
        x = _length(first.get("x", "0"), 0.0)
        y = _length(first.get("y", "0"), 0.0)
    except ValueError:
        return None
    if not _text_of(el):
        return None  # an empty text draws nothing; its anchor would only stretch its group's box
    size = _font_size(el)
    width = len(_text_of(el)) * size * _CHAR_WIDTH
    anchor = _inherited(el, "text-anchor", "start")
    left = x - width / 2 if anchor == "middle" else x - width if anchor == "end" else x
    corners = [
        transform.apply_to_point((px, py)) for px in (left, left + width) for py in (y - 0.75 * size, y + 0.25 * size)
    ]
    return (min(p.x for p in corners), min(p.y for p in corners), max(p.x for p in corners), max(p.y for p in corners))


def _text_of(el: Any) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _shape_box(el: Any) -> Box | None:
    parent = el.getparent()
    try:
        bbox = el.bounding_box(parent.composed_transform() if parent is not None else None)
    except Exception:
        return None
    if bbox is None or bbox.width is None:
        return None
    box = (bbox.left, bbox.top, bbox.right, bbox.bottom)
    return box if all(math.isfinite(v) for v in box) else None  # empty geometry reports ±inf


def _use_box(el: Any) -> Box | None:
    """A <use>'s box: its referenced element drawn at (x, y) under the use's transforms.

    inkex gives <use> no box, so plots pasted from matplotlib (text as glyph <use>s) showed none.
    """
    href = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href") or ""
    ref = el.root.getElementById(href[1:]) if href.startswith("#") and el.root is not None else None
    if ref is None or not isinstance(ref, inkex.ShapeElement):
        return None
    place = el.composed_transform() @ inkex.Transform(
        translate=(_length(el.get("x", "0"), 0.0), _length(el.get("y", "0"), 0.0))
    )
    try:
        bbox = ref.bounding_box(place @ -ref.transform)  # bounding_box re-applies ref's own transform
    except Exception:
        return None
    if bbox is None or bbox.width is None:
        return None
    box = (bbox.left, bbox.top, bbox.right, bbox.bottom)
    return box if all(math.isfinite(v) for v in box) else None


def _kind(el: Any) -> str:
    if el.get(f"{{{_INK}}}groupmode") == "layer":
        return "layer"
    tag = str(el.TAG) if hasattr(el, "TAG") else el.tag.split("}")[-1]
    sodi = el.get(f"{{{_SODI}}}type")
    if sodi == "arc":
        tag = "arc" if el.get(f"{{{_SODI}}}open") == "true" else "ellipse"
    elif sodi:
        tag = sodi
    if tag in {"path", "line", "polyline", "arc"} and _prop(el, "marker-end"):
        tag += " →"
    if tag in {"path", "line", "polyline", "arc"} and _prop(el, "marker-start"):
        tag = "← " + tag
    return tag


def _paint(el: Any) -> str:
    parts = []
    fill, stroke = _prop(el, "fill"), _prop(el, "stroke")
    if fill and fill != "none":
        parts.append(f"fill {fill}")
    if stroke and stroke != "none":
        width = _prop(el, "stroke-width")
        parts.append(f"stroke {stroke}" + (f" {_num(_length(width, 1.0))}" if width else ""))
    return ", ".join(parts)


class _Outliner:
    def __init__(self, max_lines: int = _MAX_LINES, max_depth: int = 99) -> None:
        self.lines: list[str] = []
        self.truncated = 0
        self.max_lines = max_lines
        self.max_depth = max_depth

    def visit(self, el: Any, depth: int) -> Box | None:
        """Outline `el` at `depth`; return its box. Lines are appended in document order."""
        if not isinstance(el.tag, str) or el.tag in _SKIP_TAGS:
            return None
        hidden = _prop(el, "display") == "none"

        index = len(self.lines)
        self.lines.append("")  # placeholder: the box of a group is known only after its children
        is_text = isinstance(el, inkex.TextElement)
        is_group = isinstance(el, (inkex.Group, inkex.Layer)) or el.tag == f"{{{_SVG}}}svg"
        children = [c for c in el if isinstance(c.tag, str) and c.tag not in _SKIP_TAGS] if is_group else []
        glyphs = bool(children) and all(isinstance(c, inkex.Use) for c in children)

        if glyphs:  # a run of glyph <use>s (text pasted from a plot): one line, not one per glyph
            box = _union([b for b in map(_use_box, children) if b])
        elif is_group:
            boxes = []
            if depth >= self.max_depth:
                shown = []  # deeper than asked: one summary line for the contents
            else:
                shown = children if len(children) <= _MAX_CHILDREN else children[: _MAX_CHILDREN - 2]
            for child in shown:
                box = self.visit(child, depth + 1) if len(self.lines) < self.max_lines else None
                if len(self.lines) >= self.max_lines and box is None:
                    self.truncated += 1
                if box:
                    boxes.append(box)
            rest = children[len(shown) :]
            if rest:
                kinds: dict[str, int] = {}
                for child in rest:
                    kinds[_kind(child)] = kinds.get(_kind(child), 0) + 1
                    box = self._box_only(child)
                    if box:
                        boxes.append(box)
                summary = ", ".join(f"{k} x{n}" for k, n in kinds.items())
                self.lines.append("  " * (depth + 1) + f"… {len(rest)} more ({summary})")
            box = _union(boxes)
        elif is_text:
            box = _text_box(el, el.composed_transform())  # includes the text's own transform
        elif isinstance(el, inkex.Use):
            box = _use_box(el)
        elif isinstance(el, inkex.ShapeElement):
            box = _shape_box(el)
        else:
            box = None

        fields = [el.get("id") or f"({el.tag.split('}')[-1]})"]
        label = el.get(f"{{{_INK}}}label")
        if label and label != el.get("id"):
            fields.append(f'"{label}"')
        fields.append(f"glyphs x{len(children)} (outlined text)" if glyphs else _kind(el))
        if is_text:
            text = _text_of(el)
            fields.append(repr(text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"))
        if box:
            fields.append(f"x {_span(box[0], box[2])}  y {_span(box[1], box[3])}")
        paint = "" if is_group else _paint(el)
        if paint:
            fields.append(paint)
        if hidden:
            fields.append("HIDDEN")
        elif box is None:
            del self.lines[index:]  # draws nothing (empty text or group, degenerate path): no line
            return None
        self.lines[index] = "  " * depth + "  ".join(fields)
        return None if hidden else box

    def _box_only(self, el: Any) -> Box | None:
        saved, self.lines = self.lines, []
        try:
            return self.visit(el, 0)
        finally:
            self.lines = saved


def outline(svg_bytes: bytes, root_id: str = "", max_lines: int = _MAX_LINES, max_depth: int = 99) -> str:
    """Outline the whole document, or the subtree whose id is `root_id`; groups deeper than
    `max_depth` are summarised in one line ("… 12 more (path x8, text x4)")."""
    document = inkex.load_svg(svg_bytes)
    svg = document.getroot()
    sodi_doc = svg.get(f"{{{_SODI}}}docname") or "untitled"
    header = f"{sodi_doc}: {svg.get('width', '?')} x {svg.get('height', '?')}"
    if svg.get("viewBox"):
        header += f", viewBox {svg.get('viewBox')}"
    header += " — boxes are x left..right, y top..bottom in user units (text boxes approximate)"

    outliner = _Outliner(max_lines, max_depth)
    if root_id:
        start = svg.getElementById(root_id)
        if start is None:
            raise KeyError(f"no element with id {root_id!r}")
        outliner.visit(start, 0)
    else:
        for child in svg:
            if len(outliner.lines) >= max_lines:
                outliner.truncated += 1
                continue
            outliner.visit(child, 0)
    lines = [header, *outliner.lines]
    if outliner.truncated:
        lines.append(f"… outline truncated at {max_lines} lines; outline a group by id for the rest")
    return "\n".join(lines)


def element_box(svg_bytes: bytes, element_id: str) -> Box | None:
    """Bounding box (user units, all transforms applied) of the element with `element_id`."""
    svg = inkex.load_svg(svg_bytes).getroot()
    el = svg.getElementById(element_id)
    if el is None:
        raise KeyError(f"no element with id {element_id!r}")
    return _Outliner()._box_only(el)


def user_area_to_px(svg_bytes: bytes, box: Box) -> str:
    """Inkscape's --export-area value (CSS px from the page origin) for a user-unit box."""
    svg = inkex.load_svg(svg_bytes).getroot()
    scale = svg.uutounit(1, "px")
    view_box = [float(v) for v in re.split(r"[\s,]+", (svg.get("viewBox") or "0 0 0 0").strip())[:2]]
    min_x, min_y = [*view_box, 0.0, 0.0][:2]
    left, top, right, bottom = box
    return ":".join(_fmt_px((v - o) * scale) for v, o in ((left, min_x), (top, min_y), (right, min_x), (bottom, min_y)))


def _fmt_px(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")
