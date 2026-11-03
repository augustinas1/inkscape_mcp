"""Session-wide fixtures: an in-process MCP client backed by the live server."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from fastmcp import Client

from inkscape_mcp.inkscape_detector import MACOS_APP_BINARIES
from inkscape_mcp.main import InkscapeMCPServer

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run against Inkscape versions outside 1.4.x — the only supported line."""
    binary = shutil.which("inkscape")
    if binary is None and sys.platform == "darwin":
        # The macOS app bundle puts nothing on $PATH; expose it so tests that shell out
        # to a bare `inkscape` find the same binary the server auto-detects.
        bundled = next((p for p in MACOS_APP_BINARIES if p.is_file()), None)
        if bundled is not None:
            os.environ["PATH"] = f"{bundled.parent}{os.pathsep}{os.environ.get('PATH', '')}"
            binary = shutil.which("inkscape")
    if binary is None:
        pytest.exit("Inkscape not found on $PATH. See docs/INSTALL.md.", returncode=2)
    out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    match = re.search(r"Inkscape\s+(\d+)\.(\d+)", out.stdout)
    if not match or (int(match.group(1)), int(match.group(2))) != (1, 4):
        pytest.exit(
            f"Unsupported Inkscape version. Expected 1.4.x, got: {out.stdout.strip()}",
            returncode=2,
        )


@pytest_asyncio.fixture(scope="session")
async def server() -> InkscapeMCPServer:
    """Boot the server once per session."""
    s = InkscapeMCPServer()
    ok = await s.initialize()
    assert ok, "Server failed to initialize"
    assert s.cli_wrapper is not None, "CLI wrapper should exist when Inkscape is on PATH"
    return s


@pytest_asyncio.fixture
async def mcp(server: InkscapeMCPServer):
    """A connected MCP Client speaking to the in-process server. Re-opened per test."""
    async with Client(server.mcp) as client:
        yield client


@pytest.fixture
def minimal_svg(tmp_path: Path) -> Path:
    """A small SVG with stable ids — rect, circle, text, two layers."""
    dest = tmp_path / "minimal.svg"
    shutil.copy(FIXTURES / "minimal.svg", dest)
    return dest


@pytest.fixture
def multi_path_svg(tmp_path: Path) -> Path:
    """Two overlapping paths for boolean ops."""
    dest = tmp_path / "multi.svg"
    shutil.copy(FIXTURES / "multi_path.svg", dest)
    return dest


@pytest.fixture
def trace_bitmap(tmp_path: Path) -> Path:
    """A tiny PNG suitable for bitmap tracing."""
    dest = tmp_path / "trace.png"
    shutil.copy(FIXTURES / "bitmap_for_trace.png", dest)
    return dest


@pytest.fixture
def grouped_svg(tmp_path: Path) -> Path:
    """Two groups plus a loose rect — exercises group-scoped selection."""
    dest = tmp_path / "grouped.svg"
    shutil.copy(FIXTURES / "grouped.svg", dest)
    return dest


@pytest.fixture
def stroked_svg(tmp_path: Path) -> Path:
    """Two identically-stroked paths — discriminates per-object stroke_to_path scope."""
    dest = tmp_path / "stroked.svg"
    shutil.copy(FIXTURES / "stroked.svg", dest)
    return dest


@pytest.fixture
def page_vs_drawing_svg(tmp_path: Path) -> Path:
    """Page (400x300) deliberately differs from drawing bbox (220x150)."""
    dest = tmp_path / "page_vs_drawing.svg"
    shutil.copy(FIXTURES / "page_vs_drawing.svg", dest)
    return dest


@pytest.fixture(autouse=True)
def _private_inkscape_output(tmp_path, monkeypatch):
    """Never touch the real output log, pid file, remembered paths or backups (tests use fake launches)."""
    from inkscape_mcp import selection_probe

    monkeypatch.setattr(selection_probe, "OUTPUT_LOG", tmp_path / "inkscape-output.log")
    monkeypatch.setattr(selection_probe, "_PID_FILE", tmp_path / "inkscape-output.pid")
    from inkscape_mcp.tools import live_figure

    monkeypatch.setattr(selection_probe, "DOC_PATHS", tmp_path / "doc-paths.json")
    from inkscape_mcp import change_tracker

    monkeypatch.setattr(change_tracker, "_LAST_SELECTION", tmp_path / "last-selection.json")
    monkeypatch.setattr(live_figure, "BACKUP_DIR", tmp_path / "backups")
