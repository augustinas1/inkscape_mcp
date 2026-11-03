"""get_document_xml: a subtree by id, and large XML as a file rather than inline."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from inkscape_mcp.tools import live

DOC = '<svg xmlns="http://www.w3.org/2000/svg"><g id="small"><rect id="r" width="1" height="1"/></g>{big}</svg>'


@pytest.fixture
def doc(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "_SCRATCH_DIR", tmp_path)
    state = {"xml": DOC.format(big="")}

    async def fake_export(bus, path):
        Path(path).write_text(state["xml"])

    monkeypatch.setattr(live, "_export_document_async", fake_export)
    return state


async def _get(target=""):
    return await live._op_get_document_xml(MagicMock(), "get_document_xml", target, time.perf_counter())


async def test_target_returns_just_that_subtree(doc):
    res = await _get("small")
    assert res["success"] and res["data"]["xml"].startswith('<g xmlns="http://www.w3.org/2000/svg" id="small">')
    assert "<svg" not in res["data"]["xml"]


async def test_large_xml_comes_back_as_a_file(doc):
    doc["xml"] = DOC.format(big="<desc>" + "x" * (live._INLINE_DOC_LIMIT + 10) + "</desc>")
    res = await _get()
    assert res["success"] and "xml" not in res["data"] and "not inlined" in res["message"]
    assert Path(res["data"]["path"]).read_text() == doc["xml"] and res["data"]["head"].startswith("<svg")


async def test_unknown_target_is_an_error(doc):
    res = await _get("#missing")
    assert not res["success"] and "'missing'" in res["message"]
