"""Remove leftover artefacts from SVG files: unused <defs> entries and empty groups.

Inkscape documents accumulate definitions — gradients, markers, clip paths, path effects,
patterns — that nothing references any more (copy-paste between documents is the usual
source: a figure cropped from a large master can carry thousands). They bloat files and
travel along on import. This module keeps exactly what the visible drawing reaches:

  * every id referenced from outside <defs> — ``url(#id)`` in any attribute or style,
    ``href``/``xlink:href="#id"``, and ``#id`` lists such as ``inkscape:path-effect`` —
  * plus, transitively, everything those definitions reference in turn.

Command line (in place, with an optional backup and a render check)::

    python -m inkscape_mcp.svg_cleanup [--check] [--backup DIR] [--verify] FILE_OR_DIR ...
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

SVG = "http://www.w3.org/2000/svg"
_URL = re.compile(r"url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)")
_HASH = re.compile(r"#([A-Za-z_][\w.\-:]*)")
_KEEP_ANYWAY = {"style", "script"}  # never pruned: CSS may reference ids in ways we do not parse


def _local(tag: Any) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def references(el: Any) -> set[str]:
    """Ids referenced by `el`'s own attributes (not its descendants)."""
    out: set[str] = set()
    for name, value in el.attrib.items():
        out.update(_URL.findall(value))
        local = etree.QName(name).localname
        if local == "href" or value.startswith("#"):
            # href="#id", inkscape:path-effect="#a;#b", inkscape:perspectiveID="#p", ...
            out.update(_HASH.findall(value) if value.startswith("#") else [])
    if _local(el.tag) == "style" and el.text:
        out.update(_URL.findall(el.text))
    return out


def _in_defs(el: Any) -> bool:
    return any(_local(a.tag) == "defs" for a in el.iterancestors())


@dataclass
class CleanupReport:
    defs_removed: int = 0
    empty_group_ids: list[str] = field(default_factory=list)  # outermost removed empty groups
    defs_kept: int = 0
    empty_groups_removed: int = 0
    hidden_elements: int = 0  # reported only: hidden layers/objects may be intentional

    def changed(self) -> bool:
        return bool(self.defs_removed or self.empty_groups_removed)


def prune(root: Any) -> CleanupReport:
    """Prune unused definitions and empty groups from the tree under `root`, in place."""
    report = CleanupReport()
    by_id = {el.get("id"): el for el in root.iter() if isinstance(el.tag, str) and el.get("id")}

    reachable: set[str] = set()
    frontier: list[str] = []
    for el in root.iter():
        if isinstance(el.tag, str) and not _in_defs(el) and _local(el.tag) != "defs":
            for ref in references(el):
                if ref not in reachable:
                    reachable.add(ref)
                    frontier.append(ref)
    while frontier:
        target = by_id.get(frontier.pop())
        if target is None:
            continue
        for el in target.iter():
            if not isinstance(el.tag, str):
                continue
            for ref in references(el):
                if ref not in reachable:
                    reachable.add(ref)
                    frontier.append(ref)
        # References into an ancestor's subtree keep the whole top-level definition.
        for anc in target.iterancestors():
            if anc.get("id") and anc.get("id") not in reachable and _in_defs(anc):
                reachable.add(anc.get("id"))
                frontier.append(anc.get("id"))

    for defs in [el for el in root.iter() if _local(el.tag) == "defs"]:
        for child in list(defs):
            if not isinstance(child.tag, str) or _local(child.tag) in _KEEP_ANYWAY:
                continue
            ids = {el.get("id") for el in child.iter() if isinstance(el.tag, str) and el.get("id")}
            if ids & reachable:
                report.defs_kept += 1
            else:
                defs.remove(child)
                report.defs_removed += 1

    # Empty groups (nothing inside, nothing pointing at them), innermost first. Record only the
    # outermost ones: removing those removes their nested empties too (a batch of removals that
    # also named the nested ones would fail on the second).
    removed_with_parent: list[tuple[str | None, str | None]] = []
    for el in reversed(list(root.iter())):
        if (
            _local(el.tag) == "g"
            and len(el) == 0
            and not (el.text or "").strip()
            and el.get("id") not in reachable
            and el.getparent() is not None
        ):
            removed_with_parent.append((el.get("id"), el.getparent().get("id")))
            el.getparent().remove(el)
            report.empty_groups_removed += 1
    removed = {gid for gid, _ in removed_with_parent}
    report.empty_group_ids = [gid for gid, parent in removed_with_parent if gid and parent not in removed]

    report.hidden_elements = sum(
        1
        for el in root.iter()
        if isinstance(el.tag, str)
        and not _in_defs(el)
        and ("display:none" in (el.get("style") or "").replace(" ", "") or el.get("display") == "none")
    )
    return report


