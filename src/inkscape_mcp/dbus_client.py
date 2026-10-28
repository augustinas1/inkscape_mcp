"""D-Bus client for talking to a running Inkscape GUI instance."""

from __future__ import annotations

import os
import socket
import xml.etree.ElementTree as ET
from typing import Any

from jeepney import DBusAddress, MessageType, new_method_call
from jeepney.auth import BEGIN, Authenticator
from jeepney.bus import get_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection
from jeepney.wrappers import DBusErrorResponse

# Bus name and root object path are fixed by Inkscape's GApplication setup.
BUS_NAME = "org.inkscape.Inkscape"
APP_PATH = "/org/inkscape/Inkscape"
WINDOW_PATH_PREFIX = "/org/inkscape/Inkscape/window"
DOCUMENT_PATH_PREFIX = "/org/inkscape/Inkscape/document"

_FALSEY = frozenset({"", "0", "false", "no", "off", "none"})


def coerce_bool(value: Any) -> bool:
    """Interpret an action parameter as a boolean.

    Action payloads arrive as strings from the MCP surface, and plain ``bool("false")`` is
    ``True`` — so every ``b``-signature toggle was impossible to switch off (asking to
    disable ``export-overwrite`` enabled it). Match the textual forms callers actually send.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY
    return bool(value)


class InkscapeDBus:
    """Blocking jeepney bridge to a running Inkscape 1.4.x GUI on the session bus."""

    # Where "window"-scope actions live and whether the platform publishes real window
    # actions (dialogs, tools, zoom, document-save) at all. Overridden on macOS.
    window_path_prefix = WINDOW_PATH_PREFIX
    window_scope_available = True

    def __init__(self, bus_address: str = "SESSION") -> None:
        self._bus_address = bus_address
        self._conn: DBusConnection | None = None

    @property
    def bus_address(self) -> str:
        return self._bus_address

    def _open(self) -> DBusConnection:
        return open_dbus_connection(bus=self._bus_address)

    def _connect(self) -> DBusConnection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: S110 — best-effort cleanup; failure here cannot be acted on
                pass
            self._conn = None

    def _send(self, msg: Any) -> Any:
        # Reconnect once on a broken pipe; otherwise propagate. Raise on D-Bus errors.
        try:
            conn = self._connect()
            reply = conn.send_and_get_reply(msg)
        except (BrokenPipeError, ConnectionError, OSError):
            self._close()
            conn = self._connect()
            reply = conn.send_and_get_reply(msg)
        if reply.header.message_type == MessageType.error:
            raise DBusErrorResponse(reply)
        return reply

    def _scope_path(self, scope: str, window_id: int) -> str:
        if scope == "app":
            return APP_PATH
        if scope == "window":
            return f"{self.window_path_prefix}/{window_id}"
        raise ValueError(f"scope must be 'app' or 'window', got {scope!r}")

    def is_available(self) -> bool:
        addr = DBusAddress(
            object_path=APP_PATH,
            bus_name=BUS_NAME,
            interface="org.freedesktop.DBus.Peer",
        )
        msg = new_method_call(addr, "Ping", "", ())
        try:
            self._send(msg)
            return True
        except (DBusErrorResponse, OSError, ConnectionError, KeyError, FileNotFoundError):
            return False

    def list_actions(self, scope: str = "app", window_id: int = 1) -> list[str]:
        addr = DBusAddress(
            object_path=self._scope_path(scope, window_id),
            bus_name=BUS_NAME,
            interface="org.gtk.Actions",
        )
        msg = new_method_call(addr, "List", "", ())
        reply = self._send(msg)
        return list(reply.body[0])

    def describe(self, action: str, scope: str = "app", window_id: int = 1) -> tuple[bool, str, list]:
        """Return (enabled, param_signature, defaults)."""
        addr = DBusAddress(
            object_path=self._scope_path(scope, window_id),
            bus_name=BUS_NAME,
            interface="org.gtk.Actions",
        )
        msg = new_method_call(addr, "Describe", "s", (action,))
        reply = self._send(msg)
        enabled, signature, defaults = reply.body[0]
        return bool(enabled), str(signature), list(defaults)

    def _wrap_variant(self, signature: str, value: Any) -> tuple[str, Any]:
        # gtk action params are nearly always one of these scalar types.
        if signature == "s":
            return ("s", str(value))
        if signature == "i":
            return ("i", int(value))
        if signature == "u":
            return ("u", int(value))
        if signature == "x":
            return ("x", int(value))
        if signature == "t":
            return ("t", int(value))
        if signature == "d":
            return ("d", float(value))
        if signature == "b":
            return ("b", coerce_bool(value))
        # Fall back to passing the value through with the declared signature.
        return (signature, value)

    def activate(
        self,
        action: str,
        args: list[Any] | None = None,
        scope: str = "app",
        window_id: int = 1,
    ) -> None:
        _, signature, _ = self.describe(action, scope=scope, window_id=window_id)
        variants: list[tuple[str, Any]] = []
        if signature:
            if not args:
                raise ValueError(f"action {action!r} requires a parameter of signature {signature!r}")
            variants.append(self._wrap_variant(signature, args[0]))

        addr = DBusAddress(
            object_path=self._scope_path(scope, window_id),
            bus_name=BUS_NAME,
            interface="org.gtk.Actions",
        )
        msg = new_method_call(addr, "Activate", "sava{sv}", (action, variants, {}))
        self._send(msg)

    def open_file(self, path: str) -> None:
        abs_path = os.path.abspath(path)
        addr = DBusAddress(
            object_path=APP_PATH,
            bus_name=BUS_NAME,
            interface="org.freedesktop.Application",
        )
        msg = new_method_call(addr, "Open", "asa{sv}", ([f"file://{abs_path}"], {}))
        self._send(msg)

    def list_windows(self) -> list[int]:
        addr = DBusAddress(
            object_path=self.window_path_prefix,
            bus_name=BUS_NAME,
            interface="org.freedesktop.DBus.Introspectable",
        )
        msg = new_method_call(addr, "Introspect", "", ())
        reply = self._send(msg)
        xml = reply.body[0]
        root = ET.fromstring(xml)  # noqa: S314 — XML produced by trusted local D-Bus introspection (org.freedesktop.DBus.Introspectable)
        ids: list[int] = []
        for node in root.findall("node"):
            name = node.get("name")
            if name and name.isdigit():
                ids.append(int(name))
        return sorted(ids)


class MacInkscapeDBus(InkscapeDBus):
    """jeepney bridge to an Inkscape launched under the MCP's private macOS bus.

    Inkscape on macOS publishes app actions at ``/org/inkscape/Inkscape`` and document
    actions at ``.../document/N``, but no ``.../window/N`` objects at all — so, as on
    Windows, the "window" scope addresses the document object, and true window actions
    (dialogs, tool switching, zoom, document-save) are unreachable.
    """

    window_path_prefix = DOCUMENT_PATH_PREFIX
    window_scope_available = False

    def _open(self) -> DBusConnection:
        return open_macos_connection(self._bus_address)


def open_macos_connection(address: str, timeout: float = 2.0) -> DBusConnection:
    """Open a jeepney connection to a unix-socket bus on macOS.

    jeepney's own ``open_dbus_connection`` sees ``socket.SCM_CREDS`` and sends a
    BSD-style credentials message, which macOS rejects with EINVAL. The macOS
    dbus-daemon reads peer credentials off the socket itself, so the plain NUL byte
    that jeepney sends on Linux is all the EXTERNAL handshake needs.
    """
    sock = socket.socket(family=socket.AF_UNIX)
    try:
        sock.settimeout(timeout)
        sock.connect(get_bus(address))
        sock.send(b"\0")
        authr = Authenticator(enable_fds=False, inc_null_byte=False)
        for req_data in authr:
            sock.sendall(req_data)
            data = sock.recv(1024)
            if not data:
                raise ConnectionResetError("bus closed the connection during authentication")
            authr.feed(data)
        sock.sendall(BEGIN)
    except BaseException:
        sock.close()
        raise
    sock.settimeout(None)
    return DBusConnection(sock)
