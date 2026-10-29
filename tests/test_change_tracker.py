"""Unit tests for change_tracker: what the user changed in Inkscape since the agent looked."""

from __future__ import annotations

import json

import pytest

from inkscape_mcp import change_tracker as ct

DOC = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
     sodipodi:docname="fig.svg" inkscape:version="1.4.4" width="180mm" height="120mm" viewBox="0 0 180 120">
  <sodipodi:namedview id="nv" inkscape:zoom="{zoom}"/>
  <g id="layer1" inkscape:groupmode="layer">
    <g id="gene-off"{gene_transform}>
      <rect id="body" x="41" y="28.5" width="28" height="7" style="fill:#dcdcdc;stroke:none"/>
      <text id="label" x="55" y="33">{label}<tspan id="sub">off</tspan></text>
    </g>
    {extra}
  </g>
</svg>
"""


def _fp(zoom="1", gene_transform="", label="OFF", extra="", doc=DOC):
    svg = doc.format(
        zoom=zoom,
        gene_transform=f' transform="{gene_transform}"' if gene_transform else "",
        label=label,
        extra=extra,
    )
    return ct.fingerprint(svg.encode())


@pytest.fixture(autouse=True)
def _baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "BASELINE_FILE", tmp_path / "baseline.json")


def test_fingerprint_skips_view_state_and_folds_text():
    docname, elements = _fp()
    assert docname == "fig.svg"
    assert "nv" not in elements and "sub" not in elements
    assert elements["label"]["text"] == "OFFoff"
    # Root bookkeeping that changes on every export is ignored.
    root = next(sig for sig in elements.values() if sig["tag"] == "svg")
    assert "inkscape:version" not in root["attrs"] and "sodipodi:docname" not in root["attrs"]


def test_view_changes_are_not_reported():
    _, before = _fp(zoom="1")
    _, after = _fp(zoom="3.5")
    assert ct.diff(before, after) == []


def test_moving_a_group_is_one_line():
    _, before = _fp()
    _, after = _fp(gene_transform="translate(0,12)")
    assert ct.diff(before, after) == ["~ <g#gene-off>: transform: none → translate(0,12)"]


def test_style_and_text_changes_are_itemised():
    _, before = _fp()
    _, after = _fp(label="INACTIVE")
    after["body"]["style"]["fill"] = "#ff0000"
    lines = ct.diff(before, after)
    assert "~ <rect#body>: style fill: #dcdcdc → #ff0000" in lines
    assert any('text: "OFFoff" → "INACTIVEoff"' in line for line in lines)


def test_deleted_subtree_reported_once_at_its_top():
    _, before = _fp()
    _, after = _fp()
    for key in ("gene-off", "body", "label"):
        del after[key]
    assert ct.diff(before, after) == ["- deleted <g#gene-off> (2 element(s) inside)"]


def test_added_element_names_its_parent():
    _, before = _fp()
    _, after = _fp(extra='<circle id="dot" cx="5" cy="5" r="1"/>')
    assert ct.diff(before, after) == ["+ added <circle#dot> in #layer1"]


def test_compare_marks_and_peeks(tmp_path):
    _, first = _fp()
    assert ct.compare("fig.svg", first) == {"doc": "fig.svg", "had_baseline": False, "changes": []}

    _, moved = _fp(gene_transform="translate(0,12)")
    peek = ct.compare("fig.svg", moved, mark=False)
    assert peek["changes"] == ["~ <g#gene-off>: transform: none → translate(0,12)"]
    # Peeking left the baseline alone, so the same change is still reported...
    assert ct.compare("fig.svg", moved)["changes"] == peek["changes"]
    # ...and marking consumed it.
    assert ct.compare("fig.svg", moved)["changes"] == []


def test_switching_documents_rebaselines_instead_of_diffing():
    _, elements = _fp()
    ct.compare("a.svg", elements)
    result = ct.compare("b.svg", elements)
    assert result["doc_switched_from"] == "a.svg" and result["changes"] == []
    assert "b.svg" in ct.format_report(result)


def test_report_is_silent_without_changes():
    assert ct.format_report({"doc": "fig.svg", "had_baseline": True, "changes": []}) == ""


def test_hook_prompt_emits_additional_context(monkeypatch, capsys):
    _, before = _fp()
    ct.save_baseline("fig.svg", before)
    _, after = _fp(gene_transform="translate(0,12)")
    monkeypatch.setattr(ct, "snapshot_live", lambda: ("fig.svg", after))

    assert ct.main(["hook-prompt"]) == 0
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "gene-off" in context and "translate(0,12)" in context


def test_hooks_say_nothing_when_inkscape_is_down(monkeypatch, capsys):
    monkeypatch.setattr(ct, "snapshot_live", lambda: None)
    assert ct.main(["hook-prompt"]) == 0
    assert ct.main(["hook-stop"]) == 0
    assert capsys.readouterr().out == ""
    assert not ct.BASELINE_FILE.exists()


def test_hook_stop_marks_baseline(monkeypatch):
    _, elements = _fp()
    monkeypatch.setattr(ct, "snapshot_live", lambda: ("fig.svg", elements))
    assert ct.main(["hook-stop"]) == 0
    assert ct.load_baseline()["doc"] == "fig.svg"
