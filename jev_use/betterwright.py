"""Run deterministic Playwright snippets against an existing CDP browser.

BetterWright is deliberately an optional execution path. GoLogin remains the
process that launches Orbita and owns the profile; this module only resolves the
existing debugger websocket and asks the BetterWright CLI to attach to it.
"""

from __future__ import annotations

import json
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
    "BetterWright is not installed. Install it once with:\n"
    "    bun install -g betterwright\n"
    "    betterwright setup"
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
    """Add BetterWright and Bun directories to PATH for Windows shims."""
    env = os.environ.copy()
    directories = [Path(command).parent]
    for name in ("bun", "bun.cmd", "bun.exe"):
        found = shutil.which(name)
        if found:
            directories.append(Path(found).parent)
    for variable in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        value = env.get(variable)
        if value:
            base = Path(value)
            directories.extend((base / "npm", base / ".bun" / "bin", base / "AppData" / "Roaming" / "npm"))
            # Bun installed through npm can keep the real executable below the
            # global prefix while the top-level `bun` entry is only a shim.
            node_modules = base / "npm" / "node_modules"
            if node_modules.is_dir():
                for candidate in node_modules.glob("bun*/**/bun.exe"):
                    directories.append(candidate.parent)
    bun_install = env.get("BUN_INSTALL")
    if bun_install:
        directories.append(Path(bun_install) / "bin")
    current = env.get("PATH", "")
    entries = [item for item in current.split(os.pathsep) if item]
    for directory in reversed(directories):
        text = str(directory)
        if text and text not in entries:
            entries.insert(0, text)
    env["PATH"] = os.pathsep.join(entries)
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


def run_script(
    port: int,
    code: str,
    *,
    timeout: float = 120.0,
    command: str | None = None,
) -> dict[str, Any]:
    """Run one prepared snippet against the browser on ``port``.

    ``--no-daemon`` makes the BetterWright client disconnect when this call ends;
    GoLogin still owns the browser and its caller remains responsible for calling
    ``Session.stop()`` so the profile is committed.
    """
    if not isinstance(code, str) or not code.strip():
        raise BetterWrightError("browser_script requires non-empty JavaScript code")
    if len(code) > 100_000:
        raise BetterWrightError("browser_script code is limited to 100000 characters")
    if timeout <= 0 or timeout > 900:
        raise BetterWrightError("browser_script timeout must be between 1 and 900 seconds")

    cli = command or executable()
    if not cli:
        raise BetterWrightError(INSTALL_HINT)

    # The URL is obtained from the local browser itself. We never manufacture a
    # debugger path, and the endpoint stays loopback-only for GoLogin profiles.
    cdp_url = ws_url_for(int(port))
    env = _environment_for(cli)
    env["BETTERWRIGHT_NO_DAEMON"] = "1"
    args = [
        "run",
        "--no-daemon",
        "--browser",
        cdp_url,
        "--no-ad-block",
        "-c",
        code,
    ]
    try:
        result = subprocess.run(
            [cli, *args],
            capture_output=True,
            text=True,
            timeout=float(timeout),
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise BetterWrightError(
            f"BetterWright timed out after {timeout:g}s; the action may have committed"
        ) from exc
    except OSError as exc:
        raise BetterWrightError(f"could not start BetterWright: {exc}") from exc

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        detail = stderr or stdout or f"exit {result.returncode}"
        raise BetterWrightError(f"BetterWright failed: {detail[-4000:]}")

    # The CLI emits one JSON envelope. Keep a useful fallback for versions that
    # prefix diagnostics, while never treating arbitrary text as success silently.
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        lines = [line for line in stdout.splitlines() if line.strip()]
        payload = None
        for line in reversed(lines):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if payload is None:
            raise BetterWrightError(
                f"BetterWright returned non-JSON output: {stdout[-4000:]}"
            )

    if not isinstance(payload, dict):
        raise BetterWrightError("BetterWright returned an invalid result envelope")
    if stderr:
        payload.setdefault("hostStderr", stderr[-4000:])
    return payload
