"""Live-Inkscape bridge: drive the running GUI via D-Bus + clipboard."""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from lxml import etree
from pydantic import BaseModel

from .. import bus_manager, change_tracker, mac_bus_manager
from ..clipboard import SVG_NS
from ..dbus_client import InkscapeDBus, MacInkscapeDBus
from ..extension_bridge import invoke_extension
from ..inkscape_detector import MACOS_APP_BINARIES
from ..win_dbus_client import WinInkscapeDBus


class LiveResult(BaseModel):
    success: bool
    operation: str
    message: str
    data: dict[str, Any]
    execution_time_ms: float
    error: str = ""


INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"

_BUS: InkscapeDBus | None = None

_SCRATCH_DIR = Path(os.path.expanduser("~/.cache/inkscape_mcp"))
# How long we wait for the exported file to appear after triggering export-do.
_EXPORT_POLL_TIMEOUT_S = 5.0
_EXPORT_POLL_INTERVAL_S = 0.05

_AUTO_SAVE_ENV = "INKSCAPE_MCP_AUTO_SAVE"
_AUTO_CLEANUP_ENV = "INKSCAPE_MCP_AUTO_CLEANUP"

# Scratch snapshots/rasters are debugging aids the agent may still be holding a path to,
# so keep the recent ones; anything older than this is dead weight.
_SCRATCH_MAX_AGE_S = 24 * 60 * 60
_SCRATCH_KEEP = 20

# Actions that don't modify the document. After firing these we skip the implicit
# document-save. Curated; conservative — when in doubt, treat as mutating.
_READ_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        # Selection changes (select-*, not selection-* which mutates)
        "select-all",
        "select-clear",
        "select-none",
        "select-invert",
        "select-by-id",
        "select-by-selector",
        "select-by-element",
        "select-by-class",
        "select-list",
        "select-original",
        "select-same-fill",
        "select-same-stroke",
        # Clipboard read
        "copy",
        # Document lifecycle — saving these would recurse or is irrelevant
        "document-save",
        "document-save-as",
        "document-save-copy",
        "document-revert",
        "document-new",
        "document-close",
    }
)

_READ_ONLY_PREFIXES: tuple[str, ...] = (
    "zoom-",
    "view-",
    "canvas-",
    "window-",
    "focus-",
    "export-",
    "list-",
    "query-",
    "help-",
    "about-",
    "dialog-",
)


MACOS_NO_WINDOW_SCOPE = (
    "Inkscape on macOS publishes no window-scope D-Bus actions (no /org/inkscape/Inkscape/window/N "
    "objects), so window actions — dialogs, tool switching, zoom, canvas/view toggles, "
    "document-save — are unavailable on macOS"
)


def _has_window_scope(bus: Any) -> bool:
    return getattr(bus, "window_scope_available", True)


def _action_mutates_document(action: str) -> bool:
    """True if firing `action` should be followed by document-save."""
    if action in _READ_ONLY_ACTIONS:
        return False
    return not any(action.startswith(p) for p in _READ_ONLY_PREFIXES)


def _save_active_document(bus: InkscapeDBus, window_id: int) -> tuple[bool, str]:
    """Persist the edited document, or explain why we declined to.

    There is a scope mismatch at the heart of this. Every MCP edit lands in the ACTIVE
    document (app-scope actions and effect extensions both go there), but `document-save`
    exists only at window scope — it isn't even in `--action-list`. With one window the
    two are necessarily the same document. With several, firing save at a window we
    merely guessed would save some *other* document and leave the edited one dirty on
    disk, so we skip it and say so rather than persisting the wrong file.

    Returns (saved, reason-when-not-saved).
    """
    if os.environ.get(_AUTO_SAVE_ENV) == "0":
        return False, "auto-save disabled via INKSCAPE_MCP_AUTO_SAVE=0"
    try:
        windows = bus.list_windows()
    except Exception:
        windows = []
    if len(windows) > 1:
        return False, (
            f"{len(windows)} documents are open, so the edited (active) document cannot be "
            "matched to a window for saving — the edit is applied in Inkscape but NOT written "
            "to disk. Save in the GUI, or work with a single document open."
        )
    if not _has_window_scope(bus):
        try:
            reachable = "document-save" in bus.list_actions("window", window_id)
        except Exception:
            reachable = False
        if not reachable:
            return False, (
                f"auto-save skipped: {MACOS_NO_WINDOW_SCOPE}. The edit is applied in Inkscape "
                "but NOT written to disk — save in the GUI (Cmd+S)."
            )
    try:
        bus.activate("document-save", scope="window", window_id=window_id)
        return True, ""
    except Exception as exc:
        return False, f"document-save failed: {exc}"


def _activate_with_optional_save(
    bus: InkscapeDBus,
    action: str,
    args: list[Any] | None = None,
    scope: str = "app",
    window_id: int = 1,
) -> bool:
    """Fire `action`, then fire document-save if `action` mutates the document.
    Returns True if save was fired. Save failure is swallowed — the primary edit
    succeeded, and a stale on-disk file shouldn't block the agent."""
    bus.activate(action, args, scope=scope, window_id=window_id)
    if not _action_mutates_document(action):
        return False
    saved, _ = _save_active_document(bus, window_id)
    return saved


def _resolve_window_id(bus: InkscapeDBus, requested: int) -> int:
    """Return `requested` if it's an open Inkscape window; otherwise fall back
    to the first available window. Lets callers pass the default
    ``window_id=1`` and still work after the user closes window 1 or opens
    new docs (window 2, 3, …).

    Raises ``RuntimeError`` if no windows are open at all — callers should
    have checked ``bus.is_available()`` first."""
    try:
        windows = bus.list_windows()
    except Exception:
        return requested  # Best-effort: fall through with what was asked.
    if not windows:
        raise RuntimeError("no Inkscape windows are open")
    if requested in windows:
        return requested
    return windows[0]


def _resolve_inkscape_exe(config: Any) -> str:
    exe = getattr(config, "inkscape_executable", None) if config is not None else None
    exe = exe or os.environ.get("INKS_INKSCAPE_BIN") or os.environ.get("INKSCAPE_BIN")
    if not exe and mac_bus_manager.is_macos():
        exe = next((str(p) for p in MACOS_APP_BINARIES if p.is_file()), None)
    exe = exe or "inkscape"
    # Inkscape's detector may return inkscape.COM (console wrapper); the GUI binary
    # inkscape.exe sits beside it and launches without spawning a console window.
    if exe.lower().endswith("inkscape.com"):
        gui = Path(exe).with_name("inkscape.exe")
        if gui.exists():
            return str(gui)
    return exe


def _get_bus(config: Any = None) -> Any:
    """Return the platform-appropriate live bridge.

    Windows has no autostarted D-Bus session bus and jeepney can't speak the TCP bus, so
    we use a ``gdbus.exe``-backed client bound to a managed session bus (see
    ``bus_manager``). macOS likewise has no session bus, so it gets a jeepney client bound
    to the MCP's private unix-socket bus (see ``mac_bus_manager``). Linux keeps the plain
    session-bus jeepney client. The Windows client is cheap to build and bound to a
    possibly-changing bus address, so it is not cached.
    """
    global _BUS
    if bus_manager.is_windows():
        inkscape_exe = _resolve_inkscape_exe(config)
        gdbus = bus_manager.find_gdbus(inkscape_exe)
        if not gdbus:
            raise RuntimeError("gdbus.exe not found (expected next to inkscape.exe or on PATH)")
        address = bus_manager.ensure_bus(gdbus)
        return WinInkscapeDBus(gdbus, address)
    if mac_bus_manager.is_macos():
        address = mac_bus_manager.ensure_bus()
        if not isinstance(_BUS, MacInkscapeDBus) or _BUS.bus_address != address:
            _BUS = MacInkscapeDBus(address)
        return _BUS
    if _BUS is None:
        _BUS = InkscapeDBus()
    return _BUS


def _auto_launch_inkscape(config: Any, open_path: str | None = None) -> bool:
    """Windows/macOS auto-manage: ensure a bus + a running Inkscape under it. Returns ready.

    ``open_path`` (macOS only) is the document to launch onto instead of the default canvas.
    """
    inkscape_exe = _resolve_inkscape_exe(config)
    if mac_bus_manager.is_macos():
        return mac_bus_manager.ensure_inkscape(mac_bus_manager.ensure_bus(), inkscape_exe, open_path=open_path)
    gdbus = bus_manager.find_gdbus(inkscape_exe)
    if not gdbus:
        raise RuntimeError("gdbus.exe not found (expected next to inkscape.exe or on PATH)")
    address = bus_manager.ensure_bus(gdbus)
    return bus_manager.ensure_inkscape(gdbus, address, inkscape_exe)


def _result(
    operation: str,
    success: bool,
    message: str,
    data: dict[str, Any] | None = None,
    error: str = "",
    start: float | None = None,
) -> dict[str, Any]:
    elapsed = (time.perf_counter() - start) * 1000.0 if start is not None else 0.0
    return LiveResult(
        success=success,
        operation=operation,
        message=message,
        data=data or {},
        execution_time_ms=elapsed,
        error=error,
    ).model_dump()


# Ops that describe the whole Inkscape instance rather than one document, so naming a
# window is unambiguous even with several open.
_WINDOW_AGNOSTIC_OPS = frozenset({"ping", "list_actions", "open_file"})


def _looks_like_selector(target: str) -> bool:
    # CSS-ish heuristic: '#', '.', '[', ' ', '>' etc.
    return any(ch in target for ch in "#.[ >:,*")


def _prune_scratch(max_age_s: float = _SCRATCH_MAX_AGE_S, keep: int = _SCRATCH_KEEP) -> int:
    """Drop old scratch snapshots/rasters. Every snapshot and rasterize wrote a
    uuid-named file here and nothing ever removed them."""
    try:
        files = [p for p in _SCRATCH_DIR.glob("*") if p.is_file()]
    except OSError:
        return 0
    now = time.time()
    # Keep the most recent `keep` regardless of age — a caller may still hold the path.
    stale = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[keep:]
    removed = 0
    for p in stale:
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _scratch_path() -> Path:
    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    if os.environ.get(_AUTO_CLEANUP_ENV) != "0":
        _prune_scratch()
    return _SCRATCH_DIR / f"snap-{uuid.uuid4().hex}.svg"


def _export_document(bus: InkscapeDBus, scratch: Path) -> None:
    """Drive Inkscape's export-* actions to dump the current doc as plain-ish SVG.

    Blocking: busy-polls for up to `_EXPORT_POLL_TIMEOUT_S`. Kept for synchronous
    callers and tests; anything on the request path must use `_export_document_async`.
    """
    if scratch.exists():
        scratch.unlink()
    _trigger_export(bus, scratch)

    deadline = time.monotonic() + _EXPORT_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        if scratch.exists() and scratch.stat().st_size > 0:
            return
        time.sleep(_EXPORT_POLL_INTERVAL_S)
    raise TimeoutError(f"Inkscape did not produce {scratch} within {_EXPORT_POLL_TIMEOUT_S}s")


def _trigger_export(bus: InkscapeDBus, scratch: Path) -> None:
    """Fire the export-* action sequence. Four fast local IPC round-trips."""
    bus.activate("export-filename", [str(scratch)], scope="app")
    bus.activate("export-type", ["svg"], scope="app")
    bus.activate("export-overwrite", [True], scope="app")
    bus.activate("export-do", scope="app")


