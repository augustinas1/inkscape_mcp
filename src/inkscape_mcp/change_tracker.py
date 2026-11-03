"""Tell the agent what the user changed in Inkscape since the agent last looked.

The live document is fingerprinted from an `export-do` snapshot — which, unlike the
inspect/execute extensions, leaves no "[unchanged]" step in the user's undo history —
and compared with a baseline kept on disk. The baseline lives in a file rather than in
memory because the parties that use it are separate processes: the MCP server (the
`changes` live operation) and the Claude Code hooks that call this module's CLI:

  python -m inkscape_mcp.change_tracker hook-session  # session (re)started: outline, mark
  python -m inkscape_mcp.change_tracker hook-stop     # agent finished: mark baseline
  python -m inkscape_mcp.change_tracker hook-prompt   # user wrote: report changes (+ outline of added groups), re-mark
  python -m inkscape_mcp.change_tracker outline       # print the live figure outline

Hook commands never fail loudly and give up after a few seconds: a broken hook would
otherwise get in the way of every message.
"""

from __future__ import annotations

import json
import os
import re
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
_MAX_ADDED_OUTLINES = 3
_ADDED_OUTLINE_LINES = 25
_OUTLINE_INTRO = (
    "Outline of the figure open in Inkscape (refresh with inkscape_live operation=outline; "
    "address elements by these ids):"
)


# ---- fingerprinting ----------------------------------------------------------------


def _local(name: str) -> str:
    """`{ns}local` -> `prefix:local` for the namespaces users see, else the bare name."""
    if name.startswith(f"{{{_INK}}}"):
        return "inkscape:" + name.split("}", 1)[1]
    if name.startswith(f"{{{_SODI}}}"):
        return "sodipodi:" + name.split("}", 1)[1]
    return str(etree.QName(name).localname)


_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _norm(value: str) -> str:
    """A value with Inkscape's re-serialisation noise removed.

    Inkscape rewrites attributes it did not change in meaning — `matrix(1 0 0 1 5 6)` becomes
    `matrix(1,0,0,1,5,6)`, double quotes in a font name become single — which used to be
    reported as user edits. Numbers are compared at 3 decimals; separators and quotes ignored.
    """
    text = _NUMBER.sub(lambda m: f"{round(float(m.group()), 3):g}", value)
    return " ".join(text.replace(",", " ").replace('"', "").replace("'", "").split())


def _transform(value: str) -> str:
    """Transforms compared as their matrix, so translate(5, 6) == matrix(1,0,0,1,5,6)."""
    try:
        import inkex

        return " ".join(f"{round(v, 3):g}" for v in inkex.Transform(value).to_hexad())
    except Exception:
        return _norm(value)


def _style(value: str) -> dict[str, str]:
    out = {}
    for decl in value.split(";"):
        key, _, val = decl.partition(":")
        if key.strip():
            out[key.strip()] = _norm(val)
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
        attrs = {
            _local(k): _norm(v) for k, v in el.attrib.items() if k != "id" and k not in _VOLATILE_ATTRS and k != "style"
        }
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
    moves: dict[tuple[str, str], list[str]] = {}
    for key in sorted(old.keys() & new.keys()):
        before, after = old[key], new[key]
        changes: list[str] = []
        if before["parent"] != after["parent"]:
            changes.append(f"moved from #{before['parent']} to #{after['parent']}")
        for name in sorted(before["attrs"].keys() | after["attrs"].keys()):
            a, b = before["attrs"].get(name), after["attrs"].get(name)
            same = _transform(a) == _transform(b) if name == "transform" and a and b else a == b
            if not same:
                changes.append(f"{name}: {_short(a)} → {_short(b)}")
        for name in sorted(before["style"].keys() | after["style"].keys()):
            a, b = before["style"].get(name), after["style"].get(name)
            if a != b:
                changes.append(f"style {name}: {_short(a)} → {_short(b)}")
        if before.get("text") != after.get("text"):
            changes.append(f'text: "{_short(before.get("text"))}" → "{_short(after.get("text"))}"')
        if changes == [f"moved from #{before['parent']} to #{after['parent']}"]:
            moves.setdefault((before["parent"], after["parent"]), []).append(key)
        elif changes:
            lines.append(f"~ {_label(key, after)}: " + "; ".join(changes))
    for (src, dst), keys in moves.items():  # regrouping moves many elements at once: one line
        if len(keys) <= 2:
            lines.extend(f"~ {_label(k, new[k])}: moved from #{src} to #{dst}" for k in keys)
        else:
            shown = ", ".join(keys[:4]) + (", …" if len(keys) > 4 else "")
            lines.append(f"~ {len(keys)} elements moved from #{src or 'root'} to #{dst or 'root'} ({shown})")

    if len(lines) > _MAX_LINES:
        lines = [*lines[:_MAX_LINES], f"… and {len(lines) - _MAX_LINES} more change(s)"]
    return lines


def added_roots(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> list[str]:
    """Ids of the top-most added elements (the groups the user created or pasted)."""
    added = new.keys() - old.keys()
    return sorted(k for k in added if "/" not in k and new[k]["parent"] not in added)


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
            result["added"] = added_roots(baseline["elements"], elements)
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


def snapshot_live() -> bytes | None:
    """Export the live active document, or None if the bridge isn't up."""
    from .tools.live import _SCRATCH_DIR, _export_document

    bus = _passive_bus()
    if bus is None or not bus.is_available():
        return None
    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    scratch = _SCRATCH_DIR / f"track-{uuid.uuid4().hex}.svg"
    try:
        _export_document(bus, scratch)
        return scratch.read_bytes()
    finally:
        scratch.unlink(missing_ok=True)


def _outline(svg_bytes: bytes) -> str:
    from .figure_outline import outline

    try:
        return outline(svg_bytes)
    except Exception:  # an outline is a nicety; never let it cost the change report
        return ""


def _added_outlines(svg_bytes: bytes, added: list[str]) -> str:
    """Outlines of the groups the user added (the rest of the figure the agent already knows)."""
    from .figure_outline import outline

    parts = []
    for gid in added[:_MAX_ADDED_OUTLINES]:
        try:
            body = outline(svg_bytes, gid, _ADDED_OUTLINE_LINES).split("\n", 1)[1]
        except Exception:  # noqa: S112 — an outline is a nicety; the change report must still go out
            continue
        parts.append(body)
    if not parts:
        return ""
    return "\n\nAdded, as outlined (refresh with inkscape_live operation=outline target=<id>):\n" + "\n".join(parts)


def _hook_output(event: str, context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}})


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
    if command not in {"hook-session", "hook-stop", "hook-prompt", "mark", "report", "outline"}:
        print(__doc__)
        return 2

    svg_bytes = _with_timeout(snapshot_live, _HOOK_TIMEOUT_S)
    if svg_bytes is None:
        return 0  # Inkscape not running / unreachable: say nothing
    if command == "outline":
        print(_outline(svg_bytes))
        return 0
    docname, elements = fingerprint(svg_bytes)

    if command in {"hook-stop", "mark"}:
        save_baseline(docname, elements)
        return 0
    if command == "hook-session":
        save_baseline(docname, elements)
        outline = _outline(svg_bytes)
        if outline:
            print(_hook_output("SessionStart", f"{_OUTLINE_INTRO}\n{outline}"))
        return 0

    result = compare(docname, elements)
    report = format_report(result)
    if command == "report":
        print(report or "no changes")
    elif report:
        print(_hook_output("UserPromptSubmit", report + _added_outlines(svg_bytes, result.get("added", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
