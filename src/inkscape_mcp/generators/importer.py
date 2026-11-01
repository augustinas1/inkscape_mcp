"""Import an outside SVG (a downloaded pattern, icon, drawing) and bring it into the house style.

The file is sanitised (scripts, event handlers, foreignObject, external references and editor
metadata removed), its ids are prefixed so several imports can coexist, it is scaled to the
requested width at the requested position, and its colours are optionally remapped onto the
style tokens. Nothing is stored: the result is placed in the user's document only.
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path
from typing import Any

import inkex
from lxml import etree

from . import common as c
from .registry import generator

SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
_DROP_TAGS = {"script", "foreignObject", "metadata", "namedview", "title", "desc"}
_COLOR_PROPS = ("fill", "stroke", "stop-color", "flood-color", "lighting-color")
_MAX_BYTES = 8_000_000


def _local(tag: Any) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _palette(style: dict[str, Any]) -> list[str]:
    """Every colour token in the style (deduplicated, in token order)."""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", node) and node.lower() not in out:
            out.append(node.lower())

    walk(style)
    return out


def _rgb(value: str) -> tuple[int, int, int] | None:
    try:
        col = inkex.Color(value.strip()).to_rgb()
    except Exception:
        return None
    return int(col[0]), int(col[1]), int(col[2])


def _recolor(value: str, mode: str, palette: list[str], target: str) -> str:
    if mode == "none" or not value or value.strip() in ("none", "currentColor", "inherit", "transparent"):
        return value
    if value.strip().startswith("url("):
        return value  # gradients/patterns: their stops are recoloured where they are defined
    rgb = _rgb(value)
    if rgb is None:
        return value
    lightness = colorsys.rgb_to_hls(*(v / 255 for v in rgb))[1]
    if lightness > 0.97:
        return "#ffffff"  # keep white as white: it is usually background
    if mode == "mono":
        return c.tint(target, max(0.12, 1 - lightness))  # dark -> full colour, light -> pale tint
    return min(palette, key=lambda p: sum((a - b) ** 2 for a, b in zip(rgb, _rgb(p) or (0, 0, 0), strict=True)))


def _recolor_style(css: str, mode: str, palette: list[str], target: str) -> str:
    decls = []
    for decl in css.split(";"):
        name, _, val = decl.partition(":")
        if name.strip() in _COLOR_PROPS:
            val = _recolor(val, mode, palette, target)
        if decl.strip():
            decls.append(f"{name.strip()}:{val.strip()}")
    return ";".join(decls)


def sanitise_and_prefix(root: Any, prefix: str) -> None:
    """Strip active/foreign content and prefix every id (and the references to it) in place."""
    for el in list(root.iter()):
        if not isinstance(el.tag, str):  # comments, processing instructions
            if el.getparent() is not None:
                el.getparent().remove(el)
            continue
        if _local(el.tag) in _DROP_TAGS and el is not root:
            el.getparent().remove(el)
            continue
        for attr in list(el.attrib):
            local = etree.QName(attr).localname
            value = el.get(attr) or ""
            if local.lower().startswith("on"):
                del el.attrib[attr]
            elif local == "href" and not (value.startswith("#") or value.startswith("data:image/")):
                del el.attrib[attr]  # no external or javascript: references
    ids = {el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")}
    if not ids:
        return
    pattern = re.compile(
        r"url\(\s*['\"]?#(" + "|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True)) + r")['\"]?\s*\)"
    )
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if el.get("id"):
            el.set("id", f"{prefix}-{el.get('id')}")
        for attr, value in list(el.attrib.items()):
            if "url(" in value:
                el.set(attr, pattern.sub(lambda m: f"url(#{prefix}-{m.group(1)})", value))
            if etree.QName(attr).localname == "href" and value.startswith("#") and value[1:] in ids:
                el.set(attr, f"#{prefix}-{value[1:]}")


@generator(
    "import",
    choices={"recolor": ["palette", "mono", "none"]},
    ranges={"width": (0.1, 5000)},
    docs={
        "path": "absolute path of the .svg to import",
        "width": "target width in document units (height follows the aspect ratio)",
        "recolor": "palette = nearest style colour | mono = tints of `color` by lightness | none",
        "color": "target colour for mono (hex or token path)",
    },
)
def import_svg(
    gid: str,
    style: dict[str, Any],
    path: str,
    x: float,
    y: float,
    width: float,
    recolor: str = "palette",
    color: str = "ink",
) -> str:
    """Import an outside SVG: sanitised, ids prefixed, fitted to (x, y, width), recoloured to the style."""
    src = Path(path).expanduser()
    if src.suffix.lower() != ".svg" or not src.is_file():
        raise ValueError(f"import_svg.path must be an existing .svg file (got {path!r})")
    if src.stat().st_size > _MAX_BYTES:
        raise ValueError(f"{src.name} is larger than {_MAX_BYTES // 1_000_000} MB")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)
    try:
        root = etree.parse(str(src), parser).getroot()
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"{src.name} is not well-formed SVG: {exc}") from exc
    if _local(root.tag) != "svg":
        raise ValueError(f"{src.name} has no <svg> root")

    vb = [float(v) for v in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if v] or []
    if len(vb) != 4:
        w = _length(root.get("width"))
        h = _length(root.get("height"))
        if not (w and h):
            raise ValueError(f"{src.name} has neither a viewBox nor a numeric width/height to size it by")
        vb = [0.0, 0.0, w, h]
    if vb[2] <= 0 or vb[3] <= 0:
        raise ValueError(f"{src.name} has an empty viewBox")

    sanitise_and_prefix(root, gid)
    if recolor != "none":
        palette = _palette(style)
        target = c.color(style, color)
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            for prop in _COLOR_PROPS:
                if el.get(prop):
                    el.set(prop, _recolor(el.get(prop), recolor, palette, target))
            if el.get("style"):
                el.set("style", _recolor_style(el.get("style"), recolor, palette, target))

    scale = width / vb[2]
    body = "".join(etree.tostring(child, encoding="unicode") for child in root if isinstance(child.tag, str))
    transform = f"translate({c.f(x)},{c.f(y)}) scale({c.f(scale)}) translate({c.f(-vb[0])},{c.f(-vb[1])})"
    return c.group(gid, f"imported {src.name}", body, f' transform="{transform}"')


def _length(value: str | None) -> float:
    match = re.match(r"\s*([\d.]+)", value or "")
    return float(match.group(1)) if match else 0.0
