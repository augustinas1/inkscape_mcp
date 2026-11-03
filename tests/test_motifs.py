"""Style presets and the motif generators (tissue, annotation, plots, box gene)."""

from __future__ import annotations

import json
import math
import re

import pytest
from lxml import etree

from inkscape_mcp.figure_kit import DEFAULT_STYLE, STYLE_PRESETS, load_style
from inkscape_mcp.generators import motifs, registry

SVG = "http://www.w3.org/2000/svg"
INK = "http://www.inkscape.org/namespaces/inkscape"


def _parse(fragment: str):
    return etree.fromstring(f'<svg xmlns="{SVG}" xmlns:inkscape="{INK}">{fragment}</svg>'.encode())[0]


def _nums(d: str) -> list[float]:
    return [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", d)]


# ---- presets ---------------------------------------------------------------------------


def test_presets_cover_the_same_tokens():
    for name in STYLE_PRESETS:
        style = load_style(name)
        assert set(style) == set(DEFAULT_STYLE), name
        assert {"cell", "panel", "callout", "plot", "gene_box"} <= set(style)


def test_reference_preset_is_the_published_palette():
    ref = load_style("reference")
    assert ref["species"]["rna"]["color"] == "#0098d1"
    assert ref["gene_box"]["on"] == "#57c6ff" and ref["gene_box"]["off"] == "#fd7b74"
    assert ref["font"]["math"] == "Arial"  # maths stays Arial in every preset


def test_style_file_extends_a_preset(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"extends": "reference", "species": {"rna": {"color": "#123456"}}}))
    style = load_style(f)
    assert style["species"]["rna"] == {"color": "#123456", "pale": "#8fd3ef"}
    assert style["panel"]["stroke"] == STYLE_PRESETS["reference"]["panel"]["stroke"]


@pytest.mark.parametrize(("arg", "error"), [("pastel", "neither a preset"), ("{bad", "neither a preset")])
def test_unknown_style(arg, error):
    with pytest.raises(ValueError, match=error):
        load_style(arg)


def test_bad_extends(tmp_path):
    f = tmp_path / "s.json"
    f.write_text('{"extends": "pastel"}')
    with pytest.raises(ValueError, match="not a preset"):
        load_style(f)


# ---- motifs ----------------------------------------------------------------------------


@pytest.fixture(params=["nature", "reference"])
def style(request):
    return load_style(request.param)


MOTIF_CALLS = [
    ("hex_tissue", {"x": 0, "y": 0, "width": 40, "height": 30, "highlight": 0.3, "fade": True}),
    ("panel", {"x": 0, "y": 0, "width": 40, "height": 20, "title": "Model"}),
    ("label_pill", {"x": 30, "y": 20, "text": "Burst", "target_x": 5, "target_y": 5}),
    ("zoom_callout", {"x1": 5, "y1": 5, "r1": 3, "x2": 30, "y2": 10, "r2": 8}),
    ("trajectory_fan", {"x": 0, "y": 0, "width": 40, "height": 20}),
    ("histogram_fit", {"x": 0, "y": 0, "width": 40, "height": 20, "x_label": "counts"}),
    ("ridge_plot", {"x": 0, "y": 0, "width": 40, "height": 30}),
    ("gene_box", {"x": 0, "y": 10, "state": "off", "sub": "off"}),
]


@pytest.mark.parametrize(("name", "params"), MOTIF_CALLS)
def test_motifs_are_well_formed_and_named(style, name, params):
    group = _parse(registry.build(name, {"id": "m", **params}, style, seed=1))
    ids = [el.get("id") for el in group.iter() if el.get("id")]
    assert len(ids) == len(set(ids))
    named = [el for el in group.iter() if etree.QName(el).localname in {"path", "rect", "circle", "ellipse", "text"}]
    assert named and all(el.get(f"{{{INK}}}label") for el in named)


def test_tissue_cells_touch_and_stay_in_region(style):
    group = _parse(
        registry.build("hex_tissue", {"id": "t", "x": 0, "y": 0, "width": 40, "height": 30, "size": 3}, style)
    )
    centres = []
    for el in group.iter(f"{{{SVG}}}path"):
        pts = _nums(el.get("d"))
        xs, ys = pts[0::2], pts[1::2]
        assert min(xs) >= -1e-6 and max(xs) <= 40 + 1e-6 and min(ys) >= -1e-6 and max(ys) <= 30 + 1e-6
        centres.append((sum(xs) / 6, sum(ys) / 6))
    nearest = min(math.dist(a, b) for i, a in enumerate(centres) for b in centres[i + 1 :])
    assert nearest == pytest.approx(3 * math.sqrt(3), abs=0.01)  # edge-sharing neighbours: touching tissue


