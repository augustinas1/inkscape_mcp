"""transplant: put a re-plotted group in place of its older, hand-styled version."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import inkex
import pytest
from lxml import etree

from inkscape_mcp import extension_bridge, transplant
from inkscape_mcp.tools import live

SVG = "http://www.w3.org/2000/svg"
HEAD = (
    f'<svg xmlns="{SVG}" xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" width="800" height="400" viewBox="0 0 800 400">'
)


def _cairo_plot(gid, origin, *, bars, background=False, ticks=(10, 20, 30), extra=""):
    """A cairo-export-like plot: presentation attributes, every mark translated to `origin`."""
    t = f"translate({origin[0]}, {origin[1]})"
    m = f"matrix(1.33333 0 0 1.33333 {origin[0]} {origin[1]})"
    marks = [f'<path id="{gid}-bg" fill="#ffffff" d="M 0,0 H 90 V 60 H 0 Z" transform="{t}"/>'] if background else []
    marks += [
        f'<path id="{gid}-bar{i}" fill="#000000" fill-opacity="0.07" '
        f'd="M {x},60 h 5 V {60 - h} h -5 z" transform="{t}"/>'
        for i, (x, h) in enumerate(bars)
    ]
    marks += [
        f'<path id="{gid}-tick{i}" fill="none" stroke="#000000" stroke-width="0.7" d="m {x},45 v 1.5" transform="{m}"/>'
        for i, x in enumerate(ticks)
    ]
    marks += [
        f'<path id="{gid}-spine" fill="none" stroke="#000000" stroke-width="0.7" d="M 0,45 H 70" transform="{m}"/>'
    ]
    return f'<g id="{gid}">{"".join(marks)}{extra}</g>'


def _doc(*parts):
    return (HEAD + "".join(parts) + "</svg>").encode()


def _parse(xml):
    return etree.fromstring(xml.encode() if isinstance(xml, str) else xml)


@pytest.fixture
def cairo_doc():
    # The old plot, restyled by hand: teal bars, a hand label, no white background.
    old = _cairo_plot(
        "old", (10, 20), bars=[(10, 20), (20, 30)], extra='<text id="lbl" x="5" y="5" style="fill:#333">a</text>'
    )
    old = old.replace('fill-opacity="0.07"', 'fill-opacity="0.07" style="fill:#33bbcc;fill-opacity:0.5"')
    new = _cairo_plot("new", (500, 300), bars=[(10, 40), (20, 10), (30, 25)], background=True)
    return _doc(old, new)


def test_cairo_plot_lands_on_the_old_frame_with_its_styles(cairo_doc):
    plan = transplant.plan(cairo_doc, "new", "old")
    assert plan.mode == "cairo" and plan.shift == pytest.approx((-490, -280))
    new = _parse(plan.new_xml)
    bar = new.find(".//*[@id='new-bar2']")
    assert bar.get("style") == "fill:#33bbcc;fill-opacity:0.5"  # the old plot's hand style
    assert inkex.Transform(bar.get("transform")) == inkex.Transform(translate=(10, 20))
    assert new.find(".//*[@id='new-bg']") is None and plan.dropped == ["new-bg"]  # the old plot had none
    assert new.find(".//*[@id='lbl-n']") is not None and plan.extras == ["lbl-n"]  # hand label carried over


def test_old_plot_is_parked_where_the_new_one_was(cairo_doc):
    plan = transplant.plan(cairo_doc, "new", "old")
    assert [op["action"] for op in plan.ops] == ["remove", "replace", "insert_after"]
    parked = _parse(plan.ops[2]["xml"])
    assert parked.get("id") == "old" and "replaced, off-page" in parked.get(
        "{http://www.inkscape.org/namespaces/inkscape}label"
    )
    assert inkex.Transform(parked.get("transform")) == inkex.Transform(translate=(490, 280))
    # Anchored on the slot the old plot vacated: the new one now holds it.
    assert plan.ops[2]["xpath"] == "//*[@id='new']"


def test_without_park_the_old_plot_goes(cairo_doc):
    plan = transplant.plan(cairo_doc, "new", "old", park=False)
    assert [op["action"] for op in plan.ops] == ["remove", "replace"]
    assert _parse(plan.new_xml).find(".//*[@id='lbl']") is not None  # ids kept: nothing clashes


def test_fewer_old_marks_keep_only_those_in_a_shared_row(cairo_doc):
    # Old plot: two tick marks + spine; new: a tick in the same row, and a stray mark of the same
    # kind elsewhere (a legend swatch, say) that has no counterpart row.
    old = _cairo_plot("old", (10, 20), bars=[(10, 20)], ticks=(10, 20))
    stray = (
        '<path id="new-stray" fill="none" stroke="#000000" stroke-width="0.7" d="m 50,5 v 1.5" '
        'transform="matrix(1.33333 0 0 1.33333 500 300)"/>'
    )
    new = _cairo_plot("new", (500, 300), bars=[(10, 20)], ticks=(10, 20, 30, 40), extra=stray)
    plan = transplant.plan(_doc(old, new), "new", "old")
    assert plan.dropped == ["new-stray"]


def test_glyph_runs_become_real_text():
    glyphs = (
        '<g id="{g}-num" fill="#000000" transform="translate({o})"><use xlink:href="#glyph-0-1" x="12" y="70"/></g>'
    )
    defs = '<defs><g id="glyph-0-1"><path d="M 0,0 h 4 v -6 h -4 z"/></g></defs>'
    old = _cairo_plot("old", (10, 20), bars=[(10, 20)], extra=glyphs.format(g="old", o="10, 20"))
    new = _cairo_plot("new", (500, 300), bars=[(10, 20)], extra=glyphs.format(g="new", o="500, 300"))
    data = _doc(defs, old, new)
    kept = transplant.plan(data, "new", "old")
    assert kept.outlined_text == ["new-num"]
    with pytest.raises(transplant.TransplantError, match="text_style"):
        transplant.plan(data, "new", "old", glyph_text={"new-num": "20"})
    plan = transplant.plan(data, "new", "old", glyph_text={"new-num": "20"}, text_style="font-size:8px")
    text = _parse(plan.new_xml).find(".//*[@id='new-num']")
    assert etree.QName(text).localname == "text" and "".join(text.itertext()) == "20"
    assert text.get("x") == "12" and "fill:#000000" in text.get("style") and plan.outlined_text == []


def _mpl_plot(gid, n_ticks, line_style, suffix=""):
    """matplotlib's own SVG backend: style attributes, positional ids (line2d_N) per export."""
    s = suffix
    ticks = "".join(
        f'<g id="xtick_{i + 1}{s}"><g id="line2d_{i + 1}{s}"><path d="M {10 + 20 * i},50 v 3" style="stroke:#333"/></g>'
        f'<g id="text_{i + 1}{s}"><text x="{10 + 20 * i}" y="60" style="font-size:8px">{i}</text></g></g>'
        for i in range(n_ticks)
    )
    n = n_ticks + 1
    return (
        f'<g id="{gid}"><g id="figure_1{s}"><g id="axes_1{s}">'
        f'<g id="patch_2{s}"><path d="M 0,0 H 100 V 50 H 0 Z" style="fill:none"/></g>'
        f'<g id="matplotlib.axis_1{s}">{ticks}</g>'
        f'<g id="line2d_{n}{s}"><path d="M 0,40 L 100,10" style="{line_style}"/></g>'
        f'<g id="line2d_{n + 1}{s}"><path d="M 0,10 L 100,40" style="stroke:#d1495b"/></g>'
        "</g></g></g>"
    )


