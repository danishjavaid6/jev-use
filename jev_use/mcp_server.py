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
    `auto`, which picks the harness only for an antidetect browser (GoLogin /
    Orbita). That is the one case where the port is always explicit — the SDK hands
    it back at launch — so the decision is deterministic, and a Chrome someone
    drives by profile is left on the path it has always used.
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
    session = _GOLOGIN.get("session")
    if session is not None and session.port == port:
        return True
    return any(p.vendor != "chrome" and p.port == port for p in running_profiles())


def _start_session(profile: str | None, port: int | None) -> tuple[Any, Any]:
    """A started transport and its Target — the harness where it applies, else a driver."""
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

PROMPT_TEMPLATE = """Use the jev-use browser tools to do this task in the user's own browser:

    {task}

Follow this order and do not skip steps:

1. Call browser_profiles FIRST. It costs ~0.15s and lists the running browsers and
   the profiles on disk.

   * If a browser is already drivable (`cdp:port`) and it is the profile the user
     meant, use it.
   * If the user means a GoLogin profile (listed under GOLOGIN), open it with
     browser_open(profile="<name>", vendor="gologin") — GoLogin starts it in its own
     browser, not a copied Chrome — then call browser_close when the task is done,
     because that is what saves its cookies and login state.
   * If nothing is drivable, call browser_open(profile="<name>") with the profile the
     user named — that copies it and launches it with a CDP endpoint. It takes a
     moment and uses disk; say so if the profile is large.
   * If you do not know which profile the user means and several plausibly match,
     ask rather than guessing. Never substitute a different browser: the whole point
     is that this runs against the profile holding their logins.

2. For each page you need: browser_use(url=..., goal=..., act=true) to get there.
   Jev picks every in-page action, so describe the destination, not the clicks.

3. Then get the content. browser_use orbits the page; it does not report what the
   page says.

   * **browser_extract** when the user asked a specific question ("how many tokens?",
     "what does it cost?", "which deploys failed?"). Give it typed questions and get
     typed values back — cheaper and more accurate than reading a wall of text.
   * **browser_read** when you need the whole page: an unfamiliar page, a commit
     list, deciding what to ask next. It waits for the page to render, so a blank
     result means the page really is blank — do not retry it.

   **Use the site's own UI, never a public API, for anything behind a login.** An
   unauthenticated API cannot see private repos or account data, and searching
   public sources for the user's own repos finds strangers' repos with similar
   names. If a repo, dashboard or bill is the user's, read it in their session.

4. Answer the user from what browser_read returned, and quote the specific values
   you saw (numbers, names, dates). If a page needed a login and the session had
   expired, say so plainly rather than guessing at the contents.

Report per section: what you opened, what it said, and anything you could not reach.
"""

MOBILE_PROMPT_NAME = "mobile-use"

