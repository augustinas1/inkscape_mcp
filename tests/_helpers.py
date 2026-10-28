"""Small helpers shared across test modules."""

from __future__ import annotations

import sys

from inkscape_mcp.dbus_client import InkscapeDBus, MacInkscapeDBus


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
