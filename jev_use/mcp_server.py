"""jev-use — browser use with Jev making the decisions, as an MCP server.

Any harness (Command Code, Claude Code, Cursor, Codex) can call this. The harness
brings the reasoning; this server brings the browser and Jev brings the decisions.

Deliberately browser-only and deliberately small — a large tool schema is paid for
in the model's context on every turn:

    browser_profiles   which Chromium-family browsers are open, and whether CDP works
    browser_use        drive the page toward a goal; Jev picks each action
    browser_read       return the page's text so the harness can answer questions

The desktop / accessibility surface was removed. On GNOME/Wayland it cannot attach
to an existing browser profile, and that dead end is what made agents burn turns.

Wire it up:

    cmd mcp add --scope user jev-use -- /path/to/.venv/bin/python -m jev_use.mcp_server

Speaks MCP over stdio. Nothing but JSON-RPC goes to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .browser import DEFAULT_PROFILE, attach, read as browser_read, run as browser_run
from .browser import running_profiles, cdp_alive
from .choosers import JevChooser
from .cache import PlanCache
from .driver import Driver, DriverError
from .extract import ExtractError, ask as extract_ask
from pathlib import Path
from .profiles import find_profile, local_profiles, start_profile
from .text_model import TextModel

SERVER_NAME = "jev-use"
SERVER_VERSION = "0.3.0"
PROTOCOL_VERSION = "2025-06-18"
CACHE_PATH = Path(__file__).resolve().parent.parent / ".jev-browser-cache.json"

# Single source of truth: the tool schema and the handler must agree. They drifted
# once (schema said one thing, handler another) which silently changed behaviour.
DEFAULT_MAX_STEPS = 8
DEFAULT_MIN_CONFIDENCE = 0.4
DEFAULT_SETTLE = 3.0


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


def attach_helpfully(driver: Driver, profile: str | None = None) -> Any:
    """Attach to the requested profile (default: the real one), or explain the fix."""
    return attach(driver, profile=profile)


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
    }
]


def prompt_messages(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if name != PROMPT_NAME:
        return None
    task = (arguments or {}).get("task", "").strip() or "(no task given)"
    return {
        "description": f"Browser task: {task[:80]}",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": PROMPT_TEMPLATE.format(task=task)}}
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
            "launches or modifies anything."
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
            "Open a Chrome profile that exists on disk but is not running, with a CDP "
            "endpoint, and leave it running for browser_use. Use the display name from "
            "browser_profiles (e.g. 'Work', 'Outlook 3').\n\n"
            "This COPIES the profile into a private directory first, because Chrome "
            "refuses a debugging port on the default data directory. The copy carries "
            "your cookies, so the logins are there — but it is a SNAPSHOT: signing in "
            "to the original afterwards does not reach it, so pass refresh=true to "
            "re-copy. One profile is ~0.5-1.5 GB and the copy happens once."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "description": "Display name or directory of the profile to open.",
                },
                "port": {"type": "integer", "default": 9222},
                "url": {"type": "string", "description": "Page to open. Default about:blank."},
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Re-copy the profile, picking up logins made since the last copy.",
                },
            },
            "required": ["profile"],
        },
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
                    "description": "Explicit CDP port. Omit to auto-detect.",
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
]


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
            lines.append(f"\n{exc}")
            return "\n".join(lines)

        if filter_text:
            profiles = [
                p for p in profiles if filter_text in p.name.lower() or filter_text in p.directory.lower()
            ]
        lines.append("")
        lines.append(f"AVAILABLE ({len(profiles)} profiles on disk)")
        if not profiles:
            lines.append("  (none match)")
        for profile in profiles:
            marker = "  "
            if profile.port and cdp_alive(profile.port):
                marker = "> "  # already open and drivable
            lines.append(f"{marker}{profile.describe()}")
        lines.append('')
        lines.append('Open one with browser_open(profile="<name>").')

    return "\n".join(lines)


def tool_browser_open(args: dict[str, Any]) -> str:
    wanted = args["profile"]
    port = int(args.get("port", 9222))
    try:
        match = find_profile(wanted)
    except (ValueError, FileNotFoundError) as exc:
        return str(exc)

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


def tool_browser_use(args: dict[str, Any]) -> str:
    goal = args["goal"]
    act = bool(args.get("act", False))

    driver = Driver()
    driver.start()
    try:
        target = attach_helpfully(driver, args.get("profile"))
        if args.get("url"):
            driver.call(
                "page",
                target.args(
                    action="execute_javascript",
                    javascript=f"location.href={json.dumps(args['url'])};",
                ),
            )
            deadline = time.perf_counter() + float(args.get("settle", DEFAULT_SETTLE))
            while time.perf_counter() < deadline:
                time.sleep(0.3)
                break

        result = browser_run(
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
    finally:
        driver.close()

    return f"port={target.port} pid={target.pid}\n" + render(result)


def tool_browser_extract(args: dict[str, Any]) -> str:
    questions = args.get("questions") or {}
    text = args.get("text")

    if text is None:
        driver = Driver()
        driver.start()
        try:
            target = attach_helpfully(driver, args.get("profile"))
            text = browser_read(driver, target)
        except DriverError as exc:
            return str(exc)
        finally:
            driver.close()

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
    driver = Driver()
    driver.start()
    try:
        target = attach_helpfully(driver, args.get("profile"))
        text = browser_read(driver, target)
    finally:
        driver.close()
    if not text:
        return "(the page returned no visible text)"
    return f"url={target.url}\n\n{text[:20000]}"


HANDLERS = {
    "browser_profiles": tool_browser_profiles,
    "browser_open": tool_browser_open,
    "browser_use": tool_browser_use,
    "browser_extract": tool_browser_extract,
    "browser_read": tool_browser_read,
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
