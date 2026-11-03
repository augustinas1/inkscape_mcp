"""Unit tests for figure_outline: a compact map of what a figure contains and where."""

from __future__ import annotations

import pytest

from inkscape_mcp.figure_outline import outline

DOC = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
     sodipodi:docname="fig.svg" width="180mm" height="120mm" viewBox="0 0 180 120">
  <sodipodi:namedview id="nv"/>
  <defs id="defs1"><marker id="Triangle"><path id="tri" d="M 0,0 1,1 Z"/></marker></defs>
  <g id="layer1" inkscape:groupmode="layer" inkscape:label="Layer 1">
    <g id="mrna" inkscape:label="mRNA" transform="translate(10,5)">
      <path id="strand" inkscape:label="mRNA strand" d="M 100,80 H 130"
            style="fill:none;stroke:#d1495b;stroke-width:1.4"/>
      <circle id="cap" cx="100" cy="80" r="1.5" fill="#d1495b"/>
      <text id="caption" x="115" y="90" text-anchor="middle" font-size="4">mRNA (<tspan>M</tspan>)</text>
    </g>
    <path id="arrow" d="M 10,10 H 20" style="stroke:#333;marker-end:url(#Triangle)"/>
    <path id="cycle" sodipodi:type="arc" sodipodi:open="true" sodipodi:cx="50" sodipodi:cy="50"
          sodipodi:rx="5" sodipodi:ry="5" d="M 55,50 A 5,5 0 1 1 50,45" style="fill:none;stroke:#333"/>
    <rect id="ghost" x="0" y="0" width="5" height="5" style="display:none"/>
    {extra}
  </g>
</svg>
"""


def _outline(extra: str = "", root_id: str = "") -> list[str]:
    return outline(DOC.replace("{extra}", extra).encode(), root_id).splitlines()


def _line(lines: list[str], element_id: str) -> str:
    return next(line for line in lines if line.strip().split("  ")[0] == element_id)


def test_header_names_document_and_units():
    header = _outline()[0]
    assert header.startswith("fig.svg: 180mm x 120mm, viewBox 0 0 180 120")


def test_view_state_and_defs_are_left_out():
    text = "\n".join(_outline())
    assert "nv" not in text and "Triangle" not in text and "tri" not in text


def test_labels_kinds_paint_and_text():
    lines = _outline()
    assert _line(lines, "mrna").strip().startswith('mrna  "mRNA"  g')
    strand = _line(lines, "strand")
    assert '"mRNA strand"  path' in strand and "stroke #d1495b 1.4" in strand
    assert "fill #d1495b" in _line(lines, "cap")
    assert "'mRNA (M)'" in _line(lines, "caption")
    assert "path →" in _line(lines, "arrow")
    assert "  arc  " in _line(lines, "cycle")
    assert "HIDDEN" in _line(lines, "ghost")


def test_nesting_follows_the_tree():
    lines = _outline()
    indent = {i: len(_line(lines, i)) - len(_line(lines, i).lstrip()) for i in ("layer1", "mrna", "strand")}
    assert indent["layer1"] < indent["mrna"] < indent["strand"]


def test_boxes_include_ancestor_transforms():
    lines = _outline()
    assert "x 110..140  y 85" in _line(lines, "strand")  # translate(10,5) applied
    assert "x 108.5..111.5  y 83.5..86.5" in _line(lines, "cap")


def test_group_box_is_union_of_children_not_inflated_by_text():
    mrna = _line(_outline(), "mrna")
    left = float(mrna.split("x ")[1].split("..")[0])
    assert left == pytest.approx(108.5)  # the cap, not inkex's text box (which includes the origin)


def test_hidden_children_do_not_count_toward_group_box():
    layer = _line(_outline(), "layer1")
    assert not layer.split("x ")[1].startswith("0")  # the hidden rect at the origin is ignored


def test_large_groups_are_summarised():
    dots = "".join(f'<circle id="d{i}" cx="{i}" cy="100" r="0.5"/>' for i in range(20))
    lines = _outline(extra=f'<g id="dots">{dots}</g>')
    assert any("… 12 more (circle x12)" in line for line in lines)
    assert "x -0.5..19.5" in _line(lines, "dots")  # summarised children still count toward the box


def test_subtree_by_id():
    lines = _outline(root_id="mrna")
    assert lines[1].startswith('mrna  "mRNA"') and len(lines) == 5


def test_unknown_id_raises():
    with pytest.raises(KeyError):
        _outline(root_id="nope")


def test_text_box_applies_the_texts_own_transform():
    lines = _outline('<text id="moved" x="0" y="10" font-size="4" transform="translate(50,20)">ab</text>')
    assert "x 50..54.4  y 27..31" in _line(lines, "moved")


def test_glyph_runs_are_one_line_with_a_box():
    glyphs = (
        '<defs><g id="glyph-0-1"><path d="M 0,0 h 4 v -6 h -4 z"/></g></defs>'
        '<g id="ticklabel" transform="translate(10,0)"><use xlink:href="#glyph-0-1" x="5" y="50"/>'
        '<use xlink:href="#glyph-0-1" x="10" y="50"/></g>'
    )
    doc = DOC.replace("{extra}", glyphs).replace("<svg ", '<svg xmlns:xlink="http://www.w3.org/1999/xlink" ', 1)
    lines = outline(doc.encode()).splitlines()
    assert _line(lines, "ticklabel").strip() == "ticklabel  glyphs x2 (outlined text)  x 15..24  y 44..50"
    assert not any(line.strip().startswith("use") for line in lines)


def test_elements_that_draw_nothing_get_no_line():
    lines = _outline('<text id="blank" x="1" y="1"> </text><g id="hollow"><path id="nothing" d=""/></g>')
    ids = {line.strip().split("  ")[0] for line in lines}
    assert not {"blank", "hollow", "nothing"} & ids


def test_line_cap_applies_to_top_level_too():
    many = "".join(f'<rect id="r{i}" x="{i}" y="0" width="1" height="1"/>' for i in range(30))
    doc = DOC.replace("{extra}", "").replace("</svg>", many + "</svg>")
    text = outline(doc.encode(), max_lines=12)
    assert len(text.splitlines()) <= 14 and "outline truncated at 12 lines" in text