def test_tissue_highlight_is_one_contiguous_patch(style):
    group = _parse(
        registry.build(
            "hex_tissue", {"id": "t", "x": 0, "y": 0, "width": 60, "height": 40, "highlight": 0.25}, style, 4
        )
    )
    lit = [el for el in group.iter(f"{{{SVG}}}path") if "highlighted" in el.get(f"{{{INK}}}label")]
    assert lit and all(style["cell"]["highlight"] in el.get("style") for el in lit)
    centres = [(sum(_nums(el.get("d"))[0::2]) / 6, sum(_nums(el.get("d"))[1::2]) / 6) for el in lit]
    reached, todo = {0}, [0]
    while todo:
        i = todo.pop()
        for j, c in enumerate(centres):
            if j not in reached and math.dist(centres[i], c) < 4 * math.sqrt(3) + 0.01:
                reached.add(j)
                todo.append(j)
    assert len(reached) == len(centres)


def test_zoom_tangents_touch_both_circles():
    (p1, q1), (p2, q2) = motifs._outer_tangents((0, 0), 2, (20, 5), 6)
    for p, q in ((p1, q1), (p2, q2)):
        assert math.dist(p, (0, 0)) == pytest.approx(2) and math.dist(q, (20, 5)) == pytest.approx(6)


def test_zoom_lens_hides_the_cone(style):
    group = _parse(
        registry.build("zoom_callout", {"id": "z", "x1": 5, "y1": 5, "r1": 3, "x2": 30, "y2": 10, "r2": 8}, style)
    )
    order = [el.get("id") for el in group]
    assert order.index("z-cone") < order.index("z-lens")
    assert "fill:#ffffff" in group.find(".//*[@id='z-lens']").get("style")


def test_trajectory_fan_fits_without_clamping(style):
    group = _parse(
        registry.build(
            "trajectory_fan", {"id": "f", "x": 0, "y": 0, "width": 40, "height": 20, "volatility": 4}, style, 2
        )
    )
    ys = [v for el in group.iter(f"{{{SVG}}}path") for v in _nums(el.get("d"))[1::2]]
    assert 0 <= min(ys) and max(ys) <= 20
    flat_top = sum(1 for v in ys if abs(v) < 1e-6)
    assert flat_top <= 1  # one extreme point may touch the edge; no clamped runs


def test_box_gene_colours_by_state(style):
    on = _parse(registry.build("gene_box", {"id": "g", "x": 0, "y": 0}, style))
    off = _parse(registry.build("gene_box", {"id": "g", "x": 0, "y": 0, "state": "off"}, style))
    assert style["gene_box"]["on"] in on.find(".//*[@id='g-box']").get("style")
    assert style["gene_box"]["off"] in off.find(".//*[@id='g-box']").get("style")
    assert "marker-end:url(#Triangle)" in on.find(".//*[@id='g-promoter']").get("style")
    assert "font-family:Arial;font-style:italic" in on.find(".//*[@id='g-symbol']").get("style")


def test_protein_globule_layers_and_seeds(style):
    params = {"id": "pg", "cx": 50, "cy": 50, "size": 7}
    group = _parse(registry.build("protein_globule", params, style, seed=1))
    ids = [el.get("id") for el in group]
    # defs, outline under the (crevice-toned) core and body, then the soft domains clipped to the body
    assert ids == ["pg-defs", "pg-outline", "pg-core", "pg-body", "pg-domains"]
    domains = group.find(".//*[@id='pg-domains']")
    assert domains.get("clip-path") == "url(#pg-clip)" and len(domains) >= 4  # several domains (+ rims)
    assert all("filter:url(#pg-soft)" in el.get("style") for el in domains)
    blur = group.find(".//*[@id='pg-soft-blur']")
    assert blur is not None and group.find(".//*[@id='pg-soft']").get(f"{{{INK}}}auto-region") == "false"
    assert registry.build("protein_globule", params, style, seed=1) != registry.build(
        "protein_globule", params, style, seed=2
    )


def test_protein_globule_stays_near_its_size(style):
    group = _parse(registry.build("protein_globule", {"id": "pg", "cx": 0, "cy": 0, "size": 7}, style, seed=3))
    nums = _nums(group.find(".//*[@id='pg-outline']").get("d"))
    xs = nums[0::2]
    assert -7 * 1.6 < min(xs) and max(xs) < 7 * 1.6  # lumps and outline, but no runaway spheres
