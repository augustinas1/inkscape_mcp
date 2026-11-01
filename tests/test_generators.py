"""Generator registry and the scatter / organic / flow generators."""

from __future__ import annotations

import itertools
import json
import math
import random
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from lxml import etree

from inkscape_mcp import extension_bridge
from inkscape_mcp.figure_kit import load_style
from inkscape_mcp.generators import catalogue as catalogue_mod
from inkscape_mcp.generators import common, flow, organic, registry, scatter, tiling
from inkscape_mcp.tools import live

SVG = "http://www.w3.org/2000/svg"
INK = "http://www.inkscape.org/namespaces/inkscape"
REGION = {"x": 10, "y": 20, "width": 60, "height": 40}


@pytest.fixture
def style():
    return load_style()


def _parse(fragment: str):
    return etree.fromstring(f'<svg xmlns="{SVG}" xmlns:inkscape="{INK}">{fragment}</svg>'.encode())[0]


def _numbers(d: str) -> list[float]:
    return [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", d)]


# ---- registry ------------------------------------------------------------------------


def test_catalogue_lists_every_category_with_schemas():
    entries = registry.catalogue()
    names = {e["name"] for e in entries}
    assert {"arrow", "gene", "rna_strand", "protein", "decay"} <= names
    assert {"scatter_even", "scatter_grid", "scatter_clusters", "blob", "chain", "wobble_line", "flow_lines"} <= names
    flow_entry = next(e for e in entries if e["name"] == "flow_lines")
    field = next(p for p in flow_entry["params"] if p["name"] == "field")
    assert flow_entry["seeded"] is True and field["kind"] == "choice" and "vortex" in field["choices"]
    assert all(e["doc"] for e in entries), "every generator needs a one-line doc"
    assert registry.catalogue("flow") == [flow_entry]


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({}, "params.id is required"),
        ({"id": "s", "x": 0, "y": 0, "width": 10}, "missing required param 'height'"),
        ({"id": "s", **REGION, "spacing": 0}, r"spacing must be within \[0.2, 200\]"),
        ({"id": "s", **REGION, "mark": "star"}, "mark must be one of"),
        ({"id": "s", **REGION, "periodic": "yes"}, "periodic must be bool"),
        ({"id": "s", **REGION, "colour": "red"}, r"unknown param\(s\) \['colour'\]"),
    ],
)
def test_registry_validates_params(style, params, error):
    with pytest.raises(ValueError, match=error):
        registry.build("scatter_even", params, style)


def test_ints_accepted_for_floats_and_seed_is_deterministic(style):
    a = registry.build("scatter_even", {"id": "s", **REGION}, style, seed=7)
    assert a == registry.build("scatter_even", {"id": "s", **REGION}, style, seed=7)
    assert a != registry.build("scatter_even", {"id": "s", **REGION}, style, seed=8)


def test_colour_tokens_resolve(style):
    assert common.color(style, "species.rna.color") == "#d1495b"
    assert common.color(style, "#123456") == "#123456"
    with pytest.raises(ValueError, match="unknown colour token"):
        common.color(style, "species.dna.color")


@pytest.mark.parametrize("name", ["scatter_even", "scatter_grid", "scatter_clusters", "flow_lines"])
def test_region_generators_are_named_and_unique(style, name):
    group = _parse(registry.build(name, {"id": "g", **REGION}, style, seed=1))
    ids = [el.get("id") for el in group.iter() if el.get("id")]
    assert len(ids) == len(set(ids)) and all(el.get(f"{{{INK}}}label") for el in group.iter() if el.get("id"))


# ---- scatter -------------------------------------------------------------------------


def test_poisson_disc_keeps_its_spacing_and_fills_the_region():
    pts = scatter.poisson_disc(50, 40, 3.0, random.Random(1))
    closest = min(common.dist(a, b) for a, b in itertools.combinations(pts, 2))
    assert closest >= 3.0
    assert len(pts) > 50 * 40 / (3.0**2 * 2)  # reasonably dense, not sparse
    assert all(0 <= x < 50 and 0 <= y < 40 for x, y in pts)


def test_periodic_poisson_disc_respects_spacing_across_edges():
    pts = scatter.poisson_disc(30, 30, 3.0, random.Random(2), periodic=True)
    for a, b in itertools.combinations(pts, 2):
        dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
        assert math.hypot(min(dx, 30 - dx), min(dy, 30 - dy)) >= 3.0


def test_marks_stay_inside_the_region(style):
    d = _parse(registry.build("scatter_even", {"id": "s", **REGION, "size": 2}, style, 3)).find(".//*[@id='s-marks']")
    xs = [float(v) for v in re.findall(r"M (-?[\d.]+),", d.get("d"))]
    assert min(xs) >= 10 - 1e-6 and max(xs) + 2 <= 70 + 1e-6  # each dot spans x .. x+2r


# ---- organic -------------------------------------------------------------------------