async def _export_document_async(bus: InkscapeDBus, scratch: Path) -> None:
    """Export the live document without blocking the event loop.

    The old version busy-waited with time.sleep for up to 5s on the request path, which
    froze every other in-flight tool call. Only the *wait* is slow — the D-Bus activates
    are sub-millisecond — so the poll simply yields instead. The activates stay on the
    event loop thread on purpose: InkscapeDBus shares one jeepney connection, and running
    it in a worker thread alongside main-thread users corrupts the reply stream.

    Serialised under BRIDGE_LOCK so a concurrent extension invocation can't interleave
    its own export-* settings with ours.
    """
    from ..extension_bridge import BRIDGE_LOCK

    cached = _SNAPSHOT_CACHE.get("bytes")
    if _SNAPSHOT_REUSE.get() and cached and time.monotonic() - _SNAPSHOT_CACHE["at"] < _SNAPSHOT_TTL_S:
        scratch.write_bytes(cached)  # a read-only call right after another: same document
        return
    async with BRIDGE_LOCK:
        if scratch.exists():
            scratch.unlink()
        _trigger_export(bus, scratch)

        deadline = time.monotonic() + _EXPORT_POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            if scratch.exists() and scratch.stat().st_size > 0:
                if _SNAPSHOT_REUSE.get():
                    _SNAPSHOT_CACHE.update(bytes=scratch.read_bytes(), at=time.monotonic())
                return
            await asyncio.sleep(_EXPORT_POLL_INTERVAL_S)
        raise TimeoutError(f"Inkscape did not produce {scratch} within {_EXPORT_POLL_TIMEOUT_S}s")


# Back-to-back read-only calls (outline, then preview, then rasterize) share one export: a large
# figure takes a second or more to export each time. Any other operation clears the cache, and
# the TTL bounds how long a user's edit in the GUI could go unseen.
_SNAPSHOT_TTL_S = 3.0
_SNAPSHOT_CACHE: dict[str, Any] = {}
_SNAPSHOT_REUSE: contextvars.ContextVar[bool] = contextvars.ContextVar("snapshot_reuse", default=False)
_READ_ONLY_OPS = frozenset({"outline", "get_document_xml", "preview", "rasterize", "selection", "changes", "measure"})
# Operations that leave the drawing as it is (the selection, the view or a file aside), and
# those whose every change goes through edit_xml (so a check-only run changes nothing).
_NO_DOC_CHANGE = _READ_ONLY_OPS | {
    "ping", "get_selection", "set_selection", "list_actions", "save_snapshot", "save",
    "inspect_selection", "inspect_layers", "inspect_defs", "inspect_view", "inspect_pages", "inspect_element",
}  # fmt: skip
_EDITS_VIA_EDIT_XML = frozenset({"edit_xml", "transplant", "organize", "label", "cleanup", "parked", "component"})

# Each Inkscape CLI call is a fresh ~0.5 s process: independent ones run together, three at most.
_CLI_SLOTS = asyncio.Semaphore(3)

# Per live operation: how many edit_xml runs were folded into the change baseline, and how many
# were not. An operation that changed the document without a clean fold marks the baseline dirty.
_EDIT_FOLDS: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar("edit_folds", default=None)


_SERVER_STARTED = time.time()
_PACKAGE_DIR = Path(__file__).resolve().parent.parent


