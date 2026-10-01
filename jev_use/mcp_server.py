"""jev-use — browser and Android use with Jev making the decisions, as an MCP server.

Any harness (Command Code, Claude Code, Cursor, Codex) can call this. The harness
brings the reasoning; this server brings the device and Jev brings the decisions.

Deliberately small — a large tool schema is paid for in the model's context on
every turn:

    browser_profiles   which Chromium-family browsers are open, and whether CDP works
    browser_open       launch a profile from a private copy, with a CDP endpoint
    browser_use        drive the page toward a goal; Jev picks each action
    browser_extract    ask typed questions about the page, get typed answers
    browser_read       return the page's text

    android_devices    which phones are attached over adb
    android_use        drive the phone toward a goal; Jev picks each action
    android_read       return the screen's text
    android_location   the location Facebook attributes to the signed-in account

Android is four tools rather than five on purpose. It needs no CDP equivalent
(adb is the channel, and it is always there), and it needs no `extract`: the view
hierarchy yields a kilobyte or two of text, where a web dashboard yields twenty,
so handing the harness the text costs almost nothing. `android_location` is the
one app-shaped tool — it exists because Facebook's own page is the only authority
on what the app believes about where the account is.

The desktop / accessibility surface was removed. On GNOME/Wayland it cannot
attach to an existing browser profile, and that dead end is what made agents
burn turns. Android does not go through it either — see `android.py` for why
scrcpy's window is the wrong thing to point a window-based driver at.

Wire it up:

    cmd mcp add --scope user jev-use -- /path/to/.venv/bin/python -m jev_use.mcp_server

Speaks MCP over stdio. Nothing but JSON-RPC goes to stdout.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from .browser import DEFAULT_PROFILE, attach, read as browser_read, run as browser_run
from .browser import running_profiles, cdp_alive
from .browser import navigate_and_settle, read_many as browser_read_many
from .choosers import JevChooser
from .cache import PlanCache
from .driver import Driver, DriverError
from .extract import ExtractError, ask as extract_ask
from pathlib import Path
from .profiles import find_profile, local_profiles, start_profile
from .text_model import TextModel

from . import android as android_engine
from . import gologin
from . import harness

SERVER_NAME = "jev-use"
SERVER_VERSION = "0.3.0"
PROTOCOL_VERSION = "2025-06-18"
CACHE_PATH = Path(__file__).resolve().parent.parent / ".jev-browser-cache.json"

# Single source of truth: the tool schema and the handler must agree. They drifted
# once (schema said one thing, handler another) which silently changed behaviour.
DEFAULT_MAX_STEPS = 8
DEFAULT_MIN_CONFIDENCE = 0.4
DEFAULT_SETTLE = 3.0
#: Tabs browser_read_many reads at once. Deliberately small: each worker is its own
#: driver process and its own browser tab, Chrome throttles background tabs, and the
#: returns flatten fast past a handful.
DEFAULT_READ_CONCURRENCY = 3
# Android settles for less time, for a measured reason — see android.DEFAULT_SETTLE.
ANDROID_DEFAULT_SETTLE = android_engine.DEFAULT_SETTLE
#: The location page is a webview with no ready signal, so it is polled. Single
#: sourced here so the tool schema and the handler cannot drift apart.
ANDROID_LOCATION_TIMEOUT = android_engine.LOCATION_LOAD_TIMEOUT


# -- helpers ----------------------------------------------------------------


def log(message: str) -> None:
    """Diagnostics go to stderr; stdout is reserved for JSON-RPC."""
    print(message, file=sys.stderr, flush=True)


def load_env() -> None:
    """.env next to the package, so the API key does not have to be duplicated."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def attach_helpfully(
    driver: Driver, profile: str | None = None, port: int | None = None
) -> Any:
    """Attach to the requested profile (default: the real one), or explain the fix.

    An explicit `port` wins over profile matching. It is the one route that reaches
    a browser discovery cannot see, and the only route that reaches a GoLogin
    profile: Orbita picks its debug port at launch, so there is nothing to guess.
    """
    return attach(driver, port=port, profile=profile)


def _port(args: dict[str, Any]) -> int | None:
    """The optional `port` argument, as an int or None. Never raises on junk."""
    value = args.get("port")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# -- driver sessions --------------------------------------------------------
