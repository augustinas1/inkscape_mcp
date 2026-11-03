"""mplstyle: a matplotlib style sheet from the figure's style tokens."""

from __future__ import annotations

from inkscape_mcp import mplstyle
from inkscape_mcp.figure_kit import load_style


def _rows(text):
    return dict(line.split(": ", 1) for line in text.splitlines() if line and not line.startswith("#"))


def test_text_stays_text_and_tokens_carry_over():
    style = load_style("reference")
    rows = _rows(mplstyle.to_mplstyle(style, font="Calibri", size_pt=7))
    assert rows["svg.fonttype"] == "none" and rows["font.family"] == "Calibri" and rows["font.size"] == "7"
    assert rows["axes.edgecolor"] == style["plot"]["axis"].lstrip("#")  # '#' starts a comment in .mplstyle
    assert all("#" not in v for v in rows.values())
    assert rows["axes.prop_cycle"].startswith("cycler('color', ['")


def test_cli_prints_the_sheet(capsys):
    assert mplstyle.main(["nature", "--size", "6"]) == 0
    assert "font.size: 6" in capsys.readouterr().out
