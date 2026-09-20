#!/usr/bin/env bash
# Open a Chrome profile with a CDP endpoint so browser_use can drive it.
#
# This delegates to `python -m jev_use.profiles` so the copy-and-launch logic has
# exactly one implementation, shared with the browser_open MCP tool.
#
# ---------------------------------------------------------------------------
# Why this COPIES instead of using your profile in place
#
# Chrome 153 refuses to open a remote-debugging port on the default data
# directory:
#
#     $ google-chrome --remote-debugging-port=9222 --user-data-dir=~/.config/google-chrome
#     DevTools remote debugging requires a non-default data directory.
#
# So the obvious approach is impossible — Chrome exits immediately. The supported
# route is a copy in a non-default location. Your cookies travel with the copy, so
# the logins are there; but the copy is a SNAPSHOT, so a sign-in made to the
# original afterwards will not reach it. Use --refresh to re-copy.
#
# One profile is typically 0.5-1.5 GB, copied once and then reused.
# ---------------------------------------------------------------------------
#
#   scripts/enable-cdp.sh --list                 which profiles exist
#   scripts/enable-cdp.sh --open "Work"          copy + launch with CDP
#   scripts/enable-cdp.sh --open "Outlook 3" --refresh
#
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/.." && pwd)"

PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
    PY="$(command -v python3 || true)"
fi
if [[ -z "$PY" ]]; then
    echo "error: no python found (looked for $ROOT/.venv/bin/python and python3)" >&2
    exit 1
fi

cd "$ROOT"
exec "$PY" -m jev_use.profiles "$@"