#
# Each browser_* call used to spawn its own `cua-driver mcp`, then re-run
# discovery (`ps`, up to two `/json/version` probes) and a `list_windows` bind —
# and tear all of it down again. A harness that follows browser_use with
# browser_read and browser_extract pays that three times for one page.
#
# This keeps one transport + Target alive and reuses it while it is healthy, and
# resets it only when the child process is gone, the requested profile changed, or
# a call fails. Two transports can sit here: a `Driver` (the `page` tool, whose
# process is ours alone, so the unrestricted permission mode it runs under stays
# contained) or a `Harness` (browser-harness's CDP client, one websocket, no
# process at all). `browser._js` routes to whichever it was handed.

_SESSION_LOCK = threading.Lock()
_SESSION: dict[str, Any] = {"driver": None, "target": None, "profile": None}

#: The GoLogin profile this process started, if any. Held so `browser_close` can
#: stop it: the SDK commits cookies and login state back to GoLogin on stop, so a
#: profile that is never stopped loses everything the session did.
_GOLOGIN: dict[str, Any] = {"session": None}


def _close_session_locked() -> None:
    """Close the cached driver. Caller must hold `_SESSION_LOCK`."""
    driver = _SESSION["driver"]
    _SESSION.update(driver=None, target=None, profile=None)
    if driver is not None:
        try:
            driver.close()
        except Exception:
            pass


def reset_browser_session() -> None:
    """Drop the cached session so the next call starts a fresh driver."""
    with _SESSION_LOCK:
        _close_session_locked()


def _harness_wanted(port: int | None) -> bool:
    """Whether this endpoint should be driven over the browser-harness transport.

    `JEV_USE_TRANSPORT` forces the choice (`driver` / `harness`); the default is
    `auto`, which uses a persistent CDP connection for any explicit browser port
    when the client is installed. Profile requests resolve their port once.
    """
    mode = (os.environ.get("JEV_USE_TRANSPORT") or "auto").strip().lower()
    if mode == "driver":
        return False
    if mode == "harness":
        if not harness.available():
            raise harness.HarnessError(harness.INSTALL_HINT)
        return port is not None
    if port is None or not harness.available():
        return False
    return True


def _start_session(profile: str | None, port: int | None) -> tuple[Any, Any]:
    """A started transport and its Target — the harness where it applies, else a driver."""
    if port is None and harness.available() and (os.environ.get("JEV_USE_TRANSPORT") or "auto") != "driver":
        from .browser import _pick_profile
        chosen, _ = _pick_profile(running_profiles(), profile)
        if chosen is not None:
            port = chosen.port
    if _harness_wanted(port):
        session = harness.Harness(port=port)  # type: ignore[arg-type]
        session.start()
        try:
            return session, attach(session, port=port, profile=profile)
        except Exception:
            session.close()
            raise
    driver = Driver()
    driver.start()
    try:
        return driver, attach_helpfully(driver, profile, port)
    except Exception:
        driver.close()
        raise


def _browser_session(
    profile: str | None, port: int | None = None
) -> tuple[Any, Any]:
    """A live `(transport, target)` for `profile`/`port`, reusing the cached one when valid."""
    key = f"{profile or ''}|{port if port is not None else ''}"
    with _SESSION_LOCK:
        driver = _SESSION["driver"]
        if (
            driver is not None
            and getattr(driver, "alive", False)
            and _SESSION["profile"] == key
            and _SESSION["target"] is not None
        ):
            return driver, _SESSION["target"]

        _close_session_locked()
        session, target = _start_session(profile, port)
        _SESSION.update(driver=session, target=target, profile=key)
        return session, target


def with_browser_session(
    profile: str | None, body: Any, port: int | None = None
) -> tuple[Any, Any]:
    """Run `body(driver, target)` on a reused session.

    On a DriverError the session is dropped so the next call is fresh, but the
    error is re-raised rather than retried: re-running the body could repeat a
    partial action.
    """
    driver, target = _browser_session(profile, port)
    try:
        return body(driver, target), target
    except DriverError:
        reset_browser_session()
        raise


atexit.register(reset_browser_session)


def stop_gologin_session() -> None:
    """Stop the GoLogin profile we started, if any, so its work is committed.

    Registered at exit as well as exposed as `browser_close`. Leaving a GoLogin
    profile running is not harmless the way leaving Chrome open is: the SDK only
    writes cookies and login state back to GoLogin when it stops, so a server that
    exits without stopping it drops the session on the floor.
    """
    session = _GOLOGIN.get("session")
    if session is None:
        return
    _GOLOGIN["session"] = None
    try:
        session.stop()
    except Exception:
        pass