def clean_bytes(data: bytes) -> tuple[bytes, CleanupReport]:
    """Clean an SVG document given as bytes."""
    parser = etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True, remove_blank_text=False)
    tree = etree.ElementTree(etree.fromstring(data, parser))
    report = prune(tree.getroot())
    out = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=False)
    return out, report


def _render(svg: Path, png: Path, inkscape: str) -> bool:
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            inkscape,
            str(svg),
            "--export-type=png",
            "--export-width=600",
            "--export-background=white",
            "--export-background-opacity=1",
            f"--export-filename={png}",
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    return proc.returncode == 0 and png.exists()


def renders_identically(before: bytes, after: bytes, inkscape: str) -> float | None:
    """Fraction of pixels that differ between the two renders (None if rendering failed)."""
    from PIL import Image, ImageChops

    with tempfile.TemporaryDirectory() as tmp:
        a_svg, b_svg = Path(tmp, "a.svg"), Path(tmp, "b.svg")
        a_svg.write_bytes(before)
        b_svg.write_bytes(after)
        a_png, b_png = Path(tmp, "a.png"), Path(tmp, "b.png")
        if not (_render(a_svg, a_png, inkscape) and _render(b_svg, b_png, inkscape)):
            return None
        a, b = Image.open(a_png).convert("RGB"), Image.open(b_png).convert("RGB")
        if a.size != b.size:
            return 1.0
        diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 8 else 0)
        histogram = diff.histogram()
        return histogram[255] / (a.size[0] * a.size[1])


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out.extend(sorted(p.rglob("*.svg")) if p.is_dir() else [p])
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m inkscape_mcp.svg_cleanup", description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", help="SVG files or folders (searched recursively)")
    ap.add_argument("--check", action="store_true", help="report only; change nothing")
    ap.add_argument("--backup", metavar="DIR", help="copy each original into DIR before rewriting it")
    ap.add_argument("--verify", action="store_true", help="render before/after and refuse visual changes")
    ap.add_argument(
        "--inkscape", default=shutil.which("inkscape") or "/Applications/Inkscape.app/Contents/MacOS/inkscape"
    )
    args = ap.parse_args(argv)

    status = 0
    for path in _files(args.paths):
        before = path.read_bytes()
        after, report = clean_bytes(before)
        line = (
            f"{path.name}: {report.defs_removed} unused defs, {report.empty_groups_removed} empty groups"
            f" | {len(before) // 1024} -> {len(after) // 1024} KB"
            + (f" | {report.hidden_elements} hidden (kept)" if report.hidden_elements else "")
        )
        if args.check or not report.changed():
            print(line + ("" if report.changed() else " | clean"))
            continue
        if args.verify:
            diff = renders_identically(before, after, args.inkscape)
            if diff is None or diff > 0.0005:
                print(line + f" | NOT WRITTEN: render changed ({diff})")
                status = 1
                continue
            line += " | render identical"
        if args.backup:
            Path(args.backup).mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, Path(args.backup) / path.name)
        path.write_bytes(after)
        print(line)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