def test_chain_gaps_its_crossings_and_fits_its_surface(style):
    group = _parse(registry.build("chain", {"id": "c", "cx": 50, "cy": 50}, style, seed=2))
    assert group.find(".//*[@id='c-gap1']") is not None and group.find(".//*[@id='c-over1']") is not None
    chain_pts = _numbers(group.find(".//*[@id='c-chain']").get("d"))
    surf = _numbers(group.find(".//*[@id='c-surface']").get("d"))
    # The surface is centred on the fold, not on the requested centre after rotation.
    assert abs(sum(surf[0::2]) / len(surf[0::2]) - sum(chain_pts[0::2]) / len(chain_pts[0::2])) < 3


def test_crossings_finds_a_simple_x():
    pts = [(0, 0), (10, 10), (20, 20), (20, 0), (10, 10.5), (0, 20)]
    assert organic.crossings(pts, skip=1)


def test_blob_without_fill_is_an_outline(style):
    out = _parse(registry.build("blob", {"id": "b", "cx": 0, "cy": 0, "fill": "none", "stroke": "ink"}, style, 1))
    assert "fill:none;stroke:#333333" in out.find(".//*[@id='b-outline']").get("style")


def test_wobble_line_keeps_its_endpoints(style):
    d = _parse(registry.build("wobble_line", {"id": "w", "x1": 0, "y1": 5, "x2": 40, "y2": 5}, style, 1))
    nums = _numbers(d.find(".//*[@id='w-line']").get("d"))
    assert nums[:2] == pytest.approx([0, 5], abs=1e-6) and nums[-2:] == pytest.approx([40, 5], abs=1e-6)


# ---- flow ----------------------------------------------------------------------------