atexit.register(stop_gologin_session)


def render(result: Any) -> str:
    lines = []
    if getattr(result, "subgoals", None):
        lines.append("plan: " + " -> ".join(result.subgoals))
    for step in result.steps:
        lines.append(("> " if step.executed else "  ") + step.note)
    per = result.seconds / result.actions if result.actions else 0.0
    lines.append(
        f"url={result.url}\noutcome={result.outcome} actions={result.actions} "
        f"snapshots={result.snapshots} seconds={result.seconds:.2f} "
        f"per_action_ms={per * 1000:.0f}"
    )
    return "\n".join(lines)


# -- prompts ----------------------------------------------------------------

PROMPT_NAME = "browser-use"

PROMPT_TEMPLATE = """Task: {task}
Call browser_profiles once; reuse the matching port or browser_open the requested profile.
For a known URL use browser_read(port=N, url=...) directly. For several URLs use
browser_read_many. Use browser_use only for interactions, then read to verify.
Never substitute a different browser for a requested account. GoLogin needs
vendor=\"gologin\" and browser_close afterward.
If tools are missing, search once then use `jev-use call <tool>` in the shell.
Do not build clients or debug the installation during the task. Retry once at
most; report failures and stop. Quote only what you read.
"""

MOBILE_PROMPT_NAME = "mobile-use"

MOBILE_PROMPT_TEMPLATE = """Task: {task}
Call android_devices once. Use android_read to read and android_use(act=true)
to perform the requested action. Pass serial when several devices are connected.
Read again to verify. If tools are missing use `jev-use call <tool>` in the shell.
No helper clients or repair loops. Retry a transient failure once; otherwise
report it and stop. The phone must be unlocked with USB debugging accepted.
"""

PROMPT_TEMPLATES: dict[str, str] = {
    PROMPT_NAME: PROMPT_TEMPLATE,
    MOBILE_PROMPT_NAME: MOBILE_PROMPT_TEMPLATE,
}

PROMPTS: list[dict[str, Any]] = [
    {
        "name": PROMPT_NAME,
        "description": (
            "Do a task in the user's own browser (usage pages, dashboards, repos, "
            "deployments) and report what the pages say."
        ),
        "arguments": [
            {
                "name": "task",
                "description": "The task in plain English.",
                "required": True,
            }
        ],
    },
    {
        "name": MOBILE_PROMPT_NAME,
        "description": (
            "Do a task on the user's own Android phone (apps, settings, messages) and "
            "report what the screens say."
        ),
        "arguments": [
            {
                "name": "task",
                "description": "The task in plain English.",
                "required": True,
            }
        ],
    },
]


def prompt_messages(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    template = PROMPT_TEMPLATES.get(name)
    if template is None:
        return None
    task = (arguments or {}).get("task", "").strip() or "(no task given)"
    kind = "Phone" if name == MOBILE_PROMPT_NAME else "Browser"
    return {
        "description": f"{kind} task: {task[:80]}",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": template.format(task=task)}}
        ],
    }


