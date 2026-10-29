"""Tell the agent what the user changed in Inkscape since the agent last looked.

The live document is fingerprinted from an `export-do` snapshot — which, unlike the
inspect/execute extensions, leaves no "[unchanged]" step in the user's undo history —
and compared with a baseline kept on disk. The baseline lives in a file rather than in
memory because the parties that use it are separate processes: the MCP server (the
`changes` live operation) and the Claude Code hooks that call this module's CLI:

  python -m inkscape_mcp.change_tracker hook-stop     # agent finished: mark baseline
  python -m inkscape_mcp.change_tracker hook-prompt   # user wrote: report changes, re-mark

Hook commands never fail loudly and give up after a few seconds: a broken hook would
otherwise get in the way of every message.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from lxml import etree

BASELINE_FILE = Path(os.path.expanduser("~/.cache/inkscape_mcp/change-baseline.json"))

_SVG = "http://www.w3.org/2000/svg"
_INK = "http://www.inkscape.org/namespaces/inkscape"
_SODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"

# Subtrees that are view state or metadata, not drawing.
_SKIP_TAGS = frozenset({f"{{{_SODI}}}namedview", f"{{{_SVG}}}metadata"})
# Root/export bookkeeping that changes without the drawing changing.
_VOLATILE_ATTRS = frozenset(
    {
        f"{{{_SODI}}}docname",
        f"{{{_INK}}}version",
        f"{{{_INK}}}export-filename",
        f"{{{_INK}}}export-xdpi",
        f"{{{_INK}}}export-ydpi",
    }
)
_MAX_LINES = 30
_MAX_VALUE = 60
_HOOK_TIMEOUT_S = 8.0


# ---- fingerprinting ----------------------------------------------------------------


def _local(name: str) -> str:
    """`{ns}local` -> `prefix:local` for the namespaces users see, else the bare name."""
    if name.startswith(f"{{{_INK}}}"):
        return "inkscape:" + name.split("}", 1)[1]
    if name.startswith(f"{{{_SODI}}}"):
        return "sodipodi:" + name.split("}", 1)[1]
    return str(etree.QName(name).localname)


def _style(value: str) -> dict[str, str]:
    out = {}
    for decl in value.split(";"):
        key, _, val = decl.partition(":")
        if key.strip():
            out[key.strip()] = val.strip()
    return out


def fingerprint(svg_bytes: bytes) -> tuple[str, dict[str, dict[str, Any]]]:
    """Return (document name, {element key: signature}) for an exported SVG.

    Text is fingerprinted as a unit (its tspans fold into the text content), so editing a
    label reports one changed text rather than a churn of tspans.
    """
    root = etree.fromstring(svg_bytes, parser=etree.XMLParser(huge_tree=True, resolve_entities=False))
    docname = root.get(f"{{{_SODI}}}docname", "")
    elements: dict[str, dict[str, Any]] = {}

    def key_of(el: Any, parent_key: str, index: int) -> str:
        return el.get("id") or f"{parent_key}/{_local(el.tag)}[{index}]"

    def walk(el: Any, parent_key: str, index: int) -> None:
        if not isinstance(el.tag, str) or el.tag in _SKIP_TAGS:
            return
        key = key_of(el, parent_key, index)
        attrs = {_local(k): v for k, v in el.attrib.items() if k != "id" and k not in _VOLATILE_ATTRS and k != "style"}
        sig: dict[str, Any] = {
            "tag": _local(el.tag),
            "parent": parent_key,
            "attrs": attrs,
            "style": _style(el.get("style", "")),
        }
        if sig["tag"] == "text":
            sig["text"] = "".join(el.itertext()).strip()
            elements[key] = sig
            return
        elements[key] = sig
        for i, child in enumerate(c for c in el if isinstance(c.tag, str)):
            walk(child, key, i)

    walk(root, "", 0)
    return docname, elements


# ---- diffing -----------------------------------------------------------------------


def _short(value: Any) -> str:
    text = "none" if value in (None, "") else str(value)
    return text if len(text) <= _MAX_VALUE else text[: _MAX_VALUE - 1] + "…"


def _label(key: str, sig: dict[str, Any]) -> str:
    name = f"<{sig['tag']}#{key}>" if "/" not in key else f"<{sig['tag']}> at {key}"
    if sig.get("text"):
        name += f' "{_short(sig["text"])}"'
    return name


def diff(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> list[str]:
    """Human-readable changes from `old` to `new`, top-most element of each change only."""
    lines: list[str] = []
    removed = old.keys() - new.keys()
    added = new.keys() - old.keys()

    for key in sorted(removed):
        if old[key]["parent"] not in removed:  # only the top of a deleted subtree
            size = sum(1 for k in removed if _is_descendant(k, key, old))
            extra = f" ({size} element(s) inside)" if size else ""
            lines.append(f"- deleted {_label(key, old[key])}{extra}")
    for key in sorted(added):
        if new[key]["parent"] not in added:
            size = sum(1 for k in added if _is_descendant(k, key, new))
            extra = f" ({size} element(s) inside)" if size else ""
            lines.append(f"+ added {_label(key, new[key])} in #{new[key]['parent'] or 'root'}{extra}")
    for key in sorted(old.keys() & new.keys()):
        before, after = old[key], new[key]
        changes: list[str] = []
        if before["parent"] != after["parent"]:
            changes.append(f"moved from #{before['parent']} to #{after['parent']}")
        for name in sorted(before["attrs"].keys() | after["attrs"].keys()):
            a, b = before["attrs"].get(name), after["attrs"].get(name)
            if a != b:
                changes.append(f"{name}: {_short(a)} → {_short(b)}")
        for name in sorted(before["style"].keys() | after["style"].keys()):
            a, b = before["style"].get(name), after["style"].get(name)
            if a != b:
                changes.append(f"style {name}: {_short(a)} → {_short(b)}")
        if before.get("text") != after.get("text"):
            changes.append(f'text: "{_short(before.get("text"))}" → "{_short(after.get("text"))}"')
        if changes:
            lines.append(f"~ {_label(key, after)}: " + "; ".join(changes))

    if len(lines) > _MAX_LINES:
        lines = [*lines[:_MAX_LINES], f"… and {len(lines) - _MAX_LINES} more change(s)"]
    return lines


def _is_descendant(key: str, ancestor: str, elements: dict[str, dict[str, Any]]) -> bool:
    parent = elements[key]["parent"]
    while parent:
        if parent == ancestor:
            return True
        parent = elements[parent]["parent"] if parent in elements else ""
    return False


# ---- baseline ----------------------------------------------------------------------


def load_baseline() -> dict[str, Any] | None:
    try:
        data = json.loads(BASELINE_FILE.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "elements" in data else None


def save_baseline(docname: str, elements: dict[str, dict[str, Any]]) -> None:
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BASELINE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"doc": docname, "elements": elements}))
    tmp.replace(BASELINE_FILE)


def compare(docname: str, elements: dict[str, dict[str, Any]], *, mark: bool = True) -> dict[str, Any]:
    """Diff `elements` against the stored baseline and (by default) make them the new one."""
    baseline = load_baseline()
    result: dict[str, Any] = {"doc": docname, "had_baseline": baseline is not None, "changes": []}
    if baseline is not None:
        if baseline.get("doc") != docname:
            result["doc_switched_from"] = baseline.get("doc")
        else:
            result["changes"] = diff(baseline["elements"], elements)
    if mark:
        save_baseline(docname, elements)
    return result


def format_report(result: dict[str, Any]) -> str:
    """Context block for the agent, or '' when there is nothing worth saying."""
    if result.get("doc_switched_from") is not None:
        return (
            f"Inkscape's active document is now {result['doc']!r} "
            f"(was {result['doc_switched_from']!r} when you last looked)."
        )
    if not result["changes"]:
        return ""
    return (
        f"Since your last turn the user changed {result['doc']!r} in Inkscape:\n"
        + "\n".join(result["changes"])
        + "\nTreat the live document as the source of truth; build on these edits rather than reverting them."
    )


# ---- live snapshot (CLI / hooks) ---------------------------------------------------


def _passive_bus() -> Any:
    """The platform's bridge client, WITHOUT starting a bus or Inkscape (unlike _get_bus)."""
    if sys.platform == "darwin":
        from . import mac_bus_manager
        from .dbus_client import MacInkscapeDBus

        return MacInkscapeDBus(mac_bus_manager.bus_address())
    elif sys.platform == "win32":
        from . import bus_manager
        from .win_dbus_client import WinInkscapeDBus

        gdbus = bus_manager.find_gdbus()
        if not gdbus or not bus_manager.ADDRESS_FILE.exists():
            return None
        return WinInkscapeDBus(gdbus, bus_manager.ADDRESS_FILE.read_text(encoding="utf-8").strip())
    else:
        from .dbus_client import InkscapeDBus

        return InkscapeDBus()


