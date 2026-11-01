"""Local asset library, scoped CSS on import, and keeping assets out of the committed catalogue."""

from __future__ import annotations

import json

import pytest
from lxml import etree

from inkscape_mcp.figure_kit import load_style
from inkscape_mcp.generators import assets, catalogue, importer, registry

SVG = "http://www.w3.org/2000/svg"
INK = "http://www.inkscape.org/namespaces/inkscape"
FIGURE = f"""<svg xmlns="{SVG}" xmlns:inkscape="{INK}" viewBox="0 0 200 100" width="200" height="100">
  <style>*{{stroke-linecap:butt}} #thing{{fill:#f00}} .x, svg{{opacity:1}}</style>
  <g id="layer1" inkscape:groupmode="layer" transform="translate(10,5)">
    <g id="part" transform="scale(2)"><rect id="thing" x="10" y="10" width="20" height="10" fill="#3eafdd"/></g>
    <circle id="other" cx="150" cy="60" r="10" fill="#fd6b6c"/>
  </g>
</svg>"""


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / "refs"
    lib.mkdir()
    (lib / "fig.svg").write_text(FIGURE)
    monkeypatch.setenv(assets.ENV, str(lib))
    return lib


def _parse(fragment: str):
    return etree.fromstring(f'<svg xmlns="{SVG}" xmlns:inkscape="{INK}">{fragment}</svg>'.encode())[0]


# ---- CSS scoping -----------------------------------------------------------------------


def test_scope_css_confines_rules_to_the_import():
    css = importer.scope_css("*{a:b} #thing{c:d} .x, svg{e:f} @import url(x.css); @font-face{g:h}", "im", {"thing"})
    assert "#im *{a:b}" in css and "#im #im-thing{c:d}" in css and "#im .x, #im{e:f}" in css
    assert "@import" not in css and "@font-face{g:h}" in css


def test_imported_style_cannot_restyle_the_host(tmp_path):
    src = tmp_path / "x.svg"
    src.write_text(FIGURE)
    out = _parse(
        registry.build("import_svg", {"id": "im", "path": str(src), "x": 0, "y": 0, "width": 50}, load_style())
    )
    css = out.find(f".//{{{SVG}}}style").text
    assert all(line.startswith("#im") for line in css.splitlines() if line.strip())


# ---- library ---------------------------------------------------------------------------


def test_library_dir_from_env(library):
    assert assets.library_dir() == library


def test_index_is_built_and_curated_entries_survive(library):
    (library / assets.INDEX).write_text(json.dumps({"assets": {"part": {"file": "fig.svg", "element": "part"}}}))
    entries = assets.build_index(library)
    assert set(entries) == {"fig", "part"} and entries["part"]["element"] == "part"
    assert json.loads((library / assets.INDEX).read_text())["assets"]["part"]["element"] == "part"


def test_asset_part_keeps_its_ancestor_transforms_and_fits_width(library):
    out = _parse(
        registry.build(
            "asset", {"id": "a", "name": "fig", "element": "part", "x": 0, "y": 0, "width": 40}, load_style()
        )
    )
    assert out.get(f"{{{INK}}}label") == "asset: fig"
    wrapper = out.find(f"{{{SVG}}}g")
    assert wrapper.get("transform") == "translate(10, 5)"  # layer offset carried over
    assert out.find(".//*[@id='a-other']") is None  # only the part, not the whole figure
    # Part box: rect 10..30 x 10..20, scaled 2 then shifted (10,5) -> 30..70 x 25..45; fitted to 40 wide.
    assert out.get("transform") == "translate(0,0) scale(1) translate(-30,-25)"


def test_cached_box_is_used_and_height_fits(library):
    (library / assets.INDEX).write_text(json.dumps({"assets": {"whole": {"file": "fig.svg", "box": [0, 0, 100, 50]}}}))
    out = _parse(
        registry.build("asset", {"id": "w", "name": "whole", "x": 5, "y": 5, "width": 80, "height": 20}, load_style())
    )
    assert out.get("transform") == "translate(5,5) scale(0.4) translate(0,0)"  # 100x50 into 80x20 -> height wins


def test_unknown_asset_and_missing_library(library, monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="unknown asset 'nope'"):
        registry.build("asset", {"id": "a", "name": "nope", "x": 0, "y": 0, "width": 10}, load_style())
    monkeypatch.setenv(assets.ENV, str(tmp_path / "missing"))
    monkeypatch.setattr(assets, "library_dir", lambda: None)
    with pytest.raises(ValueError, match="no asset library"):
        registry.build("asset", {"id": "a", "name": "fig", "x": 0, "y": 0, "width": 10}, load_style())


@pytest.mark.skipif(assets._inkscape() is None, reason="needs the Inkscape CLI")
def test_visible_box_ignores_clipped_away_geometry(tmp_path):
    src = tmp_path / "clipped.svg"
    src.write_text(
        f'<svg xmlns="{SVG}" viewBox="0 0 100 100" width="100" height="100"><defs><clipPath id="c">'
        '<rect x="10" y="10" width="30" height="30"/></clipPath></defs>'
        '<g id="plot" clip-path="url(#c)"><rect x="-500" y="-500" width="2000" height="2000" fill="#00f"/></g></svg>'
    )
    box = assets.visible_box(src, "plot")
    assert box == pytest.approx((10, 10, 30, 30), abs=0.5)


# ---- catalogue -------------------------------------------------------------------------


def test_committed_catalogue_never_includes_assets(library):
    style = load_style()
    assert "cat-asset-" not in catalogue.render_html(style)
    personal = catalogue.render_html(style, include_assets=True)
    assert 'id="cat-asset-fig"' in personal and 'data-copy="asset fig"' in personal


def test_assets_and_imports_are_not_generator_rows(library):
    page = catalogue.render_html(load_style())
    assert 'id="asset"' not in page and 'id="import_svg"' not in page
