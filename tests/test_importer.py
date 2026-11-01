"""import_svg: outside SVGs are sanitised, id-prefixed, fitted and recoloured to the style."""

from __future__ import annotations

import pytest
from lxml import etree

from inkscape_mcp.figure_kit import load_style
from inkscape_mcp.generators import registry

SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
INK = "http://www.inkscape.org/namespaces/inkscape"
HOSTILE = f"""<?xml version="1.0"?>
<!-- a comment -->
<svg xmlns="{SVG}" xmlns:xlink="{XLINK}" viewBox="10 20 200 100" width="400" height="200">
  <title>t</title><metadata><x/></metadata>
  <script>alert(1)</script>
  <defs><linearGradient id="g"><stop offset="0" stop-color="#ff0000"/>
    <stop offset="1" style="stop-color:#0000ff"/></linearGradient></defs>
  <rect id="r" x="10" y="20" width="50" height="50" fill="url(#g)" onclick="evil()"/>
  <circle id="c" cx="100" cy="60" r="20" style="fill:#e05060;stroke:#000000;stroke-width:2"/>
  <use id="u" xlink:href="#c" x="40"/>
  <a id="link" href="https://example.com"><rect id="r2" fill="white" width="1" height="1"/></a>
  <image id="img" xlink:href="http://example.com/x.png" width="5" height="5"/>
  <foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>
</svg>"""


@pytest.fixture
def src(tmp_path):
    path = tmp_path / "pattern.svg"
    path.write_text(HOSTILE)
    return str(path)


def _imp(src, **params):
    xml = registry.build("import_svg", {"id": "im", "path": src, "x": 5, "y": 7, "width": 50, **params}, load_style())
    return etree.fromstring(f'<svg xmlns="{SVG}" xmlns:inkscape="{INK}">{xml}</svg>'.encode())[0]


def test_sanitised(src):
    text = etree.tostring(_imp(src)).decode()
    for bad in ("script", "alert", "onclick", "foreignObject", "metadata", "<title", "example.com", "<!--"):
        assert bad not in text, bad


def test_ids_and_references_prefixed(src):
    g = _imp(src)
    assert g.find(f".//{{{SVG}}}rect[@id='im-r']").get("fill") == "url(#im-g)"
    assert g.find(f".//{{{SVG}}}use").get(f"{{{XLINK}}}href") == "#im-c"
    assert g.find(f".//{{{SVG}}}linearGradient").get("id") == "im-g"


def test_fitted_to_width_from_the_viewbox(src):
    assert _imp(src).get("transform") == "translate(5,7) scale(0.25) translate(-10,-20)"


def test_palette_recolouring_maps_onto_style_tokens(src):
    g = _imp(src, recolor="palette")
    palette = {"#d1495b", "#333333", "#2a7ab0", "#1d5f8c"}
    circle = g.find(f".//{{{SVG}}}circle").get("style")
    assert "fill:#d1495b" in circle  # the pinkish red snaps to the RNA red
    stops = [s.get("stop-color") or s.get("style").split(":")[1] for s in g.iter(f"{{{SVG}}}stop")]
    assert all(s in palette or s.startswith("#") for s in stops) and stops[0] != "#ff0000"
    assert g.find(f".//{{{SVG}}}rect[@id='im-r2']").get("fill") == "#ffffff"  # white stays white


def test_mono_uses_tints_of_one_colour(src):
    circle = _imp(src, recolor="mono", color="#2b8a69").find(f".//{{{SVG}}}circle").get("style")
    assert "stroke:#2b8a69" in circle  # black -> full colour
    fill = circle.split("fill:")[1][:7]
    assert fill != "#2b8a69" and fill.startswith("#")  # mid-tone -> a paler tint


def test_none_keeps_colours(src):
    assert "fill:#e05060" in _imp(src, recolor="none").find(f".//{{{SVG}}}circle").get("style")


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (None, "existing .svg file"),
        ("<svg xmlns='http://www.w3.org/2000/svg'/>", "neither a viewBox"),
        ("<html/>", "no <svg> root"),
        ("<svg", "not well-formed"),
    ],
)
def test_bad_inputs(tmp_path, content, error):
    path = tmp_path / "x.svg"
    if content is not None:
        path.write_text(content)
    with pytest.raises(ValueError, match=error):
        registry.build("import_svg", {"id": "i", "path": str(path), "x": 0, "y": 0, "width": 10}, load_style())


def test_entities_are_not_expanded(tmp_path):
    path = tmp_path / "xxe.svg"
    path.write_text(
        '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        f'<svg xmlns="{SVG}" viewBox="0 0 10 10"><text id="t">&x;</text></svg>'
    )
    xml = registry.build("import_svg", {"id": "i", "path": str(path), "x": 0, "y": 0, "width": 10}, load_style())
    assert "root:" not in xml
