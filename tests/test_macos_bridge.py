"""Unit tests for the macOS live-bridge path (no GUI, no real bus needed).

macOS publishes app actions and /document/N objects but no /window/N objects, so the
bridge maps "window" scope onto the document object and must fail window-only actions
with a clear message rather than an obscure D-Bus error.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from inkscape_mcp import extension_bridge, mac_bus_manager
from inkscape_mcp.dbus_client import DOCUMENT_PATH_PREFIX, InkscapeDBus, MacInkscapeDBus
from inkscape_mcp.tools import live


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("INKSCAPE_MCP_AUTO_SAVE", raising=False)
    monkeypatch.delenv("INKSCAPE_MCP_DBUS_ADDRESS", raising=False)


def _mac_bus(app=(), document=(), windows=(1,)):
    bus = MagicMock(spec=MacInkscapeDBus)
    bus.window_scope_available = False
    bus.list_windows.return_value = list(windows)
    bus.list_actions.side_effect = lambda scope="app", window_id=1: list(app if scope == "app" else document)
    return bus


# ---- client -----------------------------------------------------------------------


def test_mac_client_maps_window_scope_to_document_object():
    bus = MacInkscapeDBus("unix:path=/tmp/nonexistent-bus")
    assert bus._scope_path("window", 3) == f"{DOCUMENT_PATH_PREFIX}/3"
    assert bus._scope_path("app", 3) == "/org/inkscape/Inkscape"
    assert bus.window_scope_available is False


def test_linux_client_unchanged():
    bus = InkscapeDBus()
    assert bus.bus_address == "SESSION"
    assert bus._scope_path("window", 2) == "/org/inkscape/Inkscape/window/2"
    assert bus.window_scope_available is True


def test_mac_client_unavailable_when_bus_missing(tmp_path):
    assert MacInkscapeDBus(f"unix:path={tmp_path / 'no-such-socket'}").is_available() is False


# ---- bus manager ------------------------------------------------------------------


def test_default_address_and_override(monkeypatch):
    assert mac_bus_manager.bus_address() == "unix:path=/tmp/inkscape-bus"
    monkeypatch.setenv("INKSCAPE_MCP_DBUS_ADDRESS", "unix:path=/tmp/other")
    assert mac_bus_manager.bus_address() == "unix:path=/tmp/other"


@pytest.mark.parametrize(
    "address,expected",
    [
        ("unix:path=/tmp/inkscape-bus", Path("/tmp/inkscape-bus")),
        ("unix:path=/tmp/x,guid=abc", Path("/tmp/x")),
        ("unix:abstract=foo", None),
        ("tcp:host=localhost,port=1", None),
    ],
)
def test_socket_path(address, expected):
    assert mac_bus_manager._socket_path(address) == expected


def test_ensure_bus_reuses_reachable_daemon(monkeypatch):
    monkeypatch.setattr(mac_bus_manager, "_probe_bus", lambda addr: None)
    monkeypatch.setattr(mac_bus_manager.subprocess, "run", MagicMock(side_effect=AssertionError("spawned")))
    assert mac_bus_manager.ensure_bus() == "unix:path=/tmp/inkscape-bus"


def test_ensure_bus_starts_daemon_and_clears_refused_socket(monkeypatch, tmp_path):
    sock = tmp_path / "bus"
    sock.write_text("stale")
    monkeypatch.setenv("INKSCAPE_MCP_DBUS_ADDRESS", f"unix:path={sock}")
    monkeypatch.setattr(mac_bus_manager, "find_dbus_daemon", lambda: "/opt/homebrew/bin/dbus-daemon")
    probes = iter(["refused", None])
    monkeypatch.setattr(mac_bus_manager, "_probe_bus", lambda addr: next(probes))
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(mac_bus_manager.subprocess, "run", run)

    assert mac_bus_manager.ensure_bus() == f"unix:path={sock}"
    assert not sock.exists()
    argv = run.call_args.args[0]
    assert argv == ["/opt/homebrew/bin/dbus-daemon", "--session", f"--address=unix:path={sock}", "--fork"]


def test_ensure_bus_never_replaces_a_socket_that_may_be_live(monkeypatch, tmp_path):
    """A probe that fails for any reason other than 'refused' must not orphan the daemon."""
    sock = tmp_path / "bus"
    sock.write_text("live")
    monkeypatch.setenv("INKSCAPE_MCP_DBUS_ADDRESS", f"unix:path={sock}")
    monkeypatch.setattr(mac_bus_manager, "_probe_bus", lambda addr: "TimeoutError: timed out")
    monkeypatch.setattr(mac_bus_manager.subprocess, "run", MagicMock(side_effect=AssertionError("spawned")))

    with pytest.raises(mac_bus_manager.BusError, match="not replacing it"):
        mac_bus_manager.ensure_bus()
    assert sock.exists()


def test_probe_distinguishes_absent_and_refused():
    import socket
    import tempfile

    # Short dir: macOS caps AF_UNIX paths at 104 bytes, which pytest's tmp_path exceeds.
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        path = Path(d) / "s"
        assert mac_bus_manager._probe_bus(f"unix:path={path}") == "absent"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.close()  # socket file remains, nobody listening
        assert mac_bus_manager._probe_bus(f"unix:path={path}") == "refused"


def test_ensure_bus_explains_missing_daemon(monkeypatch):
    monkeypatch.setattr(mac_bus_manager, "_probe_bus", lambda addr: "absent")
    monkeypatch.setattr(mac_bus_manager, "find_dbus_daemon", lambda: None)
    with pytest.raises(mac_bus_manager.BusError, match="brew install dbus"):
        mac_bus_manager.ensure_bus()


def test_ensure_inkscape_launches_under_bus(monkeypatch, tmp_path):
    exe = tmp_path / "inkscape"
    exe.write_text("")
    registered = iter([False, True])
    monkeypatch.setattr(mac_bus_manager, "_inkscape_registered", lambda addr: next(registered))
    popen = MagicMock()
    monkeypatch.setattr(mac_bus_manager.subprocess, "Popen", popen)

    assert mac_bus_manager.ensure_inkscape("unix:path=/tmp/b", str(exe), "/tmp/doc.svg") is True
    assert popen.call_args.args[0] == [str(exe), "/tmp/doc.svg"]
    assert popen.call_args.kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/b"
    assert popen.call_args.kwargs["start_new_session"] is True
    # Claude Code's ~1M RLIMIT_NOFILE crashed every extension child Inkscape forked.
    assert popen.call_args.kwargs["preexec_fn"] is mac_bus_manager._cap_open_files


# ---- graceful window-scope failures -----------------------------------------------


def test_auto_save_skipped_with_reason_when_document_save_unreachable():
    bus = _mac_bus(document=["undo", "redo"])
    saved, reason = live._save_active_document(bus, 1)
    assert saved is False
    assert "macOS" in reason and "Cmd+S" in reason
    bus.activate.assert_not_called()


def test_auto_save_fires_if_document_scope_has_document_save():
    bus = _mac_bus(document=["document-save"])
    assert live._save_active_document(bus, 1) == (True, "")
    bus.activate.assert_called_once_with("document-save", scope="window", window_id=1)


def test_apply_action_window_only_action_fails_clearly_on_mac():
    bus = _mac_bus(app=["object-flip-horizontal"], document=["undo"])
    result = live._op_apply_action(bus, "apply_action", "dialog-open", "XMLEditor", 1, 0.0)
    assert result["success"] is False
    assert result["error"] == "unavailable on macOS"
    assert "window-scope" in result["message"]
    bus.activate.assert_not_called()


def test_apply_action_document_scope_action_works_on_mac():
    bus = _mac_bus(app=[], document=["undo"])
    result = live._op_apply_action(bus, "apply_action", "undo", "", 1, 0.0)
    assert result["success"] is True
    assert result["data"]["scope"] == "window"


def test_ping_reports_missing_window_scope():
    bus = _mac_bus(app=["a", "b"], document=["undo"])
    bus.is_available.return_value = True
    result = live._op_ping(bus, "ping", 0.0)
    assert result["success"] is True
    assert result["data"]["window_scope_available"] is False
    assert "macOS" in result["message"]


def test_ping_down_explains_owned_instance():
    bus = _mac_bus()
    bus.is_available.return_value = False
    result = live._op_ping(bus, "ping", 0.0)
    assert result["success"] is False
    assert "launches and owns" in result["message"]


async def test_list_actions_labels_document_scope(monkeypatch):
    monkeypatch.setattr(live, "_load_action_descriptions", lambda *a: {})
    bus = _mac_bus(app=["a"], document=["undo"])
    result = await live._op_list_actions(bus, "list_actions", 1, "", 0.0)
    assert "document actions" in result["message"]
    assert result["data"]["window_scope_available"] is False


# ---- install location -------------------------------------------------------------


def test_extensions_install_under_application_support_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    expected = Path.home() / "Library/Application Support/org.inkscape.Inkscape/config/inkscape/extensions/inkscape_mcp"
    assert extension_bridge._user_extensions_dir() == expected


def test_extensions_install_under_xdg_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert extension_bridge._user_extensions_dir() == Path.home() / ".config/inkscape/extensions/inkscape_mcp"


# ---- auto-launch ------------------------------------------------------------------


@pytest.fixture
def _down_mac_bridge(monkeypatch):
    bus = _mac_bus()
    bus.is_available.return_value = False
    monkeypatch.setattr(live, "_get_bus", lambda config=None: bus)
    monkeypatch.setattr(live.mac_bus_manager, "is_macos", lambda: True)
    monkeypatch.setattr(live.bus_manager, "is_windows", lambda: False)
    launches: list = []

    def _launch(config, open_path=None):
        launches.append(open_path)
        bus.is_available.return_value = True
        return True

    monkeypatch.setattr(live, "_auto_launch_inkscape", _launch)
    return bus, launches


async def test_open_file_launches_straight_onto_the_file(_down_mac_bridge, tmp_path):
    """Launching on the default canvas and then opening the file left two documents."""
    bus, launches = _down_mac_bridge
    doc = tmp_path / "fig.svg"
    doc.write_text("<svg/>")
    result = await live.inkscape_live("open_file", target=str(doc))
    assert result["success"] is True
    assert result["data"] == {"path": str(doc), "launched": True}
    assert launches == [str(doc)]
    bus.open_file.assert_not_called()


async def test_other_ops_still_launch_the_default_canvas(_down_mac_bridge):
    _, launches = _down_mac_bridge
    await live.inkscape_live("delete_selected", window_id=0)
    assert launches == [None]