def snapshot_live() -> tuple[str, dict[str, dict[str, Any]]] | None:
    """Fingerprint the live active document, or None if the bridge isn't up."""
    from .tools.live import _SCRATCH_DIR, _export_document

    bus = _passive_bus()
    if bus is None or not bus.is_available():
        return None
    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    scratch = _SCRATCH_DIR / f"track-{uuid.uuid4().hex}.svg"
    try:
        _export_document(bus, scratch)
        return fingerprint(scratch.read_bytes())
    finally:
        scratch.unlink(missing_ok=True)


def _with_timeout(fn: Any, timeout_s: float) -> Any:
    """Run fn in a daemon thread; None if it errors or overruns (jeepney calls can block)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # hooks must never surface errors
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout_s)
    return box.get("value")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command not in {"hook-stop", "hook-prompt", "mark", "report"}:
        print(__doc__)
        return 2

    snap = _with_timeout(snapshot_live, _HOOK_TIMEOUT_S)
    if snap is None:
        return 0  # Inkscape not running / unreachable: say nothing
    docname, elements = snap

    if command in {"hook-stop", "mark"}:
        save_baseline(docname, elements)
        return 0

    report = format_report(compare(docname, elements))
    if command == "report":
        print(report or "no changes")
    elif report:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": report}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