# -- tools ------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "browser_profiles",
        "description": "List running browsers and saved profiles. Call FIRST, once; reuse a matching live CDP port.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "only_running": {"type": "boolean", "default": False},
                "available": {"type": "boolean", "default": False},
                "filter": {
                    "type": "string",
                    "description": "Only profiles whose name contains this substring.",
                },
            },
        },
    },
    {
        "name": "browser_open",
        "description": "Open a Chrome profile SNAPSHOT (refresh to update): CDP requires a non-default data directory. For native GoLogin use vendor=gologin. Returns the port to use.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "description": "Display name (or id) of the profile to open.",
                },
                "vendor": {
                    "type": "string",
                    "enum": ["auto", "chrome", "gologin"],
                    "default": "auto",
                    "description": (
                        "'chrome' copies and launches a Chrome profile; 'gologin' starts "
                        "a GoLogin profile in its own browser; 'auto' tries Chrome first."
                    ),
                },
                "port": {
                    "type": "integer",
                    "description": (
                        "Explicit CDP port to launch on. Default 9222 for Chrome; for "
                        "GoLogin, omit to let it choose (recommended — its port is free)."
                    ),
                },
                "url": {"type": "string", "description": "Page to open. Default about:blank."},
                "headless": {
                    "type": "boolean",
                    "default": False,
                    "description": "GoLogin only: start Orbita without a visible window.",
                },
                "background": {"type": "boolean", "default": True, "description": "Chrome only: run without opening or focusing a window. False shows the browser."},
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Chrome only: re-copy the profile, picking up logins made since the last copy.",
                },
            },
            "required": ["profile"],
        },
    },
    {
        "name": "browser_close",
        "description": "Stop the GoLogin profile this server opened and save its cookies.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_use",
        "description": "Perform browser interactions toward a short goal. Set act=true to act. For reading known URLs use browser_read(url=...) instead. Read after actions to verify.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What to accomplish in the page."},
                "url": {
                    "type": "string",
                    "description": "Optional URL to load before starting.",
                },
                "act": {
                    "type": "boolean",
                    "default": False,
                    "description": "Actually click. Default false: decide and validate only.",
                },
                "max_steps": {"type": "integer", "default": DEFAULT_MAX_STEPS},
                "min_confidence": {
                    "type": "number",
                    "default": DEFAULT_MIN_CONFIDENCE,
                    "description": "Stop and report rather than act below this confidence.",
                },
                "settle": {
                    "type": "number",
                    "default": DEFAULT_SETTLE,
                    "description": "Upper bound in seconds to wait for a navigation to land.",
                },
                "port": {
                    "type": "integer",
                    "description": (
                        "Explicit CDP port, overriding profile matching. Use this for a "
                        "browser discovery cannot see: a GoLogin/Orbita profile is started "
                        "with a debugging port by the GoLogin app/SDK (which returns the "
                        "port), as are other antidetect browsers. Omit to auto-detect."
                    ),
                },
                "profile": {
                    "type": "string",
                    "description": (
                        "Which browser profile to drive, matched as a substring of its "
                        "directory (e.g. 'google-chrome'). Defaults to the user's real "
                        f"profile ({DEFAULT_PROFILE.name}); attaching to a different "
                        "browser than the one asked for is refused, not silently done."
                    ),
                },
                "use_cache": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Replay a plan that already succeeded for this page and goal, "
                        "skipping Jev entirely: one snapshot, zero model calls. A plan "
                        "that no longer resolves is dropped and re-planned."
                    ),
                },
                "decompose": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Split a compound goal into ordered subgoals before acting. "
                        "Jev decides one step at a time and cannot hold a plan, so this "
                        "is the accuracy fix for anything sequential. Needs a text "
                        "model; without one the goal is used unchanged."
                    ),
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "browser_extract",
        "description": "Extract typed answers from page text using Jev. For ordinary questions use browser_read.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "object",
                    "description": (
                        "name -> {type: choice|score|noul, instructions, criteria}. "
                        "choice needs criteria as an object, score as a list of 2-10 "
                        "ordered levels, noul needs neither."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Ask about this text instead of reading the page.",
                },
                "profile": {"type": "string", "description": "Which browser profile."},
                "port": {"type": "integer"},
            },
            "required": ["questions"],
        },
    },
    {
        "name": "browser_read",
        "description": "Navigate to an optional URL and return visible page text. No decision model needed. Pass port from browser_open.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Navigate here and read in one call; no decision model needed."},
                "max_chars": {"type": "integer", "default": 6000, "description": "Output limit, up to 20000."},
                "port": {"type": "integer", "description": "Explicit CDP port."},
                "profile": {
                    "type": "string",
                    "description": "Which browser profile to read, as in browser_use.",
                },
            },
        },
    },
    {
        "name": "browser_read_many",
        "description": "Read known URLs in parallel tabs. Pass the browser port. Prefer one call over a loop of reads.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The pages to read, in the order you want them back.",
                },
                "concurrency": {
                    "type": "integer",
                    "default": DEFAULT_READ_CONCURRENCY,
                    "description": "How many tabs to read at once (1-8).",
                },
                "profile": {
                    "type": "string",
                    "description": "Which browser profile to read, as in browser_use.",
                },
                "port": {
                    "type": "integer",
                    "description": (
                        "Explicit CDP port to read, as in browser_use. Wins over `profile`."
                    ),
                },
            },
            "required": ["urls"],
        },
    },
    {
        "name": "android_devices",
        "description": "List connected Android devices. Call once before phone tasks.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "android_use",
        "description": "Perform phone actions toward a short goal. Set act=true to act. Pass serial for multiple devices; android_read afterward verifies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What to accomplish on the phone."},
                "serial": {
                    "type": "string",
                    "description": (
                        "Which device, from android_devices. Required when more than one "
                        "is attached; guessing is refused rather than done."
                    ),
                },
                "act": {
                    "type": "boolean",
                    "default": False,
                    "description": "Actually tap. Default false: decide and validate only.",
                },
                "max_steps": {"type": "integer", "default": DEFAULT_MAX_STEPS},
                "min_confidence": {
                    "type": "number",
                    "default": DEFAULT_MIN_CONFIDENCE,
                    "description": "Stop and report rather than act below this confidence.",
                },
                "settle": {
                    "type": "number",
                    "default": ANDROID_DEFAULT_SETTLE,
                    "description": "Upper bound in seconds to wait for the screen to move.",
                },
                "use_cache": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Replay a plan that already succeeded on this same screen and "
                        "goal, skipping Jev entirely."
                    ),
                },
                "decompose": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Split a compound goal into ordered subgoals first. Needs a text "
                        "model; without one the goal is used unchanged."
                    ),
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "android_read",
        "description": "Return the current phone screen text without a model request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string", "description": "Which device, from android_devices."},
            },
        },
    },
    {
        "name": "android_location",
        "description": "Read the Facebook account location. Quote location only when state=location.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string", "description": "Which device, from android_devices."},
                "timeout": {
                    "type": "number",
                    "default": ANDROID_LOCATION_TIMEOUT,
                    "description": "Upper bound in seconds to wait for the page to name a location.",
                },
            },
        },
    },
]


