"""Small helpers shared across test modules."""

from __future__ import annotations

import os
import sys

from inkscape_mcp.dbus_client import InkscapeDBus, MacInkscapeDBus

MACOS_LIVE_OPT_IN = (
    "macOS: the MCP's Inkscape is the user's working instance, and these tests edit/select in its "
    "ACTIVE document — opt in with INKSCAPE_MCP_LIVE=1 (with only a scratch document open)"
)


def live_tests_blocked() -> str | None:
    """Why tests that act on the running Inkscape must not run now, or None if they may.

    On macOS the only reachable Inkscape is the one the MCP server launched for the user, so
    touching it implicitly would write into their figure (it did: test_insert_svg left green
    circles in a saved figure). Linux behaviour is unchanged: run when the GUI is up.
    """
    if sys.platform == "darwin" and os.environ.get("INKSCAPE_MCP_LIVE") != "1":
        return MACOS_LIVE_OPT_IN
    if not live_bus().is_available():
        return "Inkscape GUI not running — start it to exercise the live bridge"
    return None


def live_bus() -> InkscapeDBus:
    """The bus the live bridge uses on this platform, without starting anything.

    Linux: the user's session bus. macOS: the MCP's private bus, where only an Inkscape
    the server (or the live tier) launched is reachable.
    """
    if sys.platform == "darwin":
        from inkscape_mcp import mac_bus_manager

        return MacInkscapeDBus(mac_bus_manager.bus_address())
    return InkscapeDBus()


def payload(result):
    """Unwrap a FastMCP tool result into its dict payload."""
    if result.structured_content is not None:
        return result.structured_content
    if result.data is not None:
        return result.data
    return {"text": "\n".join(b.text for b in result.content if hasattr(b, "text"))}