@pytest.mark.parametrize("field", flow.FIELDS)
def test_streamlines_keep_apart_and_stay_in_region(field):
    rng = random.Random(4)
    vec = flow.make_field(field, 0, 0, 40, 30, 20, 30, rng)
    lines = flow.streamlines(vec, 0, 0, 40, 30, 2.0, 0.5, 4.0, rng)
    assert lines, field
    pts = [(p, i) for i, line in enumerate(lines) for p in line]
    assert all(0 <= p[0] <= 40 and 0 <= p[1] <= 30 for p, _ in pts)
    # Points on different lines are never closer than the test distance (minus one step of slack).
    by_cell: dict = {}
    for p, i in pts:
        by_cell.setdefault((int(p[0] // 2), int(p[1] // 2)), []).append((p, i))
    for (cx, cy), members in by_cell.items():
        near = [m for dx in (-1, 0, 1) for dy in (-1, 0, 1) for m in by_cell.get((cx + dx, cy + dy), [])]
        for p, i in members:
            assert all(common.dist(p, q) >= 1.0 - 0.4 for q, j in near if j != i)


def test_flow_lines_has_no_tiny_loops(style):
    lines = flow.streamlines(flow.make_field("vortex", 0, 0, 40, 40, 20, 0, random.Random(1)), 0, 0, 40, 40, 2.0, 0.5,
                             4.0, random.Random(1))  # fmt: skip
    for line in lines:
        xs, ys = [p[0] for p in line], [p[1] for p in line]
        assert max(max(xs) - min(xs), max(ys) - min(ys)) >= 3.0


# ---- live op -------------------------------------------------------------------------


@pytest.fixture
def live_doc(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "_SCRATCH_DIR", tmp_path)

    async def fake_export(bus, path):
        Path(path).write_text(f'<svg xmlns="{SVG}"><defs/><g id="layer1"/></svg>')

    monkeypatch.setattr(live, "_export_document_async", fake_export)
    invoke = AsyncMock(return_value=extension_bridge.ExchangeResult(success=True, data={"ok": True, "affected": 1}))
    monkeypatch.setattr(live, "invoke_extension", invoke)
    monkeypatch.setattr(live, "_save_active_document", lambda bus, w: (False, "skipped"))
    return invoke


async def _component(**payload):
    return await live._op_component(MagicMock(), "component", "", json.dumps(payload), 1, time.perf_counter())


async def test_catalogue_kind(live_doc):
    res = await _component(kind="catalogue", category="flow")
    assert res["success"] is True and [g["name"] for g in res["data"]["generators"]] == ["flow_lines"]


async def test_seed_is_passed_through(live_doc):
    a = await _component(kind="chain", params={"id": "c", "cx": 0, "cy": 0}, seed=5)
    b = await _component(kind="chain", params={"id": "c", "cx": 0, "cy": 0}, seed=5)
    c = await _component(kind="chain", params={"id": "c", "cx": 0, "cy": 0}, seed=6)
    assert a["data"]["xml"] == b["data"]["xml"] != c["data"]["xml"]


async def test_bad_seed_rejected(live_doc):
    res = await _component(kind="chain", params={"id": "c", "cx": 0, "cy": 0}, seed="x")
    assert res["success"] is False and "seed must be an integer" in res["message"]


# ---- tiling --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p", "q", "expected"),
    [
        ((-5, 5), (15, 5), ((0, 5), (10, 5))),  # crosses both sides
        ((2, 2), (8, 8), ((2, 2), (8, 8))),  # fully inside
        ((-5, -5), (-1, 20), None),  # fully outside
    ],
)
def test_clip_segment(p, q, expected):
    got = tiling.clip_segment(p, q, 0, 0, 10, 10)
    if expected is None:
        assert got is None
    else:
        assert [v for pt in got for v in pt] == pytest.approx([v for pt in expected for v in pt])


def _endpoints(d: str) -> list[tuple[float, float]]:
    nums = _numbers(re.sub(r"A [^MLZ]*?(?= [ML]|$)", "", d))
    return list(zip(nums[0::2], nums[1::2], strict=False))


@pytest.mark.parametrize("lattice", ["square", "triangle", "hex"])
def test_grid_lines_stay_in_region(style, lattice):
    out = _parse(registry.build("grid_lines", {"id": "g", **REGION, "lattice": lattice, "angle": 17}, style))
    pts = _endpoints(out.find(".//*[@id='g-lines']").get("d"))
    assert len(pts) > 10
    assert all(10 - 1e-6 <= x <= 70 + 1e-6 and 20 - 1e-6 <= y <= 60 + 1e-6 for x, y in pts)


def test_square_grid_line_count(style):
    out = _parse(
        registry.build("grid_lines", {"id": "g", "x": 0, "y": 0, "width": 20, "height": 10, "spacing": 5}, style)
    )
    d = out.find(".//*[@id='g-lines']").get("d")
    assert d.count("M ") == 3 + 5  # horizontals at y=0,5,10 and verticals at x=0..20 (centred family)


@pytest.mark.parametrize("motif", ["arcs", "diagonals", "triangles"])
def test_truchet_never_leaves_its_region(style, motif):
    out = _parse(registry.build("truchet", {"id": "t", **REGION, "size": 7, "motif": motif}, style, seed=2))
    d = out.find(".//*[@id='t-tiles']").get("d")
    nums = _numbers(d.replace("A", " "))
    xs = [v for v in nums if v > 9]  # coordinates (arc radii and flags are small numbers)
    assert max(xs) <= 70 + 1e-6


def test_truchet_seeds_differ(style):
    a = registry.build("truchet", {"id": "t", **REGION}, style, seed=1)
    assert a != registry.build("truchet", {"id": "t", **REGION}, style, seed=2)


@pytest.mark.parametrize(("figure", "count"), [("rings", 4), ("rays", 12), ("polygons", 3)])
def test_radial_counts(style, figure, count):
    out = _parse(registry.build("radial", {"id": "r", "cx": 0, "cy": 0, "figure": figure, "count": count}, style))
    d = out.find(".//*[@id='r-lines']").get("d")
    assert d.count("M ") == count


def test_iso_box_has_three_shaded_faces_above_its_corner(style):
    out = _parse(registry.build("iso_box", {"id": "b", "cx": 50, "cy": 50, "color": "#2a7ab0"}, style))
    fills = [out.find(f".//*[@id='b-{f}']").get("style").split(";")[0] for f in ("top", "left", "right")]
    assert fills[2] == "fill:#2a7ab0" and len(set(fills)) == 3
    ys = _numbers(" ".join(out.find(f".//*[@id='b-{f}']").get("d") for f in ("top", "left", "right")))[1::2]
    assert max(ys) == pytest.approx(50)  # the front-bottom corner is the lowest point


# ---- catalogue -----------------------------------------------------------------------


def test_catalogue_renders_every_generator_without_errors(style):
    svg = catalogue_mod.render(style)
    root = etree.fromstring(svg.encode())
    registry._load_all()
    for name in registry.REGISTRY:
        assert root.find(f".//*[@id='cat-{name}-0']") is not None, f"{name} missing from catalogue"
    texts = " ".join("".join(t.itertext()) for t in root.iter(f"{{{SVG}}}text"))
    assert "no demo layout" not in texts and "must be" not in texts and "unknown" not in texts


def test_catalogue_captions_reproduce_the_cells(style):
    gen = registry.get("chain")
    assert [cap for _, _, cap in catalogue_mod.cells(gen)] == ["seed 1", "seed 2", "seed 3"]
    gene_caps = [cap for _, _, cap in catalogue_mod.cells(registry.get("gene"))]
    assert gene_caps[1] == '{"state": "off"}'


async def test_catalogue_render_op(live_doc, monkeypatch, tmp_path):
    async def fake_export(cmd, out):
        out.write_bytes(b"\x89PNG")
        return ""

    monkeypatch.setattr(live, "_run_png_export", fake_export)
    res = await _component(kind="catalogue", render=True, category="tiling")
    assert res["success"] is True
    svg = Path(res["data"]["svg"]).read_text()
    assert "cat-truchet-0" in svg and "cat-chain-0" not in svg and 'id="Triangle"' in svg
