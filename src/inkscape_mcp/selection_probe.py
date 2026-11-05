"""Read Inkscape's selection without an extension run — so without an undo entry.

Extensions (mcp_inspect) can read the selection, but every run leaves an "[unchanged]" step
in the user's undo history; doing that on every message would bury their own steps. Inkscape's
`select-list` action instead prints the selected ids to Inkscape's stdout, which the server
captures when it launches Inkscape (mac_bus_manager). This module triggers the action over
D-Bus and reads the new lines back.

Only works for an Inkscape the server launched (and that is still running); otherwise
`selected_ids` returns None and callers fall back to the inspect extension.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

OUTPUT_LOG = Path(os.path.expanduser("~/.cache/inkscape_mcp/inkscape-output.log"))
# Which file each open document came from (Inkscape does not say over D-Bus): written by
# open_file, read by `save` and by the hooks to tell whether the figure belongs to a project.
DOC_PATHS = Path(os.path.expanduser("~/.cache/inkscape_mcp/doc-paths.json"))
_PID_FILE = OUTPUT_LOG.with_suffix(".pid")
# The D-Bus activate returns after the action ran, and its output is already in the log by
# then (measured); one short re-check covers a late flush. An empty selection prints nothing.
_RECHECK_S = 0.02


def record_launch(pid: int) -> None:
    _PID_FILE.write_text(str(pid))


def capturing() -> bool:
    """True when the running Inkscape was launched by us with stdout going to OUTPUT_LOG."""
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)
    except PermissionError:
        pass  # exists, but we may not signal it (e.g. from a sandbox)
    except (OSError, ValueError):
        return False
    return OUTPUT_LOG.exists()


def parse(text: str) -> list[str]:
    """Ids from select-list output: one `<id> cloned: … ref: …` line per selected object."""
    ids = []
    for line in text.splitlines():
        if " cloned: " in line:
            ids.append(line.split(" ", 1)[0])
    return ids


def selected_ids(bus: Any) -> list[str] | None:
    """The ids selected in Inkscape's active document, or None if the output is not captured."""
    if not capturing():
        return None
    offset = OUTPUT_LOG.stat().st_size
    bus.activate("select-list", None, scope="app")
    if OUTPUT_LOG.stat().st_size == offset:
        time.sleep(_RECHECK_S)
    with OUTPUT_LOG.open("rb") as fh:
        fh.seek(offset)
        return parse(fh.read().decode("utf-8", errors="replace"))


def known_paths() -> dict[str, str]:
    """{document name: absolute file path} for documents opened through the server."""
    import json

    try:
        data = json.loads(DOC_PATHS.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
