"""component apply modes (free / clip / pattern) end-to-end through the real edit_xml plugin code.

The live export and the extension call are replaced by an in-memory document that the
plugin's own run_ops mutates, so these tests exercise the exact ops Inkscape would receive.
"""

from __future__ import annotations

import json
import re
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import inkex
import pytest
from lxml import etree

from inkscape_mcp import extension_bridge
from inkscape_mcp.tools import live

SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
EXT_DIR = Path(__file__).parent.parent / "src" / "inkscape_mcp" / "plugins"
DOC = f"""<svg xmlns="{SVG}" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
 xmlns:xlink="{XLINK}" width="160mm" height="70mm" viewBox="0 0 160 70"><defs id="defs1"/>
 <g id="layer1" inkscape:groupmode="layer" transform="translate(5,3)">
  <path id="cell" d="M 10,10 H 50 V 50 H 10 Z" style="fill:#f4f4f4;stroke:#333333"/>
  <rect id="box" x="85" y="8" width="60" height="50" style="fill:#eeeeee;stroke:#333333;stroke-width:0.5"/>
 </g></svg>"""


@pytest.fixture
def doc(monkeypatch, tmp_path):
    fake_inkex = types.ModuleType("inkex")
    fake_inkex.EffectExtension = object
    monkeypatch.setitem(sys.modules, "inkex", fake_inkex)
    monkeypatch.syspath_prepend(str(EXT_DIR))
    sys.modules.pop("mcp_edit_xml", None)
    import mcp_edit_xml as plugin  # type: ignore

    monkeypatch.setitem(sys.modules, "inkex", inkex)  # the op itself needs the real inkex
    state = {"tree": etree.fromstring(DOC.encode()), "ops": None}

    async def fake_export(bus, path):
        Path(path).write_bytes(etree.tostring(state["tree"]))

    async def fake_invoke(bus, action, spec, timeout_s=0):
        state["ops"] = spec["ops"]
        return extension_bridge.ExchangeResult(success=True, data=plugin.run_ops(state["tree"], spec))

    monkeypatch.setattr(live, "_export_document_async", fake_export)
    monkeypatch.setattr(live, "invoke_extension", fake_invoke)
    monkeypatch.setattr(live, "_save_active_document", lambda b, w: (False, ""))
    monkeypatch.setattr(live, "_SCRATCH_DIR", tmp_path)
    return state


async def _component(**payload):
    return await live._op_component(MagicMock(), "component", "", json.dumps(payload), 1, time.perf_counter())


def _find(state, element_id):
    return state["tree"].find(f".//*[@id='{element_id}']")


async def test_clip_inserts_after_the_shape_with_a_live_clip(doc):
    res = await _component(kind="scatter_even", params={"id": "dots"}, seed=1, apply={"mode": "clip", "to": "cell"})
    assert res["success"] is True, res
    clipped = _find(doc, "dots-clipped")
    assert clipped.getprevious().get("id") == "cell" and clipped.get("clip-path") == "url(#dots-clip)"
    use = _find(doc, "dots-clip")[0]
    assert use.get(f"{{{XLINK}}}href") == "#cell"
    # Region defaulted to the cell's document bbox (10..50 + layer offset), drawn in layer coords.
    inner = _find(doc, "dots")
    assert inner.get("transform") == "translate(-5, -3)"
    xs = [float(v) for v in re.findall(r"M (-?[\d.]+),", _find(doc, "dots-marks").get("d"))]
    assert 15 - 1e-6 <= min(xs) and max(xs) <= 55


async def test_pattern_fill_keeps_the_rest_of_the_style(doc):
    res = await _component(
        kind="scatter_even", params={"id": "dots"}, apply={"mode": "pattern", "tile": [12, 10], "to": "box"}
    )
    assert res["success"] is True, res
    pattern = _find(doc, "dots-pattern")
    assert pattern.getparent().get("id") == "defs1"
    assert (pattern.get("width"), pattern.get("height"), pattern.get("patternUnits")) == ("12", "10", "userSpaceOnUse")
    assert _find(doc, "box").get("style") == "stroke:#333333;stroke-width:0.5;fill:url(#dots-pattern)"
    assert "periodic" not in res["message"]  # periodic is implied, not reported as an error


async def test_pattern_without_target_only_adds_the_pattern(doc):
    res = await _component(kind="flow_lines", params={"id": "fl"}, apply={"mode": "pattern", "tile": [20, 20]})
    assert res["success"] is True and _find(doc, "fl-pattern") is not None
    assert "fill:url" not in _find(doc, "box").get("style")


async def test_into_a_transformed_group_keeps_document_coordinates(doc):
    res = await _component(kind="blob", params={"id": "b", "cx": 40, "cy": 30}, into="layer1")
    assert res["success"] is True
    assert _find(doc, "b").get("transform") == "translate(-5, -3)"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"kind": "blob", "params": {"id": "b", "cx": 0, "cy": 0}, "apply": {"mode": "explode"}}, "apply.mode"),
        ({"kind": "blob", "params": {"id": "b", "cx": 0, "cy": 0}, "apply": {"mode": "clip"}}, "apply.to"),
        (
            {"kind": "blob", "params": {"id": "b", "cx": 0, "cy": 0}, "apply": {"mode": "pattern", "tile": [5, 5]}},
            "no region",
        ),
        ({"kind": "scatter_even", "params": {"id": "s"}, "apply": {"mode": "pattern", "tile": [0, 5]}}, "apply.tile"),
        (
            {"kind": "scatter_even", "params": {"id": "s"}, "apply": {"mode": "clip", "to": "nope"}},
            "no element with id 'nope'",
        ),
    ],
)
async def test_apply_errors(doc, payload, error):
    res = await _component(**payload)
    assert res["success"] is False and error in res["message"]
    assert doc["ops"] is None  # nothing was sent to Inkscape
