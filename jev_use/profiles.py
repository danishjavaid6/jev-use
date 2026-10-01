"""Chrome profile management: find the profiles that exist, and launch one with CDP.

Why this exists, and why it copies.

Chrome 153 **refuses to open a remote-debugging port on the default data directory**:

    $ google-chrome --remote-debugging-port=9222 --user-data-dir=~/.config/google-chrome
    DevTools remote debugging requires a non-default data directory.
    Specify this using --user-data-dir.

That is a security change (a CDP port on the default profile is a cookie-theft
primitive), and it means the obvious approach — point the port at the profile you
already use — is impossible. Chrome exits immediately if you try.

The supported route is a **copy in a non-default location**, which is verified to
work: copy `Local State` plus the one profile directory you want into our own state
directory, then launch with that as `--user-data-dir` and `--profile-directory`. The
cookies travel with the copy, so the logins are there.

The cost, stated plainly: a copy is a **snapshot**. Signing in to the original after
copying does not reach the copy — pass `refresh=True` to re-copy. One profile is
typically 0.5-1.5 GB, and the copy is done once, not per run.
"""

from __future__ import annotations

import json
import re
import sys
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import host

PROFILE_ROOT = host.chrome_user_data_root()
WORK_ROOT = host.work_root() / "profiles"
DEFAULT_PORT = 9222
LAUNCH_TIMEOUT = 25.0