MOBILE_PROMPT_TEMPLATE = """Use the jev-use phone tools to do this task on the user's own Android phone:

    {task}

Follow this order and do not skip steps:

1. Call android_devices FIRST. It answers the two things that go wrong silently:

   * Nothing listed — the phone is not connected. USB, or `adb connect <ip>:5555` for
     wireless debugging; on the phone, Settings > Developer options > USB debugging.
   * `[unauthorized]` — the phone has not accepted the debugging prompt. Unlock it and
     accept the dialog; nothing works until it is accepted.

   If several devices are attached, pass `serial` explicitly. The server refuses to
   guess, and it will not substitute a different phone.

2. android_use(goal=..., act=true) to get there. Jev picks every tap, so describe the
   destination, not the taps. Leave `decompose` on unless the goal is already one step.
   `act=false` (the default) decides and validates WITHOUT touching the phone — the
   right first move for anything destructive. `go_back` and `go_home` are offered on
   every screen; backing out of a screen is often the fastest route, not a failure.

3. android_use CHANGES the screen; it does not report what it says. Use **android_read**
   to answer a question about what the phone shows. It reads the view hierarchy, so the
   text is exact rather than OCR — no vision model involved.

4. Answer from what android_read returned, and quote the specific values you saw
   (names, numbers, times, toggle states). If a screen needed a login and the session
   had expired, say so plainly rather than guessing at the contents.

android_location reports the location Facebook attributes to the signed-in account,
read from Facebook's own page inside the app. Read `state` before the value:
`location` (quote it), `login` (signed out — say so), `unknown` (not rendered — retry).

Report per section: what you opened, what it said, and anything you could not reach.
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
        "description": (
            "List every Chrome profile: those already running (with whether a CDP "
            "endpoint answers) and those that exist on disk but are closed. Call this "
            "FIRST. Pass `only_running: true` for just the running browsers, or "
            "`available: true` for profiles you could open with browser_open. It never "
            "launches or modifies anything.\n\n"
            "Running antidetect browsers (GoLogin/Orbita, shown as [gologin]) appear here "
            "too when they were started with a debugging port. They cannot be opened with "
            "browser_open — drive them by passing their port to browser_use."
        ),
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
        "description": (
            "Open a browser profile that is not running, with a CDP endpoint, and "
            "leave it running for browser_use. Use the display name from "
            "browser_profiles (e.g. 'Work', 'Outlook 3').\n\n"
            "Two kinds of profile, chosen by `vendor`:\n\n"
            "* **A Chrome profile** (default) is COPIED into a private directory first, "
            "because Chrome refuses a debugging port on the default data directory. "
            "The copy carries your cookies, so the logins are there — but it is a "
            "SNAPSHOT: signing in to the original afterwards does not reach it, so pass "
            "refresh=true to re-copy. One profile is ~0.5-1.5 GB, copied once.\n\n"
            "* **A GoLogin profile** (`vendor=\"gologin\"`) is NOT copied. GoLogin starts "
            "it in its own browser (Orbita) with its fingerprint, proxy and cookies "
            "intact, and this returns the port it came up on. This needs a GoLogin API "
            "token (`jev-use install --gologin-token=<token>`). It can take a while: "
            "GoLogin downloads the profile, and Orbita the first time. Call browser_close "
            "when the task is done so GoLogin saves the profile.\n\n"
            "With the default vendor=\"auto\", a name is looked up as a Chrome profile "
            "first and falls back to GoLogin only when there is no Chrome profile by "
            "that name."
        ),
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
        "description": (
            "Stop the GoLogin profile that browser_open started, saving its cookies and "
            "login state back to GoLogin. Call this when the task is done: the GoLogin "
            "SDK only commits the profile on stop, so a profile left running loses what "
            "the session did. It does nothing to Chrome profiles (leaving those open is "
            "harmless)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_use",
        "description": (
            "Drive the browser toward a plain-English goal, with Jev choosing each "
            "action. Operates on the page's DOM over CDP: Jev selects only from "
            "elements this server enumerated, so it cannot invent an action, and the "
            "choice is validated against the snapshot it came from. Returns a step "
            "log and timing. Set act=false (default) to decide without clicking.\n\n"
            "This orbits the page rather than reading it — to answer a question about "
            "what a page says, call browser_read afterwards."
        ),
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
        "description": (
            "Ask Jev typed questions about the current page and get typed values instead "
            "of a wall of text. Prefer this over browser_read whenever the user asked a "
            "specific question. "
            "This is how to ANSWER a question about a page: browser_read returns 10-20 KB "
            "of raw text for a dashboard, whereas browser_extract returns values.\n\n"
            "It is also more trustworthy — Jev cannot generate a number it did not "
            "read, so a figure in the answer came from the page. All questions are "
            "evaluated in parallel in one call, so asking six costs about the same as "
            "asking one.\n\n"
            "Example:\n"
            '  {"usage":    {"type":"noul",   "instructions":"Is this page logged out?"},\n'
            '   "tokens":   {"type":"score",  "instructions":"How many tokens are used?",\n'
            '                "criteria":["none","under 1M","1M-100M","over 100M"]},\n'
            '   "plan":     {"type":"choice", "instructions":"Which plan is shown?",\n'
            '                "criteria":{"free":"Free tier","goat":"Paid GOAT plan","other":"Something else"}}}'
        ),
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
        "description": (
            "Return the visible text of the current page, WAITING for it to render "
            "(single-page apps return an empty shell until their data arrives, which "
            "readyState does not reflect). Use this to answer questions about what a "
            "page says — usage, billing, commits, deployments. Pairs with browser_use "
            "to get somewhere first. For a specific question prefer browser_extract."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
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
        "description": (
            "Read SEVERAL pages at once — one tab each, in parallel — and return the "
            "text of each. Use this instead of calling browser_read N times when a "
            "task spans many URLs (a list of sites, a batch of tickets): it is a "
            "single tool call, it overlaps the page loads, and you pay one turn "
            "instead of N. Results come back in the order you asked, each labelled "
            "with its URL; a page that fails is reported in place rather than "
            "failing the batch. A maximum of 8 URLs is opened at once."
        ),
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
        "description": (
            "List the Android devices attached over adb, with their serial and model, "
            "and whether each is usable. Call this FIRST for any phone task — it also "
            "reports the two states that explain every failed attempt: 'unauthorized' "
            "(the phone has not accepted the debugging prompt) and no devices at all. "
            "Never launches or modifies anything."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "android_use",
        "description": (
            "Drive the phone toward a plain-English goal, with Jev choosing each "
            "action. It reads the live view hierarchy (uiautomator) and taps real "
            "elements, so Jev selects only from elements this server enumerated and "
            "cannot invent a tap at a bare coordinate. Returns a step log and timing. "
            "Set act=false (default) to decide without touching the phone.\n\n"
            "This changes the screen rather than reporting it — to answer a question "
            "about what the phone shows, call android_read afterwards.\n\n"
            "Navigation is cheap here: `go_back` and `go_home` are offered on every "
            "screen, and backing out of a screen is often the fastest route."
        ),
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
        "description": (
            "Return the text the phone is currently showing, read from the view "
            "hierarchy rather than from a screenshot — so it is exact text, not OCR, "
            "and needs no vision model. Use it to answer a question about what is on "
            "screen. Password fields contribute their presence but never their value."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string", "description": "Which device, from android_devices."},
            },
        },
    },
    {
        "name": "android_location",
        "description": (
            "Report the location Facebook currently attributes to the account signed "
            "in on the phone. It opens Facebook's own 'primary location' page INSIDE "
            "the app — through the app's internal deep link, so the page renders as an "
            "app screen rather than in Chrome — and reads it. Use this when the user "
            "asks which country or city Facebook thinks they are in, or whether an "
            "account reads as belonging to a particular one.\n\n"
            "It answers for whichever account is signed in at that moment: switch "
            "accounts in the app and call it again for the other one. The result says "
            "whether it found a location page or a sign-in page, so 'not signed in' is "
            "never confused with 'did not render'.\n\n"
            "This READS Facebook's own inference — the profile's current city, the "
            "connection's IP, check-ins and the device location. It reads it, it cannot "
            "change it, and it is not the same thing as a payout country or a region "
            "setting."
        ),
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


def tool_browser_read(args: dict[str, Any]) -> str:
    text, target = with_browser_session(
        args.get("profile"),
        lambda driver, target: browser_read(driver, target),
        _port(args),
    )
    if not text:
        return "(the page returned no visible text)"
    return f"url={target.url}\n\n{text[:20000]}"


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