def test_matplotlib_svg_marks_pair_by_role_not_by_numbered_id():
    old = _mpl_plot("old", 3, "stroke:#d6589f;stroke-width:2").replace(
        '<g id="old">', '<g id="old" transform="translate(10,10)">'
    )
    new = _mpl_plot("new", 5, "stroke:#2a7ab0", suffix="-1").replace(
        '<g id="new">', '<g id="new" transform="translate(400,10)">'
    )
    plan = transplant.plan(_doc(old, new), "new", "old")
    assert plan.mode == "matplotlib-svg" and plan.shift == pytest.approx((-390, 0))
    out = _parse(plan.new_xml)
    # The first data line is line2d_6 in the new export but line2d_4 in the old one: still paired.
    assert (
        out.find(".//*[@id='line2d_6-1']/{http://www.w3.org/2000/svg}path").get("style")
        == "stroke:#d6589f;stroke-width:2"
    )
    assert out.find(".//*[@id='line2d_7-1']/{http://www.w3.org/2000/svg}path").get("style") == "stroke:#d1495b"
    assert plan.dropped == []  # all five ticks share the old ticks' row


def test_unknown_ids_and_nesting_are_refused(cairo_doc):
    with pytest.raises(transplant.TransplantError, match="no source"):
        transplant.plan(cairo_doc, "nope", "old")
    nested = _doc(_cairo_plot("old", (10, 20), bars=[(1, 1)], extra=_cairo_plot("new", (5, 5), bars=[(1, 1)])))
    with pytest.raises(transplant.TransplantError, match="contain each other"):
        transplant.plan(nested, "new", "old")


# ---- live op -------------------------------------------------------------------------------


@pytest.fixture
def live_doc(monkeypatch, tmp_path, cairo_doc):
    monkeypatch.setattr(live, "_SCRATCH_DIR", tmp_path)

    async def fake_export(bus, path):
        Path(path).write_bytes(cairo_doc)

    monkeypatch.setattr(live, "_export_document_async", fake_export)
    invoke = AsyncMock(return_value=extension_bridge.ExchangeResult(success=True, data={"ok": True, "affected": 3}))
    monkeypatch.setattr(live, "invoke_extension", invoke)
    monkeypatch.setattr(live, "_save_active_document", lambda b, w: (False, ""))
    return invoke


async def _op(payload):
    return await live._op_transplant(
        MagicMock(), "transplant", "", json.dumps(payload), 1, MagicMock(), time.perf_counter()
    )


async def test_live_check_changes_nothing(live_doc):
    res = await _op({"source": "new", "target": "old", "check": True})
    assert res["success"] and "nothing changed" in res["message"] and res["data"]["dropped"] == ["new-bg"]
    assert Path(res["data"]["xml_path"]).read_text().startswith("<g")
    live_doc.assert_not_awaited()


async def test_live_apply_is_one_batch(live_doc):
    res = await _op({"source": "new", "target": "old"})
    assert res["success"] and "transplanted #new into #old's place" in res["message"]
    ops = live_doc.call_args.args[2]["ops"]
    assert [op["action"] for op in ops] == ["remove", "replace", "insert_after"]


async def test_live_needs_both_ids(live_doc):
    res = await _op({"source": "new"})
    assert not res["success"] and "source and target" in res["message"]