def slug(directory: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", directory.lower()).strip("-")


def browser_binary() -> str:
    """Absolute path to Chrome/Chromium, or a message naming the fix.

    Resolved through `host`, because on Windows Chrome is essentially never on
    PATH — it is registered in the registry's App Paths and lives under
    %LOCALAPPDATA% or %PROGRAMFILES%.
    """
    found = host.find_browser_binary()
    if found:
        return found
    raise FileNotFoundError(
        f"no Chrome/Chromium binary found. To install one, {host.browser_install_hint()}."
    )


@dataclass
class LocalProfile:
    """A profile that exists on disk, whether or not it is running."""

    directory: str  # "Profile 1" — the --profile-directory value
    name: str  # "Work" — the human name from Local State
    prepared: bool  # a working copy exists
    running: bool = False
    port: int | None = None

    @property
    def workdir(self) -> Path:
        return WORK_ROOT / slug(self.directory)

    def describe(self) -> str:
        state = "cdp:" + str(self.port) if self.port else ("running" if self.running else "closed")
        return (
            f"{self.name!r} ({self.directory}) {state}"
            + ("  [copied]" if self.prepared else "")
        )


def _read_info_cache() -> dict[str, dict]:
    """Chrome's own profile registry: directory -> {name, ...}."""
    state = PROFILE_ROOT / "Local State"
    if not state.exists():
        return {}
    try:
        payload = json.loads(state.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}
    cache = payload.get("profile", {}).get("info_cache", {})
    return cache if isinstance(cache, dict) else {}


def _running_with_port() -> tuple[set[str], dict[str, int]]:
    """(directory names currently running, directory -> live CDP port)."""
    from .browser import running_profiles

    running: set[str] = set()
    ports: dict[str, int] = {}
    for profile in running_profiles():
        directory = Path(profile.profile_dir).name
        # A prepared copy carries the slug of its source profile.
        for candidate in _profile_directories():
            if slug(candidate) == directory:
                running.add(candidate)
                if profile.port and _cdp_alive(profile.port):
                    ports[candidate] = profile.port
    return running, ports


def _profile_directories() -> list[str]:
    return list(_read_info_cache()) or [p.name for p in _on_disk_profile_dirs()]


def _on_disk_profile_dirs() -> list[Path]:
    if not PROFILE_ROOT.exists():
        return []
    found = []
    for entry in PROFILE_ROOT.iterdir():
        if entry.is_dir() and (entry / "Preferences").exists():
            found.append(entry)
    return found


def _cdp_alive(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def local_profiles(*, inspect_running: bool = True) -> list[LocalProfile]:
    """Every profile Chrome knows about, with its display name and state."""
    running, ports = _running_with_port() if inspect_running else (set(), {})
    profiles: list[LocalProfile] = []
    for directory, meta in sorted(_read_info_cache().items()):
        name = (meta or {}).get("name") or directory
        prepared = (WORK_ROOT / slug(directory) / "Local State").exists()
        profiles.append(
            LocalProfile(
                directory=directory,
                name=name,
                prepared=prepared,
                running=directory in running,
                port=ports.get(directory),
            )
        )
    if not profiles:
        for path in _on_disk_profile_dirs():
            profiles.append(
                LocalProfile(directory=path.name, name=path.name, prepared=False)
            )
    return profiles


def profile_registry() -> list[LocalProfile]:
    """Disk metadata only. MCP already takes its own running-browser snapshot."""
    return local_profiles(inspect_running=False)


def find_profile(wanted: str) -> LocalProfile:
    """Resolve a profile by display name or directory, case-insensitively."""
    profiles = local_profiles()
    if not profiles:
        raise FileNotFoundError(
            f"no Chrome profiles found under {PROFILE_ROOT}. Is Chrome installed?"
        )
    exact = [p for p in profiles if wanted.lower() in (p.name.lower(), p.directory.lower())]
    if len(exact) == 1:
        return exact[0]
    partial = [p for p in profiles if wanted.lower() in p.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(exact) > 1 or len(partial) > 1:
        names = ", ".join(sorted({p.name for p in (exact or partial)}))
        raise ValueError(f"{wanted!r} matches several profiles: {names}")
    available = ", ".join(sorted(p.name for p in profiles)[:20])
    raise ValueError(f"no profile matches {wanted!r}. Available: {available}")


def prepare_profile(directory: str, *, refresh: bool = False) -> Path:
    """Copy Local State + one profile into our own data directory.

    Returns the directory to pass as `--user-data-dir`. Idempotent: an existing copy
    is reused unless `refresh` is set.
    """
    source = PROFILE_ROOT / directory
    if not source.exists():
        raise FileNotFoundError(f"profile directory does not exist: {source}")

    workdir = WORK_ROOT / slug(directory)
    marker = workdir / "Local State"
    if marker.exists() and not refresh:
        return workdir

    workdir.parent.mkdir(parents=True, exist_ok=True)
    staging = workdir.with_name(workdir.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    # Host-dispatched: `cp -a` on POSIX, `robocopy` on Windows. A profile is often
    # >1 GB and Python's copytree is far slower than either.
    for item in ("Local State", "First Run", "Last Version"):
        src = PROFILE_ROOT / item
        if src.exists():
            try:
                host.copy_file(src, staging / item)
            except OSError:
                pass  # an unreadable optional file must not fail the whole copy
    host.copy_tree(source, staging / directory)

    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    staging.rename(workdir)
    return workdir


def start_profile(
    directory: str,
    *,
    port: int = DEFAULT_PORT,
    refresh: bool = False,
    url: str = "about:blank",
    timeout: float = LAUNCH_TIMEOUT,
    background: bool = False,
) -> tuple[LocalProfile, str]:
    """Launch one profile from a prepared copy, with CDP, and wait for the endpoint."""
    if _cdp_alive(port):
        raise RuntimeError(
            f"port {port} already has a CDP endpoint. Close that browser, or pass a "
            "different port."
        )

    workdir = prepare_profile(directory, refresh=refresh)
    binary = browser_binary()
    process = subprocess.Popen(
        [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={workdir}",
            f"--profile-directory={directory}",
            "--no-first-run",
            "--no-default-browser-check",
            *(["--headless=new"] if background else []),
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **host.detach_kwargs(),  # survive this process; do not die with our shell
    )

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if _cdp_alive(port):
            profile = LocalProfile(directory=directory, name=directory, prepared=True, port=port)
            for known in local_profiles():
                if known.directory == directory:
                    profile = known
                    profile.port = port
                    break
            return profile, str(workdir)
        if process.poll() is not None:
            raise RuntimeError(
                f"browser exited before CDP opened (exit {process.returncode}): {binary}. "
                "For Chrome set JEV_USE_BROWSER_BINARY to the real Chrome executable. "
                "For GoLogin use vendor=gologin with an API token."
            )
        time.sleep(0.4)

    raise TimeoutError(
        f"launched {directory} from {workdir} but no CDP endpoint answered on port "
        f"{port} within {timeout:.0f}s. The profile copy may be incomplete, or the "
        "port may be in use."
    )


# -- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`python -m jev_use.profiles --list` / `--open NAME`.

    The shell script delegates here so there is one implementation of the copy and
    launch, not two that can drift.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="jev_use.profiles")
    parser.add_argument("--list", action="store_true", help="list profiles and exit")
    parser.add_argument("--open", metavar="NAME", help="open a profile with CDP")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--url", default="about:blank")
    parser.add_argument("--refresh", action="store_true", help="re-copy the profile")
    parser.add_argument("--filter", help="only names containing this")
    args = parser.parse_args(argv)

    if args.list or not args.open:
        profiles = local_profiles()
        if args.filter:
            needle = args.filter.lower()
            profiles = [
                p for p in profiles if needle in p.name.lower() or needle in p.directory.lower()
            ]
        print(f"{len(profiles)} profile(s) under {PROFILE_ROOT}")
        for profile in profiles:
            print("  " + profile.describe())
        if not args.open:
            print('\nOpen one with: --open "<name>"')
        return 0

    try:
        match = find_profile(args.open)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if match.port and _cdp_alive(match.port):
        print(f"OK: {match.name!r} is already open and drivable on port {match.port}.")
        return 0

    print(f"copying {match.directory!r} into {match.workdir} (one time, then reused)")
    try:
        profile, workdir = start_profile(
            match.directory, port=args.port, refresh=args.refresh, url=args.url
        )
    except (TimeoutError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"OK: {profile.name!r} is open with CDP on port {args.port}.")
    print(f"    copy: {workdir}")
    print("    logins come from the copy, taken at copy time (--refresh to update).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
