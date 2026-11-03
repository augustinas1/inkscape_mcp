"""Figure-level live ops: selection without undo, save, labels, parked objects, edit renders, reload."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from lxml import etree

from inkscape_mcp import extension_bridge, selection_probe
from inkscape_mcp.figure_kit import load_style
from inkscape_mcp.generators import registry
from inkscape_mcp.generators.motifs import math_runs
from inkscape_mcp.tools import hot_reload, live, live_figure

SVG = "http://www.w3.org/2000/svg"
DOC = f"""<svg xmlns="{SVG}" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
 xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" sodipodi:docname="fig.svg"
 width="100" height="100" viewBox="0 0 100 100">
 <text id="src" x="5" y="20">Translation</text><text id="dst" x="70" y="20">Protein</text>
 <path id="arr" d="M 30,18 H 65" style="stroke:#333;marker-end:url(#m)"/>
 <g id="grp"><text id="inner" x="5" y="60">Cell growth</text></g>
 <rect id="away" x="300" y="10" width="5" height="5"/>
</svg>"""
BOXES = {
    "src": (5, 12, 28, 21),
    "dst": (70, 12, 90, 21),
    "arr": (30, 17, 65, 19),
    "inner": (5, 52, 30, 61),
    "grp": (5, 52, 30, 61),
    "away": (300, 10, 305, 15),
}


# ---- selection probe ------------------------------------------------------------------------


def test_select_list_output_is_parsed():
    out = "r1 cloned: false ref: 1 href: 0 total href: 0\nnoise\ng2 cloned: false ref: 1 href: 0 total href: 0\n"
    assert selection_probe.parse(out) == ["r1", "g2"]


def test_selection_is_read_from_inkscapes_output(tmp_path, monkeypatch):
    log = tmp_path / "out.log"
    log.write_text("earlier output\n")
    monkeypatch.setattr(selection_probe, "OUTPUT_LOG", log)
    monkeypatch.setattr(selection_probe, "_PID_FILE", tmp_path / "out.pid")
    bus = MagicMock()
    assert selection_probe.selected_ids(bus) is None  # not launched by us: no capture
    selection_probe.record_launch(os.getpid())

    def activate(*args, **kwargs):
        with log.open("a") as fh:
            fh.write("a1 cloned: false ref: 1 href: 0 total href: 0\n")

    bus.activate.side_effect = activate
    assert selection_probe.selected_ids(bus) == ["a1"]  # only what select-list printed now
    bus.activate.assert_called_with("select-list", None, scope="app")


# ---- labels ---------------------------------------------------------------------------------


def test_labels_name_texts_groups_and_arrows_by_what_they_connect():
    labels = live_figure.propose_labels(etree.fromstring(DOC.encode()), BOXES)
    assert labels["arr"] == "arrow: Translation → Protein"
    assert labels["src"] == "Translation" and labels["grp"] == "Cell growth"
    assert "away" not in labels  # a plain shape: nothing to say about it


def test_ids_touched_by_an_edit():
    ops = [
        {"xpath": "//*[@id='a']", "action": "set_attr"},
        {"xpath": "/svg:svg", "action": "append", "xml": '<g id="new"><rect id="x"/></g>'},
        {"xpath": "//*[@id='a']", "action": "remove"},
    ]
    assert live_figure.ids_touched(ops) == ["a", "new"]


# ---- ops with a fake live document -----------------------------------------------------------


@pytest.fixture
def doc(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "_SCRATCH_DIR", tmp_path)
    monkeypatch.setattr(live_figure, "DOC_PATHS", tmp_path / "paths.json")
    monkeypatch.setattr(live_figure, "BACKUP_DIR", tmp_path / "backups")

    async def fake_export(bus, path):
        Path(path).write_text(DOC)

    async def fake_query(exe, svg_path, svg_bytes):
        return dict(BOXES)

    monkeypatch.setattr(live, "_export_document_async", fake_export)
    monkeypatch.setattr(live, "_get_bus", lambda config=None: MagicMock())
    monkeypatch.setattr(live_figure, "query_all", fake_query)
    invoke = AsyncMock(return_value=extension_bridge.ExchangeResult(success=True, data={"ok": True, "affected": 1}))
    monkeypatch.setattr(live, "invoke_extension", invoke)
    monkeypatch.setattr(live, "_save_active_document", lambda b, w: (False, ""))
    return invoke


async def test_selection_returns_ids_outlines_and_a_render(doc, monkeypatch, tmp_path):
    async def ids(bus):
        return ["src", "grp"], "select-list"

    async def fake_render(exe, path, data, wanted, stem, boxes=None):
        return tmp_path / "sel.png", {i: BOXES[i] for i in wanted}

    monkeypatch.setattr(live_figure, "selected_ids", ids)
    monkeypatch.setattr(live_figure, "render_ids", fake_render)
    res = await live_figure.op_selection(MagicMock(), "selection", "", MagicMock(), time.perf_counter())
    assert res["data"]["ids"] == ["src", "grp"] and res["data"]["via"] == "select-list"
    assert "src  text  'Translation'" in res["data"]["outline"] and "inner" in res["data"]["outline"]
    assert res["data"]["png"].endswith("sel.png")
    doc.assert_not_awaited()  # reading the selection runs no extension


async def test_save_writes_the_known_file_with_a_backup(doc, tmp_path):
    target = tmp_path / "fig.svg"
    target.write_text("old")
    res = await live_figure.op_save(MagicMock(), "save", "", time.perf_counter())
    assert not res["success"] and "don't know which file" in res["message"]
    live_figure.remember_path(str(target))
    res = await live_figure.op_save(MagicMock(), "save", "", time.perf_counter())
    assert res["success"] and target.read_text() == DOC
    assert Path(res["data"]["backup"]).read_text() == "old"


async def test_label_check_lists_and_apply_is_one_batch(doc):
    res = await live_figure.op_label(
        MagicMock(), "label", json.dumps({"check": True}), 1, MagicMock(), time.perf_counter()
    )
    assert res["success"] and "arrow: Translation → Protein" in res["message"]
    doc.assert_not_awaited()
    res = await live_figure.op_label(MagicMock(), "label", "", 1, MagicMock(), time.perf_counter())
    ops = doc.call_args.args[2]["ops"]
    assert res["success"] and {"xpath": "//*[@id='arr']", "action": "set_attr", "name": "inkscape:label",
                               "value": "arrow: Translation → Protein"} in ops  # fmt: skip


async def test_parked_lists_and_deletes_off_page_objects(doc):
    res = await live_figure.op_parked(MagicMock(), "parked", "", 1, MagicMock(), time.perf_counter())
    assert [p[0] for p in res["data"]["parked"]] == ["away"]
    doc.assert_not_awaited()
    res = await live_figure.op_parked(
        MagicMock(), "parked", json.dumps({"delete": True}), 1, MagicMock(), time.perf_counter()
    )
    assert res["success"] and doc.call_args.args[2]["ops"] == [{"xpath": "//*[@id='away']", "action": "remove"}]


async def test_edit_with_render_returns_a_before_after_picture(doc, monkeypatch):
    seen = {}

    async def fake_before_after(config, before, ids):
        seen["ids"] = ids
        return {"render": "/tmp/edit.png"}

    monkeypatch.setattr(live_figure, "before_after", fake_before_after)
    payload = {
        "ops": [{"xpath": "//*[@id='arr']", "action": "set_attr", "name": "d", "value": "M 0,0"}],
        "render": True,
    }
    res = await live._op_edit_xml(MagicMock(), "edit_xml", "", json.dumps(payload), 1, time.perf_counter())
    assert res["success"] and res["data"]["render"] == "/tmp/edit.png" and seen["ids"] == ["arr"]
    assert "render" not in doc.call_args.args[2]  # the flag is not passed to the extension


# ---- snapshot reuse, reload, unknown operations ---------------------------------------------


async def test_read_only_calls_share_one_export(monkeypatch, tmp_path):
    calls = []

    def trigger(bus, path):
        calls.append(path)
        Path(path).write_text(DOC)

    monkeypatch.setattr(live, "_trigger_export", trigger)
    live._SNAPSHOT_CACHE.clear()
    token = live._SNAPSHOT_REUSE.set(True)
    try:
        await live._export_document_async(MagicMock(), tmp_path / "a.svg")
        await live._export_document_async(MagicMock(), tmp_path / "b.svg")
        assert len(calls) == 1 and (tmp_path / "b.svg").read_text() == DOC
        live._SNAPSHOT_REUSE.set(False)
        await live._export_document_async(MagicMock(), tmp_path / "c.svg")
        assert len(calls) == 2  # an editing operation always exports afresh
    finally:
        live._SNAPSHOT_REUSE.reset(token)
        live._SNAPSHOT_CACHE.clear()


def test_changed_modules_are_reloaded(monkeypatch):
    entry = hot_reload.live_entry()
    assert entry is live.inkscape_live
    reloaded = []
    monkeypatch.setattr(hot_reload.importlib, "reload", lambda m: reloaded.append(m.__name__) or m)
    monkeypatch.setattr(hot_reload, "_mtime", lambda name: 1e12)  # every file "changed"
    hot_reload.live_entry()
    assert reloaded == list(hot_reload.RELOADABLE)  # dependencies first, tools.live last
    hot_reload._loaded_at.clear()


async def test_unknown_operation_lists_the_valid_ones(monkeypatch):
    monkeypatch.setattr(live, "_get_bus", lambda config=None: MagicMock(is_available=lambda: True))
    res = await live.inkscape_live("frobnicate", window_id=0)
    assert not res["success"] and "valid:" in res["message"] and "transplant" in res["message"]


# ---- maths labels ---------------------------------------------------------------------------


def test_math_runs_follow_the_figure_convention():
    assert math_runs("G_on") == [("italic", "G"), ("sub", "on")]
    assert math_runs("mRNA") == [("plain", "mRNA")]
    assert math_runs("N_{0}+1") == [("italic", "N"), ("sub", "0"), ("plain", "+1")]


def test_math_label_writes_italic_variables_and_upright_subscripts():
    xml = registry.build("math_label", {"id": "m", "x": 1, "y": 2, "text": "k_off = 3"}, load_style())
    el = etree.fromstring(xml.replace("<text ", f'<text xmlns="{SVG}" xmlns:inkscape="i" ', 1))
    spans = el.findall(f"{{{SVG}}}tspan")
    assert [s.get("style") for s in spans] == ["font-style:italic", "font-size:65%;baseline-shift:sub"]
    assert "font-family:Arial" in el.get("style") and el.get("{i}label") == "koff = 3"


# ---- organize -------------------------------------------------------------------------------


def test_flatten_folds_plain_wrappers_and_keeps_meaningful_groups():
    root = etree.fromstring(
        f'<svg xmlns="{SVG}" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape">'
        '<g id="w1" transform="translate(10,0)" style="fill:#f00"><g id="w2" transform="scale(2)">'
        '<rect id="r" width="1" height="1" fill="#00f"/></g></g>'
        '<g id="clipped" clip-path="url(#c)"><rect id="r2" width="1" height="1"/></g>'
        '<g id="named" inkscape:label="legend"><rect id="r3" width="1" height="1"/></g></svg>'.encode()
    )
    assert live_figure.flatten(root) == 2
    r = root.find(".//*[@id='r']")
    assert r.getparent() is root and r.get("transform") == "matrix(2,0,0,2,10,0)"
    assert "fill" not in (r.get("style") or "")  # its own fill attribute outranks the inherited one
    assert root.find(".//*[@id='clipped']") is not None and root.find(".//*[@id='named']") is not None


@pytest.mark.parametrize("differing, applied", [(0, True), (12, False)])
async def test_organize_applies_only_when_the_render_is_identical(doc, monkeypatch, differing, applied):
    async def fake_identical(exe, a, b):
        return differing

    monkeypatch.setattr(live_figure, "_identical", fake_identical)
    payload = {"layers": [{"label": "Panel A", "area": [0, 0, 100, 100]}], "labels": False}
    res = await live_figure.op_organize(
        MagicMock(), "organize", json.dumps(payload), 1, MagicMock(), time.perf_counter()
    )
    assert res["success"] is applied
    if applied:
        layer = etree.fromstring(doc.call_args.args[2]["ops"][-1]["xml"])
        assert layer.get("{http://www.inkscape.org/namespaces/inkscape}label") == "Panel A"
        # "grp" was a plain wrapper, folded into its text; "away" lies outside the layer's area.
        assert {c.get("id") for c in layer} == {"src", "dst", "arr", "inner"}
    else:
        assert "12 pixel(s) would change" in res["message"]
        doc.assert_not_awaited()
