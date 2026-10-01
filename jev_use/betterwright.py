"""Run deterministic Playwright snippets against an existing CDP browser.

BetterWright is deliberately an optional execution path. GoLogin remains the
process that launches Orbita and owns the profile; this module only resolves the
existing debugger websocket and attaches the persistent BetterWright SDK to it.
"""

from __future__ import annotations

import json
import atexit
import math
import queue
import signal
import threading
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class BetterWrightError(RuntimeError):
    """A BetterWright attachment or snippet failure."""


DISMISS_OVERLAYS_JS = """\
await overlays.dismiss();
// Close only the Facebook 'Sign in as' chooser requested by the user.
// Never click an unrelated Close button or dismiss the password form.
if (/^https:\\/\\/(?:[^/]+\\.)?facebook\\.com(?:\\/|$)/i.test(page.url())) {
    const chooser = page.locator('[role="dialog"], dialog').filter({
        has: page.getByText('Sign in as', { exact: true })
    });
    if (await chooser.count() === 1 && await chooser.isVisible()) {
        await chooser.getByRole('button', { name: 'Close', exact: true }).click();
        await chooser.waitFor({ state: 'hidden', timeout: 5000 });
    }
}
"""


INSTALL_HINT = (
    "BetterWright is not installed. With Node.js 22+, install it once with:\n"
    "    npm install -g betterwright"
)


def executable() -> str | None:
    """Return the global BetterWright executable, including Windows shims.

    MCP hosts on Windows often start with a reduced PATH. Bun's global npm
    directory is nevertheless stable, so check it explicitly after PATH lookup.
    """
    for name in ("betterwright", "betterwright.cmd", "betterwright.exe"):
        found = shutil.which(name)
        if found:
            return found
    roots: list[Path] = []
    for variable in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        value = os.environ.get(variable)
        if value:
            base = Path(value)
            roots.extend((base / "npm", base / ".bun" / "bin", base / "AppData" / "Roaming" / "npm"))
    bun_install = os.environ.get("BUN_INSTALL")
    if bun_install:
        roots.append(Path(bun_install) / "bin")
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        for name in ("betterwright.cmd", "betterwright.exe", "betterwright"):
            candidate = root / name
            if candidate.is_file():
                return str(candidate)
    return None


def _environment_for(command: str) -> dict[str, str]:
    """Preserve the host environment; Node's absolute path comes from the launcher."""
    env = os.environ.copy()
    env["PATH"] = str(Path(command).parent) + os.pathsep + env.get("PATH", "")
    return env


def ws_url_for(port: int, timeout: float = 5.0) -> str:
    """Resolve a local CDP port to the browser websocket endpoint."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/json/version", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BetterWrightError(
            f"port {port} is not answering /json/version ({exc})"
        ) from exc
    url = (payload or {}).get("webSocketDebuggerUrl")
    if not isinstance(url, str) or not url:
        raise BetterWrightError(
            f"port {port} answered /json/version without webSocketDebuggerUrl"
        )
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise BetterWrightError(
            "refusing a non-loopback BetterWright endpoint; GoLogin attachment "
            "must stay local"
        )
    return url


class _Bridge:
    """One SDK worker per browser, with a hard host-side deadline."""

    def __init__(self, command: str):
        node = os.environ.get("JEV_USE_NODE") or shutil.which("node")
        if not node:
            raise BetterWrightError("Node.js 22+ is required for browser_script")
        script = Path(__file__).resolve().parent.parent / "bin" / "betterwright-bridge.js"
        self.process = subprocess.Popen(
            [node, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            env=_environment_for(command),
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        self.messages: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.sequence = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put({"error": "BetterWright bridge exited. Check Node.js 22+ and the BetterWright installation."})

    def run(self, request: dict, timeout: float) -> dict:
        with self.lock:
            self.sequence += 1
            request["id"] = self.sequence
            try:
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()
                response = self.messages.get(timeout=timeout)
            except (queue.Empty, OSError) as exc:
                self.close()
                raise BetterWrightError(
                    f"BetterWright exceeded its {timeout:g}s deadline or disconnected; "
                    "the action may have committed. Inspect state before retrying."
                ) from exc
            if response.get("error"):
                raise BetterWrightError(response["error"])
            if response.get("id") != self.sequence or not isinstance(response.get("result"), dict):
                self.close()
                raise BetterWrightError("invalid BetterWright bridge response")
            return response["result"]

    def close(self):
        # Only our Node bridge and SDK worker; never the externally launched browser.
        if os.name == "nt":
            if self.process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               capture_output=True, timeout=5, check=False)
        else:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


_BRIDGES: dict[int, tuple[str, _Bridge]] = {}


def close_sessions() -> None:
    for _, bridge in list(_BRIDGES.values()):
        bridge.close()
    _BRIDGES.clear()


atexit.register(close_sessions)


def run_script(port: int, code: str, *, timeout: float = 120.0,
               command: str | None = None, page_url: str | None = None,
               dismiss_overlays: bool = True) -> dict[str, Any]:
    """Reuse BetterWright's SDK connection and in-memory state between calls."""
    if not isinstance(code, str) or not code.strip():
        raise BetterWrightError("browser_script requires non-empty JavaScript code")
    if len(code) > 100_000:
        raise BetterWrightError("browser_script code is limited to 100000 characters")
    if not math.isfinite(timeout) or not 1 <= timeout <= 900:
        raise BetterWrightError("browser_script timeout must be between 1 and 900 seconds")
    cli = command or executable()
    if not cli:
        raise BetterWrightError(INSTALL_HINT)
    ws = ws_url_for(int(port))
    existing = _BRIDGES.get(port)
    if existing and (existing[0] != ws or existing[1].process.poll() is not None):
        existing[1].close()
        del _BRIDGES[port]
    target_id = None
    tab_count = None
    if port not in _BRIDGES or page_url:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            tabs = [t for t in json.load(response) if t.get("type") == "page"]
        tab_count = len(tabs)
        candidates = [t for t in tabs if t.get("url") == page_url] if page_url else tabs
        if len(candidates) != 1:
            raise BetterWrightError("Multiple or missing tabs: supply page_url with the exact existing workflow tab URL")
        target_id = candidates[0]["id"]
        page_url = candidates[0]["url"]
    if port not in _BRIDGES:
        _BRIDGES[port] = (ws, _Bridge(cli))
    try:
        return _BRIDGES[port][1].run(
            {"ws": ws, "cli": cli, "code": code, "timeout": timeout, "page_url": page_url, "target_id": target_id, "tab_count": tab_count, "dismiss_overlays": dismiss_overlays}, timeout)
    except BetterWrightError:
        # Do not reuse a timed-out worker or replay possibly committed actions.
        failed = _BRIDGES.pop(port, None)
        if failed:
            failed[1].close()
        raise
