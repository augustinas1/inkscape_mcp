"""figure_kit: pre-styled components, style tokens, and the `component` live op."""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from lxml import etree

from inkscape_mcp import extension_bridge, figure_kit
from inkscape_mcp.tools import live

SVG = "http://www.w3.org/2000/svg"
INK = "http://www.inkscape.org/namespaces/inkscape"


def _parse(fragment: str):
    return etree.fromstring(f'<svg xmlns="{SVG}" xmlns:inkscape="{INK}">{fragment}</svg>'.encode())[0]


@pytest.fixture
def style():
    return figure_kit.load_style()


@pytest.mark.parametrize(
    ("kind", "params"),
    [
        ("arrow", {"x1": 0, "y1": 0, "x2": 10, "y2": 0, "symbol": "\u03c1"}),
        ("gene", {"x": 25, "y": 50, "state": "on"}),
        ("gene", {"x": 25, "y": 20, "state": "off"}),
        ("rna_strand", {"x": 104, "y": 82}),
        ("protein", {"cx": 117, "cy": 111}),
        ("decay", {"x": 150, "y": 80, "species": "rna"}),
        ("decay", {"x": 150, "y": 80, "species": "protein"}),
    ],
)
def test_every_component_is_well_formed_and_fully_named(style, kind, params):
    group = _parse(figure_kit.build(kind, {"id": "c", **params}, style))
    assert group.get("id") == "c" and group.get(f"{{{INK}}}label")
    ids = [el.get("id") for el in group.iter() if el.get("id")]
    assert len(ids) == len(set(ids)), "duplicate ids"
    for el in group.iter():
        if etree.QName(el).localname in {"path", "rect", "circle", "text", "g"}:
            assert el.get("id", "").startswith("c") and el.get(f"{{{INK}}}label"), etree.tostring(el)[:80]
    # Restrained style: no opacity, gradients or filters anywhere.
    text = etree.tostring(group).decode()
    assert "opacity" not in text and "Gradient" not in text and "filter" not in text


def test_rna_strand_matches_the_house_style(style):
    group = _parse(figure_kit.rna_strand("m", 104, 82, style))
    strand = group.find(".//*[@id='m-strand']").get("style")
    assert "stroke:#d1495b" in strand and "stroke-width:0.9" in strand and "stroke-linecap:round" in strand
    bases = group.find(".//*[@id='m-bases']").get("d")
    assert bases.count("M ") == 24 and all(seg.strip().endswith("v 1.8") for seg in bases.split("M ")[1:])
    assert "".join(group.find(".//*[@id='m-label']").itertext()) == "mRNA (M)"


def test_arrow_uses_the_stock_marker_and_italic_label(style):
    group = _parse(figure_kit.arrow("a", 77, 82, 95, 82, style, symbol="\u03c3", sub="on"))
    assert "marker-end:url(#Triangle)" in group.find(".//*[@id='a-arrow']").get("style")
    label = group.find(".//*[@id='a-label']")
    assert "font-style:italic" in label.get("style") and "font-family:Arial" in label.get("style")
    assert float(label.get("y")) < 82  # a rightward arrow's "left" is above it
    assert label[0].text == "on" and "font-style:normal" in label[0].get("style")


def test_decay_tints_are_opaque_and_fade(style):
    group = _parse(figure_kit.decay("d", 150, 80, style, species="rna"))
    fills = [el.get("style").split("fill:")[1][:7] for el in group.iter() if el.get("id", "").startswith("d-unit")]
    lightness = [sum(int(f[i : i + 2], 16) for i in (1, 3, 5)) for f in fills]
    assert len(lightness) == 5 and all(a < b for a, b in itertools.pairwise(lightness))


def test_style_file_overrides_tokens(tmp_path):
    path = tmp_path / "style.json"
    path.write_text(json.dumps({"species": {"rna": {"color": "#123456"}}, "stroke": {"strand": 1.2}}))
    style = figure_kit.load_style(path)
    assert style["species"]["rna"] == {"color": "#123456", "pale": "#df808c"}  # deep merge keeps "pale"
    strand = _parse(figure_kit.rna_strand("m", 0, 0, style)).find(".//*[@id='m-strand']").get("style")
    assert "stroke:#123456" in strand and "stroke-width:1.2" in strand
    assert figure_kit.DEFAULT_STYLE["species"]["rna"]["color"] == "#d1495b"  # defaults untouched


