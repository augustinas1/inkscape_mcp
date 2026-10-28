"""Inkscape executable detection."""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Fallbacks if `inkscape` is not on $PATH.
_FALLBACK_PATHS: tuple[Path, ...] = (
    Path("/usr/bin/inkscape"),
    Path("/usr/local/bin/inkscape"),
    Path("/snap/bin/inkscape"),
    Path("/var/lib/flatpak/exports/bin/org.inkscape.Inkscape"),
    Path("~/.local/bin/inkscape").expanduser(),
)

# The official macOS build is an app bundle and puts nothing on $PATH.
MACOS_APP_BINARIES: tuple[Path, ...] = (
    Path("/Applications/Inkscape.app/Contents/MacOS/inkscape"),
    Path("~/Applications/Inkscape.app/Contents/MacOS/inkscape").expanduser(),
)


class InkscapeDetector:
    """Find the Inkscape executable on Linux and macOS."""

    def detect_inkscape_installation(self) -> str | None:
        """Return the path to a usable `inkscape` binary, or None."""
        # On macOS the bundle binary wins over $PATH: the live bridge launches it as the
        # GUI under a private bus, which a Homebrew/shell wrapper on $PATH may not do.
        if sys.platform == "darwin":
            for candidate in MACOS_APP_BINARIES:
                if candidate.is_file() and self._is_executable(candidate):
                    return str(candidate)

        on_path = shutil.which("inkscape")
        if on_path:
            return on_path

        for candidate in _FALLBACK_PATHS:
            if candidate.is_file() and self._is_executable(candidate):
                return str(candidate)

        logger.warning("Inkscape not found on $PATH or any known fallback location")
        return None

    @staticmethod
    def _is_executable(path: Path) -> bool:
        import os

        return os.access(path, os.X_OK)
