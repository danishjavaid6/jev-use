"""Everything that differs between Linux, macOS and Windows, in one place.

The browser engine was written on Linux and had five platform assumptions baked
into it: where Chrome keeps its profiles, which binary name to look for, how to
copy a gigabyte of profile data, how to detach a launched browser, and how to
read the process table. Each of those is a one-line difference and each one, left
inline, is a second place that can drift.

So this module owns them. Everything else in `jev_use` is platform-agnostic and
imports from here, which also means the platform seams are visible in one file
instead of scattered through the engine.

Nothing here raises on an unsupported platform: it degrades to the POSIX default
and lets the caller report the real problem (no Chrome, no driver) rather than a
confusing import-time failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = not IS_WINDOWS and not IS_MACOS

# Seconds. The Windows process listing shells out to PowerShell, which is slower
# to start than `ps` by an order of magnitude, so the two are not the same budget.
PROCESS_TIMEOUT = 20.0 if IS_WINDOWS else 10.0


# -- where Chrome keeps things ----------------------------------------------


def chrome_user_data_root() -> Path:
    """The directory holding `Local State` and the `Default`/`Profile N` folders.

    Chrome uses the same layout on every platform; only the parent differs:

        Linux    ~/.config/google-chrome
        macOS    ~/Library/Application Support/Google/Chrome
        Windows  %LOCALAPPDATA%\\Google\\Chrome\\User Data
    """
    home = Path.home()
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(local) / "Google" / "Chrome" / "User Data"
    if IS_MACOS:
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    return home / ".config" / "google-chrome"


def work_root() -> Path:
    """Where we keep our private profile copies and the runtime venv marker.

    Deliberately NOT the profile root: the whole point is a non-default data
    directory, because Chrome refuses a CDP port on the default one.
    """
    home = Path.home()
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(local) / "jev-use"
    if IS_MACOS:
        return home / "Library" / "Application Support" / "jev-use"
    return home / ".local" / "state" / "jev-use"


# -- finding the browser ----------------------------------------------------

#: Executable *stems*. Chrome ships as `chrome` on Linux (the `google-chrome`
#: wrapper execs it) and `chrome.exe` on Windows; comparing stems makes both the
#: same string. Never match on a substring — see `_is_browser_process`.
BROWSER_BINARY_NAMES = frozenset(
    {
        "chrome",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "brave",
        "brave-browser",
        "msedge",
    }
)

_WINDOWS_CHROME_PATHS = (
    r"Google\Chrome\Application\chrome.exe",
    r"Google\Chrome Beta\Application\chrome.exe",
    r"Google\Chrome SxS\Application\chrome.exe",
)
_WINDOWS_CHROME_REGISTRY = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
)
_MACOS_CHROME_PATHS = (
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "Chromium.app/Contents/MacOS/Chromium",
)


def _windows_chrome_from_registry() -> str | None:
    """Chrome's own App Paths entry — the one place it always registers."""
    try:
        import winreg  # noqa: PLC0415 - Windows-only import, must not run elsewhere
    except ImportError:  # pragma: no cover - only on non-Windows
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_path in _WINDOWS_CHROME_REGISTRY:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, None)
            except OSError:
                continue
            if value and Path(value).exists():
                return str(value)
    return None


def find_browser_binary() -> str | None:
    """An absolute path to Chrome/Chromium, or None if it is not installed.

    PATH first (covers Linux and any macOS Homebrew install), then the places a
    per-user install hides: on Windows Chrome is almost never on PATH, so the
    registry and `%LOCALAPPDATA%` are the paths that actually resolve.
    """
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    home = Path.home()
    candidates: list[Path] = []
    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        for base in (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates += [Path(base) / rel for rel in _WINDOWS_CHROME_PATHS]
        candidates.append(local / "Google" / "Chrome" / "Application" / "chrome.exe")
        from_registry = _windows_chrome_from_registry()
        if from_registry:
            return from_registry
    elif IS_MACOS:
        for rel in _MACOS_CHROME_PATHS:
            candidates += [Path("/Applications") / rel, home / "Applications" / rel]

    for path in candidates:
        if path.exists():
            return str(path)
    return None


def browser_install_hint() -> str:
    if IS_WINDOWS:
        return "install Google Chrome (https://www.google.com/chrome/)"
    if IS_MACOS:
        return "install Google Chrome into /Applications"
    return "install google-chrome, chromium or brave with your package manager"


# -- copying a profile ------------------------------------------------------


def copy_file(src: Path, dst: Path) -> None:
    """Copy one file with its metadata. Used for Local State / First Run."""
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path) -> None:
    """Copy a directory, preserving attributes.

    A profile is 0.5-1.5 GB, so this is deliberately not `shutil.copytree`: on
    Linux `cp -a` and on Windows `robocopy` are several times faster, and this
    runs on every `--refresh`.

    Windows note: robocopy signals success with exit codes 0-7 and failure with
    8+, which is the opposite of every other tool here. Treating a nonzero exit
    as failure would report a perfectly good copy as broken.
    """
    if IS_WINDOWS:
        result = subprocess.run(
            [
                "robocopy", str(src), str(dst),
                "/E",              # include empty directories, recurse
                "/R:0", "/W:0",    # no retries: a locked file is skipped, not stalled
                "/MT:16",          # parallel threads
                "/NFL", "/NDL", "/NJH", "/NJS", "/NP",  # quiet: no per-file spam
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode >= 8:
            raise RuntimeError(
                f"robocopy failed ({result.returncode}) copying {src} -> {dst}: "
                f"{(result.stdout or result.stderr or '').strip()[:400]}"
            )
        return

    subprocess.run(["cp", "-a", str(src), str(dst)], check=True, capture_output=True)


def detach_kwargs() -> dict:
    """Popen kwargs that leave the launched browser running after we exit.

    POSIX uses a new session; Windows has no sessions, so it uses the process
    creation flags that mean the same thing (no console, not our child group).
    """
    if IS_WINDOWS:
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
        }
    return {"start_new_session": True}


# -- reading the process table ----------------------------------------------

#: argv[0] may be quoted on Windows (`"C:\Program Files\...\chrome.exe" --flag`).
_ARGV0 = re.compile(r'^\s*(?:"([^"]+)"|(\S+))')


def argv0(args: str) -> str:
    """The executable from a raw command line, quotes stripped."""
    match = _ARGV0.match(args or "")
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def executable_stem(path: str) -> str:
    """The bare program name from a path, lowercase, extension stripped.

    Splits on BOTH separators rather than using `Path(...).stem`. On POSIX a
    backslash is an ordinary filename character, so `Path.stem` over a Windows
    path returns the whole string and `chrome.exe` never matches `chrome` — which
    silently breaks discovery the moment this code runs somewhere other than the
    platform the path came from.
    """
    name = re.split(r"[\\/]", path or "")[-1]
    return Path(name).stem.lower()


def _posix_process_table(timeout: float) -> list[tuple[int, str]]:
    out = subprocess.run(
        ["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=timeout
    ).stdout
    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1]))
    return rows


