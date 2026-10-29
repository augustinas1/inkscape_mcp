"""macOS session-bus + Inkscape auto-manager for the live bridge.

The macOS counterpart of ``bus_manager`` (Windows). macOS has no autostarted D-Bus
session bus — Homebrew's ``dbus-launch`` expects a launchd socket and fails — and a
normally-launched Inkscape (Dock, Finder, ``open -a``) is not on any bus. So, as on
Windows, the server:

  1. ensures a private ``dbus-daemon`` is listening on a fixed unix socket
     (``unix:path=/tmp/inkscape-bus`` unless overridden), and
  2. launches and owns its own Inkscape *under that bus* (``DBUS_SESSION_BUS_ADDRESS``
     set), so its ``org.gtk.Actions`` surface is exported.

Unlike Windows, jeepney speaks unix-socket addresses natively, so no ``gdbus`` binary is
needed: probing and the client itself both go through ``MacInkscapeDBus``.

Discovery / overrides (env):
  INKSCAPE_MCP_DBUS_ADDRESS   bus address (default ``unix:path=/tmp/inkscape-bus``)
  INKSCAPE_MCP_DBUS_DAEMON    full path to dbus-daemon (Homebrew: ``brew install dbus``)
  INKS_INKSCAPE_BIN / INKSCAPE_BIN   inkscape binary (already used elsewhere)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from jeepney import DBusAddress, new_method_call
from jeepney.auth import AuthenticationError

from .bus_manager import BusError, default_canvas
from .dbus_client import MacInkscapeDBus, open_macos_connection

log = logging.getLogger(__name__)

DEFAULT_ADDRESS = "unix:path=/tmp/inkscape-bus"

# Homebrew's prefixes; an MCP server launched by a GUI client often lacks them on PATH.
_DEFAULT_DBUS_DAEMONS = (
    "/opt/homebrew/bin/dbus-daemon",
    "/usr/local/bin/dbus-daemon",
)

# First launch on macOS rebuilds the font cache and can take well over the Windows budget.
_INKSCAPE_REGISTER_TIMEOUT_S = 60.0
_BUS_START_TIMEOUT_S = 10.0
# macOS OPEN_MAX: ample for Inkscape, small enough for glib's pre-exec fd walk.
_MAX_OPEN_FILES = 10240
_POLL_INTERVAL_S = 0.3


def is_macos() -> bool:
    return sys.platform == "darwin"


def bus_address() -> str:
    return os.environ.get("INKSCAPE_MCP_DBUS_ADDRESS") or DEFAULT_ADDRESS


def _socket_path(address: str) -> Path | None:
    """Filesystem path of a ``unix:path=...`` address, or None for any other kind."""
    if not address.startswith("unix:"):
        return None
    for part in address[len("unix:") :].split(","):
        key, _, value = part.partition("=")
        if key == "path":
            return Path(value)
    return None


def find_dbus_daemon() -> str | None:
    override = os.environ.get("INKSCAPE_MCP_DBUS_DAEMON")
    if override and Path(override).exists():
        return override
    found = shutil.which("dbus-daemon")
    if found:
        return found
    for cand in _DEFAULT_DBUS_DAEMONS:
        if Path(cand).exists():
            return cand
    return None


# ---- bus lifecycle -----------------------------------------------------------------


def _probe_bus(address: str) -> str | None:
    """Return None if a dbus-daemon is answering at ``address``, else why not.

    The reason distinguishes "nothing is listening" (``"absent"`` / ``"refused"``, safe
    to start a fresh daemon) from every other failure, where a daemon may well be alive.
    """
    try:
        conn = open_macos_connection(address)
    except FileNotFoundError:
        return "absent"
    except ConnectionRefusedError:
        return "refused"
    except (OSError, ValueError, AuthenticationError) as exc:
        return f"{type(exc).__name__}: {exc}"
    try:
        addr = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus", interface="org.freedesktop.DBus")
        conn.send_and_get_reply(new_method_call(addr, "GetId"), timeout=5)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def _bus_reachable(address: str) -> bool:
    """True if a dbus-daemon is answering at ``address`` (independent of Inkscape)."""
    return _probe_bus(address) is None


def ensure_bus() -> str:
    """Return a reachable session-bus address, reusing or starting a dbus-daemon.

    The address is fixed, so a daemon left running by an earlier MCP server is simply
    reused. The socket file is removed only when connecting to it is *refused* — proof
    that its daemon is dead. Any other probe failure raises instead: deleting the socket
    of a live daemon would orphan it, and the Inkscape on it, for good.
    """
    address = bus_address()
    reason = _probe_bus(address)
    if reason is None:
        return address
    if reason not in ("absent", "refused"):
        raise BusError(f"a dbus-daemon socket exists at {address} but did not answer ({reason}); not replacing it")

    daemon = find_dbus_daemon()
    if not daemon:
        raise BusError("dbus-daemon not found. Install it ('brew install dbus') or set INKSCAPE_MCP_DBUS_DAEMON.")

    sock = _socket_path(address)
    if reason == "refused" and sock is not None and sock.exists():
        sock.unlink()

    # --fork daemonises, so the bus outlives this server; the explicit --address
    # overrides the launchd listener in Homebrew's session.conf, which can't work here.
    proc = subprocess.run(  # noqa: S603 — fixed daemon path, address from env/default
        [daemon, "--session", f"--address={address}", "--fork"],
        capture_output=True,
        text=True,
        timeout=_BUS_START_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise BusError(f"dbus-daemon failed to start: {(proc.stderr or proc.stdout).strip()}")

    deadline = time.monotonic() + _BUS_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if _bus_reachable(address):
            log.info("Started dbus-daemon session bus at %s", address)
            return address
        time.sleep(_POLL_INTERVAL_S)
    raise BusError(f"dbus-daemon did not become reachable at {address} in time")


# ---- inkscape lifecycle ------------------------------------------------------------


def _inkscape_registered(address: str) -> bool:
    """True once Inkscape has a document object on the bus (see ``bus_manager``)."""
    bus = MacInkscapeDBus(address)
    try:
        return bool(bus.list_windows())
    except Exception:
        return False
    finally:
        bus._close()


def _cap_open_files() -> None:
    """Lower the soft open-files limit in the Inkscape child before exec.

    Claude Code raises RLIMIT_NOFILE to ~1M, and every process it spawns inherits that.
    Inkscape's bundled glib sizes its pre-exec fd walk by that limit, so each extension
    run (every bridge edit and inspect is one) overflowed the forked child's stack and
    crashed it before the script started — the call then timed out.
    """
    if sys.platform != "win32":  # resource is POSIX-only; this module is imported on Windows too
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft == resource.RLIM_INFINITY or soft > _MAX_OPEN_FILES:
            resource.setrlimit(resource.RLIMIT_NOFILE, (_MAX_OPEN_FILES, hard))


def ensure_inkscape(address: str, inkscape_executable: str, open_path: str | None = None) -> bool:
    """Ensure Inkscape is running on ``address``; launch it under the bus if not.

    Returns True once Inkscape is registered. The process is started in its own session
    so restarting or closing the MCP server does not take the user's editor with it.
    """
    if _inkscape_registered(address):
        return True

    if not inkscape_executable or not Path(inkscape_executable).exists():
        raise BusError(f"Inkscape executable not found: {inkscape_executable!r}")

    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = address
    argv = [inkscape_executable, open_path or default_canvas()]

    log.info("Launching Inkscape under MCP session bus: %s", argv)
    subprocess.Popen(  # noqa: S603 — inkscape path validated above
        argv,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        preexec_fn=_cap_open_files,
    )

    deadline = time.monotonic() + _INKSCAPE_REGISTER_TIMEOUT_S
    while time.monotonic() < deadline:
        if _inkscape_registered(address):
            return True
        time.sleep(_POLL_INTERVAL_S)
    return False