def _gologin_lines(filter_text: str) -> list[str]:
    """The GoLogin section of browser_profiles, or an empty list.

    A GoLogin profile is listed here rather than under AVAILABLE because it is not
    a Chrome profile: it opens in its own browser, not through browser_open's copy.
    Listing needs the API token, since GoLogin profiles live on the account; without
    one we only say that GoLogin is present and how to enable it.
    """
    if not gologin.token():
        from . import host

        root = host.gologin_browser_root()
        if root.exists():
            return [
                "",
                f"GOLOGIN — GoLogin is installed ({root}) but no API token is set.",
                "  Add one to open its profiles in their own browser:",
                "    jev-use install --gologin-token=<token>",
            ]
        return []

    try:
        found = gologin.profiles()
    except gologin.GoLoginError as exc:
        return ["", f"GOLOGIN — {exc}"]

    if filter_text:
        found = [p for p in found if filter_text in p.name.lower()]
    lines = [
        "",
        f"GOLOGIN ({len(found)} profiles; each runs in its own browser, not Chrome)",
    ]
    if not found:
        lines.append("  (none match)")
    for profile in found:
        lines.append(f"  {profile.describe()}")
    lines.append('')
    lines.append('Open one with browser_open(profile="<name>", vendor="gologin").')
    return lines


def tool_browser_profiles(args: dict[str, Any]) -> str:
    """Running browsers and on-disk profiles, one line each."""
    filter_text = (args.get("filter") or "").lower()

    # Running browsers first: these are what browser_use can attach to right now.
    running: dict[str, Any] = {}
    for profile in running_profiles():
        existing = running.get(profile.profile_dir)
        # Keep the process that actually serves CDP when several share a profile.
        if existing is None or (profile.cdp and not existing.cdp):
            running[profile.profile_dir] = profile

    if not args.get("available"):
        lines = ["RUNNING"] if running else []
        usable = 0
        from .profiles import WORK_ROOT, local_profiles as _locals

        by_slug = {p.workdir.name: p for p in _locals()}
        for profile in running.values():
            if profile.port is None:
                suffix = ""
            elif cdp_alive(profile.port):
                suffix, usable = "", usable + 1
            else:
                suffix = "  (port set but not answering)"
            # A prepared copy reports its slug; show the profile's real name.
            label = profile.describe()
            if str(WORK_ROOT) in profile.profile_dir:
                source = by_slug.get(Path(profile.profile_dir).name)
                if source:
                    label = f"{source.name!r} ({source.directory}) open from a copy"
            lines.append("  " + label + suffix)
        if not usable:
            if any(p.vendor == "gologin" for p in running.values()):
                lines.append(
                    "  (none drivable — a GoLogin/Orbita profile exposes CDP only when it "
                    "is started with a debugging port; start it through the GoLogin app/SDK "
                    "and pass the port it returns to browser_use)"
                )
            else:
                lines.append(
                    "  (none drivable — a running Chrome cannot expose CDP on its default "
                    "profile; use browser_open to launch a profile from its own copy)"
                )
        if args.get("only_running"):
            return "\n".join(lines)

    # On-disk profiles: what browser_open could start.
    lines = lines if not args.get("available") else []
    if not args.get("only_running"):
        try:
            profiles = local_profiles()
        except FileNotFoundError as exc:
            # No Chrome at all is not the end of the report: a GoLogin user still
            # wants to see their profiles, so the section below is reached either way.
            profiles = []
            lines.append(f"\n{exc}")

        if filter_text:
            profiles = [
                p for p in profiles if filter_text in p.name.lower() or filter_text in p.directory.lower()
            ]
        lines.append("")
        lines.append(f"AVAILABLE ({len(profiles)} Chrome profiles on disk)")
        if not profiles:
            lines.append("  (none match)")
        for profile in profiles:
            marker = "  "
            if profile.port and cdp_alive(profile.port):
                marker = "> "  # already open and drivable
            lines.append(f"{marker}{profile.describe()}")
        lines.append('')
        lines.append('Open one with browser_open(profile="<name>").')

        lines += _gologin_lines(filter_text)

    return "\n".join(lines)


