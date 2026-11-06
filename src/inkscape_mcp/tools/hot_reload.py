"""Reload the live-figure modules when their code changes, so new operations need no reconnect.

The MCP client fixes a tool's schema when the server starts, but `inkscape_live` dispatches on
a plain-text `operation`, so a new operation only needs the server's Python to be current.
Before each live call, if any of these modules' files changed since they were loaded, they are
reloaded in dependency order. Stateful plumbing (the D-Bus clients, the extension bridge and its
lock, the bus manager, the generator registry) is deliberately not reloaded; changes there still
need a server restart, which `ping` reports.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

# Dependencies first: tools.live imports from the modules above it.
RELOADABLE = (
    "inkscape_mcp.figure_outline",
    "inkscape_mcp.figure_measure",
    "inkscape_mcp.svg_cleanup",
    "inkscape_mcp.transplant",
    "inkscape_mcp.selection_probe",
    "inkscape_mcp.change_tracker",
    "inkscape_mcp.mplstyle",
    "inkscape_mcp.tools.live_figure",
    "inkscape_mcp.tools.live",
)
_loaded_at: dict[str, float] = {}


def _mtime(name: str) -> float:
    module = sys.modules.get(name)
    path = getattr(module, "__file__", None)
    return Path(path).stat().st_mtime if path else 0.0


def live_entry() -> Any:
    """The current `inkscape_live` function, reloading changed modules first."""
    for name in RELOADABLE:
        if name not in sys.modules:
            importlib.import_module(name)
        _loaded_at.setdefault(name, _mtime(name))
    if any(_mtime(name) > _loaded_at[name] for name in RELOADABLE):
        for name in RELOADABLE:
            importlib.reload(sys.modules[name])
            _loaded_at[name] = _mtime(name)
    return sys.modules["inkscape_mcp.tools.live"].inkscape_live
