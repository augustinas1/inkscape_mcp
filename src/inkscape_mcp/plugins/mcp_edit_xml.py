#!/usr/bin/env python3
"""mcp_edit_xml — structural XML edit applied to the live Inkscape document.

This is the inkex transport for ``inkscape_live(operation="edit_xml")``. By
routing edits through an EffectExtension, the mutation becomes a single
undoable command in Inkscape's normal flow — preserving the user's undo
history (unlike the older ``document-revert`` path which discarded it).

Reads ``~/.cache/inkscape_mcp/exchange/input.json`` describing the edit:

    {
        "xpath":  "//svg:defs",
        "action": "append|insert_before|insert_after|replace|remove|set_attr|set_text",
        "xml":    "<filter id=\"x\"/>",      # append/insert_*/replace
        "name":   "fill", "value": "#ff0000",  # set_attr
        "text":   "new text"                   # set_text
    }

or a batch, applied as ONE undoable step and all-or-nothing:

    {"ops": [{"xpath": ..., "action": ..., ...}, ...]}

The batch is first run against a copy of the document; only if every op succeeds is it
replayed on the real one, so a failing op leaves the document untouched and the error
names its index.

**`set_attr` empty-string semantics:** ``value=""`` deletes the named
attribute (canonical "absent") rather than writing ``attr=""``. Several SVG
renderers — Inkscape 1.4 included — treat the empty-string form as a
malformed reference and silently hide the element. See the project
plan §4 Phase 5 lessons for the discovery history.

Writes ``~/.cache/inkscape_mcp/exchange/result.json`` with either
``{"ok": True, "action": ..., "xpath": ..., "affected": <int>}`` on success
or ``{"ok": False, "error": "<message>"}`` on failure. Unexpected
exceptions also get their traceback dumped to ``stderr.txt`` before being
re-raised so Inkscape logs them.
"""

import copy
import json
import os
import traceback
from pathlib import Path

import inkex
from lxml import etree

EXCHANGE_DIR = Path(os.path.expanduser("~/.cache/inkscape_mcp/exchange"))

_NS_MAP = {
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
    "inkscape": "http://www.inkscape.org/namespaces/inkscape",
    "sodipodi": "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_EDIT_XML_ACTIONS = frozenset(
    {
        "append",
        "insert_before",
        "insert_after",
        "remove",
        "set_attr",
        "set_text",
        "replace",
    }
)


def _parse_svg_fragment(xml_str: str) -> list:
    """Parse an XML fragment into its top-level elements, in document order.

    Without the wrapper, lxml gives the user-supplied fragment ``xmlns=""``
    which Inkscape refuses to render. The wrapper declares the SVG default
    namespace plus the usual Inkscape/sodipodi/xlink/rdf/dc prefixes so any
    element the caller writes ends up in the correct namespace.

    Several sibling elements are allowed. Requiring exactly one made it impossible to
    insert a group of shapes in a single undoable edit — and `insert_svg`, which builds
    on this, is *typically* handed several siblings.
    """
    nsdecls = " ".join(f'xmlns:{p}="{u}"' for p, u in _NS_MAP.items())
    wrapped = f'<root xmlns="{_NS_MAP["svg"]}" {nsdecls}>{xml_str}</root>'
    root = etree.fromstring(wrapped.encode("utf-8"))
    elements = [c for c in root if isinstance(c.tag, str)]
    if not elements:
        raise ValueError("fragment contained no elements")
    return elements


class _EditError(Exception):
    """An op that cannot be applied; the message is reported to the caller."""


def _select(root, xpath) -> list:
    if not xpath or not isinstance(xpath, str):
        raise _EditError("'xpath' is required")
    try:
        matches = root.xpath(xpath, namespaces=_NS_MAP)
    except etree.XPathEvalError as exc:
        raise _EditError(f"bad xpath {xpath!r}: {exc}") from exc

    # An XPath can legitimately evaluate to something other than a node-set —
    # `//@id` yields strings, `count(...)` a float. Those have no .append/.getparent,
    # and AttributeError isn't in the mutation handler below, so the exception escaped
    # effect(), result.json was never written, and the caller sat out the full 15s
    # timeout instead of getting an error.
    if not isinstance(matches, list):
        raise _EditError(
            f"xpath {xpath!r} evaluated to {type(matches).__name__}, not elements — edit_xml needs an element node-set"
        )
    if not matches:
        raise _EditError(f"xpath matched no nodes: {xpath!r}")
    non_elements = [m for m in matches if not isinstance(m, etree._Element)]
    if non_elements:
        raise _EditError(
            f"xpath {xpath!r} matched {len(non_elements)} non-element result(s) "
            f"(e.g. {type(non_elements[0]).__name__}) — edit_xml only mutates elements"
        )
    return matches