def _open_gologin(wanted: str, args: dict[str, Any]) -> str:
    """Launch a GoLogin profile in its own browser and report the CDP port."""
    try:
        match = gologin.find(wanted)
    except gologin.GoLoginError as exc:
        return str(exc)

    existing = _GOLOGIN.get("session")
    if existing is not None and existing.profile.id == match.id and cdp_alive(existing.port):
        return f"GoLogin profile {match.name!r} is already open and drivable on port {existing.port}."

    url = str(args.get("url") or "").strip()
    try:
        session = gologin.launch(
            match,
            port=_port(args),
            url=None if url in ("", "about:blank") else url,
            headless=bool(args.get("headless", False)),
        )
    except gologin.GoLoginError as exc:
        return str(exc)

    _GOLOGIN["session"] = session
    transport = (
        "browser-harness CDP transport (one websocket, no per-call driver spawn)"
        if harness.available()
        else "the cua-driver `page` tool; install the harness for the faster path:\n"
        "    pip install 'jev-use[harness]'"
    )
    return (
        f"opened GoLogin profile {match.name!r} ({match.id}) in its own browser on "
        f"port {session.port}\n"
        "  this is the profile's real identity — fingerprint, proxy and cookies "
        "intact, not a copied Chrome profile.\n"
        f"  driving over: {transport}\n"
        f"  browser_use(port={session.port}) or browser_use(profile={match.name!r}) "
        "can attach now.\n"
        "  Call browser_close when the task is done, to stop it and save the profile."
    )


def tool_browser_open(args: dict[str, Any]) -> str:
    wanted = args["profile"]
    vendor = str(args.get("vendor") or "auto").lower()
    port = int(args.get("port", 9222))

    # Chrome first for "auto": a name that matches both must keep selecting Chrome,
    # which is what it did before GoLogin existed.
    match = None
    problem = ""
    if vendor in ("auto", "chrome"):
        try:
            match = find_profile(wanted)
        except (ValueError, FileNotFoundError) as exc:
            problem = str(exc)
            if vendor == "chrome":
                return problem

    if match is None:
        if vendor == "gologin" or (vendor == "auto" and gologin.token()):
            return _open_gologin(wanted, args)
        return problem or f"no profile matches {wanted!r}"

    if match.port and cdp_alive(match.port):
        return f"{match.name!r} is already open and drivable on port {match.port}."

    try:
        profile, workdir = start_profile(
            match.directory,
            port=port,
            refresh=bool(args.get("refresh", False)),
            url=str(args.get("url") or "about:blank"),
            background=bool(args.get("background", True)),
        )
    except (TimeoutError, FileNotFoundError, RuntimeError) as exc:
        return str(exc)

    return (
        f"opened {profile.name!r} ({profile.directory}) on port {port}\n"
        f"  copy: {workdir}\n"
        f"  logins come from the copy, taken at copy time.\n"
        f"  browser_use(profile={profile.directory!r}) can attach now."
    )


