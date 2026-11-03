"""edit_xml batches: several ops applied as one undoable, all-or-nothing extension run."""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from lxml import etree

from inkscape_mcp import extension_bridge
from inkscape_mcp.tools import live

EXT_DIR = Path(__file__).parent.parent / "src" / "inkscape_mcp" / "plugins"
SVG = "http://www.w3.org/2000/svg"
INK = "http://www.inkscape.org/namespaces/inkscape"
DOC = f"""<svg xmlns="{SVG}" xmlns:inkscape="{INK}" id="svg1">
  <g id="layer1"><rect id="a" x="1"/><rect id="b" x="2"/><text id="t">old</text></g>
</svg>"""


@pytest.fixture
def plugin(monkeypatch):
    """The real extension module, with inkex stubbed so it imports outside Inkscape."""
    fake_inkex = types.ModuleType("inkex")
    fake_inkex.EffectExtension = object
    monkeypatch.setitem(sys.modules, "inkex", fake_inkex)
    monkeypatch.syspath_prepend(str(EXT_DIR))
    sys.modules.pop("mcp_edit_xml", None)
    import mcp_edit_xml  # type: ignore

    return mcp_edit_xml


def _root():
    return etree.fromstring(DOC.encode())


def test_batch_applies_every_op(plugin):
    root = _root()
    result = plugin.run_ops(
        root,
        {
            "ops": [
                {"xpath": "//*[@id='a']", "action": "set_attr", "name": "x", "value": "10"},
                {"xpath": "//*[@id='b']", "action": "set_attr", "name": "inkscape:label", "value": "B"},
                {"xpath": "//*[@id='t']", "action": "set_text", "text": "new"},
                {"xpath": "//*[@id='layer1']", "action": "append", "xml": '<circle id="c" r="1"/>'},
            ]
        },
    )
    assert result == {"ok": True, "action": "batch", "ops": 4, "affected": 4, "affected_per_op": [1, 1, 1, 1]}
    assert root.find(".//*[@id='a']").get("x") == "10"
    assert root.find(".//*[@id='b']").get(f"{{{INK}}}label") == "B"
    assert root.find(".//*[@id='t']").text == "new"
    assert root.find(f".//{{{SVG}}}circle").get("id") == "c"


def test_failing_op_leaves_document_untouched(plugin):
    root = _root()
    before = etree.tostring(root)
    result = plugin.run_ops(
        root,
        {
            "ops": [
                {"xpath": "//*[@id='a']", "action": "set_attr", "name": "x", "value": "10"},
                {"xpath": "//*[@id='missing']", "action": "remove"},
            ]
        },
    )
    assert result["ok"] is False and result["error"].startswith("op 1 (remove): xpath matched no nodes")
    assert etree.tostring(root) == before


def test_later_ops_see_earlier_ones(plugin):
    root = _root()
    result = plugin.run_ops(
        root,
        {
            "ops": [
                {"xpath": "//*[@id='layer1']", "action": "append", "xml": '<g id="new"/>'},
                {"xpath": "//*[@id='new']", "action": "append", "xml": '<rect id="inner"/>'},
            ]
        },
    )
    assert result["ok"] is True
    assert root.find(".//*[@id='new']/*").get("id") == "inner"


def test_single_op_spec_still_works(plugin):
    root = _root()
    assert plugin.run_ops(root, {"xpath": "//*[@id='a']", "action": "remove"})["affected"] == 1
    assert root.find(".//*[@id='a']") is None


def test_empty_value_still_deletes_in_batch(plugin):
    root = _root()
    plugin.run_ops(root, {"ops": [{"xpath": "//*[@id='a']", "action": "set_attr", "name": "x", "value": ""}]})
    assert "x" not in root.find(".//*[@id='a']").attrib


# ---- server side ---------------------------------------------------------------------


@pytest.fixture
def mock_invoke(monkeypatch):
    mock = AsyncMock(
        return_value=extension_bridge.ExchangeResult(
            success=True, data={"ok": True, "action": "batch", "ops": 2, "affected": 2}
        )
    )
    monkeypatch.setattr(live, "invoke_extension", mock)
    monkeypatch.setattr(live, "_save_active_document", lambda bus, w: (False, "skipped"))
    return mock


async def _run(target: str, payload: dict) -> dict:
    return await live._op_edit_xml(MagicMock(), "edit_xml", target, json.dumps(payload), 1, time.perf_counter())


async def test_batch_is_one_extension_call_with_default_xpath(mock_invoke):
    res = await _run(
        "//*[@id='a']",
        {"ops": [{"action": "set_attr", "name": "x", "value": "1"}, {"xpath": "//*[@id='b']", "action": "remove"}]},
    )
    assert res["success"] is True and "one undo step" in res["message"]
    mock_invoke.assert_awaited_once()
    spec = mock_invoke.call_args.args[2]
    assert spec == {
        "ops": [
            {"xpath": "//*[@id='a']", "action": "set_attr", "name": "x", "value": "1"},
            {"xpath": "//*[@id='b']", "action": "remove"},
        ]
    }


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"ops": []}, "non-empty list"),
        ({"ops": [{"action": "remove"}]}, "op 0 has no xpath"),
        ({"ops": [{"xpath": "//x", "action": "explode"}]}, "op 0: unknown action"),
        ({"ops": ["nope"]}, "op 0 must be an object"),
    ],
)
async def test_bad_batches_rejected_before_invoke(mock_invoke, payload, error):
    res = await _run("", payload)
    assert res["success"] is False and error in res["message"]
    mock_invoke.assert_not_awaited()


async def test_extension_rejection_says_nothing_changed(mock_invoke):
    mock_invoke.return_value = extension_bridge.ExchangeResult(
        success=True, data={"ok": False, "error": "op 1 (remove): xpath matched no nodes"}
    )
    res = await _run("", {"ops": [{"xpath": "//a", "action": "remove"}, {"xpath": "//b", "action": "remove"}]})
    assert res["success"] is False and "nothing changed" in res["message"] and "op 1" in res["message"]


async def test_batch_and_fragments_can_come_from_files(mock_invoke, tmp_path):
    frag = tmp_path / "frag.svg"
    frag.write_text('<g id="big"/>')
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({"ops": [{"xpath": "//*[@id='a']", "action": "replace", "xml_path": str(frag)}]}))
    res = await _run("", {"ops_path": str(ops)})
    assert res["success"] is True
    assert mock_invoke.call_args.args[2]["ops"] == [
        {"xpath": "//*[@id='a']", "action": "replace", "xml": '<g id="big"/>'}
    ]


async def test_missing_ops_file_is_a_clear_error(mock_invoke, tmp_path):
    res = await _run("", {"ops_path": str(tmp_path / "nope.json")})
    assert res["success"] is False and "nope.json" in res["message"]
    mock_invoke.assert_not_awaited()