def _server_freshness() -> dict[str, Any]:
    """When this server process started, and whether its source changed since (needs a restart)."""
    newest = max((f.stat().st_mtime for f in _PACKAGE_DIR.rglob("*.py")), default=0.0)
    info: dict[str, Any] = {"server_started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_SERVER_STARTED))}
    if newest > _SERVER_STARTED:
        info["stale"] = True
        info["stale_note"] = (
            "server code changed after this process started — restart it (/mcp → reconnect, or "
            "restart the client) to load the new operations"
        )
    return info


def _op_ping(bus: InkscapeDBus, operation: str, start: float) -> dict[str, Any]:
    freshness = _server_freshness()
    if not bus.is_available():
        message = "Inkscape D-Bus bridge not reachable (is the GUI running?)"
        if not _has_window_scope(bus):
            message = (
                "Inkscape D-Bus bridge not reachable. On macOS the server launches and owns its "
                "own Inkscape on the first non-ping live operation; an Inkscape you started "
                "yourself is not on the MCP's bus."
            )
        return _result(
            operation,
            False,
            message,
            data={"ok": False, "windows": [], "app_actions": 0, "window_actions": 0, **freshness},
            start=start,
        )
    windows = bus.list_windows()
    app_actions = bus.list_actions("app")
    window_actions = bus.list_actions("window", windows[0]) if windows else []
    data: dict[str, Any] = {
        "ok": True,
        "windows": windows,
        "app_actions": len(app_actions),
        "window_actions": len(window_actions),
        **freshness,
    }
    message = f"Inkscape bridge live ({len(windows)} window(s))"
    if freshness.get("stale"):
        message += f"; WARNING: {freshness['stale_note']}"
    if not _has_window_scope(bus):
        # "windows" are document objects here and "window_actions" are document-scope.
        data["window_scope_available"] = False
        message += f"; {MACOS_NO_WINDOW_SCOPE}"
    return _result(
        operation,
        True,
        message,
        data=data,
        start=start,
    )


_INLINE_DOC_LIMIT = 40_000  # chars; beyond this the XML goes to a file (clients choke on MBs)


async def _op_get_document_xml(bus: InkscapeDBus, operation: str, target: str, start: float) -> dict[str, Any]:
    """The document's XML, or with `target` (an id) just that element's subtree.

    Small results come back inline; large ones as a file path (plus the first lines), so a
    1.7 MB figure does not flood the conversation — read the file, or narrow with `target`.
    """
    scratch = _scratch_path()
    await _export_document_async(bus, scratch)
    xml = scratch.read_text(encoding="utf-8")
    what = "document"
    if target:
        element_id = target.lstrip("#")
        doc = etree.fromstring(xml.encode("utf-8"), parser=etree.XMLParser(huge_tree=True))
        el = doc.find(f".//*[@id='{element_id}']")
        if el is None:
            return _result(operation, False, f"no element with id {element_id!r}", error="unknown id", start=start)
        xml = etree.tostring(el, encoding="unicode")
        scratch = scratch.with_name(f"{scratch.stem}-{re.sub(r'[^A-Za-z0-9_-]', '_', element_id)}.svg")
        scratch.write_text(xml, encoding="utf-8")
        what = f"#{element_id}"
    if len(xml) <= _INLINE_DOC_LIMIT:
        return _result(
            operation, True, f"{what} XML ({len(xml)} chars)", data={"xml": xml, "path": str(scratch)}, start=start
        )
    return _result(
        operation,
        True,
        f"{what} XML is {len(xml)} chars — written to {scratch} (not inlined). Read that file, or pass "
        "target=<id> for one subtree; outline is the cheap way to find ids.",
        data={"path": str(scratch), "chars": len(xml), "head": xml[:400]},
        start=start,
    )


async def _op_get_selection(bus: InkscapeDBus, operation: str, start: float) -> dict[str, Any]:
    """Read the current selection via the mcp_inspect extension.

    Identical surface to ``inspect_selection``. Returns ``{count, ids, objects[],
    text}`` — no longer touches the system clipboard.
    """
    return await _do_inspect(bus, "selection", operation, start)


def _op_set_selection(bus: InkscapeDBus, operation: str, target: str, start: float) -> dict[str, Any]:
    if not target:
        return _result(
            operation,
            False,
            "target (id or selector) is required",
            error="missing target",
            start=start,
        )
    action = "select-by-selector" if _looks_like_selector(target) else "select-by-id"
    bus.activate(action, [target], scope="app")
    return _result(
        operation,
        True,
        f"activated {action!r} for {target!r}",
        data={"action": action, "target": target},
        start=start,
    )


_FRAGMENT_NSDECLS = (
    f'xmlns="{SVG_NS}" xmlns:inkscape="{INKSCAPE_NS}" '
    'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
    'xmlns:xlink="http://www.w3.org/1999/xlink"'
)


def _fragment_children(payload: str) -> list[Any]:
    """Parse a payload into the elements to insert, accepting a wrapper or bare nodes.

    A bare fragment (`<rect .../>`) and a wrapped one (`<svg>…</svg>`) are both
    accepted: the clipboard route silently dropped the former while reporting success,
    which is exactly the kind of quiet failure this tool should not have.
    """
    text = payload.strip()
    if not text:
        return []
    parser = etree.XMLParser(remove_blank_text=False, recover=False)
    try:
        root = etree.fromstring(text.encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError:
        # Bare sibling nodes have no single root, or use inkscape:/sodipodi:/xlink: prefixes
        # without declaring them — wrap (declaring the usual prefixes) and retry.
        try:
            root = etree.fromstring(f"<svg {_FRAGMENT_NSDECLS}>{text}</svg>".encode(), parser=parser)
        except etree.XMLSyntaxError:
            return []
    local = etree.QName(root).localname if isinstance(root.tag, str) else ""
    if local == "svg":
        return [c for c in root if isinstance(c.tag, str)]
    return [root]


async def _op_path_offset(
    bus: InkscapeDBus,
    operation: str,
    target: str,
    payload_json: str,
    window_id: int,
    start: float,
) -> dict[str, Any]:
    """Offset a path's outline using Inkscape's own Offset live path effect.

    This is the GUI half of the operation `inkscape_vector` refuses. Inkscape's offset
    is unreachable headlessly — `path-outset` / `path-inset` / `path-offset` are all
    inert via the CLI, and the Offset LPE never recomputes on a headless export even
    when forced through `object-to-path`. In a running GUI Inkscape evaluates the LPE
    itself, so we attach the effect and let it do the geometry rather than
    reimplementing bezier offsetting.

    payload (JSON): {"offset": <number>, "unit": "px", "join": "miter|round|bevel"}
    Positive grows the outline, negative shrinks it.

    Magnitude caveat, measured against 1.4.4: Inkscape applies its own unit conversion
    to the LPE's `offset` and does not appear to honour `unit="px"` — a request of 10
    moved each edge by 10/(96/25.4) ≈ 2.65 user units. The *shape* of the change is a
    true offset (both axes grow by the same absolute amount, unlike a scale), but the
    exact magnitude follows Inkscape. Deliberately not compensated here: a fudge factor
    would be wrong at other document scales. Measure the result with `inspect_element`
    if the precise size matters.
    """
    if not target:
        return _result(operation, False, "target (path id) is required", error="missing target", start=start)
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError as exc:
        return _result(operation, False, f"payload is not valid JSON: {exc}", error=str(exc), start=start)
    if not isinstance(payload, dict):
        return _result(operation, False, "payload must decode to a JSON object", error="bad payload", start=start)

    try:
        offset = float(payload.get("offset", 0))
    except (TypeError, ValueError):
        return _result(operation, False, "'offset' must be a number", error="bad offset", start=start)
    if offset == 0:
        return _result(operation, False, "'offset' must be non-zero", error="bad offset", start=start)

    join = str(payload.get("join", "miter"))
    unit = str(payload.get("unit", "px"))
    effect_id = f"mcp-offset-{uuid.uuid4().hex[:8]}"

    # The effect definition lives in <defs>; the path then references it and keeps its
    # current `d` as `inkscape:original-d`, which is what the LPE reads from.
    lpe = (
        f'<inkscape:path-effect id="{effect_id}" effect="offset" '
        f'offset="{offset:g}" unit="{unit}" linejoin_type="{join}" miter_limit="4" '
        f'attempt_force_join="false" update_on_knot_move="true" is_visible="true" lpeversion="1"/>'
    )
    add_def = await _op_edit_xml(
        bus, operation, "/svg:svg/svg:defs", json.dumps({"action": "append", "xml": lpe}), window_id, start
    )
    if not add_def.get("success"):
        return add_def

    # `original-d` must carry the pre-effect geometry, so seed it from the current `d`.
    seed = await _op_execute_inkex(
        bus,
        operation,
        (
            f"p = svg.getElementById({target!r})\n"
            "if p is None:\n"
            "    set_result({'ok': False, 'error': 'no such path'})\n"
            "else:\n"
            "    d = p.get('d')\n"
            "    set_result({'ok': d is not None, 'd': d})\n"
        ),
        start,
    )
    seeded = (seed.get("data") or {}).get("result") or {}
    if not seeded.get("ok"):
        return _result(
            operation,
            False,
            f"{target!r} is not a path with a 'd' attribute — offset applies to paths only",
            data={"path_id": target},
            error="not a path",
            start=start,
        )

    applied = await _op_edit_xml(
        bus,
        operation,
        f'//*[@id="{target}"]',
        json.dumps({"action": "set_attr", "name": "inkscape:original-d", "value": seeded["d"]}),
        window_id,
        start,
    )
    if not applied.get("success"):
        return applied

    linked = await _op_edit_xml(
        bus,
        operation,
        f'//*[@id="{target}"]',
        json.dumps({"action": "set_attr", "name": "inkscape:path-effect", "value": f"#{effect_id}"}),
        window_id,
        start,
    )
    if linked.get("success"):
        linked["message"] = f"applied offset {offset:g}{unit} to {target!r} via the Offset LPE"
        linked["data"].update({"path_id": target, "offset": offset, "effect_id": effect_id})
    return linked


async def _op_insert_svg(
    bus: InkscapeDBus,
    operation: str,
    payload: str,
    window_id: int,
    start: float,
) -> dict[str, Any]:
    """Insert geometry by appending it to the document, not via the clipboard.

    The old route staged the fragment with `xclip` and fired `paste-in-place`. That was
    wrong three ways, all of them silent: `xclip` daemonises, so `subprocess.run`
    returned before it owned the selection and the paste could pick up the *previous*
    payload; paste-in-place renormalises geometry, so a rect authored at (250,200)
    landed at (0,-50), off-canvas; and a fragment without an `<svg>` wrapper was
    dropped entirely. All three reported `success: true, saved: true`.

    Appending through the edit_xml extension keeps the authored coordinates, has no
    clipboard to race, and returns a node count we can verify.
    """
    children = _fragment_children(payload)
    if not children:
        return _result(
            operation,
            False,
            (
                "payload contained no SVG elements to insert — expected an element, a list of "
                "sibling elements, or an <svg>…</svg> wrapper"
                if payload.strip()
                else "payload (SVG fragment) is required"
            ),
            error="empty payload",
            start=start,
        )

    xml = "".join(etree.tostring(c, encoding="unicode") for c in children)
    result = await _op_edit_xml(
        bus,
        operation,
        "/svg:svg",
        json.dumps({"action": "append", "xml": xml}),
        window_id,
        start,
    )
    if result.get("success"):
        result["message"] = f"inserted {len(children)} element(s) into the live document"
        result["data"]["inserted"] = len(children)
        result["data"]["bytes"] = len(payload)
    return result


# Inkex code that reads the staged fragment and appends its child elements into the
# live document root. Used where Inkscape exposes no paste-in-place action (e.g. 1.4.x).
_INSERT_VIA_INKEX_CODE = (
    "import os as _os\n"
    "from lxml import etree as _ET\n"
    "_p = _os.path.expanduser('~/.cache/inkscape_mcp/exchange/insert_fragment.svg')\n"
    "_root = _ET.parse(_p).getroot()\n"
    "_added = 0\n"
    "for _c in list(_root):\n"
    "    svg.append(_c)\n"
    "    _added += 1\n"
    "result = {'added': _added}\n"
)


async def _op_insert_svg_via_inkex(
    bus: Any,
    operation: str,
    payload: str,
    start: float,
) -> dict[str, Any]:
    """Insert an SVG fragment by appending its children to the live document via inkex.

    Inkscape 1.4.x exposes no ``paste-in-place`` action over D-Bus, so the clipboard
    route is unavailable. We stage the fragment to the exchange dir and run the
    ``mcp_execute`` extension to splice its child elements into the document — one
    undoable ``MCP: Execute`` command, like every other bridge edit.
    """
    if not payload.strip():
        return _result(operation, False, "payload (SVG fragment) is required", error="empty payload", start=start)

    from ..extension_bridge import BRIDGE_LOCK, EXCHANGE_DIR, _invoke_extension_locked

    # The staged fragment shares the exchange dir's fixed-name problem, so it has to be
    # written and consumed inside the same critical section as the invocation itself.
    async with BRIDGE_LOCK:
        EXCHANGE_DIR.mkdir(parents=True, exist_ok=True)
        (EXCHANGE_DIR / "insert_fragment.svg").write_text(payload, encoding="utf-8")
        result = await _invoke_extension_locked(
            bus,
            _EXECUTE_EXTENSION,
            {"python_code": _INSERT_VIA_INKEX_CODE},
            scope="app",
            window_id=1,
            timeout_s=30.0,
        )
    if not result.success or result.data.get("ok") is False:
        message = result.error or result.data.get("error", "insert_svg failed")
        if result.stderr:
            message = f"{message}\nstderr:\n{result.stderr}"
        return _result(operation, False, message, data={"stderr": result.stderr}, error=result.error, start=start)

    added = (result.data.get("result") or {}).get("added", 0)
    return _result(
        operation,
        True,
        f"inserted {added} element(s) into the live document",
        data={"added": added, "bytes": len(payload)},
        start=start,
    )


def _op_delete_selected(bus: InkscapeDBus, operation: str, window_id: int, start: float) -> dict[str, Any]:
    # delete-selection is the explicit object-delete; `delete` is context-sensitive (text/nodes).
    # window_id must be threaded through: the follow-up document-save is window-scoped, so
    # defaulting it to 1 silently no-ops the save on any other window.
    saved = _activate_with_optional_save(bus, "delete-selection", scope="app", window_id=window_id)
    return _result(
        operation,
        True,
        "activated delete-selection",
        data={"action": "delete-selection", "window_id": window_id, "saved": saved},
        start=start,
    )


def _op_apply_action(
    bus: InkscapeDBus,
    operation: str,
    target: str,
    payload: str,
    window_id: int,
    start: float,
) -> dict[str, Any]:
    if not target:
        return _result(
            operation,
            False,
            "target (action name) is required",
            error="missing target",
            start=start,
        )
    app_actions = set(bus.list_actions("app"))
    window_actions = set(bus.list_actions("window", window_id))
    if target in app_actions:
        scope = "app"
    elif target in window_actions:
        scope = "window"
    elif not _has_window_scope(bus):
        return _result(
            operation,
            False,
            (
                f"action {target!r} is not registered on app or document scope. "
                f"{MACOS_NO_WINDOW_SCOPE}; if {target!r} is one of those, use it in the GUI instead."
            ),
            data={"action": target, "window_scope_available": False},
            error="unavailable on macOS",
            start=start,
        )
    else:
        return _result(
            operation,
            False,
            f"action {target!r} is not registered on app or window scope",
            error="unknown action",
            start=start,
        )
    args = [payload] if payload != "" else None
    saved = _activate_with_optional_save(bus, target, args, scope=scope, window_id=window_id)
    return _result(
        operation,
        True,
        f"activated {target!r} on {scope} scope",
        data={"action": target, "scope": scope, "payload": payload, "saved": saved},
        start=start,
    )


_ACTION_DESCRIPTIONS: dict[str, str] | None = None


def _load_action_descriptions(cli_executable: str = "inkscape") -> dict[str, str]:
    """Memoised parse of ``inkscape --action-list`` output. First call shells out
    to Inkscape (~150-300 ms); subsequent calls hit the in-memory cache.

    Returns ``{action_name: description}``. Empty dict if Inkscape isn't on PATH
    or the call times out — the annotated lists then degrade gracefully to
    name-only entries with empty descriptions.
    """
    import subprocess  # local import; only needed on first call

    global _ACTION_DESCRIPTIONS
    if _ACTION_DESCRIPTIONS is not None:
        return _ACTION_DESCRIPTIONS

    result: dict[str, str] = {}
    try:
        completed = subprocess.run(  # noqa: S603 — fixed `inkscape --action-list`, no user input
            [cli_executable, "--action-list"],
            capture_output=True,
            timeout=10,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        _ACTION_DESCRIPTIONS = result
        return result

    # Format: "name<padding>:  description"
    for line in completed.stdout.splitlines():
        if ":  " not in line:
            continue
        name, _, desc = line.partition(":  ")
        name = name.strip()
        if name:
            result[name] = desc.strip()
    _ACTION_DESCRIPTIONS = result
    return result


async def _op_list_actions(
    bus: InkscapeDBus,
    operation: str,
    window_id: int,
    target: str,
    start: float,
) -> dict[str, Any]:
    """List all registered D-Bus actions, annotated with descriptions.

    ``target`` is an optional case-insensitive substring filter applied to both
    name and description. Use it to narrow the 1244-action surface (e.g.
    ``target="layer-"`` for layer ops, ``target="snap"`` for snap toggles).
    """
    # First call shells out to `inkscape --action-list` with a 10s timeout; keep that off
    # the event loop. Subsequent calls hit the module-level cache and return immediately.
    descriptions = await asyncio.to_thread(_load_action_descriptions)
    app_names = sorted(bus.list_actions("app"))
    window_names = sorted(bus.list_actions("window", window_id))

    def _annotate(names: list[str]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for n in names:
            # Fall back to the base name's description for the .noprefs variant —
            # Inkscape's --action-list reports only the base form for extensions.
            desc = descriptions.get(n)
            if desc is None and n.endswith(".noprefs"):
                desc = descriptions.get(n[: -len(".noprefs")], "")
            out.append({"name": n, "description": desc or ""})
        return out

    app = _annotate(app_names)
    window = _annotate(window_names)

    if target:
        needle = target.lower()

        def _match(item: dict[str, str]) -> bool:
            return needle in item["name"].lower() or needle in item["description"].lower()

        app = [a for a in app if _match(a)]
        window = [w for w in window if _match(w)]

    msg = f"{len(app)} app + {len(window)} window actions"
    if target:
        msg += f" matching {target!r}"
    data: dict[str, Any] = {"app": app, "window": window, "filter": target}
    if not _has_window_scope(bus):
        msg = msg.replace("window actions", "document actions") + f" ({MACOS_NO_WINDOW_SCOPE})"
        data["window_scope_available"] = False
    return _result(operation, True, msg, data=data, start=start)


def _op_open_file(bus: InkscapeDBus, operation: str, target: str, start: float) -> dict[str, Any]:
    if not target:
        return _result(operation, False, "target (file path) is required", error="missing target", start=start)
    if not os.path.isabs(target):
        return _result(
            operation,
            False,
            f"path must be absolute: {target!r}",
            error="relative path",
            start=start,
        )
    bus.open_file(target)
    from .live_figure import remember_path

    remember_path(target)  # Inkscape does not report a document's file over D-Bus; `save` needs it
    return _result(operation, True, f"requested open of {target!r}", data={"path": target}, start=start)


_EDIT_XML_EXTENSION = "org.inkscape.mcp.edit-xml.noprefs"
_INSPECT_EXTENSION = "org.inkscape.mcp.inspect.noprefs"
_PATH_EDIT_EXTENSION = "org.inkscape.mcp.path-edit.noprefs"
_EXECUTE_EXTENSION = "org.inkscape.mcp.execute.noprefs"

_PATH_EDIT_OPS = frozenset(
    {
        "move_node",
        "set_handle_in",
        "set_handle_out",
        "insert_node",
        "smooth",
        "cusp",
        "subdivide",
    }
)


async def _do_inspect(
    bus: InkscapeDBus,
    category: str,
    operation: str,
    start: float,
    extra_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Route a single inspect category through the mcp_inspect extension.
    ``extra_spec`` adds category-specific fields (e.g. ``{"target": "<id>"}``
    for the ``element`` category)."""
    spec: dict[str, Any] = {"category": category}
    if extra_spec:
        spec.update(extra_spec)
    result = await invoke_extension(bus, _INSPECT_EXTENSION, spec, timeout_s=10.0)
    if not result.success:
        message = result.error or f"inspect '{category}' failed"
        if result.stderr:
            message = f"{message}\nstderr:\n{result.stderr}"
        return _result(
            operation,
            False,
            message,
            data={"category": category, "stderr": result.stderr},
            error=result.error,
            start=start,
        )

    # Honour an explicit ok:false from the extension (e.g. unknown category).
    if result.data.get("ok") is False:
        return _result(
            operation,
            False,
            result.data.get("error", "inspect failed"),
            data=result.data,
            error=result.data.get("error", ""),
            start=start,
        )

    summary = _inspect_summary(category, result.data)
    return _result(operation, True, summary, data=result.data, start=start)


def _inspect_summary(category: str, data: dict[str, Any]) -> str:
    if category == "selection":
        return f"{data.get('count', 0)} object(s) selected"
    if category == "layers":
        return f"{len(data.get('layers', []))} layer(s)"
    if category == "defs":
        counts = sum(
            len(data.get(k, []))
            for k in (
                "gradients",
                "filters",
                "markers",
                "patterns",
                "clip_paths",
                "masks",
                "symbols",
            )
        )
        return f"{counts} def(s) catalogued"
    if category == "view":
        return f"view: {data.get('viewbox', '?')}, zoom {data.get('zoom', '?')}"
    if category == "pages":
        return f"{data.get('count', 0)} page(s)"
    if category == "element":
        bbox = data.get("bbox") or {}
        return (
            f"<{data.get('tag', '?')}> id={data.get('id', '?')} "
            f"bbox=({bbox.get('x', '?')},{bbox.get('y', '?')},{bbox.get('w', '?')},{bbox.get('h', '?')})"
        )
    return f"inspect {category} succeeded"


async def _op_inspect_selection(bus: InkscapeDBus, operation: str, start: float) -> dict[str, Any]:
    return await _do_inspect(bus, "selection", operation, start)


async def _op_inspect_layers(bus: InkscapeDBus, operation: str, start: float) -> dict[str, Any]:
    return await _do_inspect(bus, "layers", operation, start)


async def _op_inspect_defs(bus: InkscapeDBus, operation: str, start: float) -> dict[str, Any]:
    return await _do_inspect(bus, "defs", operation, start)


async def _op_inspect_view(bus: InkscapeDBus, operation: str, start: float) -> dict[str, Any]:
    return await _do_inspect(bus, "view", operation, start)


async def _op_inspect_pages(bus: InkscapeDBus, operation: str, start: float) -> dict[str, Any]:
    return await _do_inspect(bus, "pages", operation, start)


async def _op_inspect_element(
    bus: InkscapeDBus,
    operation: str,
    target: str,
    start: float,
) -> dict[str, Any]:
    """Per-element analytical query: bbox + computed style + attributes + ancestor chain."""
    if not target:
        return _result(operation, False, "target (element id) is required", error="missing target", start=start)
    return await _do_inspect(bus, "element", operation, start, {"target": target})


async def _op_execute_inkex(
    bus: InkscapeDBus,
    operation: str,
    payload: str,
    start: float,
) -> dict[str, Any]:
    """Run arbitrary inkex Python via the mcp_execute extension.

    ``payload`` is the Python source as a raw string (NOT JSON-encoded). The
    extension exec()s it with ``svg``, ``inkex``, ``etree`` in scope and
    returns whatever the code assigns to ``result`` or returns from a
    ``main()`` function. Mutations to ``svg`` produce one ``MCP: Execute``
    undo entry; read-only code creates an ``[unchanged]`` entry.
    """
    if not payload or not payload.strip():
        return _result(
            operation,
            False,
            "payload (python code) is required",
            error="missing payload",
            start=start,
        )

    spec = {"python_code": payload}
    result = await invoke_extension(bus, _EXECUTE_EXTENSION, spec, timeout_s=30.0)

    if not result.success:
        message = result.error or "execute extension failed"
        if result.stderr:
            message = f"{message}\nstderr:\n{result.stderr}"
        return _result(
            operation,
            False,
            message,
            data={"stderr": result.stderr},
            error=result.error,
            start=start,
        )

    if result.data.get("ok") is False:
        err = result.data.get("error", "execute failed")
        tb = result.data.get("traceback", "")
        return _result(
            operation,
            False,
            f"{err}\n{tb}" if tb else err,
            data=result.data,
            error=err,
            start=start,
        )

    return _result(
        operation,
        True,
        f"executed {len(payload)} bytes of inkex code",
        data={"result": result.data.get("result")},
        start=start,
    )


async def _op_component(
    bus: InkscapeDBus, operation: str, target: str, payload: str, window_id: int, start: float
) -> dict[str, Any]:
    """Run a registered generator (schematic parts, scatter, organic, flow, ...); optionally insert it.

    payload (JSON):
      kind    — any registered generator (kind "catalogue" lists them with their parameters;
                optional "category" filter; "render": true draws the visual contact sheet as PNG,
                or with "format": "html" (+ optional absolute "path") a browsable page),
                or "style" to return the tokens
      params  — the generator's parameters, including a stable "id"; coordinates are document
                user units (as `outline` reports them), whatever group the result lands in
      seed    — integer; the same seed always gives the same output (default 0)
      style   — path to a style.json overriding the default tokens (or $INKSCAPE_MCP_STYLE)
      into    — insert into this parent id;  replace — replace this id (defaults to `target`)
      position — with into: "top" (default, in front) or "bottom" (behind everything — backgrounds)
      apply   — how to use the output (default {"mode": "free"}):
                {"mode": "clip", "to": "<shape id>"}  inside the shape, clipped by a live link to it;
                    region params default to the shape's bounding box
                {"mode": "pattern", "tile": [w, h], "to": "<id>"?}  as a seamless <pattern> fill
                    (scatters switch to periodic); "to" sets it as that object's fill

    Without into/replace/apply-to it only returns the fragment (preview it, then insert).
    Inserting is one undo step; the stock arrow marker is added if the fragment needs it.
    """
    from .. import figure_kit
    from ..figure_outline import element_box
    from ..generators import registry

    try:
        args = json.loads(payload) if payload else {}
    except json.JSONDecodeError as exc:
        return _result(operation, False, f"payload is not valid JSON: {exc}", error=str(exc), start=start)
    if not isinstance(args, dict):
        return _result(operation, False, "payload must decode to a JSON object", error="bad payload", start=start)
    style_path = args.get("style") or os.environ.get("INKSCAPE_MCP_STYLE")
    style = figure_kit.load_style(style_path)
    kind = args.get("kind", "")
    if kind == "style":
        return _result(
            operation, True, json.dumps(style, indent=1), data={"style": style, "path": style_path}, start=start
        )
    if kind == "catalogue" and args.get("render"):
        from ..generators import catalogue

        _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        if args.get("format") == "html":
            out = Path(str(args.get("path") or _SCRATCH_DIR / "catalogue.html")).expanduser()
            if not out.is_absolute():
                return _result(operation, False, "path must be absolute", error="relative path", start=start)
            include_assets = bool(args.get("assets", True))  # the user's own library, when present
            out.write_text(catalogue.render_html(style, args.get("category"), include_assets), encoding="utf-8")
            return _result(operation, True, f"catalogue page written to {out}", data={"html": str(out)}, start=start)
        sheet = _SCRATCH_DIR / f"catalogue-{uuid.uuid4().hex[:8]}.svg"
        svg = catalogue.render(style, args.get("category"))
        sheet.write_text(
            svg.replace("</svg>\n", f"<defs>{figure_kit.TRIANGLE_MARKER}</defs></svg>\n"), encoding="utf-8"
        )
        png = sheet.with_suffix(".png")
        cmd = [_resolve_inkscape_exe(None), "--export-type=png", f"--export-dpi={float(args.get('dpi', 120))}",
               f"--export-filename={png}", str(sheet)]  # fmt: skip
        failure = await _run_png_export(cmd, png)
        if failure:
            return _result(operation, False, failure, data={"svg": str(sheet)}, error="export failed", start=start)
        return _result(
            operation,
            True,
            f"catalogue rendered: {png} (SVG {sheet}) — each cell is captioned with the seed/params that reproduce it",
            data={"png": str(png), "svg": str(sheet)},
            start=start,
        )
    if kind == "catalogue":
        entries = registry.catalogue(args.get("category"))
        lines = [f"{e['category']:>10}  {e['name']:<16} {e['doc']}" for e in entries]
        return _result(operation, True, "\n".join(lines), data={"generators": entries}, start=start)
    seed = args.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        return _result(operation, False, f"seed must be an integer (got {seed!r})", error="bad seed", start=start)

    apply = args.get("apply") or {"mode": "free"}
    mode = apply.get("mode", "free") if isinstance(apply, dict) else None
    if mode not in ("free", "clip", "pattern"):
        return _result(operation, False, "apply.mode must be free, clip or pattern", error="bad apply", start=start)
    apply_to = str(apply.get("to") or "").lstrip("#")
    replace_id = str(args.get("replace") or target or "").lstrip("#")
    into = str(args.get("into") or "").lstrip("#")
    params = dict(args.get("params") or {})
    try:
        gen = registry.get(kind)
    except ValueError as exc:
        return _result(operation, False, str(exc), error="bad component", start=start)

    if mode == "pattern":
        tile = apply.get("tile")
        if not (isinstance(tile, list) and len(tile) == 2 and all(isinstance(v, int | float) and v > 0 for v in tile)):
            return _result(operation, False, "apply.tile must be [width, height] > 0", error="bad apply", start=start)
        if not {"x", "y", "width", "height"} <= set(gen.params):
            return _result(operation, False, f"{kind} has no region; it cannot tile", error="bad apply", start=start)
        tile_w, tile_h = float(tile[0]), float(tile[1])
        params.update({"x": 0, "y": 0, "width": tile_w, "height": tile_h})
        if "periodic" in gen.params and "periodic" not in params:
            params["periodic"] = True

    needs_doc = bool(replace_id or into or apply_to or mode == "pattern")
    doc_bytes = b""
    if needs_doc:
        _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = _SCRATCH_DIR / f"component-{uuid.uuid4().hex[:8]}.svg"
        try:
            await _export_document_async(bus, snapshot)
            doc_bytes = snapshot.read_bytes()
        finally:
            snapshot.unlink(missing_ok=True)
        doc = etree.fromstring(doc_bytes, parser=etree.XMLParser(huge_tree=True))
        for ref in (replace_id, into, apply_to):
            if ref and doc.find(f".//*[@id='{ref}']") is None:
                return _result(operation, False, f"no element with id {ref!r}", error="unknown id", start=start)

    if mode == "clip":
        if not apply_to:
            return _result(
                operation, False, "apply.to (the clipping shape's id) is required", error="bad apply", start=start
            )
        box = element_box(doc_bytes, apply_to)
        if box and {"x", "y", "width", "height"} <= set(gen.params):
            params.setdefault("x", box[0])
            params.setdefault("y", box[1])
            params.setdefault("width", box[2] - box[0])
            params.setdefault("height", box[3] - box[1])
        elif box and {"cx", "cy"} <= set(gen.params):
            params.setdefault("cx", (box[0] + box[2]) / 2)
            params.setdefault("cy", (box[1] + box[3]) / 2)

    try:
        xml = registry.build(kind, params, style, seed)
    except ValueError as exc:
        return _result(operation, False, str(exc), error="bad component", start=start)
    gid = str(params.get("id"))

    if not needs_doc:
        return _result(
            operation,
            True,
            f"{kind} fragment built (not inserted) — preview it, or pass into/replace/apply.to to insert",
            data={**_xml_payload(xml, gid), "kind": kind},
            start=start,
        )

    ops: list[dict[str, Any]] = []
    if mode == "pattern":
        pattern = (
            f'<pattern id="{gid}-pattern" inkscape:label="{kind} pattern" patternUnits="userSpaceOnUse" '
            f'x="0" y="0" width="{tile_w:g}" height="{tile_h:g}">{xml}</pattern>'
        )
        ops.append(_defs_append_op(doc, pattern))
        if apply_to:
            ops.append(_fill_op(doc, apply_to, f"url(#{gid}-pattern)"))
        where = f"as pattern #{gid}-pattern" + (f", filling #{apply_to}" if apply_to else "")
    elif mode == "clip":
        shape = doc.find(f".//*[@id='{apply_to}']")
        inner = _in_local_coords(doc_bytes, xml, shape.getparent().get("id") if shape is not None else None)
        clipped = (
            f'<g id="{gid}-clipped" inkscape:label="{kind} (clipped to {apply_to})" '
            f'clip-path="url(#{gid}-clip)">{inner}</g>'
        )
        ops.append({"xpath": f"//*[@id='{apply_to}']", "action": "insert_after", "xml": clipped})
        ops.append(
            _defs_append_op(
                doc,
                f'<clipPath id="{gid}-clip" clipPathUnits="userSpaceOnUse">'
                f'<use xlink:href="#{apply_to}" href="#{apply_to}"/></clipPath>',
            )
        )
        where = f"clipped to #{apply_to}"
    elif replace_id:
        old = doc.find(f".//*[@id='{replace_id}']")
        parent_id = old.getparent().get("id") if old is not None and old.getparent() is not None else None
        ops.append(
            {
                "xpath": f"//*[@id='{replace_id}']",
                "action": "replace",
                "xml": _in_local_coords(doc_bytes, xml, parent_id),
            }
        )
        where = f"replacing #{replace_id}"
    else:
        local = _in_local_coords(doc_bytes, xml, into)
        parent = doc.find(f".//*[@id='{into}']")
        first = next((c for c in parent if isinstance(c.tag, str)), None) if parent is not None else None
        if args.get("position") == "bottom" and first is not None:
            # Behind everything else in the parent (a background), in the same single undo step.
            ops.append({"xpath": f"//*[@id='{into}']/*[1]", "action": "insert_before", "xml": local})
            where = f"at the bottom of #{into}"
        else:
            ops.append({"xpath": f"//*[@id='{into}']", "action": "append", "xml": local})
            where = f"into #{into}"

    marker = style["marker"]
    if f"url(#{marker})" in xml and marker == "Triangle" and doc.find(f".//*[@id='{marker}']") is None:
        # Appended AFTER the referencing paths: inkscape:collect="always" drops a marker
        # that is unreferenced at the moment it lands.
        ops.append(_defs_append_op(doc, figure_kit.TRIANGLE_MARKER))
    result = await _op_edit_xml(bus, operation, "", json.dumps({"ops": ops}), window_id, start)
    if result.get("success"):
        result["message"] = f"{kind} inserted {where}; " + result["message"]
        result["data"].update(_xml_payload(xml, gid))
    return result


_INLINE_XML_LIMIT = 8000


def _xml_payload(xml: str, gid: str) -> dict[str, Any]:
    """The fragment inline when small; otherwise a file path (big generators make 100 KB+)."""
    if len(xml) <= _INLINE_XML_LIMIT:
        return {"xml": xml}
    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = _SCRATCH_DIR / f"component-{re.sub(r'[^A-Za-z0-9_-]', '_', gid)}-{uuid.uuid4().hex[:6]}.svg"
    path.write_text(xml, encoding="utf-8")
    return {"xml_path": str(path), "xml_bytes": len(xml), "xml_head": xml[:240]}


def _defs_append_op(doc: Any, xml: str) -> dict[str, Any]:
    """An edit op appending `xml` to the document's <defs> (creating <defs> if absent)."""
    if doc.find(f"{{{SVG_NS}}}defs") is not None:
        return {"xpath": "/svg:svg/svg:defs[1]", "action": "append", "xml": xml}
    return {"xpath": "/svg:svg", "action": "append", "xml": f"<defs>{xml}</defs>"}


def _fill_op(doc: Any, element_id: str, fill: str) -> dict[str, Any]:
    """An edit op setting `element_id`'s fill in its style attribute, keeping the rest of its style."""
    el = doc.find(f".//*[@id='{element_id}']")
    decls = [d for d in (el.get("style") or "").split(";") if d.strip() and d.split(":")[0].strip() != "fill"]
    return {
        "xpath": f"//*[@id='{element_id}']",
        "action": "set_attr",
        "name": "style",
        "value": ";".join([*decls, f"fill:{fill}"]),
    }


def _in_local_coords(doc_bytes: bytes, xml: str, parent_id: str | None) -> str:
    """Give the fragment's root the inverse of its new parent's transform.

    Generator coordinates are document user units (what `outline` reports); a parent group
    with a transform would otherwise shift the inserted output.
    """
    import inkex

    if not parent_id:
        return xml
    svg = inkex.load_svg(doc_bytes).getroot()
    parent = svg.getElementById(parent_id)
    if parent is None:
        return xml
    to_doc = parent.composed_transform()
    if to_doc == inkex.Transform():
        return xml
    frags = _fragment_children(xml)
    if len(frags) != 1:
        return xml
    root = frags[0]
    root.set("transform", str(-to_doc))
    return str(etree.tostring(root, encoding="unicode"))


async def _run_png_export(cmd: list[str], out_path: Path) -> str:
    """Run an Inkscape CLI export; return '' on success or a failure message."""
    # asyncio subprocess rather than subprocess.run: a 60s blocking wait here froze every
    # other in-flight tool call (this runs on the request path).
    async with _CLI_SLOTS:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return "inkscape export timed out after 60s"
    if proc.returncode != 0 or not out_path.exists():
        stderr_tail = (stderr_b or b"").decode("utf-8", errors="replace")[-1000:]
        return f"inkscape export failed (rc={proc.returncode}); stderr: {stderr_tail}"
    return ""


async def _query_box(exe: str, svg_path: Path, element_id: str, svg_bytes: bytes) -> Any:
    """Inkscape's visual bounding box of `element_id` in user units, or None if the query fails."""
    import inkex

    try:
        async with _CLI_SLOTS:
            proc = await asyncio.create_subprocess_exec(
                exe,
                f"--query-id={element_id}",
                "--query-x",
                "--query-y",
                "--query-width",
                "--query-height",
                str(svg_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        x, y, w, h = (float(v) for v in out.decode().split()[:4])
    except (OSError, ValueError, TimeoutError):
        return None
    if w <= 0 or h <= 0:
        return None
    svg = inkex.load_svg(svg_bytes).getroot()
    scale = svg.uutounit(1, "px")  # query values are CSS px from the viewBox origin
    vb = [float(v) for v in re.split(r"[\s,]+", (svg.get("viewBox") or "0 0").strip())[:2]]
    left, top = vb[0] + x / scale, vb[1] + y / scale
    return (left, top, left + w / scale, top + h / scale)


async def _op_preview(
    bus: InkscapeDBus, operation: str, target: str, payload: str, config: Any, start: float
) -> dict[str, Any]:
    """Render a draft in the live document's context WITHOUT changing the document.

    payload (JSON):
      xml      — fragment to try (one or more elements), or xml_path to a file holding it (as
                 component returns for large fragments); optional: without it, preview just
                 renders `focus` from the live document
      replace  — id of the element the draft would replace (defaults to `target`)
      into     — id of the parent to append the draft to when not replacing (default: the
                 parent of `replace`, else the first layer, else the root)
      focus    — id to frame (default: the draft's first id, else `replace`)
      margin   — padding around the framed box, in document units (default 3)
      dpi      — default 600
      before   — also render the unchanged document over the same area (default: true
                 when replacing)

    Works on an export snapshot: Inkscape's document and undo history are untouched.
    """
    from ..figure_outline import element_box, user_area_to_px

    try:
        args = json.loads(payload) if payload else {}
    except json.JSONDecodeError as exc:
        return _result(operation, False, f"payload is not valid JSON: {exc}", error=str(exc), start=start)
    if not isinstance(args, dict):
        return _result(operation, False, "payload must decode to a JSON object", error="bad payload", start=start)

    replace_id = str(args.get("replace") or target or "").lstrip("#")
    if args.get("xml_path") and not args.get("xml"):
        try:
            args["xml"] = Path(str(args["xml_path"])).read_text(encoding="utf-8")
        except OSError as exc:
            return _result(operation, False, f"cannot read xml_path: {exc}", error="bad xml_path", start=start)
    frags = _fragment_children(str(args["xml"])) if args.get("xml") else []
    if args.get("xml") and not frags:
        return _result(operation, False, "xml did not parse to any element", error="bad xml", start=start)
    focus = str(args.get("focus") or next((f.get("id") for f in frags if f.get("id")), None) or replace_id)
    if not focus:
        return _result(
            operation,
            False,
            "nothing to frame: give the draft an id, or pass focus/replace",
            error="no focus",
            start=start,
        )

    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"preview-{uuid.uuid4().hex[:8]}"
    before_svg = _SCRATCH_DIR / f"{stem}-before.svg"
    await _export_document_async(bus, before_svg)
    original = before_svg.read_bytes()

    doc = etree.fromstring(original, parser=etree.XMLParser(huge_tree=True))
    if frags:
        if replace_id:
            old = doc.find(f".//*[@id='{replace_id}']")
            if old is None:
                return _result(
                    operation, False, f"no element with id {replace_id!r} to replace", error="unknown id", start=start
                )
            parent = old.getparent()
            index = parent.index(old)
            parent.remove(old)
            for offset, frag in enumerate(frags):
                parent.insert(index + offset, frag)
        else:
            into = args.get("into")
            if into:
                parent = doc.find(f".//*[@id='{into}']")
                if parent is None:
                    return _result(
                        operation, False, f"no element with id {into!r} to append into", error="unknown id", start=start
                    )
            else:
                layer = doc.find(f".//{{{SVG_NS}}}g[@{{{INKSCAPE_NS}}}groupmode='layer']")
                parent = layer if layer is not None else doc
            for frag in frags:
                parent.append(frag)
    draft = etree.tostring(doc)
    if doc.find(f".//*[@id='{focus}']") is None:
        return _result(operation, False, f"no element with id {focus!r}", error="unknown id", start=start)
    exe = str(getattr(config, "inkscape_executable", None) or _resolve_inkscape_exe(config))
    after_svg = _SCRATCH_DIR / f"{stem}.svg"
    after_svg.write_bytes(draft)

    # Frame on what Inkscape actually draws; the outline's estimate is the fallback (it can
    # overshoot on text, which used to frame the whole page).
    box = await _query_box(exe, after_svg, focus, draft) or element_box(draft, focus)
    if box is None:
        return _result(operation, False, f"{focus!r} has no visible extent to frame", error="empty", start=start)
    margin = float(args.get("margin", 3))
    framed = (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)
    area = user_area_to_px(draft, framed)
    dpi = float(args.get("dpi", 600))

    renders = {"after": (after_svg, draft)}
    if args.get("before", bool(replace_id and frags)):
        renders["before"] = (before_svg, original)
    jobs: list[tuple[str, Path, list[str]]] = []
    for name, (svg_path, data) in renders.items():
        svg_path.write_bytes(data)
        png = svg_path.with_suffix(".png")
        cmd = [
            exe,
            "--app-id-tag=mcp",
            f"--export-filename={png}",
            "--export-type=png",
            f"--export-area={area}",
            f"--export-dpi={dpi}",
            str(svg_path),
        ]
        jobs.append((name, png, cmd))
    failures = await asyncio.gather(*(_run_png_export(cmd, png) for _, png, cmd in jobs))
    paths: dict[str, str] = {}
    for (name, png, cmd), failure in zip(jobs, failures, strict=True):
        if failure:
            return _result(operation, False, failure, data={"argv": cmd}, error="export failed", start=start)
        paths[name] = str(png)

    framed_text = ", ".join(f"{v:.1f}" for v in framed)
    return _result(
        operation,
        True,
        f"preview of {focus!r} rendered (document unchanged): " + ", ".join(f"{k} {v}" for k, v in paths.items()),
        data={"paths": paths, "focus": focus, "box": [round(v, 2) for v in box], "framed": framed_text, "dpi": dpi},
        start=start,
    )


async def _op_rasterize(
    bus: InkscapeDBus,
    operation: str,
    target: str,
    payload: str,
    cli_wrapper: Any,
    config: Any,
    start: float,
) -> dict[str, Any]:
    """Render the current document (or a single element / area) to PNG.

    ``target`` — element id to render (uses ``--export-id``). Empty → full doc.
    ``payload`` — optional JSON: ``{"area": "x:y:w:h", "dpi": 96, "filename": "<abs>"}``.
    ``area`` is in document user units (the coordinates every other op and ``outline``
    report — mm for a mm-based document), converted here to the CSS-px corners Inkscape's
    ``--export-area`` expects.

    Saves a fresh snapshot of the live doc first so the export reflects
    in-memory state, then shells to Inkscape's CLI for the export. Returns
    the path to the produced PNG plus its byte size; the agent can read
    the file directly when it needs the pixels.
    """
    import json as _json

    if cli_wrapper is None or config is None:
        return _result(
            operation,
            False,
            "rasterize requires cli_wrapper + config",
            error="missing dependencies",
            start=start,
        )

    args: dict[str, Any] = {}
    if payload:
        try:
            parsed = _json.loads(payload)
            if isinstance(parsed, dict):
                args = parsed
        except _json.JSONDecodeError as exc:
            return _result(operation, False, f"payload is not valid JSON: {exc}", error=str(exc), start=start)

    area = args.get("area")
    dpi = args.get("dpi")
    filename = args.get("filename")

    # Snapshot the live doc so the export reflects in-memory state.
    snapshot = _scratch_path()
    await _export_document_async(bus, snapshot)

    # Choose output path.
    if filename:
        out_path = Path(filename)
        if not out_path.is_absolute():
            return _result(
                operation,
                False,
                f"filename must be absolute: {filename!r}",
                error="relative path",
                start=start,
            )
    else:
        out_path = _SCRATCH_DIR / f"raster-{uuid.uuid4().hex[:8]}.png"

    cmd = [
        str(config.inkscape_executable),
        "--app-id-tag=mcp",
        f"--export-filename={out_path}",
        "--export-type=png",
    ]
    if target:
        cmd.extend([f"--export-id={target}", "--export-id-only"])
    if area:
        try:
            x, y, w, h = (float(v) for v in str(area).split(":"))
        except ValueError:
            return _result(
                operation,
                False,
                f"area must be 'x:y:w:h' in document units (got {area!r})",
                error="bad area",
                start=start,
            )
        from ..figure_outline import user_area_to_px

        cmd.append(f"--export-area={user_area_to_px(snapshot.read_bytes(), (x, y, x + w, y + h))}")
    if dpi:
        try:
            cmd.append(f"--export-dpi={float(dpi)}")
        except (TypeError, ValueError):
            return _result(operation, False, f"dpi must be a number: {dpi!r}", error="bad dpi", start=start)
    cmd.append(str(snapshot))

    failure = await _run_png_export(cmd, out_path)
    if failure:
        return _result(operation, False, failure, data={"argv": cmd}, error="export failed", start=start)

    size = out_path.stat().st_size
    return _result(
        operation,
        True,
        f"rasterized to {out_path} ({size} bytes)",
        data={
            "path": str(out_path),
            "bytes": size,
            "target": target or None,
            "area": area,
            "dpi": dpi,
        },
        start=start,
    )


async def _op_edit_xml(
    bus: InkscapeDBus,
    operation: str,
    target_xpath: str,
    payload_json: str,
    window_id: int,
    start: float,
) -> dict[str, Any]:
    """Structural XML edit on the live document via the edit_xml inkex extension.

    Routing through an extension (rather than save → mutate-on-disk → revert)
    preserves Inkscape's undo history: each invocation registers as one
    undoable command.

    payload (JSON): {"action": "<verb>", ...action-args}
    actions:
      append/insert_before/insert_after/replace → require "xml" (or "xml_path", a file) arg
      remove → no args
      set_attr → require "name", "value"
      set_text → require "text"

    Batch: {"ops": [{"xpath": ..., "action": ..., ...}, ...]} applies every op as ONE
    undoable step, all-or-nothing; an op without "xpath" uses `target`. A large batch can
    live in a file: {"ops_path": "/path/ops.json"}.
    """
    valid_actions = {
        "append",
        "insert_before",
        "insert_after",
        "remove",
        "set_attr",
        "set_text",
        "replace",
    }

    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError as exc:
        return _result(operation, False, f"payload is not valid JSON: {exc}", error=str(exc), start=start)
    if not isinstance(payload, dict):
        return _result(
            operation,
            False,
            "payload must decode to a JSON object",
            error="bad payload",
            start=start,
        )
    try:
        payload = _resolve_edit_files(payload)
    except (OSError, ValueError) as exc:
        return _result(operation, False, str(exc), error="bad path", start=start)

    if payload.pop("render", False):  # before/after picture of what the edit touched
        from . import live_figure

        before = await live_figure.snapshot()
        ops = payload["ops"] if "ops" in payload else [{**payload, "xpath": target_xpath}]
        edited = await _op_edit_xml(bus, operation, target_xpath, json.dumps(payload), window_id, start)
        if edited["success"]:
            after = None
            if _LAST_EDIT:  # the plugin's own copy of the edited document: no second export
                after_path = _scratch_path()
                after_path.write_bytes(_LAST_EDIT[0])
                after = (after_path, _LAST_EDIT[0])
            shown = await live_figure.before_after(None, before, live_figure.ids_touched(ops), after)
            edited["data"] = {**edited.get("data", {}), **shown}
            if shown.get("render"):
                edited["message"] += f"; before|after: {shown['render']}"
        return edited

    if "ops" in payload:
        return await _op_edit_xml_batch(bus, operation, target_xpath, payload["ops"], window_id, valid_actions, start)
    if not target_xpath:
        return _result(operation, False, "target (xpath) is required", error="missing target", start=start)

    action = payload.get("action")
    if action not in valid_actions:
        return _result(
            operation,
            False,
            f"unknown action {action!r}; valid: {sorted(valid_actions)}",
            error="bad action",
            start=start,
        )

    # Spec shape: extension reads xpath/action and per-action keys merged from payload.
    spec: dict[str, Any] = {"xpath": target_xpath, "action": action}
    for key in ("xml", "name", "value", "text"):
        if key in payload:
            spec[key] = payload[key]

    result = await invoke_extension(bus, _EDIT_XML_EXTENSION, spec, timeout_s=15.0)

    if not result.success:
        message = result.error or "edit_xml extension failed"
        if result.stderr:
            message = f"{message}\nstderr:\n{result.stderr}"
        return _result(
            operation,
            False,
            message,
            data={"action": action, "xpath": target_xpath, "stderr": result.stderr},
            error=result.error,
            start=start,
        )

    # The extension reports its own outcome in the payload; a well-formed JSON body still
    # means success=True at the transport level. Without this check a bad XPath came back
    # as "affected 0 node(s)" with success=True — and we'd fire a pointless save on top.
    if result.data.get("ok") is False:
        return _result(
            operation,
            False,
            result.data.get("error", "edit_xml failed"),
            data={"action": action, "xpath": target_xpath, **result.data},
            error=result.data.get("error", ""),
            start=start,
        )

    await _after_edit()
    # Persist the in-memory mutation to disk so re-reads see current state.
    # Honours the same kill switch as Pattern A: INKSCAPE_MCP_AUTO_SAVE=0 to skip.
    saved, save_note = _save_active_document(bus, window_id)

    affected = result.data.get("affected", 0)
    return _result(
        operation,
        True,
        f"edit_xml {action!r} affected {affected} node(s)",
        data={
            "action": action,
            "xpath": target_xpath,
            "affected": affected,
            "saved": saved,
            "save_skipped_reason": save_note,
            **{k: v for k, v in result.data.items() if k != "affected"},
        },
        start=start,
    )


_LAST_EDIT: list[bytes] = []  # [after] of the latest successful edit_xml run, when the plugin wrote it


async def _after_edit() -> None:
    """Use the files the edit plugin left after a successful run: the edited document seeds the
    snapshot cache (the next read-only call skips its export) and the edit is folded into the
    change baseline. Counts a run it could not fold, so the operation marks the baseline dirty."""
    from ..extension_bridge import EXCHANGE_DIR

    _LAST_EDIT.clear()
    try:  # read in one go, before anything yields: the next extension run replaces these files
        before = (EXCHANGE_DIR / "before.svg").read_bytes()
        after = (EXCHANGE_DIR / "after.svg").read_bytes()
    except OSError:
        before = after = b""
    folded = False
    if before and after:
        _LAST_EDIT.append(after)
        _SNAPSHOT_CACHE.update(bytes=after, at=time.monotonic())
        try:
            folded = await asyncio.to_thread(change_tracker.fold_edit, before, after)
        except Exception:  # the baseline is a nicety: a doc it cannot read just gets re-marked
            folded = False
    folds = _EDIT_FOLDS.get()
    if folds is not None:
        folds["folded" if folded else "missed"] += 1


def _resolve_edit_files(payload: dict[str, Any]) -> dict[str, Any]:
    """Inline `ops_path` (a JSON file: {"ops": [...]} or a list) and every op's `xml_path`."""
    if payload.get("ops_path"):
        data = json.loads(Path(str(payload["ops_path"])).read_text(encoding="utf-8"))
        ops = data.get("ops") if isinstance(data, dict) else data
        if not isinstance(ops, list):
            raise ValueError(f"ops_path {payload['ops_path']}: expected {{'ops': [...]}} or a list")
        payload = {**{k: v for k, v in payload.items() if k != "ops_path"}, "ops": ops}

    def with_xml(op: Any) -> Any:
        if isinstance(op, dict) and op.get("xml_path") and "xml" not in op:
            return {**op, "xml": Path(str(op["xml_path"])).read_text(encoding="utf-8")}
        return op

    if isinstance(payload.get("ops"), list):
        return {**payload, "ops": [with_xml(op) for op in payload["ops"]]}
    return dict(with_xml(payload))


async def _op_edit_xml_batch(
    bus: InkscapeDBus,
    operation: str,
    default_xpath: str,
    ops: Any,
    window_id: int,
    valid_actions: set[str],
    start: float,
) -> dict[str, Any]:
    """Several edit_xml ops in one extension run: one undo entry, all-or-nothing."""
    if not isinstance(ops, list) or not ops:
        return _result(operation, False, "'ops' must be a non-empty list", error="bad payload", start=start)
    specs: list[dict[str, Any]] = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            return _result(operation, False, f"op {i} must be an object", error="bad payload", start=start)
        xpath = op.get("xpath") or default_xpath
        if not xpath:
            return _result(
                operation, False, f"op {i} has no xpath (and no target)", error="missing target", start=start
            )
        if op.get("action") not in valid_actions:
            return _result(
                operation,
                False,
                f"op {i}: unknown action {op.get('action')!r}; valid: {sorted(valid_actions)}",
                error="bad action",
                start=start,
            )
        specs.append(
            {"xpath": xpath, **{k: v for k, v in op.items() if k in ("action", "xml", "name", "value", "text")}}
        )

    result = await invoke_extension(bus, _EDIT_XML_EXTENSION, {"ops": specs}, timeout_s=30.0)
    if not result.success:
        message = result.error or "edit_xml extension failed"
        if result.stderr:
            message = f"{message}\nstderr:\n{result.stderr}"
        return _result(
            operation,
            False,
            message,
            data={"action": "batch", "stderr": result.stderr},
            error=result.error,
            start=start,
        )
    if result.data.get("ok") is False:
        return _result(
            operation,
            False,
            f"batch not applied (nothing changed): {result.data.get('error', 'edit_xml failed')}",
            data={"action": "batch", **result.data},
            error=result.data.get("error", ""),
            start=start,
        )

    await _after_edit()
    saved, save_note = _save_active_document(bus, window_id)
    return _result(
        operation,
        True,
        f"edit_xml batch: {len(specs)} op(s), {result.data.get('affected', 0)} node(s), one undo step",
        data={"action": "batch", "saved": saved, "save_skipped_reason": save_note, **result.data},
        start=start,
    )


async def _op_transplant(
    bus: InkscapeDBus, operation: str, target: str, payload: str, window_id: int, config: Any, start: float
) -> dict[str, Any]:
    """Put a re-plotted group in place of its older, hand-styled version (see transplant.py).

    payload (JSON):
      source      — id of the new plot (pasted anywhere, usually off the page)
      target      — id of the old, restyled plot it replaces (or pass it as `target`)
      park        — keep the old plot, moved to where the new one was (default true)
      skip        — ids of old-plot objects not to carry over
      glyph_text  — {glyph-run id: "text"}: outlined text in the new plot to turn into real text
      text_style  — CSS for those texts, e.g. "font-family:Calibri;font-size:10.6667px"
      check       — report what would happen and write the new group to a file; change nothing
      preview     — render the result in place (as the preview op) instead of applying it
    One undo step when applied.
    """
    from .. import transplant

    try:
        args = json.loads(payload) if payload else {}
    except json.JSONDecodeError as exc:
        return _result(operation, False, f"payload is not valid JSON: {exc}", error=str(exc), start=start)
    source, old = str(args.get("source", "")).lstrip("#"), str(args.get("target") or target).lstrip("#")
    if not source or not old:
        return _result(operation, False, "payload needs source and target ids", error="bad payload", start=start)

    scratch = _scratch_path()
    await _export_document_async(bus, scratch)
    try:
        plan = transplant.plan(
            scratch.read_bytes(),
            source,
            old,
            park=bool(args.get("park", True)),
            skip=list(args.get("skip", [])),
            glyph_text=dict(args.get("glyph_text", {})),
            text_style=str(args.get("text_style", "")),
        )
    except transplant.TransplantError as exc:
        return _result(operation, False, str(exc), error="transplant", start=start)
    report = {
        "mode": plan.mode,
        "shift": [round(v, 3) for v in plan.shift],
        "styled": plan.styled,
        "dropped": plan.dropped,
        "copied": plan.extras,
        "outlined_text": plan.outlined_text,
    }
    if args.get("check") or args.get("preview"):
        xml_path = scratch.with_name(f"transplant-{re.sub(r'[^A-Za-z0-9_-]', '_', source)}.svg")
        xml_path.write_text(plan.new_xml, encoding="utf-8")
        report["xml_path"] = str(xml_path)
        if args.get("check"):
            return _result(
                operation, True, "transplant check (nothing changed): " + plan.summary(), data=report, start=start
            )
        shown = await _op_preview(
            bus,
            operation,
            old,
            json.dumps({"xml": plan.new_xml, "replace": old, "dpi": args.get("dpi", 300)}),
            config,
            start,
        )
        shown["message"] = "transplant preview (nothing changed): " + plan.summary() + "\n" + shown["message"]
        shown["data"] = {**shown.get("data", {}), **report}
        return shown
    edit = {"ops": plan.ops, "render": bool(args.get("render"))}
    applied = await _op_edit_xml(bus, operation, "", json.dumps(edit), window_id, start)
    if applied["success"]:
        applied["message"] = (
            f"transplanted #{source} into #{old}'s place: "
            + plan.summary()
            + (f"; before|after: {applied['data']['render']}" if applied.get("data", {}).get("render") else "")
        )
        applied["data"] = {**applied.get("data", {}), **report}
    return applied


async def _op_path_edit(
    bus: InkscapeDBus,
    operation: str,
    target: str,
    payload_json: str,
    window_id: int,
    start: float,
) -> dict[str, Any]:
    """Per-node bezier edits routed through the mcp_path_edit extension.

    ``target`` is the path element id. ``payload`` is JSON: ``{"op": "<sub-op>", ...}``
    with sub-op-specific fields (see plugins/mcp_path_edit.py docstring). Each
    invocation registers as one undoable "MCP: Path Edit" command.
    """
    if not target:
        return _result(operation, False, "target (path id) is required", error="missing target", start=start)
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError as exc:
        return _result(operation, False, f"payload is not valid JSON: {exc}", error=str(exc), start=start)
    if not isinstance(payload, dict):
        return _result(
            operation,
            False,
            "payload must decode to a JSON object",
            error="bad payload",
            start=start,
        )

    sub_op = payload.get("op")
    if sub_op not in _PATH_EDIT_OPS:
        return _result(
            operation,
            False,
            f"unknown path_edit op {sub_op!r}; valid: {sorted(_PATH_EDIT_OPS)}",
            error="bad op",
            start=start,
        )

    spec: dict[str, Any] = {"path_id": target}
    # Forward every payload key (op + per-op args). The extension validates
    # per-op required fields; we'd duplicate that validation by gating here.
    for key, value in payload.items():
        spec[key] = value

    result = await invoke_extension(bus, _PATH_EDIT_EXTENSION, spec, timeout_s=15.0)

    if not result.success:
        message = result.error or "path_edit extension failed"
        if result.stderr:
            message = f"{message}\nstderr:\n{result.stderr}"
        return _result(
            operation,
            False,
            message,
            data={"op": sub_op, "path_id": target, "stderr": result.stderr},
            error=result.error,
            start=start,
        )

    if result.data.get("ok") is False:
        return _result(
            operation,
            False,
            result.data.get("error", "path_edit failed"),
            data=result.data,
            error=result.data.get("error", ""),
            start=start,
        )

    saved, save_note = _save_active_document(bus, window_id)

    summary = result.data.get("summary", f"path_edit {sub_op!r} applied")
    return _result(
        operation,
        True,
        summary,
        data={
            "op": sub_op,
            "path_id": target,
            "saved": saved,
            "save_skipped_reason": save_note,
            **{k: v for k, v in result.data.items() if k not in ("ok", "op", "path_id")},
        },
        start=start,
    )


async def _op_changes(bus: InkscapeDBus, operation: str, payload: str, start: float) -> dict[str, Any]:
    """Report what changed in the live document since the last check, then re-baseline.

    Fingerprints an export snapshot (no undo entry, unlike the inspect extensions) and
    diffs it against the baseline shared with the Claude Code hooks (see change_tracker).
    ``payload`` may be ``{"mark": false}`` to peek without moving the baseline.
    """
    from .. import change_tracker

    mark = True
    if payload:
        try:
            mark = bool(json.loads(payload).get("mark", True))
        except (ValueError, AttributeError) as exc:
            return _result(operation, False, f"payload must be JSON like {{'mark': false}}: {exc}", start=start)

    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    scratch = _SCRATCH_DIR / f"track-{uuid.uuid4().hex}.svg"
    try:
        await _export_document_async(bus, scratch)
        docname, elements = change_tracker.fingerprint(scratch.read_bytes())
    finally:
        scratch.unlink(missing_ok=True)
    result = change_tracker.compare(docname, elements, mark=mark)

    if result.get("doc_switched_from") is not None:
        message = f"active document is now {docname!r} (baseline was {result['doc_switched_from']!r})"
    elif not result["had_baseline"]:
        message = f"no earlier baseline; {'recorded one' if mark else 'nothing to compare'} for {docname!r}"
    elif result["changes"]:
        message = f"{len(result['changes'])} change(s) to {docname!r} since the last check:\n" + "\n".join(
            result["changes"]
        )
    else:
        message = f"no changes to {docname!r} since the last check"
    return _result(operation, True, message, data=result, start=start)


async def _op_outline(bus: InkscapeDBus, operation: str, target: str, start: float) -> dict[str, Any]:
    """Compact outline of the live document (or of the subtree with id `target`).

    Reads an export snapshot, so — like ``changes`` — it leaves no undo entry.
    """
    from .. import figure_outline

    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    scratch = _SCRATCH_DIR / f"outline-{uuid.uuid4().hex}.svg"
    try:
        await _export_document_async(bus, scratch)
        text = figure_outline.outline(scratch.read_bytes(), target.lstrip("#"))
    except KeyError as exc:
        return _result(operation, False, str(exc.args[0]), error="unknown id", start=start)
    finally:
        scratch.unlink(missing_ok=True)
    return _result(operation, True, text, data={"outline": text}, start=start)


async def _op_cleanup(bus: InkscapeDBus, operation: str, payload: str, window_id: int, start: float) -> dict[str, Any]:
    """Remove leftover artefacts from the live document, as one undo step.

    The same rules as svg_cleanup: unused definitions (including the unused glyphs of a pasted
    plot's font), empty groups and empty texts. Inkscape's own "Clean up document" is not used:
    it only removes definitions Inkscape marked collectable, so pasted clip paths, path effects
    and glyphs all survive it. payload {"check": true} only reports.
    """
    from .. import svg_cleanup

    check = False
    if payload:
        try:
            check = bool(json.loads(payload).get("check", False))
        except (ValueError, AttributeError) as exc:
            return _result(operation, False, f"payload must be JSON like {{'check': true}}: {exc}", start=start)

    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    snap = _SCRATCH_DIR / f"cleanup-{uuid.uuid4().hex[:8]}.svg"
    try:
        await _export_document_async(bus, snap)
        original = etree.fromstring(snap.read_bytes(), parser=etree.XMLParser(huge_tree=True))
    finally:
        snap.unlink(missing_ok=True)
    for i, el in enumerate(original.iter()):  # number every element, to see exactly which go
        if isinstance(el.tag, str):
            el.set(_CLEANUP_MARK, str(i))
    cleaned = copy.deepcopy(original)
    report = svg_cleanup.prune(cleaned)

    summary = (
        f"{report.defs_removed} unused definition(s), {report.empty_groups_removed} empty group(s), "
        f"{report.empty_text_removed} empty text(s)"
    )
    if report.hidden_elements:
        summary += f"; {report.hidden_elements} hidden element(s) left alone"
    data = {k: v for k, v in vars(report).items() if k != "empty_group_ids"}
    if check or not report.changed():
        verb = "would remove" if report.changed() else "nothing to remove"
        return _result(operation, True, f"cleanup check: {verb} — {summary}", data={"check": True, **data}, start=start)

    ops = _cleanup_ops(original, cleaned)
    applied = await _op_edit_xml(bus, operation, "", json.dumps({"ops": ops}), window_id, start)
    if applied["success"]:
        applied["message"] = f"cleaned up, one undo step: {summary}"
        applied["data"] = {**applied.get("data", {}), "removed": data}
    return applied


_CLEANUP_MARK = "data-mcp-cleanup"


def _cleanup_ops(original: Any, cleaned: Any) -> list[dict[str, Any]]:
    """edit_xml ops turning `original` into `cleaned`: changed <defs> replaced, leftovers removed.

    Both trees carry _CLEANUP_MARK numbers (stripped from what is sent), so removed elements are
    known exactly; only the top of each removed subtree is named, so no op hits a node already gone.
    """
    defs_tag = f"{{{SVG_NS}}}defs"
    survivors = {el.get(_CLEANUP_MARK) for el in cleaned.iter() if isinstance(el.tag, str)}

    def unmarked(el: Any) -> str:
        dup = copy.deepcopy(el)
        for node in dup.iter():
            if isinstance(node.tag, str):
                node.attrib.pop(_CLEANUP_MARK, None)
        return str(etree.tostring(dup, encoding="unicode"))

    ops: list[dict[str, Any]] = []
    # prune() empties definitions but never removes a <defs> itself, so the two lists align.
    for k, (old, new) in enumerate(zip(original.iter(defs_tag), cleaned.iter(defs_tag), strict=True), start=1):
        if etree.tostring(old) != etree.tostring(new):
            ops.append({"xpath": f"(//svg:defs)[{k}]", "action": "replace", "xml": unmarked(new)})
    for el in original.iter():
        if not isinstance(el.tag, str) or el.get(_CLEANUP_MARK) in survivors:
            continue
        parent = el.getparent()
        if parent is not None and parent.get(_CLEANUP_MARK) not in survivors:
            continue  # goes with its parent
        if any(a.tag == defs_tag for a in el.iterancestors()):
            continue  # handled by the <defs> replacement
        if el.get("id"):
            ops.append({"xpath": f"//*[@id='{el.get('id')}']", "action": "remove"})
    return ops


async def _op_save_snapshot(bus: InkscapeDBus, operation: str, start: float) -> dict[str, Any]:
    scratch = _scratch_path()
    await _export_document_async(bus, scratch)
    xml = scratch.read_text(encoding="utf-8")
    return _result(
        operation,
        True,
        f"snapshot written to {scratch}",
        data={"path": str(scratch), "xml": xml, "bytes": len(xml)},
        start=start,
    )


async def inkscape_live(
    operation: str,
    target: str = "",
    payload: str = "",
    window_id: int = 1,
    cli_wrapper: Any = None,
    config: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Drive a running Inkscape GUI via D-Bus + clipboard staging."""
    folds = {"folded": 0, "missed": 0}
    token = _EDIT_FOLDS.set(folds)
    try:
        return await _inkscape_live(operation, target, payload, window_id, cli_wrapper, config, **kwargs)
    finally:
        _EDIT_FOLDS.reset(token)
        # Edits that went through edit_xml are already in the change baseline; any other
        # operation that may have changed the document leaves hook-stop to re-export it.
        if folds["missed"] or (operation not in _NO_DOC_CHANGE and operation not in _EDITS_VIA_EDIT_XML):
            change_tracker.mark_dirty()


async def _inkscape_live(
    operation: str,
    target: str,
    payload: str,
    window_id: int,
    cli_wrapper: Any,
    config: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    start = time.perf_counter()
    _SNAPSHOT_REUSE.set(operation in _READ_ONLY_OPS)
    if operation not in _READ_ONLY_OPS:
        _SNAPSHOT_CACHE.clear()
    try:
        bus = _get_bus(config)
    except Exception as exc:
        return _result(
            operation,
            False,
            f"could not initialize live bridge: {exc}",
            error=str(exc),
            start=start,
        )

    # ping is the only op that must not raise when the bridge is down. ping reports
    # current state only — it never auto-launches Inkscape.
    if operation == "ping":
        try:
            return _op_ping(bus, operation, start)
        except Exception as exc:
            return _result(operation, False, f"ping failed: {exc}", error=str(exc), start=start)

    # measuring a file needs no Inkscape
    if operation == "measure":
        from . import live_figure

        if live_figure.measure_path(payload):
            return await live_figure.op_measure(bus, operation, target, payload, start)

    # Auto-manage (Windows/macOS): if the bridge isn't live yet, start the bus + Inkscape.
    if (bus_manager.is_windows() or mac_bus_manager.is_macos()) and not bus.is_available():
        # macOS: when the triggering op is open_file, launch straight onto that file.
        # Launching on the default canvas and then opening the file leaves two documents,
        # which makes every window_id ambiguous — and macOS has no window-scope
        # document-close to get rid of the stray canvas.
        launch_path = None
        if mac_bus_manager.is_macos() and operation == "open_file" and os.path.isabs(target) and os.path.isfile(target):
            launch_path = target
            from .live_figure import remember_path

            remember_path(target)
        try:
            ready = _auto_launch_inkscape(config, open_path=launch_path)
        except Exception as exc:
            return _result(
                operation,
                False,
                f"auto-launch of Inkscape failed: {exc}",
                error=str(exc),
                start=start,
            )
        if launch_path and ready:
            return _result(
                operation,
                True,
                f"launched Inkscape on {launch_path!r}",
                data={"path": launch_path, "launched": True},
                start=start,
            )

    if not bus.is_available():
        return _result(
            operation,
            False,
            "Inkscape D-Bus bridge not reachable (is the GUI running?)",
            error="bridge unavailable",
            start=start,
        )

    # `window_id` is an assertion, not a router. Inkscape 1.4 exposes no focus-window
    # action, and the bridge plugins are effect extensions that Inkscape runs against
    # whatever desktop is active — so a caller asking for window 1 while window 2 is
    # focused used to silently get window 2's document, and (with auto-save on) write
    # to it. Refusing when the target is ambiguous is the only honest option; callers
    # that don't care leave window_id at 0 and read `active_document` from the result.
    requested_window = window_id
    try:
        window_id = _resolve_window_id(bus, window_id or 1)
    except RuntimeError as exc:
        return _result(operation, False, str(exc), error="no windows", start=start)

    if requested_window:
        try:
            open_windows = bus.list_windows()
        except Exception:
            open_windows = []
        if requested_window not in open_windows:
            return _result(
                operation,
                False,
                f"window_id={requested_window} is not open (open windows: {open_windows or 'none'})",
                error="unknown window",
                start=start,
            )
        if len(open_windows) > 1 and operation not in _WINDOW_AGNOSTIC_OPS:
            return _result(
                operation,
                False,
                (
                    f"window_id={requested_window} cannot be honoured: {len(open_windows)} documents "
                    "are open and Inkscape 1.4 provides no way to target one — every operation acts "
                    "on the ACTIVE document. Focus the window you want and retry with window_id=0, "
                    "then confirm via the active_document field in the response."
                ),
                data={"open_windows": open_windows},
                error="ambiguous window",
                start=start,
            )

    try:
        if operation == "get_document_xml":
            return await _op_get_document_xml(bus, operation, target, start)
        if operation == "get_selection":
            return await _op_get_selection(bus, operation, start)
        if operation == "set_selection":
            return _op_set_selection(bus, operation, target, start)
        if operation == "insert_svg":
            # One code path on every platform now: the clipboard route it used to take
            # on Linux lost coordinates and raced xclip, and the Windows inkex route
            # went through mcp_execute, whose mutations are not reliably persisted.
            return await _op_insert_svg(bus, operation, payload, window_id, start)
        if operation == "delete_selected":
            return _op_delete_selected(bus, operation, window_id, start)
        if operation == "apply_action":
            return _op_apply_action(bus, operation, target, payload, window_id, start)
        if operation == "list_actions":
            return await _op_list_actions(bus, operation, window_id, target, start)
        if operation == "open_file":
            return _op_open_file(bus, operation, target, start)
        if operation == "save_snapshot":
            return await _op_save_snapshot(bus, operation, start)
        if operation == "changes":
            return await _op_changes(bus, operation, payload, start)
        if operation == "cleanup":
            return await _op_cleanup(bus, operation, payload, window_id, start)
        if operation == "outline":
            return await _op_outline(bus, operation, target, start)
        if operation == "edit_xml":
            return await _op_edit_xml(bus, operation, target, payload, window_id, start)
        if operation == "measure":
            from . import live_figure

            return await live_figure.op_measure(bus, operation, target, payload, start)
        if operation in ("selection", "save", "label", "organize", "parked"):
            from . import live_figure

            if operation == "selection":
                return await live_figure.op_selection(bus, operation, payload, config, start)
            if operation == "save":
                return await live_figure.op_save(bus, operation, payload, start)
            if operation == "label":
                return await live_figure.op_label(bus, operation, payload, window_id, config, start)
            if operation == "organize":
                return await live_figure.op_organize(bus, operation, payload, window_id, config, start)
            return await live_figure.op_parked(bus, operation, payload, window_id, config, start)
        if operation == "transplant":
            return await _op_transplant(bus, operation, target, payload, window_id, config, start)
        if operation == "path_edit":
            return await _op_path_edit(bus, operation, target, payload, window_id, start)
        if operation == "path_offset":
            return await _op_path_offset(bus, operation, target, payload, window_id, start)
        if operation == "inspect_selection":
            return await _op_inspect_selection(bus, operation, start)
        if operation == "inspect_layers":
            return await _op_inspect_layers(bus, operation, start)
        if operation == "inspect_defs":
            return await _op_inspect_defs(bus, operation, start)
        if operation == "inspect_view":
            return await _op_inspect_view(bus, operation, start)
        if operation == "inspect_pages":
            return await _op_inspect_pages(bus, operation, start)
        if operation == "inspect_element":
            return await _op_inspect_element(bus, operation, target, start)
        if operation == "execute_inkex":
            return await _op_execute_inkex(bus, operation, payload, start)
        if operation == "component":
            return await _op_component(bus, operation, target, payload, window_id, start)
        if operation == "preview":
            return await _op_preview(bus, operation, target, payload, config, start)
        if operation == "rasterize":
            return await _op_rasterize(bus, operation, target, payload, cli_wrapper, config, start)
        from typing import get_args

        from ..mcp_tool_types import InkscapeLiveOperation

        return _result(
            operation,
            False,
            f"unknown operation {operation!r}; valid: {', '.join(get_args(InkscapeLiveOperation))}",
            error="unknown operation",
            start=start,
        )
    except Exception as exc:
        return _result(operation, False, f"{operation} failed: {exc}", error=str(exc), start=start)