def tool_browser_close(args: dict[str, Any]) -> str:
    """Stop the GoLogin profile this server started, committing its state."""
    reset_browser_session()
    session = _GOLOGIN.get("session")
    if session is None:
        return "no GoLogin profile was started by this server, so there is nothing to close."
    _GOLOGIN["session"] = None
    try:
        session.stop()
    except Exception as exc:  # noqa: BLE001 - report whatever the SDK raises
        return f"could not stop the GoLogin profile {session.profile.name!r}: {exc}"
    return (
        f"stopped GoLogin profile {session.profile.name!r}; its cookies and login "
        "state are saved back to GoLogin."
    )


def tool_browser_use(args: dict[str, Any]) -> str:
    goal = args["goal"]
    act = bool(args.get("act", False))

    def body(driver: Driver, target: Any) -> Any:
        if args.get("url"):
            _background_tab(driver, target)
            # Points the page at the URL and waits for it to actually arrive. The
            # baseline is read from the live page, not the target's cached URL.
            navigate_and_settle(
                driver, target, args["url"], float(args.get("settle", DEFAULT_SETTLE))
            )

        return browser_run(
            driver,
            target,
            goal,
            JevChooser(),
            act=act,
            max_steps=int(args.get("max_steps", DEFAULT_MAX_STEPS)),
            min_confidence=float(args.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
            settle=float(args.get("settle", DEFAULT_SETTLE)),
            writer=TextModel(),
            decompose=bool(args.get("decompose", True)),
            cache=PlanCache(CACHE_PATH) if args.get("use_cache", True) else None,
        )

    result, target = with_browser_session(args.get("profile"), body, _port(args))
    return f"port={target.port} pid={target.pid}\n" + render(result)


def tool_browser_extract(args: dict[str, Any]) -> str:
    questions = args.get("questions") or {}
    text = args.get("text")

    if text is None:
        try:
            text, _ = with_browser_session(
                args.get("profile"),
                lambda driver, target: browser_read(driver, target),
                _port(args),
            )
        except DriverError as exc:
            return str(exc)

    try:
        result = extract_ask(text or "", questions)
    except ExtractError as exc:
        return f"extract failed: {exc}"

    lines = [
        f"model={result['model']} chars={result['chars_asked']}",
        "",
    ]
    for name, answer in result["answers"].items():
        if "error" in answer:
            lines.append(f"{name}: {answer['error']}")
            continue
        value = answer.get("value")
        conf = answer.get("confidence")
        tail = f"  (confidence {conf})" if conf is not None else ""
        lines.append(f"{name}: {value}{tail}")
        probs = answer.get("probabilities")
        if probs:
            ranked = sorted(probs.items(), key=lambda kv: -float(kv[1]))[:3]
            lines.append("    " + ", ".join(f"{k}={float(v):.2f}" for k, v in ranked))
    return "\n".join(lines)


def _background_tab(driver: Any, target: Any) -> None:
    if isinstance(driver, harness.Harness) and not (target.url_hint or "").startswith("target:"):
        tab = driver.open_tab("about:blank")
        target.url_hint = f"target:{tab['id']}"


def tool_browser_read(args: dict[str, Any]) -> str:
    def body(driver: Any, target: Any) -> str:
        if args.get("url"):
            _background_tab(driver, target)
            navigate_and_settle(driver, target, args["url"], 2.0)
        return browser_read(driver, target)

    text, target = with_browser_session(
        args.get("profile"),
        body,
        _port(args),
    )
    if not text:
        return "(the page returned no visible text)"
    limit = max(500, min(int(args.get("max_chars", 6000)), 20000))
    return f"port={target.port} url={target.url}\n\n{text[:limit]}"


def tool_browser_read_many(args: dict[str, Any]) -> str:
    urls = [u.strip() for u in (args.get("urls") or []) if isinstance(u, str) and u.strip()]
    if not urls:
        return "no urls given"
    concurrency = max(1, min(int(args.get("concurrency", DEFAULT_READ_CONCURRENCY)), 8))
    try:
        # The shared session supplies the port/window; the batch builds its own
        # transport per worker, because one transport serialises its calls.
        session, target = _browser_session(args.get("profile"), _port(args))
        new_driver = (
            (lambda: harness.Harness(port=target.port))
            if isinstance(session, harness.Harness)
            else None
        )
        results = browser_read_many(
            target, urls, concurrency=concurrency, new_driver=new_driver
        )
    except DriverError as exc:
        return str(exc)
    blocks = [f"url={url}\n{text[:20000]}" for url, text in results]
    header = f"{len(results)} page(s), {concurrency} at a time"
    return header + "\n\n" + "\n\n---\n\n".join(blocks)


# -- android ----------------------------------------------------------------


def android_device(args: dict[str, Any]) -> Any:
    """Resolve the target phone, or raise AdbError with the fix in the message."""
    return android_engine.pick_device(args.get("serial"))


def tool_android_devices(args: dict[str, Any]) -> str:
    found = android_engine.devices()
    if not found:
        return (
            "no Android device attached.\n"
            "  * plug it in over USB, or `adb connect <ip>:5555` for wireless debugging\n"
            "  * on the phone: Settings > Developer options > USB debugging"
        )

    lines = [f"{len(found)} device(s) attached:"]
    for device in found:
        lines.append(("> " if device.usable else "  ") + device.describe())
    if not any(d.usable for d in found):
        lines.append("")
        lines.append(
            "none usable — unlock the phone and accept the 'Allow USB debugging' prompt"
        )
    return "\n".join(lines)


def tool_android_use(args: dict[str, Any]) -> str:
    goal = args["goal"]
    act = bool(args.get("act", False))
    try:
        device = android_device(args)
        result = android_engine.run(
            device.serial,
            goal,
            JevChooser(),
            act=act,
            max_steps=int(args.get("max_steps", DEFAULT_MAX_STEPS)),
            min_confidence=float(args.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
            settle=float(args.get("settle", ANDROID_DEFAULT_SETTLE)),
            writer=TextModel(),
            decompose=bool(args.get("decompose", True)),
            cache=PlanCache(CACHE_PATH) if args.get("use_cache", True) else None,
        )
    except android_engine.AdbError as exc:
        return str(exc)

    return f"device={device.serial}\n" + render(result)


def tool_android_read(args: dict[str, Any]) -> str:
    try:
        device = android_device(args)
        text = android_engine.snapshot(device.serial).read()
    except android_engine.AdbError as exc:
        return str(exc)

    if not text.strip():
        return "(the screen returned no visible text)"
    return f"device={device.serial}\n\n{text[:20000]}"


def tool_android_location(args: dict[str, Any]) -> str:
    try:
        device = android_device(args)
        found = android_engine.account_location(
            device.serial,
            timeout=float(args.get("timeout", ANDROID_LOCATION_TIMEOUT)),
        )
    except android_engine.AdbError as exc:
        return str(exc)

    lines = [found.describe()]
    if not found.location:
        # Nothing was parsed, so hand the caller the page's own words rather than a
        # bare "(none)" — a sign-in screen and a half-drawn one read very
        # differently and only the text tells them apart.
        text = found.text.strip()
        if text:
            lines += ["", "shows:", text[:2000]]
    return "\n".join(lines)


HANDLERS = {
    "browser_profiles": tool_browser_profiles,
    "browser_open": tool_browser_open,
    "browser_close": tool_browser_close,
    "browser_use": tool_browser_use,
    "browser_extract": tool_browser_extract,
    "browser_read": tool_browser_read,
    "browser_read_many": tool_browser_read_many,
    "android_devices": tool_android_devices,
    "android_use": tool_android_use,
    "android_read": tool_android_read,
    "android_location": tool_android_location,
}


# -- JSON-RPC over stdio ----------------------------------------------------


def send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": PROMPTS}}

    if method == "prompts/get":
        params = request.get("params") or {}
        payload = prompt_messages(params.get("name", ""), params.get("arguments") or {})
        if payload is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"unknown prompt {params.get('name')!r}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"unknown tool {name!r}"}],
                    "isError": True,
                },
            }
        try:
            text = handler(arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except DriverError as exc:
            # A driver refusal is an operational message, not a crash: return it as
            # text so the caller acts on it instead of seeing a stack trace.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }
        except android_engine.AdbError as exc:
            # Same contract for the phone: "no device attached" and "unauthorized"
            # are instructions, not exceptions.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }
        except Exception as exc:  # noqa: BLE001 - surface everything to the caller
            log(traceback.format_exc())
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if request_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> int:
    load_env()
    if not os.environ.get("TYPESAFE_API_KEY"):
        log("warning: TYPESAFE_API_KEY is not set — browser_use will fail at the first decision")
    log(f"{SERVER_NAME} {SERVER_VERSION} ready (browser only)")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(request)
        if response is not None:
            send(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