def test_protein_mirror_flips_the_fold(style):
    plain = _parse(figure_kit.protein("p", 100, 100, style)).find(".//*[@id='p-chain']").get("d")
    mirrored = _parse(figure_kit.protein("p", 100, 100, style, mirror=True)).find(".//*[@id='p-chain']").get("d")
    assert plain.startswith("M 92.7,104.7") and mirrored.startswith("M 107.3,104.7")


@pytest.mark.parametrize(
    ("kind", "params", "error"),
    [
        ("teapot", {"id": "x"}, "unknown generator"),
        ("arrow", {"x1": 0, "y1": 0, "x2": 1, "y2": 1}, "params.id is required"),
        ("gene", {"id": "g", "x": 0, "y": 0, "colour": "red"}, r"gene: unknown param\(s\) \[.colour.\]"),
    ],
)
def test_build_errors(style, kind, params, error):
    with pytest.raises(ValueError, match=error):
        figure_kit.build(kind, params, style)


# ---- the live op ---------------------------------------------------------------------


@pytest.fixture
def live_doc(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "_SCRATCH_DIR", tmp_path)
    doc = {"xml": f'<svg xmlns="{SVG}"><defs id="defs1"/><g id="layer1"/></svg>'}

    async def fake_export(bus, path):
        Path(path).write_text(doc["xml"])

    monkeypatch.setattr(live, "_export_document_async", fake_export)
    invoke = AsyncMock(return_value=extension_bridge.ExchangeResult(success=True, data={"ok": True, "affected": 2}))
    monkeypatch.setattr(live, "invoke_extension", invoke)
    monkeypatch.setattr(live, "_save_active_document", lambda bus, w: (False, "skipped"))
    return doc, invoke


async def _component(target="", **payload):
    return await live._op_component(MagicMock(), "component", target, json.dumps(payload), 1, time.perf_counter())


async def test_component_without_destination_only_returns_xml(live_doc):
    _, invoke = live_doc
    res = await _component(kind="rna_strand", params={"id": "m2", "x": 10, "y": 10})
    assert res["success"] is True and res["data"]["xml"].startswith('<g id="m2"')
    invoke.assert_not_awaited()


async def test_inserting_an_arrow_adds_the_missing_marker_after_it(live_doc):
    _, invoke = live_doc
    res = await _component(kind="arrow", params={"id": "a", "x1": 0, "y1": 0, "x2": 9, "y2": 0}, into="layer1")
    assert res["success"] is True
    ops = invoke.call_args.args[2]["ops"]
    assert [op["action"] for op in ops] == ["append", "append"]
    assert ops[0]["xpath"] == "//*[@id='layer1']" and ops[1]["xpath"] == "/svg:svg/svg:defs[1]"
    assert 'id="Triangle"' in ops[1]["xml"]


async def test_marker_not_duplicated_when_present(live_doc):
    doc, invoke = live_doc
    doc["xml"] = f'<svg xmlns="{SVG}"><defs><marker id="Triangle"/></defs><g id="old"/></svg>'
    res = await _component("old", kind="arrow", params={"id": "a", "x1": 0, "y1": 0, "x2": 9, "y2": 0})
    assert res["success"] is True
    ops = invoke.call_args.args[2]["ops"]
    assert len(ops) == 1 and ops[0] == {
        "xpath": "//*[@id='old']",
        "action": "replace",
        "xml": ops[0]["xml"],
    }


async def test_style_kind_reports_tokens(live_doc, tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"ink": "#000000"}')
    res = await _component(kind="style", style=str(path))
    assert res["success"] is True and res["data"]["style"]["ink"] == "#000000"


async def test_bad_component_is_reported(live_doc):
    res = await _component(kind="teapot", params={"id": "x"})
    assert res["success"] is False and "unknown generator" in res["message"]


# ---- stale-server detection (ping) ---------------------------------------------------


def test_ping_flags_a_stale_server(monkeypatch, tmp_path):
    (tmp_path / "mod.py").write_text("x = 1")
    monkeypatch.setattr(live, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(live, "_SERVER_STARTED", time.time() + 60)
    assert "stale" not in live._server_freshness()
    monkeypatch.setattr(live, "_SERVER_STARTED", time.time() - 60)
    fresh = live._server_freshness()
    assert fresh["stale"] is True and "restart" in fresh["stale_note"]
    bus = MagicMock(is_available=lambda: True, list_windows=lambda: [1], list_actions=lambda *a: [])
    monkeypatch.setattr(live, "_has_window_scope", lambda b: True)
    res = live._op_ping(bus, "ping", time.perf_counter())
    assert res["data"]["stale"] is True and "WARNING" in res["message"]
