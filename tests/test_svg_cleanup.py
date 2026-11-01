"""svg_cleanup: keep exactly the definitions the drawing reaches; drop leftovers and empty groups."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from lxml import etree

from inkscape_mcp import extension_bridge, svg_cleanup
from inkscape_mcp.figure_kit import load_style
from inkscape_mcp.generators import registry
from inkscape_mcp.tools import live

SVG = "http://www.w3.org/2000/svg"
DOC = f"""<svg xmlns="{SVG}" xmlns:xlink="http://www.w3.org/1999/xlink"
 xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" viewBox="0 0 100 100">
 <defs id="defs">
  <linearGradient id="base"><stop offset="0" stop-color="#f00"/></linearGradient>
  <linearGradient id="used-grad" xlink:href="#base"/>
  <linearGradient id="unused-grad"><stop offset="0" stop-color="#00f"/></linearGradient>
  <marker id="arrow"><path id="arrow-path" d="M 0,0 L 1,1"/></marker>
  <clipPath id="clip"><use xlink:href="#shape"/></clipPath>
  <inkscape:path-effect id="lpe1" effect="spiro"/><inkscape:path-effect id="lpe2" effect="bspline"/>
  <inkscape:path-effect id="lpe-unused" effect="spiro"/>
  <filter id="blur"><feGaussianBlur stdDeviation="1"/></filter>
  <pattern id="orphan-pattern"><rect id="pr" width="1" height="1"/></pattern>
  <style id="css">.a {{ fill: red }}</style>
 </defs>
 <rect id="shape" width="10" height="10" style="fill:url(#used-grad);filter:url( '#blur' )"/>
 <path id="p" d="M 0,0 L 5,5" style="marker-end:url(#arrow)" inkscape:path-effect="#lpe1;#lpe2"/>
 <g id="clipped" clip-path="url(#clip)"><circle id="c" r="3"/></g>
 <g id="empty-outer"><g id="empty-inner"/></g>
 <rect id="hidden" style="display:none" width="1" height="1"/>
</svg>"""


@pytest.fixture
def cleaned():
    root = etree.fromstring(DOC.encode())
    report = svg_cleanup.prune(root)
    return root, report


def _ids(root):
    return {el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")}


def test_every_reference_kind_keeps_its_target(cleaned):
    root, _ = cleaned
    kept = _ids(root)
    # url(#) in style (with quotes and spaces), href chains, marker, clipPath via <use>, path-effect lists, <style>
    assert {"used-grad", "base", "blur", "arrow", "clip", "lpe1", "lpe2", "css"} <= kept


def test_unused_definitions_and_empty_groups_go(cleaned):
    root, report = cleaned
    assert not {"unused-grad", "lpe-unused", "orphan-pattern", "pr", "empty-outer", "empty-inner"} & _ids(root)
    assert report.defs_removed == 3
    assert report.empty_groups_removed == 2 and report.empty_group_ids == ["empty-outer"]  # outermost only


def test_hidden_elements_are_reported_not_removed(cleaned):
    root, report = cleaned
    assert "hidden" in _ids(root) and report.hidden_elements == 1


def test_clean_document_is_unchanged():
    root = etree.fromstring(DOC.encode())
    svg_cleanup.prune(root)
    once = etree.tostring(root)
    report = svg_cleanup.prune(root)
    assert not report.changed() and etree.tostring(root) == once


def test_cli_check_backup_and_write(tmp_path, capsys):
    f = tmp_path / "fig.svg"
    f.write_text(DOC)
    assert svg_cleanup.main(["--check", str(tmp_path)]) == 0
    assert f.read_text() == DOC and "3 unused defs" in capsys.readouterr().out
    assert svg_cleanup.main(["--backup", str(tmp_path / "orig"), str(f)]) == 0
    assert (tmp_path / "orig" / "fig.svg").read_text() == DOC
    assert "unused-grad" not in f.read_text() and "used-grad" in f.read_text()


def test_importer_prunes_leftovers(tmp_path):
    src = tmp_path / "x.svg"
    src.write_text(DOC)
    xml = registry.build("import_svg", {"id": "im", "path": str(src), "x": 0, "y": 0, "width": 10}, load_style())
    assert "unused-grad" not in xml and "orphan-pattern" not in xml and "im-used-grad" in xml


# ---- live op -------------------------------------------------------------------------


@pytest.fixture
def live_doc(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "_SCRATCH_DIR", tmp_path)
    state = {"xml": DOC}

    async def fake_export(bus, path):
        Path(path).write_text(state["xml"])

    monkeypatch.setattr(live, "_export_document_async", fake_export)
    invoke = AsyncMock(return_value=extension_bridge.ExchangeResult(success=True, data={"ok": True, "affected": 2}))
    monkeypatch.setattr(live, "invoke_extension", invoke)
    monkeypatch.setattr(live, "_save_active_document", lambda b, w: (False, ""))
    return state, invoke


async def test_cleanup_check_changes_nothing(live_doc):
    _, invoke = live_doc
    bus = MagicMock()
    res = await live._op_cleanup(bus, "cleanup", json.dumps({"check": True}), 1, time.perf_counter())
    assert res["success"] is True and "would remove" in res["message"] and "3 unused definition" in res["message"]
    bus.activate.assert_not_called()
    invoke.assert_not_awaited()


async def test_cleanup_vacuums_defs_and_removes_empty_groups(live_doc):
    _, invoke = live_doc
    bus = MagicMock()
    res = await live._op_cleanup(bus, "cleanup", "", 1, time.perf_counter())
    assert res["success"] is True
    bus.activate.assert_called_once_with("vacuum-defs", None, scope="window", window_id=1)
    ops = invoke.call_args.args[2]["ops"]
    assert [op["xpath"] for op in ops] == ["//*[@id='empty-outer']"]  # nested empties go with it
