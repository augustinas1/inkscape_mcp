"""Print-size measurements and style-rule checks of a figure."""

from __future__ import annotations

import json
import time

import pytest

from inkscape_mcp.figure_measure import measure, report
from inkscape_mcp.tools import live_figure

NS = 'xmlns="http://www.w3.org/2000/svg"'


def svg(body: str, head: str = 'width="100mm" height="50mm" viewBox="0 0 100 50"') -> bytes:
    return f"<svg {NS} {head}>{body}</svg>".encode()


def check(result: dict, code: str, nth: int = 0) -> dict:
    return [c for c in result["checks"] if c["code"] == code][nth]


def sizes(result: dict) -> list[float]:
    return sorted(s["pt"] for s in result["texts"]["styles"])


def test_mm_document_with_mm_viewbox():
    r = measure(svg('<text x="1" y="5" style="font-size:3.5277778px">A</text>'))
    assert r["page_mm"] == [100, 50]
    assert sizes(r) == [10.0]  # 3.5278 mm = 10 pt


def test_mm_width_with_px_viewbox():
    head = 'width="187.7mm" height="72mm" viewBox="0 0 709.5 272.2"'
    r = measure(svg('<text x="1" y="5" style="font-size:13.333333px">A</text>', head), rules=None)
    assert r["pt_per_unit"] == pytest.approx(0.75, abs=0.001)
    assert sizes(r) == [10.0]


def test_tspan_size_transform_scale_and_bold():
    body = (
        '<g transform="scale(2)"><text id="t" x="1" y="5" style="font-family:\'Calibri\',sans-serif">'
        '<tspan style="font-size:1.7638889px;font-weight:700">Hi</tspan></text></g>'
    )
    r = measure(svg(body))
    style = r["texts"]["styles"][0]
    assert (style["family"], style["weight"], style["pt"]) == ("Calibri", "bold", 10.0)


def test_near_duplicate_colours_group_and_distinct_ones_do_not():
    body = "".join(
        f'<rect width="5" height="5" fill="{c}"/>' for c in ("#70cbcf", "#6fcccf", "#6FCCCF", "#fff", "#cc2222")
    )
    r = measure(svg(body))["colors"]
    assert r["fills"]["#ffffff"] == 1  # 3-digit hex normalised
    assert r["fills"]["#6fcccf"] == 2
    assert len(r["near_duplicates"]) == 1
    assert {c for c, _ in r["near_duplicates"][0]} == {"#70cbcf", "#6fcccf"}


def test_hue_families_separate_from_greys():
    body = "".join(
        f'<rect width="5" height="5" fill="{c}"/>' for c in ("#cc3333", "#d94040", "#3366cc", "#808080", "#cccccc")
    )
    r = measure(svg(body))["colors"]
    assert len(r["hue_families"]) == 2
    assert {c for c, _ in r["greys"]} == {"#808080", "#cccccc"}
    r = measure(svg(body), rules={"max_hue_families": 1})
    assert not check(r, "P03")["ok"] and check(r, "P03")["warn"]  # extra hues may be categories


def texts(*items: tuple[str, float, float]) -> bytes:
    return svg("".join(f'<text id="{i}" x="{x}" y="{y}" style="font-size:3px">{i}</text>' for i, x, y in items))


def test_alignment_near_miss_is_flagged_once_on_the_minority():
    r = measure(texts(("a", 10, 5), ("b", 10, 15), ("c", 10, 25), ("d", 10.2, 35)))
    found = r["alignment"]["texts"]
    assert [(f["id"], f["dx_mm"]) for f in found] == [("d", 0.2)]
    assert not check(r, "P02")["ok"]


def test_exact_match_and_clearly_different_x_are_not_flagged():
    r = measure(texts(("a", 10, 5), ("b", 10, 15), ("c", 10.01, 25), ("d", 12, 35)))
    assert r["alignment"]["texts"] == []
    assert check(r, "P02")["ok"]


