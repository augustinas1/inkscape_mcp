"""Swap a re-plotted figure part into the place of its older, hand-styled version.

A plot pasted from matplotlib is usually restyled by hand in Inkscape — colours set in `style`,
text retyped as real text, a legend box added — and then the data changes. `plan` puts the new
plot (`source`) exactly where the old one (`target`) is and keeps the hand work:

* **frame** — the new plot moves so its axes land on the old plot's axes;
* **styles** — each plot element takes the `style` of its counterpart in the old plot.
  Counterparts are matched by what matplotlib wrote, which hand edits leave alone: the
  presentation attributes (``fill="#cc619e" fill-opacity="0.3"``) of a cairo export, or the ids
  (``line2d_3``) of matplotlib's own SVG backend;
* **deletions** — plot elements the old plot no longer has (a white background, outlined
  legend text the user replaced) are dropped. When the old plot has fewer elements of a kind,
  the new plot keeps only those sharing a row or column with one of them (ticks, labels);
* **additions** — the user's own objects inside the old plot (real text, a legend box, extra
  lines) are copied over unchanged;
* **outlined text** — glyph ``<use>`` runs can be replaced by real text (`glyph_text`).

The old plot is kept, moved to where the new one was (off the page, usually), unless
``park=False``. The result is a list of edit_xml ops — one undo step when applied.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import inkex
from lxml import etree

from .figure_outline import _Outliner

_SVG = "http://www.w3.org/2000/svg"
_INK = "http://www.inkscape.org/namespaces/inkscape"
_SODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
_XML = "http://www.w3.org/XML/1998/namespace"
# Presentation attributes matplotlib's cairo export writes on every mark (hand edits go to `style`).
_PAINT = ("fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-dasharray")
# matplotlib's own SVG backend: line2d_3, patch_2, text_5, matplotlib.axis_1 (+ Inkscape's -N on paste).
_MPL_ID = re.compile(r"^([A-Za-z][\w.]*?_\d+)(?:-\d+)*$")
_NOT_DRAWN = frozenset({"defs", "style", "metadata", "title", "desc", "script"})
_ORIGIN_TOL = 1.0  # user units: a mark nudged by hand still belongs to the plot
_LANE_TOL = 0.75


class TransplantError(ValueError):
    pass


@dataclass
class Plan:
    ops: list[dict[str, Any]]
    new_xml: str
    mode: str
    shift: tuple[float, float]
    styled: int = 0
    dropped: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    outlined_text: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.mode} plot moved by ({self.shift[0]:.2f}, {self.shift[1]:.2f})",
            f"{self.styled} element(s) restyled",
        ]
        if self.dropped:
            parts.append(f"dropped {len(self.dropped)} the old plot no longer had ({_ids(self.dropped)})")
        if self.extras:
            parts.append(f"copied {len(self.extras)} hand-made object(s) ({_ids(self.extras)})")
        if self.outlined_text:
            parts.append(
                f"{len(self.outlined_text)} outlined text run(s) left as glyphs ({_ids(self.outlined_text)}) — "
                "pass glyph_text={id: text} to make them editable"
            )
        return "; ".join(parts + self.notes)


def _ids(ids: list[str], n: int = 8) -> str:
    return ", ".join(ids[:n]) + (", …" if len(ids) > n else "")


def _local(el: Any) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _kids(el: Any) -> list[Any]:
    return [c for c in el if isinstance(c.tag, str)]


def _href(el: Any) -> str:
    return el.get("{http://www.w3.org/1999/xlink}href") or el.get("href") or ""


def _is_glyphs(el: Any) -> bool:
    """A cairo text run: a group of <use>s of #glyph-… symbols (not matplotlib's tick markers)."""
    kids = _kids(el)
    return _local(el) == "g" and bool(kids) and all(_local(c) == "use" and _href(c).startswith("#glyph") for c in kids)


def _paint_sig(el: Any) -> tuple[Any, ...] | None:
    """What matplotlib's cairo export wrote for this mark; None for hand-made objects."""
    if _is_glyphs(el):
        return ("glyphs", el.get("fill", ""))
    kids = _kids(el)
    if _local(el) == "g" and el.get("clip-path") and len(kids) == 1:
        inner = _paint_sig(kids[0])
        return ("clip", *inner) if inner else None
    attrs = tuple((a, el.get(a)) for a in _PAINT if el.get(a) is not None)
    return (_local(el), *attrs) if attrs else None


def _translation(el: Any) -> tuple[float, float]:
    t = inkex.Transform(el.get("transform", ""))
    return (t.e, t.f)


def _linear(el: Any) -> tuple[float, ...]:
    t = inkex.Transform(el.get("transform", ""))
    return tuple(round(v, 3) for v in (t.a, t.b, t.c, t.d))