def _windows_process_table(timeout: float) -> list[tuple[int, str]]:
    """PowerShell CIM, falling back to wmic on machines that still have it.

    `wmic` is deprecated and absent from recent Windows, which is why it is the
    fallback rather than the primary.
    """
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,CommandLine | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    attempts = [
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        ["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
    ]
    for command in attempts:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.SubprocessError):
            continue
        rows = _parse_windows_table(command[0], result.stdout or "")
        if rows:
            return rows
    return []


def _parse_windows_table(tool: str, out: str) -> list[tuple[int, str]]:
    text = (out or "").strip()
    if not text:
        return []
    if tool.startswith("wmic"):
        # wmic emits `Node,CommandLine,ProcessId`. Deliberately NOT csv.reader:
        # the command line routinely starts with a quote (to protect a path with
        # spaces) and can contain commas, so the CSV reader strips the opening
        # quote and shears the path in half. Splitting on the first and last
        # comma instead leaves the command line byte-for-byte intact, which is
        # what argv0 and the option parser already know how to read.
        rows: list[tuple[int, str]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            head, _, pid_text = line.rpartition(",")
            if not pid_text.strip().isdigit():
                continue
            _, _, command_line = head.partition(",")
            if command_line.strip():
                rows.append((int(pid_text.strip()), command_line.strip()))
        return rows

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    # ConvertTo-Json emits a bare object for a single match, an array otherwise.
    if isinstance(payload, dict):
        payload = [payload]
    rows = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("ProcessId")
        command_line = entry.get("CommandLine")
        if isinstance(pid, int) and command_line:
            rows.append((pid, str(command_line)))
    return rows


def process_table(timeout: float | None = None) -> list[tuple[int, str]]:
    """Every running process as (pid, full command line).

    Raises nothing: discovery is best-effort by design, and the callers all treat
    an empty table as "nothing found" rather than as an error.
    """
    budget = PROCESS_TIMEOUT if timeout is None else timeout
    try:
        if IS_WINDOWS:
            return _windows_process_table(budget)
        return _posix_process_table(budget)
    except (subprocess.SubprocessError, OSError, ValueError):
        return []


# -- the cua-driver binary --------------------------------------------------


def cua_driver_candidates() -> list[Path]:
    """Where the installers actually put the binary, per platform.

    Linux/macOS: `install.sh` symlinks into ~/.local/bin.
    Windows: `install.ps1` uses a versioned directory behind a *junction* and
    appends to the User PATH. That PATH change is not visible to an already
    running process, so a launcher cannot rely on `which` and must know these
    paths.
    """
    home = Path.home()
    root = home / ".cua-driver" / "packages" / "current"
    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        return [
            local / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe",
            local / "Programs" / "trycua" / "cua-driver-rs" / "bin" / "cua-driver.exe",
            root / "cua-driver.exe",
        ]
    return [home / ".local" / "bin" / "cua-driver", root / "cua-driver"]


def find_cua_driver() -> str | None:
    """Resolve the driver: explicit override, then PATH, then known install dirs."""
    override = os.environ.get("CUA_DRIVER_COMMAND", "").strip()
    if override:
        return override
    found = shutil.which("cua-driver")
    if found:
        return found
    for path in cua_driver_candidates():
        if path.exists():
            return str(path)
    return None


def driver_install_hint() -> str:
    """The one-line install command for this platform.

    Windows is the reason this function exists: `install.sh` contains no Windows
    handling at all (it has a separate 81 KB `install.ps1`), so handing a Windows
    user the curl-pipe-bash command is a dead end.
    """
    if IS_WINDOWS:
        return (
            'powershell -ExecutionPolicy Bypass -c "irm '
            'https://cua.ai/driver/install.ps1 | iex"'
        )
    return '/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"'


def platform_tag() -> str:
    """A short platform label, for diagnostics and the venv marker."""
    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos"
    return "linux"


def launcher_hint() -> str:
    """The one-time "give Chrome a debug port" command, per platform.

    Quoted in every "no CDP endpoint" error, because a caller who cannot find
    this command is stuck: the whole engine depends on it.
    """
    return "scripts/enable-cdp.ps1" if IS_WINDOWS else "scripts/enable-cdp.sh"