def _set_attr(node, name: str, raw_value) -> None:
    # Empty string = delete the attribute (canonical "absent") rather than write
    # attr="". Some SVG renderers treat attr="" as malformed and hide the element
    # entirely (notably filter="" on Inkscape 1.4). Callers wanting an explicit empty
    # string can use set_text on a child or bypass via edit_xml(replace).
    if raw_value == "":
        # Pop both Clark-notation and prefix-mapped forms.
        if name in node.attrib:
            del node.attrib[name]
        elif ":" in name:
            prefix, local = name.split(":", 1)
            ns_uri = _NS_MAP.get(prefix)
            if ns_uri:
                clark = f"{{{ns_uri}}}{local}"
                if clark in node.attrib:
                    del node.attrib[clark]
    elif ":" in name and not name.startswith("{"):
        prefix, local = name.split(":", 1)
        ns_uri = _NS_MAP.get(prefix)
        node.set(f"{{{ns_uri}}}{local}" if ns_uri else name, str(raw_value))
    else:
        node.set(name, str(raw_value))


def _apply(root, op) -> int:
    """Apply one edit op to the tree under `root`; return the number of nodes affected."""
    if not isinstance(op, dict):
        raise _EditError("each op must be an object")
    action = op.get("action")
    if action not in _EDIT_XML_ACTIONS:
        raise _EditError(f"unknown action {action!r}; valid: {sorted(_EDIT_XML_ACTIONS)}")
    xpath = op.get("xpath")
    matches = _select(root, xpath)

    affected = 0
    try:
        for node in matches:
            if action == "append":
                for frag in _parse_svg_fragment(op["xml"]):
                    node.append(frag)
            elif action == "insert_before":
                for frag in _parse_svg_fragment(op["xml"]):
                    node.addprevious(frag)
            elif action == "insert_after":
                # Reversed: each addnext lands immediately after `node`, so
                # inserting in reverse preserves the fragment's own order.
                for frag in reversed(_parse_svg_fragment(op["xml"])):
                    node.addnext(frag)
            elif action == "remove":
                parent = node.getparent()
                if parent is not None:
                    parent.remove(node)
            elif action == "set_attr":
                _set_attr(node, op["name"], op.get("value", ""))
            elif action == "set_text":
                node.text = op.get("text", "")
            elif action == "replace":
                parent = node.getparent()
                if parent is None:
                    raise ValueError("cannot replace the document root")
                frags = _parse_svg_fragment(op["xml"])
                parent.replace(node, frags[0])
                # Any further siblings follow the one that took node's place.
                for extra in reversed(frags[1:]):
                    frags[0].addnext(extra)
            affected += 1
    except (KeyError, ValueError, etree.XMLSyntaxError) as exc:
        raise _EditError(f"mutation failed: {exc}") from exc
    return affected


def run_ops(root, spec) -> dict:
    """Apply a single op or an {"ops": [...]} batch to `root`; return the result payload.

    A batch is rehearsed on a copy first, so it is all-or-nothing.
    """
    if not isinstance(spec, dict):
        return {"ok": False, "error": "input.json must decode to an object"}
    if "ops" not in spec:
        try:
            affected = _apply(root, spec)
        except _EditError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "action": spec.get("action"), "xpath": spec.get("xpath"), "affected": affected}

    ops = spec["ops"]
    if not isinstance(ops, list) or not ops:
        return {"ok": False, "error": "'ops' must be a non-empty list"}
    rehearsal = copy.deepcopy(root)
    for i, op in enumerate(ops):
        try:
            _apply(rehearsal, op)
        except _EditError as exc:
            label = op.get("action") if isinstance(op, dict) else repr(op)
            return {"ok": False, "error": f"op {i} ({label}): {exc}"}
    counts = [_apply(root, op) for op in ops]
    return {"ok": True, "action": "batch", "ops": len(ops), "affected": sum(counts), "affected_per_op": counts}


class McpEditXml(inkex.EffectExtension):
    def effect(self) -> None:
        EXCHANGE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            spec = json.loads((EXCHANGE_DIR / "input.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            _write_result({"ok": False, "error": f"could not read input.json: {exc}"})
            return

        # self.svg is the <svg> root element in modern inkex; older versions
        # exposed an ElementTree. Handle both so the extension is portable.
        root = self.svg.getroot() if hasattr(self.svg, "getroot") else self.svg
        result = run_ops(root, spec)
        if result["ok"]:
            # Name the document that was actually mutated. Inkscape runs an effect against
            # the ACTIVE desktop and offers no way to pick another, so this is the caller's
            # only way to confirm the edit landed where they intended.
            result["active_document"] = _document_identity(root)
        _write_result(result)


def _write_result(payload: dict) -> None:
    (EXCHANGE_DIR / "result.json").write_text(json.dumps(payload))


def _document_identity(root) -> dict:
    """Identify the document this extension ran against.

    Reports only what is knowable: `path` is set ONLY when `sodipodi:docname` is already
    absolute. Resolving a bare docname against the CWD would be wrong — an extension's
    CWD is the extension install directory, not the document's location.

    Duplicated in mcp_inspect.py: these plugins are copied into Inkscape's extension
    directory as standalone scripts, so they cannot share a module.
    """
    docname = root.get("{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}docname") or ""
    path = docname if docname.startswith("/") or (len(docname) > 2 and docname[1] == ":") else ""
    return {"docname": docname, "path": path, "root_id": root.get("id") or ""}


if __name__ == "__main__":
    try:
        McpEditXml().run()
    except SystemExit:
        raise
    except Exception:
        EXCHANGE_DIR.mkdir(parents=True, exist_ok=True)
        (EXCHANGE_DIR / "stderr.txt").write_text(traceback.format_exc())
        raise