def _top_painted(group: Any) -> list[Any]:
    """matplotlib's marks at the top of their subtree (a clip group, not the path inside it)."""
    out: list[Any] = []

    def walk(el: Any) -> None:
        for child in _kids(el):
            if _paint_sig(child):
                out.append(child)
            else:
                walk(child)

    walk(group)
    return out


def _cairo_marks(group: Any) -> tuple[tuple[float, float], set[Any]] | None:
    """(plot origin, plot marks) of a cairo export, or None if `group` is not one.

    The origin is the translation most marks share. A mark the user moved (a copied separator,
    a nudged axis piece) still counts when it is drawn like a mark at the origin — same paint,
    same scale — so it is a template, not a hand-made object; a legend swatch that was flipped
    and resized into a legend box is not.
    """
    painted = _top_painted(group)
    counts = Counter((round(_translation(e)[0], 1), round(_translation(e)[1], 1)) for e in painted)
    if not counts or counts.most_common(1)[0][1] < 3:
        return None
    origin = counts.most_common(1)[0][0]
    core = [e for e in painted if _near(_translation(e), origin, _ORIGIN_TOL)]
    kinds = {(_paint_sig(e), _linear(e)) for e in core}
    return origin, {e for e in painted if (_paint_sig(e), _linear(e)) in kinds}


