# Installation

Tested with Inkscape 1.4.4 on Ubuntu 24.04. Python 3.12+ required.

## Prerequisites

```bash
# Inkscape itself
sudo add-apt-repository ppa:inkscape.dev/stable
sudo apt update
sudo apt install inkscape

# Build deps for the Python extensions pulled in by inkex / pygobject
sudo apt install libcairo2-dev libgirepository-2.0-dev libgirepository1.0-dev pkg-config python3-dev

# Clipboard staging for inkscape_live's insert_svg. Either package works on either
# session type — the backend is auto-detected, and on Wayland xclip is used via
# XWayland when wl-clipboard isn't installed.
sudo apt install xclip
# sudo apt install wl-clipboard  # preferred on a native Wayland session
```

You also need **Python 3.12+** and **[uv](https://docs.astral.sh/uv/install/)** (recommended) or any modern pip.

## Install

Three equivalent paths — pick whichever matches your setup.

### A. uvx (recommended for end users)

No clone, no venv to manage; `uvx` fetches `inkscape-mcp` from PyPI on demand and caches it.

```bash
uvx inkscape_mcp --help
```

### B. pip into a venv

```bash
python3 -m venv ~/.venvs/inkscape_mcp
~/.venvs/inkscape_mcp/bin/pip install inkscape-mcp
~/.venvs/inkscape_mcp/bin/inkscape_mcp --help
```

### C. uv into a project

```bash
uv add inkscape-mcp
uv run inkscape_mcp --help
```

> The PyPI distribution is `inkscape-mcp` (hyphen); the Python import name is `inkscape_mcp` (underscore — Python identifiers can't contain hyphens). pip accepts either spelling — it normalizes per PEP 503.

## Register with Claude Code

```bash
claude mcp add inkscape_mcp -s user -- uvx inkscape_mcp
claude mcp list | grep inkscape_mcp   # should show "✓ Connected"
```

The resulting entry in your Claude config:

```json
{
  "mcpServers": {
    "inkscape_mcp": {
      "command": "uvx",
      "args": ["inkscape_mcp"]
    }
  }
}
```

If you installed via venv (option B), point `command` at the venv binary instead:

```json
{
  "command": "/home/you/.venvs/inkscape_mcp/bin/inkscape_mcp"
}
```

## macOS

```bash
# Inkscape: the official app bundle in /Applications (auto-detected, nothing on $PATH needed)
# dbus-daemon for the live bridge
brew install dbus
```

The live bridge starts a private `dbus-daemon` on `unix:path=/tmp/inkscape-bus` and launches its
own Inkscape under it on the first live operation (Homebrew's `dbus-launch` does not work on macOS,
so don't use it). Overrides: `INKSCAPE_MCP_DBUS_ADDRESS` (bus address), `INKSCAPE_MCP_DBUS_DAEMON`
(path to `dbus-daemon`, if it isn't in `/opt/homebrew/bin` or `/usr/local/bin`), `INKSCAPE_BIN`.

Window-scope actions (dialogs, tool switching, zoom, `document-save`) do not exist on macOS, so
they fail with an explanatory message and live edits are not auto-saved.

From a source checkout without `uv`, a plain venv works too:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -e .
claude mcp add inkscape_mcp -s user -- "$PWD/.venv/bin/inkscape_mcp"
```

The live test tier launches its own Inkscape on macOS, so it is opt-in:
`INKSCAPE_MCP_LIVE=1 .venv/bin/python -m pytest tests/live` (quit any Inkscape on the MCP bus first).

## Override the Inkscape binary

To pin a non-default Inkscape build:

```bash
export INKSCAPE_BIN=/opt/inkscape-nightly/bin/inkscape
```

Inspect from the agent with `inkscape_system(operation="diagnostics")` — it reports which value came from which source (env var, config file, default).

## From source (for development)

```bash
git clone https://github.com/aravindev/inkscape_mcp.git ~/git/inkscape_mcp
cd ~/git/inkscape_mcp
uv sync
uv run inkscape_mcp --help
```

Register the dev checkout with Claude Code:

```bash
claude mcp add inkscape_mcp -s user -- uv --directory ~/git/inkscape_mcp run inkscape_mcp
```

## Next

- [INKSCAPE.md](INKSCAPE.md) — Inkscape install details and CLI verification.
- [USAGE.md](USAGE.md) — calling tools.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — common build/runtime errors.