def test_large_rect_near_equal_sizes():
    body = '<rect id="p" width="40" height="20"/><rect id="q" width="40.3" height="20" y="25"/>'
    r = measure(svg(body))
    assert [(a["id"], a["dim"]) for a in r["alignment"]["rects"]] == [("q", "width")]


def test_arrow_weights_use_transform_scale_and_only_warn():
    head = 'width="100mm" height="50mm" viewBox="0 0 283.4646 141.7323"'  # 1 uu = 1 pt
    body = (
        '<path id="thin" d="M0 0H5" stroke="#000" stroke-width="0.1"/>'  # no marker: not an arrow
        '<g transform="scale(2)"><path id="arr" d="M0 0H5" style="stroke:#000;stroke-width:0.2;'
        'marker-end:url(#m)"/></g>'
    )
    r = measure(svg(body, head))
    assert check(r, "P07")["ok"] and "1 arrows 0.4-0.4 pt" in check(r, "P07")["text"]
    r = measure(svg(body.replace("0.2;", "0.8;"), head))  # 1.6 pt: a big connecting arrow, perhaps
    found = check(r, "P07")
    assert not found["ok"] and found["warn"] and "arr 1.6" in found["text"]
    assert "P07 ⚠" in report(r)


def test_panel_outlines_at_least_one_point():
    head = 'width="100mm" height="50mm" viewBox="0 0 283.4646 141.7323"'
    body = '<rect id="panel" width="200" height="80" rx="10" fill="none" stroke="#999" stroke-width="{w}"/>'
    assert check(measure(svg(body.format(w=1.4), head)), "P07")["ok"]
    found = check(measure(svg(body.format(w=0.5), head)), "P07")
    assert not found["ok"] and not found.get("warn") and "panel 0.5" in found["text"]
    pill = body.replace('height="80"', 'height="14"')  # about 5 mm tall: a label pill, not a panel
    assert not [c for c in measure(svg(pill.format(w=0.5), head))["checks"] if "panel" in c["text"]]


def test_font_names_ignore_case():
    body = (
        '<text x="1" y="5" style="font-family:Calibri">a</text><text x="1" y="9" style="font-family:calibri">b</text>'
    )
    found = check(measure(svg(body)), "P04")
    assert found["ok"] and "Calibri only (2)" in found["text"]


def test_font_and_size_rules():
    body = (
        '<text id="a" x="1" y="5" style="font-family:Calibri;font-size:1.8px">x</text>'
        '<text id="b" x="1" y="25" style="font-family:Helvetica Neue;font-size:3.5278px">y</text>'
    )
    r = measure(svg(body))
    assert [c["ok"] for c in r["checks"] if c["code"] == "P04"] == [False, False, False]
    assert "Helvetica Neue (1)" in check(r, "P04")["text"]
    assert "ids a" in check(r, "P04", 1)["text"]  # 1.8 mm = 5.1 pt
    assert "✗" in report(r)


def test_root_id_restricts_and_unknown_raises():
    body = '<g id="g"><text id="in" x="1" y="5" style="font-size:3px">in</text></g><text x="1" y="9">out</text>'
    assert measure(svg(body), "g")["texts"]["count"] == 1
    assert measure(svg(body))["texts"]["count"] == 2
    with pytest.raises(KeyError):
        measure(svg(body), "nope")


@pytest.mark.asyncio
async def test_live_op_measures_a_file_without_a_bus(tmp_path):
    path = tmp_path / "f.svg"
    path.write_bytes(texts(("a", 10, 5)))
    payload = json.dumps({"path": str(path), "json": True, "rules": {"family": "X"}})
    res = await live_figure.op_measure(None, "measure", "", payload, time.perf_counter())
    assert res["success"] and "P04" in res["message"]
    assert res["data"]["measurements"]["texts"]["count"] == 1
    bad = await live_figure.op_measure(None, "measure", "", json.dumps({"path": str(tmp_path / "no.svg")}), 0.0)
    assert not bad["success"]
