"""preview: render a draft in the live document's context without changing the document,
and rasterize areas given in document units."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lxml import etree

from inkscape_mcp.figure_outline import element_box, user_area_to_px
from inkscape_mcp.tools import live

DOC = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     width="180mm" height="120mm" viewBox="0 0 180 120">
  <g id="layer1" inkscape:groupmode="layer">
    <g id="grp" transform="translate(10,0)"><rect id="old" x="20" y="30" width="10" height="5"/></g>
  </g>
</svg>"""
PX_PER_MM = 96 / 25.4


def test_user_area_to_px_scales_mm_to_css_px():
    area = user_area_to_px(DOC.encode(), (10, 20, 30, 40))
    assert [float(v) for v in area.split(":")] == pytest.approx([v * PX_PER_MM for v in (10, 20, 30, 40)], abs=0.01)


def test_user_area_to_px_honours_viewbox_origin():
    doc = DOC.replace('viewBox="0 0 180 120"', 'viewBox="5 5 180 120"')
    assert user_area_to_px(doc.encode(), (5, 5, 15, 15)).startswith("0:0:")


def test_element_box_applies_ancestor_transforms():
    assert element_box(DOC.encode(), "old") == pytest.approx((30, 30, 40, 35))


QUERY = {"stdout": b""}  # what the fake `inkscape --query-*` prints; empty = the query failed


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Fake the live export and the Inkscape CLI; record every export command."""
    monkeypatch.setattr(live, "_SCRATCH_DIR", tmp_path)

    async def fake_export(bus, path):
        Path(path).write_text(DOC)

    monkeypatch.setattr(live, "_export_document_async", fake_export)
    calls: list[list[str]] = []

    async def fake_exec(*argv, **kwargs):
        proc = MagicMock(returncode=0)
        stdout = b""
        if any(a.startswith("--query-id=") for a in argv):
            stdout = QUERY["stdout"]
        else:
            calls.append(list(argv))
            out = next(a.split("=", 1)[1] for a in argv if a.startswith("--export-filename="))
            Path(out).write_bytes(b"\x89PNG")

        async def communicate():
            return stdout, b""

        proc.communicate = communicate
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    QUERY["stdout"] = b""
    return calls


def _arg(argv, flag):
    return next(a.split("=", 1)[1] for a in argv if a.startswith(f"--{flag}="))


async def _preview(target="", **payload):
    config = MagicMock(inkscape_executable="/bin/inkscape")
    return await live._op_preview(MagicMock(), "preview", target, json.dumps(payload), config, time.perf_counter())


async def test_replace_renders_after_and_before_over_the_drafts_area(cli):
    res = await _preview(target="old", xml='<circle id="new" cx="25" cy="32" r="4"/>', margin=1)
    assert res["success"] is True, res
    assert set(res["data"]["paths"]) == {"after", "before"}
    # The draft replaced #old inside #grp, so it picked up translate(10,0).
    assert res["data"]["box"] == pytest.approx([31, 28, 39, 36])
    after_svg = Path(_arg(cli[0], "export-filename")).with_suffix(".svg")
    tree = etree.parse(str(after_svg))
    assert tree.find(".//*[@id='old']") is None and tree.find(".//*[@id='new']") is not None
    # Both renders frame the same area: draft box + margin, in px.
    expected = [v * PX_PER_MM for v in (30, 27, 40, 37)]
    for argv in cli:
        assert [float(v) for v in _arg(argv, "export-area").split(":")] == pytest.approx(expected, abs=0.01)


async def test_append_defaults_to_first_layer_and_skips_before(cli):
    res = await _preview(xml='<rect id="added" x="100" y="100" width="4" height="4"/>')
    assert res["success"] is True
    assert list(res["data"]["paths"]) == ["after"]
    tree = etree.parse(str(Path(_arg(cli[0], "export-filename")).with_suffix(".svg")))
    assert tree.find(".//*[@id='added']").getparent().get("id") == "layer1"


async def test_focus_without_xml_renders_live_element(cli):
    res = await _preview(focus="old", margin=0)
    assert res["success"] is True and res["data"]["box"] == pytest.approx([30, 30, 40, 35])


@pytest.mark.parametrize(
    ("target", "payload", "error"),
    [
        ("missing", {"xml": '<rect id="x" width="1" height="1"/>'}, "no element with id 'missing'"),
        ("", {"xml": '<rect width="1" height="1"/>'}, "nothing to frame"),
        ("", {"xml": "not xml <"}, "did not parse"),
        ("", {"into": "nope", "xml": '<rect id="x" width="1" height="1"/>'}, "append into"),
    ],
)
async def test_preview_errors(cli, target, payload, error):
    res = await _preview(target=target, **payload)
    assert res["success"] is False and error in res["message"]


async def test_rasterize_area_is_in_document_units(cli):
    res = await live._op_rasterize(
        MagicMock(),
        "rasterize",
        "",
        json.dumps({"area": "10:20:30:40"}),
        MagicMock(),
        MagicMock(inkscape_executable="/bin/inkscape"),
        time.perf_counter(),
    )
    assert res["success"] is True
    expected = [v * PX_PER_MM for v in (10, 20, 40, 60)]  # x:y:w:h -> x0:y0:x1:y1
    assert [float(v) for v in _arg(cli[0], "export-area").split(":")] == pytest.approx(expected, abs=0.01)


async def test_fragments_may_use_undeclared_inkscape_prefixes(cli):
    res = await _preview(target="old", xml='<g id="kit" inkscape:label="kit"><rect id="r" width="2" height="2"/></g>')
    assert res["success"] is True, res
    tree = etree.parse(str(Path(_arg(cli[0], "export-filename")).with_suffix(".svg")))
    assert tree.find(".//*[@id='kit']").get("{http://www.inkscape.org/namespaces/inkscape}label") == "kit"


async def test_framing_uses_inkscapes_measured_box(cli):
    # Inkscape reports the draft's box in CSS px: (10, 20) 30 x 40 px -> user units (mm).
    QUERY["stdout"] = b"10\n20\n30\n40\n"
    res = await _preview(xml='<rect id="new" x="0" y="0" width="99" height="99"/>', into="layer1", margin=0)
    assert res["data"]["box"] == pytest.approx(
        [10 / PX_PER_MM, 20 / PX_PER_MM, 40 / PX_PER_MM, 60 / PX_PER_MM], abs=0.01
    )
    assert [float(v) for v in _arg(cli[0], "export-area").split(":")] == pytest.approx([10, 20, 40, 60], abs=0.02)