def _near(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _classify(group: Any, is_mark: Any) -> tuple[list[Any], list[Any]]:
    """(plot marks, hand-made objects) inside `group`; wrapper groups are looked through."""
    marks: list[Any] = []
    extras: list[Any] = []

    def walk(el: Any) -> None:
        for child in _kids(el):
            if _local(child) in _NOT_DRAWN:
                continue  # the plot's own definitions stay where they are; nothing to carry over
            if is_mark(child):
                marks.append(child)
            elif _local(child) == "g" and any(is_mark(d) for d in child.iterdescendants() if isinstance(d.tag, str)):
                walk(child)
            else:
                extras.append(child)

    walk(group)
    return marks, extras


def _box(el: Any, shift: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float, float, float] | None:
    box = _Outliner()._box_only(el)
    if box is None:
        return None
    return (box[0] + shift[0], box[1] + shift[1], box[2] + shift[0], box[3] + shift[1])


def _shares_lane(box: Any, others: list[Any]) -> bool:
    """Same row (top or bottom edge) or column (left or right edge) as any of `others`."""
    if box is None:
        return False
    for o in others:
        if o is None:
            continue
        if any(abs(box[i] - o[i]) <= _LANE_TOL for i in range(4)):
            return True
    return False


def _copy_styles(dst: Any, template: Any) -> None:
    """Give `dst` and its descendants the `style` of `template`'s, element by element."""
    for a, b in zip(dst.iter(), template.iter(), strict=False):
        if not isinstance(a.tag, str) or a.tag != b.tag:
            continue
        if b.get("style") is not None:
            a.set("style", b.get("style"))
        elif "style" in a.attrib:
            del a.attrib["style"]


def _fresh_ids(el: Any, taken: set[str]) -> None:
    for node in el.iter():
        if isinstance(node.tag, str) and node.get("id"):
            new = node.get("id") + "-n"
            while new in taken:
                new += "n"
            taken.add(new)
            node.set("id", new)


def _set_transform(el: Any, t: Any) -> None:
    if t == inkex.Transform():
        el.attrib.pop("transform", None)
    else:
        el.set("transform", str(t))


def _glyph_to_text(glyphs: Any, text: str, style: str, tid: str) -> Any:
    first = _kids(glyphs)[0]
    x, y = first.get("x", "0"), first.get("y", "0")
    fill = glyphs.get("fill")
    el = etree.Element(
        f"{{{_SVG}}}text",
        {"id": tid, f"{{{_XML}}}space": "preserve", "x": x, "y": y, "style": style + (f";fill:{fill}" if fill else "")},
    )
    if glyphs.get("transform"):
        el.set("transform", glyphs.get("transform"))
    span = etree.SubElement(el, f"{{{_SVG}}}tspan", {"id": f"{tid}-s", f"{{{_SODI}}}role": "line", "x": x, "y": y})
    span.text = text
    return el


def plan(
    svg_bytes: bytes,
    source_id: str,
    target_id: str,
    *,
    park: bool = True,
    skip: list[str] | None = None,
    glyph_text: dict[str, str] | None = None,
    text_style: str = "",
) -> Plan:
    """Work out the edit that puts `source` in `target`'s place with `target`'s styling."""
    root = inkex.load_svg(svg_bytes).getroot()
    src, tgt = root.getElementById(source_id), root.getElementById(target_id)
    for name, el in (("source", src), ("target", tgt)):
        if el is None:
            raise TransplantError(f"no {name} element with id {source_id if name == 'source' else target_id!r}")
    if src in tgt.iterancestors() or tgt in src.iterancestors():
        raise TransplantError("source and target must not contain each other")
    skip_ids = set(skip or [])

    # ---- what is a plot mark, and how the two plots line up -----------------------------
    sig: Callable[[Any], Any]  # what pairs a new mark with its old counterpart
    s_cairo, t_cairo = _cairo_marks(src), _cairo_marks(tgt)
    if s_cairo and t_cairo:
        mode = "cairo"
        (s_origin, s_set), (t_origin, t_set) = s_cairo, t_cairo
        s_is_mark, t_is_mark = s_set.__contains__, t_set.__contains__
        sig = _paint_sig
        s_lead = next(e for e in s_set if _near(_translation(e), s_origin, _ORIGIN_TOL))
        t_lead = next(e for e in t_set if _near(_translation(e), t_origin, _ORIGIN_TOL))
        # The exact (unrounded) origins of one mark on each side.
        s_pt = s_lead.getparent().composed_transform().apply_to_point(_translation(s_lead))
        t_pt = t_lead.getparent().composed_transform().apply_to_point(_translation(t_lead))
        shift = (t_pt.x - s_pt.x, t_pt.y - s_pt.y)
    else:
        mode = "matplotlib-svg"

        def is_mark(el: Any) -> bool:
            return _mpl_stem(el) is not None and not any(
                _mpl_stem(d) for d in el.iterdescendants() if isinstance(d.tag, str)
            )

        s_is_mark = t_is_mark = is_mark

        def sig(el: Any) -> str:
            return _mpl_role(el, (src, tgt))

        shift = _shift_by_frame(src, tgt, is_mark, sig)

    s_marks, _ = _classify(src, s_is_mark)
    t_marks, t_extras = _classify(tgt, t_is_mark)
    if not s_marks:
        raise TransplantError(f"no plot elements recognised in {source_id!r}")

    # ---- the new group: a copy of source, restyled, pruned, in target's frame ------------
    new = copy.deepcopy(src)
    by_id = {el.get("id"): el for el in new.iter() if isinstance(el.tag, str) and el.get("id")}
    result = Plan(ops=[], new_xml="", mode=mode, shift=shift)

    templates: dict[Any, list[Any]] = {}
    for el in t_marks:
        templates.setdefault(sig(el), []).append(el)
    source_by_sig: dict[Any, list[Any]] = {}
    for el in s_marks:
        source_by_sig.setdefault(sig(el), []).append(el)

    for key, members in source_by_sig.items():
        targets = templates.get(key, [])
        if mode == "matplotlib-svg" and len(targets) == len(members):
            # Roles hold several marks (two data lines in one axes): pair them in order.
            pairs = list(zip(members, targets, strict=True))
        else:
            # Same paint in matplotlib = same kind of mark; a hand style that differs among them is
            # usually data-specific (an arrow for an infinite value), so the commonest wins.
            styles = Counter(t.get("style") or "" for t in targets)
            template = None
            if targets:
                common, n = styles.most_common(1)[0]
                template = next(t for t in targets if (t.get("style") or "") == common)
                if len(styles) > 1:
                    kind = key[0] if isinstance(key, tuple) else key
                    result.notes.append(f"{len(targets)} old {kind} marks differ in style; used the commonest ({n})")
            keep = members
            if len(targets) < len(members):  # fewer in the old plot: keep those in a row/column with one
                t_boxes = [_box(t) for t in targets]
                keep = [m for m in members if _shares_lane(_box(m, shift), t_boxes)]
            pairs = [(m, template if m in keep else None) for m in members]
        for el, template in pairs:
            copy_el = by_id.get(el.get("id"))
            if copy_el is None:
                continue
            if template is None or el.get("id") in skip_ids:
                copy_el.getparent().remove(copy_el)
                result.dropped.append(el.get("id") or f"<{_local(el)}>")
                continue
            _copy_styles(copy_el, template)
            result.styled += 1

    # Outlined text -> real text, where asked.
    for gid, text in (glyph_text or {}).items():
        g = by_id.get(gid)
        if g is None or g.getparent() is None or not _is_glyphs(g):
            raise TransplantError(f"glyph_text: {gid!r} is not an outlined-text run in the source")
        if not text_style:
            raise TransplantError("glyph_text needs text_style, e.g. 'font-family:Calibri;font-size:10.67px'")
        g.getparent().replace(g, _glyph_to_text(g, text, text_style, gid))
    result.outlined_text = [
        el.get("id") for el in new.iter() if isinstance(el.tag, str) and el.get("id") and _is_glyphs(el)
    ]

    # Into target's frame: new document position = shift + old document position.
    move = inkex.Transform(translate=shift)
    m_s, m_t = src.composed_transform(), tgt.composed_transform()
    for child in _kids(new):
        _set_transform(child, -m_t @ move @ m_s @ inkex.Transform(child.get("transform", "")))
    _set_transform(new, inkex.Transform(tgt.get("transform", "")))
    label = tgt.get(f"{{{_INK}}}label") or src.get(f"{{{_INK}}}label")
    if label:
        new.set(f"{{{_INK}}}label", label)

    # The user's own objects in the old plot, copied in place.
    taken = {el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")}
    for extra in t_extras:
        if extra.get("id") in skip_ids:
            continue
        dup = copy.deepcopy(extra)
        if park:
            _fresh_ids(dup, taken)
        rel = -m_t @ extra.getparent().composed_transform()
        _set_transform(dup, rel @ inkex.Transform(extra.get("transform", "")))
        new.append(dup)
        result.extras.append(dup.get("id") or f"<{_local(dup)}>")

    new_xml = etree.tostring(new, encoding="unicode")
    result.new_xml = new_xml
    ops: list[dict[str, Any]] = [
        {"xpath": f"//*[@id='{source_id}']", "action": "remove"},
        {"xpath": f"//*[@id='{target_id}']", "action": "replace", "xml": new_xml},
    ]
    if park:
        old = copy.deepcopy(tgt)
        p_s, p_t = src.getparent().composed_transform(), tgt.getparent().composed_transform()
        _set_transform(old, -p_s @ -move @ p_t @ inkex.Transform(tgt.get("transform", "")))
        old.set(f"{{{_INK}}}label", f"old {label or target_id} (replaced, off-page)")
        ops.append({**_anchor(src, target_id, source_id), "xml": etree.tostring(old, encoding="unicode")})
    result.ops = ops
    return result


def _anchor(src: Any, target_id: str, source_id: str) -> dict[str, Any]:
    """Where the parked old plot goes: source's old place, named by a neighbour that survives."""

    def survivor(el: Any) -> str | None:
        if el is None or not el.get("id"):
            return None
        return source_id if el.get("id") == target_id else el.get("id")  # target's slot now holds source

    prev, nxt = src.getprevious(), src.getnext()
    while prev is not None and not isinstance(prev.tag, str):
        prev = prev.getprevious()
    while nxt is not None and not isinstance(nxt.tag, str):
        nxt = nxt.getnext()
    if survivor(prev):
        return {"xpath": f"//*[@id='{survivor(prev)}']", "action": "insert_after"}
    if survivor(nxt):
        return {"xpath": f"//*[@id='{survivor(nxt)}']", "action": "insert_before"}
    parent = src.getparent()
    if parent is not None and parent.get("id"):
        return {"xpath": f"//*[@id='{parent.get('id')}']", "action": "append"}
    raise TransplantError("cannot place the old plot: the source has no id'd neighbour or parent")


# Containers whose number names something (which axes, which axis); other numbers only count.
_NAMED = frozenset({"figure", "axes", "matplotlib.axis", "legend"})


def _mpl_stem(el: Any) -> str | None:
    m = _MPL_ID.match(el.get("id") or "")
    return m.group(1) if m else None


def _mpl_role(el: Any, roots: tuple[Any, ...]) -> str:
    """Where a matplotlib-svg mark sits: 'axes_1/matplotlib.axis_2/ytick/text', say.

    matplotlib numbers ids in drawing order (line2d_13 is the first data line only if there
    are twelve tick lines), so roles, not ids, pair marks between two exports.
    """
    parts = []
    node = el
    while node is not None and node not in roots:
        stem = _mpl_stem(node)
        if stem:
            base = stem.rsplit("_", 1)[0]
            parts.append(stem if base in _NAMED else base)
        node = node.getparent()
    return "/".join(reversed(parts))


def _shift_by_frame(src: Any, tgt: Any, is_mark: Any, stem: Any) -> tuple[float, float]:
    """matplotlib-svg mode: align the largest same-sized mark both plots have once (the axes)."""
    s_marks, _ = _classify(src, is_mark)
    t_marks, _ = _classify(tgt, is_mark)
    s_by = Counter(stem(m) for m in s_marks)
    t_by = Counter(stem(m) for m in t_marks)
    best = None
    for key in s_by.keys() & t_by.keys():
        if s_by[key] != 1 or t_by[key] != 1:
            continue
        a = _box(next(m for m in s_marks if stem(m) == key))
        b = _box(next(m for m in t_marks if stem(m) == key))
        if a is None or b is None:
            continue
        wa, ha, wb, hb = a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]
        if wa <= 0 or ha <= 0 or abs(wa - wb) > 0.02 * wa or abs(ha - hb) > 0.02 * ha:
            continue
        if best is None or wa * ha > best[0]:
            best = (wa * ha, (b[0] - a[0], b[1] - a[1]))
    if best is None:
        raise TransplantError("could not line the plots up: no same-sized frame element in both")
    return best[1]
