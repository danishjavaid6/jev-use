"""Browser use driven by Jev, over an explicit CDP port.

Built on the driver's **`page`** tool rather than the typed `browser_*` tools, and
that choice is the whole design. The typed surface needs `browser_prepare`, which
needs either an existing-profile grant or a provable accessibility binding. On
GNOME/Wayland the second is unavailable — a long-running Chrome exposes one
accessibility element where a fresh one exposes ~160 — so the typed path refuses
with `browser_binding_ambiguous` no matter how many times you retry it.

`page` takes an **explicit `cdp_port`**. It needs no window binding, no
accessibility tree, no grant and no compositor cooperation. Point it at a Chrome
that already has `--remote-debugging-port` and everything works.

Measured against a driver-owned Chromium:

    snapshot (one execute_javascript)   ~150 ms
    click    (one execute_javascript)   ~600 ms
    AT-SPI for comparison           567-750 / 1350 ms

Free text is absent by design: Jev cannot generate strings.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import host
from .choosers import Decision, validate
from .driver import Driver, DriverError
from .host import BROWSER_BINARY_NAMES, argv0, chrome_user_data_root
from .host import process_table as _process_table

DEFAULT_PORT = 9222
DEFAULT_PROFILE = chrome_user_data_root()
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")

BROWSER_OPERATIONS = {
    "click_element": "Activate exactly one of the listed page elements",
    "type_text": "Write a value into one of the listed form fields",
    "scroll_down": "The target is further down the page, scroll to reveal it",
    "scroll_up": "The target is further up the page, scroll to reveal it",
    "wait": "The page is still loading or animating",
    "done": "The page already shows the goal satisfied",
    "impossible": "Nothing on this page can make progress toward the goal",
}

SCROLL_PIXELS = 700

# One call builds the whole candidate table and tags each element, so a ref is a
# plain integer we can re-find next call without holding DOM nodes.
TABLE_JS = r"""
(() => {
  const sel = 'a,button,input,select,textarea,[role=button],[role=link],[onclick],[contenteditable=true]';
  const out = [];
  let i = 0;
  for (const e of Array.from(document.querySelectorAll(sel))) {
    const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.top > innerHeight) continue;
    if (getComputedStyle(e).visibility === 'hidden') continue;
    e.setAttribute('data-jev-ref', String(i));
    out.push({
      ref: i,
      tag: e.tagName.toLowerCase(),
      fillable: e.matches('input,textarea,select') || e.isContentEditable === true,
      text: (e.innerText || e.value || e.getAttribute('aria-label')
             || e.getAttribute('placeholder') || e.getAttribute('name')
             || e.getAttribute('href') || '').trim().replace(/\s+/g, ' ').slice(0, 90),
    });
    i++;
  }
  return JSON.stringify({url: location.href, title: document.title,
                         count: out.length, elements: out});
})()
"""


def click_js(ref: int) -> str:
    """Click by ref. `ref` is an integer we generated, never page text."""
    return (
        "(() => {"
        f"const e = document.querySelector('[data-jev-ref=\"{int(ref)}\"]');"
        "if (!e) return JSON.stringify({ok:false, reason:'ref not found'});"
        "e.scrollIntoView({block:'center', inline:'center'});"
        "try { e.focus({preventScroll:true}); } catch (_) {}"
        "e.click();"
        "return JSON.stringify({ok:true, tag:e.tagName.toLowerCase()});"
        "})()"
    )


def type_js(ref: int, text: str) -> str:
    """Fill a field and announce it.

    Setting `.value` alone is invisible to React and friends, so this goes through
    the prototype's native setter and then dispatches the events a real keystroke
    would. `text` is JSON-encoded, never interpolated raw.
    """
    return (
        "(() => {"
        f"const e = document.querySelector('[data-jev-ref=\"{int(ref)}\"]');"
        "if (!e) return JSON.stringify({ok:false, reason:'ref not found'});"
        "e.scrollIntoView({block:'center'});"
        "e.focus();"
        f"const v = {json.dumps(text)};"
        "const proto = e.constructor && e.constructor.prototype;"
        "const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');"
        "if (desc && desc.set) { desc.set.call(e, v); } else { e.value = v; }"
        "e.dispatchEvent(new Event('input', {bubbles:true}));"
        "e.dispatchEvent(new Event('change', {bubbles:true}));"
        "return JSON.stringify({ok:true, value: String(e.value).slice(0,40)});"
        "})()"
    )


def describe_field(el: dict[str, Any]) -> str:
    tag = el.get("tag", "input")
    text = (el.get("text") or "").strip()
    return f'{tag} field "{text}"' if text else f"{tag} field"


def scroll_js(delta: int) -> str:
    return (
        "(() => {"
        f"window.scrollBy(0, {int(delta)});"
        "return JSON.stringify({ok:true, y: Math.round(window.scrollY)});"
        "})()"
    )


# -- discovery --------------------------------------------------------------


@dataclass
class Profile:
    """A running Chromium-family browser, and whether we can speak CDP to it."""

    pid: int
    profile_dir: str
    port: int | None
    window_id: int | None = None
    title: str = ""

    @property
    def cdp(self) -> bool:
        return self.port is not None

    def describe(self) -> str:
        name = Path(self.profile_dir).name or self.profile_dir
        return (
            f"pid={self.pid} profile={name} dir={self.profile_dir} "
            f"cdp={'yes:' + str(self.port) if self.cdp else 'no'}"
            + (f" window={self.window_id}" if self.window_id else "")
        )


#: Executable *stems*, straight from the platform layer. Chrome is `chrome` on
#: Linux (the `google-chrome` wrapper execs it) and `chrome.exe` on Windows; both
#: compare equal once the extension is stripped.
BROWSER_BINARIES = BROWSER_BINARY_NAMES


def _is_browser_process(args: str) -> bool:
    """True only for a real Chromium-family BROWSER process.

    Matching "chrome" anywhere in the argument list was a real bug: it reported
    phantom profiles for anything with chrome in its name — `chrome-devtools-mcp`
    is an MCP server, not a browser — and the phantoms showed up as extra rows in
    `browser_profiles` and as false "Chrome is running" answers.

    Only argv[0] is considered, and argv[0] may be quoted on Windows, where a path
    with a space is normal: `"C:\\Program Files\\Google\\Chrome\\...\\chrome.exe"`.
    The comparison is on the executable's stem, so `chrome` and `chrome.exe` are
    the same browser.
    """
    return host.executable_stem(argv0(args)) in BROWSER_BINARIES


def _option(args: str, name: str) -> str | None:
    """The value of `--name=...`, tolerating the Windows quoting style.

    Chrome on Windows writes `--user-data-dir="C:\\Users\\me\\AppData\\Local\\..."`,
    so a bare `\\S+` capture would keep the quotes and never match a real path.
    """
    match = re.search(rf"--{re.escape(name)}=(?:\"([^\"]*)\"|(\S+))", args)
    if not match:
        return None
    return match.group(1) or match.group(2)


def running_profiles() -> list[Profile]:
    """Every running Chromium-family browser, with its profile dir and CDP port.

    Answers "which Chrome profiles are open" without touching the desktop: both the
    profile dir and the debug flag are on the process command line.

    The process table comes from `host`, because `ps` does not exist on Windows.
    """
    found: dict[int, Profile] = {}
    for pid, args in _process_table():
        if not _is_browser_process(args):
            continue
        # Skip renderers, gpu, utility, zygote and crashpad children.
        if any(f"--type={t}" in args for t in ("renderer", "gpu", "utility", "zygote")):
            continue
        if "--crashpad" in args or "--monitor-self" in args:
            continue

        profile = _option(args, "user-data-dir") or str(DEFAULT_PROFILE)

        port_text = _option(args, "remote-debugging-port")
        port: int | None = int(port_text) if port_text and port_text.isdigit() else None

        existing = found.get(pid)
        if existing is None:
            found[pid] = Profile(pid=pid, profile_dir=profile, port=port)
        elif port is not None:
            existing.port = port

    return sorted(found.values(), key=lambda p: p.pid)


def cdp_alive(port: int, timeout: float = 1.5) -> bool:
    """True when a DevTools endpoint answers on this port."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def discover_port() -> int | None:
    """The first live CDP port among running browsers, else None."""
    for profile in running_profiles():
        if profile.port is not None and cdp_alive(profile.port):
            return profile.port
    for port in (DEFAULT_PORT, 9223, 9224):
        if cdp_alive(port):
            return port
    return None


# -- target -----------------------------------------------------------------


@dataclass
class Target:
    port: int
    pid: int
    window_id: int
    url_hint: str | None = None
    url: str = ""

    def args(self, **extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "pid": self.pid,
            "window_id": self.window_id,
            "cdp_port": self.port,
        }
        if self.url_hint:
            base["target_url_contains"] = self.url_hint
        base.update(extra)
        return base


def _chrome_window(driver: Driver, prefer_pid: int | None = None) -> tuple[int, int]:
    """The Chrome window to talk to, preferring the process that owns the port.

    Matching on the name alone is ambiguous the moment two browsers are open, and
    driving the wrong one is worse than not driving any.
    """
    windows = driver.call("list_windows", {}).json().get("windows", [])

    def is_chrome(w: dict[str, Any]) -> bool:
        return "chrome" in (w.get("app_name") or "").lower() or "chrome" in (
            w.get("title") or ""
        ).lower()

    candidates = [w for w in windows if is_chrome(w)]
    if prefer_pid is not None:
        owned = [w for w in candidates if w.get("pid") == prefer_pid]
        if owned:
            return int(owned[0]["pid"]), int(owned[0]["window_id"])

    if not candidates:
        raise DriverError(
            "no Chrome window found. The window must be on the active workspace and the "
            "WinRects helper ACTIVE on GNOME/Wayland."
        )
    return int(candidates[0]["pid"]), int(candidates[0]["window_id"])


def _resolve_profile_hint(wanted: str) -> str:
    """Turn a display name into the directory Chrome uses for it.

    Callers naturally say "Work", not "Profile 1" — and not the slug of a prepared
    copy, which is what the running process actually reports. Resolve through the
    profile registry so any of the three works.
    """
    try:
        from .profiles import find_profile
    except ImportError:  # pragma: no cover - defensive
        return wanted
    try:
        return find_profile(wanted).directory
    except (ValueError, FileNotFoundError):
        return wanted


def _pick_profile(
    profiles: list[Profile], wanted: str | None
) -> tuple[Profile | None, str]:
    """Choose which running browser to drive.

    The default is your real profile, and if it has no CDP endpoint that is an
    ERROR — not a reason to silently drive some other Chrome. Silently attaching to
    the wrong browser is worse than refusing: the caller asked for their logins, and
    a different profile would answer every question with a logged-out page.
    """
    if not profiles:
        return None, "no Chromium-family browser is running"

    if wanted:
        from .profiles import slug

        directory = _resolve_profile_hint(wanted)
        needles = {wanted.lower(), directory.lower(), slug(directory)}
        # A prepared copy lives at <work>/<slug>, so match on the dir basename too.
        candidates = [
            p
            for p in profiles
            if any(n in p.profile_dir.lower() or n == Path(p.profile_dir).name.lower() for n in needles)
        ]
        if not candidates:
            available = ", ".join(sorted({Path(p.profile_dir).name for p in profiles}))
            return None, (
                f"no running browser matches profile {wanted!r} (have: {available}). "
                "If it is not open yet, use browser_open first."
            )
    else:
        candidates = [p for p in profiles if p.profile_dir == str(DEFAULT_PROFILE)] or profiles

    live = [p for p in candidates if p.port is not None and cdp_alive(p.port)]
    if live:
        return live[0], ""

    chosen = candidates[0]
    return None, (
        f"the {'default ' if not wanted else ''}profile {Path(chosen.profile_dir).name!r} "
        f"is running without a CDP endpoint"
    )


def attach(
    driver: Driver,
    port: int | None = None,
    url_hint: str | None = None,
    profile: str | None = None,
) -> Target:
    """Bind to a CDP-capable Chrome, or fail fast with the exact remediation."""
    owner_pid: int | None = None
    profiles = running_profiles()

    if port is None:
        chosen, problem = _pick_profile(profiles, profile)
        if chosen is None:
            seen = "\n".join(f"  {p.describe()}" for p in profiles) or "  (none found)"
            raise DriverError(
                f"No usable CDP endpoint: {problem}.\n"
                "Chrome is single-instance and the debug port can only be set at launch,\n"
                "so run this once (it refuses while Chrome is open):\n\n"
                f"    {host.launcher_hint()}\n\n"
                f"Running browsers:\n{seen}"
            )
        port, owner_pid = chosen.port, chosen.pid
    else:
        owner_pid = next((p.pid for p in profiles if p.port == port), None)

    if not cdp_alive(port):
        raise DriverError(f"port {port} is not answering /json/version")

    pid, window_id = _chrome_window(driver, prefer_pid=owner_pid)
    return Target(port=port, pid=pid, window_id=window_id, url_hint=url_hint)


# -- observation ------------------------------------------------------------


class Observation:
    """One page snapshot, shaped for the chooser.

    Quacks like the desktop observation so `JevChooser` needs no browser branch:
    it reads `target_map()`, `operations()` and `readouts()`.
    """

    def __init__(
        self,
        target: Target,
        payload: dict[str, Any],
        navigate_url: str | None = None,
        can_write: bool = False,
    ):
        self.can_write = can_write
        self.target = target
        self.url = payload.get("url") or ""
        self.title = payload.get("title") or ""
        self.navigate_url = navigate_url
        self.pid = target.pid
        self.window_id = target.window_id
        self._candidates = [
            {
                "id": str(el["ref"]),
                "role": el.get("tag", "element"),
                "label": el.get("text", ""),
                "description": (
                    f'{el.get("tag", "element")} "{el.get("text", "")}"'
                    if el.get("text")
                    else el.get("tag", "element")
                ),
                "fillable": bool(el.get("fillable")),
                "frame": None,
            }
            for el in payload.get("elements", [])
        ]
        self._fields = [c for c in self._candidates if c["fillable"]]

    @property
    def candidates(self) -> list[dict[str, Any]]:
        return self._candidates

    @property
    def targets(self) -> list[dict[str, Any]]:
        return self._candidates

    def by_id(self, candidate_id: str) -> dict[str, Any] | None:
        return next((c for c in self._candidates if c["id"] == str(candidate_id)), None)

    def target_map(self) -> dict[str, str]:
        return {c["id"]: c["description"] for c in self._candidates}

    def option_map(self) -> dict[str, str]:
        return self.target_map()

    @property
    def fields(self) -> list[dict[str, Any]]:
        return self._fields

    def operations(self) -> dict[str, str]:
        operations = dict(BROWSER_OPERATIONS)
        if not self._candidates:
            operations.pop("click_element", None)
        # Typing is only offered when there is somewhere to type AND a writer to
        # produce the string. Jev cannot generate text, so offering the operation
        # without a writer would just create a choice it can never satisfy.
        if not self._fields or not self.can_write:
            operations.pop("type_text", None)
        if self.navigate_url:
            operations["navigate"] = f"Load {self.navigate_url}"
        return operations

    def target_heads(self) -> dict[str, dict[str, str]]:
        heads = {"click_target": self.target_map()}
        if "type_text" in self.operations():
            heads["text_target"] = {c["id"]: c["description"] for c in self._fields}
        return heads

    def targets_for(self, operation: str) -> list[dict[str, Any]]:
        if operation == "click_element":
            return self.targets
        if operation == "type_text":
            return self._fields
        return []

    def readouts(self, limit: int = 10) -> list[str]:
        shown = [self.url] if self.url else []
        if self.title:
            shown.append(self.title)
        shown += [c["description"] for c in self._candidates[:limit]]
        return shown


def _js(driver: Driver, target: Target, javascript: str) -> str:
    return driver.call(
        "page", target.args(action="execute_javascript", javascript=javascript)
    ).text


def _parse_js_payload(raw: str) -> dict[str, Any]:
    """Unwrap the driver envelope, then parse the JSON our script returned.

    The driver answers with `prefix: "<escaped json string>"`. The outer layer must be
    unquoted FIRST: searching for the first `{` in the raw text lands inside the
    escaped string and every subsequent parse fails.
    """
    text = _parse_js_string(raw)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise DriverError(f"could not read a snapshot from the page tool: {raw[:160]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DriverError(f"page tool returned unparseable JSON: {exc}") from exc


def snapshot(
    driver: Driver, target: Target, goal: str = "", can_write: bool = False
) -> Observation:
    """One call builds the whole candidate table (~150 ms)."""
    payload = _parse_js_payload(_js(driver, target, TABLE_JS))
    match = URL_PATTERN.search(goal or "")
    target.url = payload.get("url") or target.url
    return Observation(target, payload, match.group(0) if match else None, can_write=can_write)


# `nodes` is the settle signal, not the text. Text is a bad one: a clock, a
# relative timestamp or an animation keeps changing it forever, so a page that is
# completely rendered never looks "settled". The DOM structure stops growing the
# moment the data has landed, which is exactly what we want to wait for.
READ_JS = (
    "JSON.stringify({url: location.href, state: document.readyState, "
    "nodes: document.getElementsByTagName('*').length, "
    "text: document.body ? document.body.innerText : ''})"
)

# A single read returns whatever happens to be there, which on any SPA is the
# empty shell. Measured against a real run: Cloudflare read back blank, and the
# Vercel deployments list read back empty three times in a row while the agent
# blamed a "stuck Production/Error filter" that was never the problem. Waiting
# here removes an entire class of wasted turns.
READ_TIMEOUT = 6.0
READ_POLL = 0.3


def read(
    driver: Driver,
    target: Target,
    *,
    wait: bool = True,
    timeout: float = READ_TIMEOUT,
) -> str:
    """The page's visible text, waiting for it to actually arrive.

    Uses our own `execute_javascript` rather than the tool's `get_text`. Measured:
    `get_text` does not accept a `cdp_port`, fails to auto-discover one from the pid,
    and silently falls back to the accessibility tree — which on Chromium returns a
    handful of frame elements rather than the page.

    `wait` matters more than it looks. `document.readyState === 'complete'` fires
    BEFORE a single-page app has rendered its data, so both a blank read and a
    premature one are normal without a wait.

    It returns when the DOM has stopped growing, or at the timeout.

    Both parts of that are load-bearing, and each was a measured bug:

    * **Length is not a signal.** An earlier version returned early when the text
      looked "substantial", and the Cloudflare dashboard's nav chrome cleared 1600
      characters before a single billing figure rendered.
    * **Text is not a signal either.** Settling on *text* failed the other way: a
      page carrying a clock or a relative timestamp never stops changing, so Vercel
      hit the timeout and returned its nav. Settling on the *element count* stops
      when the data has landed and is indifferent to a ticking clock.
    * **Empty is not settled.** Requiring non-empty text stops a still-blank page
      from being returned as "blank" — Cloudflare read back 0 chars that way.
    """
    deadline = time.perf_counter() + (timeout if wait else 0.0)
    best = ""
    previous_nodes: int | None = None

    while True:
        payload = _parse_js_payload(_js(driver, target, READ_JS))
        target.url = payload.get("url") or target.url
        text = (payload.get("text") or "").strip()
        state = payload.get("state") or ""
        nodes = int(payload.get("nodes") or 0)

        if len(text) > len(best):
            best = text

        # Settled: the DOM stopped growing and there is something to read.
        if state == "complete" and len(text) > 0 and previous_nodes == nodes:
            return text
        previous_nodes = nodes

        if time.perf_counter() >= deadline:
            return text or best
        time.sleep(READ_POLL)


def _parse_js_string(raw: str) -> str:
    """Normalise a JS result out of the driver's `prefix: "<escaped value>"` shape.

    The envelope has a PREFIX before the opening quote (`cdp.runtime.evaluate...: "`),
    so the test is not "does the text start with a quote" — it is "does the text from
    the first quote onward parse as a JSON string". Bare JSON fails that and passes
    through untouched, which is what makes this tolerant of both shapes.
    """
    text = (raw or "").strip()
    if text.startswith("\u2705"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    text = text.strip()

    start = text.find('"')
    if start == -1:
        return text
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return text


# -- the loop ---------------------------------------------------------------


@dataclass
class Step:
    index: int
    decision: Decision
    observation: Observation
    executed: bool
    note: str = ""


@dataclass
class Result:
    goal: str
    steps: list[Step] = field(default_factory=list)
    outcome: str = "max_steps"
    seconds: float = 0.0
    snapshots: int = 0
    url: str = ""
    subgoals: list[str] = field(default_factory=list)

    @property
    def actions(self) -> int:
        return sum(1 for s in self.steps if s.executed)


def type_into(driver: Driver, target: Target, ref: str, text: str) -> None:
    _js(driver, target, type_js(int(ref), text))


def execute(driver: Driver, target: Target, decision: Decision, observation: Observation) -> None:
    if decision.kind == "type_text":
        raise ValueError("type_text needs the text resolved first; use run()")
    if decision.kind == "click_element":
        _js(driver, target, click_js(int(decision.element_id or 0)))
        return
    if decision.kind in ("scroll_down", "scroll_up"):
        delta = SCROLL_PIXELS if decision.kind == "scroll_down" else -SCROLL_PIXELS
        _js(driver, target, scroll_js(delta))
        return
    if decision.kind == "navigate" and observation.navigate_url:
        _js(driver, target, f"location.href={json.dumps(observation.navigate_url)};")
        return
    if decision.kind == "wait":
        time.sleep(1.0)
        return
    raise ValueError(f"no browser executor for {decision.kind!r}")


def _run_one(
    driver: Driver,
    target: Target,
    goal: str,
    chooser: Any,
    *,
    act: bool,
    max_steps: int,
    min_confidence: float,
    settle: float,
    writer: Any = None,
) -> Result:
    """One goal, one Jev loop. No planning here — see run()."""
    started = time.perf_counter()
    result = Result(goal=goal)
    history: list[str] = []
    can_write = writer is not None and writer.available

    for index in range(max_steps):
        observation = snapshot(driver, target, goal, can_write=can_write)
        result.snapshots += 1
        result.url = observation.url

        decision = validate(chooser.choose(goal, observation, history), observation)
        if not decision.accepted:
            result.steps.append(
                Step(index, decision, observation, False, f"refused: {decision.rejection}")
            )
            result.outcome = "refused"
            break
        if decision.kind in ("done", "impossible"):
            result.steps.append(Step(index, decision, observation, False, decision.kind))
            result.outcome = decision.kind
            break
        if decision.confidence < min_confidence:
            note = f"low confidence {decision.confidence:.2f} < {min_confidence:.2f}"
            result.steps.append(Step(index, decision, observation, False, note))
            result.outcome = "low_confidence"
            break

        label = (observation.by_id(decision.element_id or "") or {}).get("description", "")

        # A text field is chosen by Jev; the string is written by the helper,
        # because Jev cannot generate one.
        text: str | None = None
        if decision.kind == "type_text":
            if writer is None:
                result.steps.append(
                    Step(index, decision, observation, False,
                         f"refused: type_text needs a text model (field {label})")
                )
                result.outcome = "refused"
                break
            text = writer.write(goal, label, observation.title)
            if text is None:
                result.steps.append(
                    Step(index, decision, observation, False,
                         f"refused: the writer declined to fill {label}")
                )
                result.outcome = "refused"
                break
            label = f'{label} <- {text!r}'

        note = f"{decision.kind} {label} (conf {decision.confidence:.2f})"
        if act:
            before = observation.url
            if decision.kind == "type_text" and text is not None:
                type_into(driver, target, decision.element_id or "0", text)
            else:
                execute(driver, target, decision, observation)
            if decision.kind in ("click_element", "navigate"):
                deadline = time.perf_counter() + settle
                while time.perf_counter() < deadline:
                    try:
                        if snapshot(driver, target, goal, can_write).url != before:
                            break
                    except DriverError:
                        pass
                    time.sleep(0.2)
            note = f"acted: {note}"
        else:
            note = f"would: {note}"

        result.steps.append(Step(index, decision, observation, act, note))
        history.append(f"{decision.kind} {label}".strip())

    result.seconds = time.perf_counter() - started
    result.url = result.url or target.url
    return result


def plan_from(result: Result) -> list[dict[str, str]]:
    """Reduce a successful run to a replayable plan.

    Stores the element's DESCRIPTION, never the ref: refs are snapshot-scoped
    integers that mean something different on the next page load. A description is
    re-resolved against a fresh snapshot at replay time.
    """
    plan: list[dict[str, str]] = []
    for step in result.steps:
        if not step.executed:
            continue
        decision = step.decision
        if decision.kind == "click_element":
            found = step.observation.by_id(decision.element_id or "")
            if found is None:
                return []
            plan.append({"kind": "click_element", "target": found["description"]})
        elif decision.kind == "type_text":
            found = step.observation.by_id(decision.element_id or "")
            if found is None:
                return []
            plan.append({"kind": "type_text", "target": found["description"]})
        elif decision.kind in ("scroll_down", "scroll_up"):
            plan.append({"kind": decision.kind, "target": ""})
    return plan


def replay(
    driver: Driver,
    target: Target,
    goal: str,
    plan: list[dict[str, str]],
    *,
    act: bool = False,
    settle: float = 3.0,
    writer: Any = None,
    observation: Observation | None = None,
) -> Result:
    """Execute a cached plan against one fresh snapshot. No model calls.

    A description that no longer resolves ABORTS rather than guessing — a stale plan
    is worse than no plan.
    """
    started = time.perf_counter()
    result = Result(goal=goal)
    can_write = writer is not None and writer.available

    if observation is None:
        observation = snapshot(driver, target, goal, can_write=can_write)
    result.snapshots = 1
    result.url = observation.url

    for index, entry in enumerate(plan):
        kind = entry.get("kind", "")
        wanted = entry.get("target", "")

        if kind in ("scroll_down", "scroll_up"):
            decision = Decision(kind=kind, confidence=1.0, source="cache")
        else:
            found = next(
                (c for c in observation.candidates if c["description"] == wanted), None
            )
            if found is None:
                result.steps.append(
                    Step(index, Decision(kind=kind, source="cache"), observation, False,
                         f"replay_miss: no element described {wanted!r}")
                )
                result.outcome = "replay_miss"
                result.seconds = time.perf_counter() - started
                return result
            decision = Decision(
                kind=kind, element_id=found["id"], confidence=1.0, source="cache"
            )

        label = wanted or kind
        text: str | None = None
        if kind == "type_text":
            if writer is None:
                result.steps.append(
                    Step(index, decision, observation, False, "replay_miss: no writer")
                )
                result.outcome = "replay_miss"
                result.seconds = time.perf_counter() - started
                return result
            text = writer.write(goal, wanted, observation.title)
            if text is None:
                result.steps.append(
                    Step(index, decision, observation, False, f"replay_miss: writer declined {wanted}")
                )
                result.outcome = "replay_miss"
                result.seconds = time.perf_counter() - started
                return result
            label = f"{wanted} <- {text!r}"

        note = f"cache: {kind} {label}"
        if act:
            before = observation.url
            if kind == "type_text" and text is not None:
                type_into(driver, target, decision.element_id or "0", text)
            elif kind in ("scroll_down", "scroll_up"):
                execute(driver, target, decision, observation)
            else:
                execute(driver, target, decision, observation)
            if kind in ("click_element", "navigate"):
                deadline = time.perf_counter() + settle
                while time.perf_counter() < deadline:
                    try:
                        fresh = snapshot(driver, target, goal, can_write)
                        result.snapshots += 1
                        if fresh.url != before:
                            observation = fresh
                            result.url = fresh.url
                            break
                    except DriverError:
                        pass
                    time.sleep(0.2)
            note = f"cache: acted {label}"

        result.steps.append(Step(index, decision, observation, act, note))

    result.outcome = "replayed"
    result.seconds = time.perf_counter() - started
    return result


def run(
    driver: Driver,
    target: Target,
    goal: str,
    chooser: Any,
    *,
    act: bool = False,
    max_steps: int = 10,
    min_confidence: float = 0.4,
    settle: float = 3.0,
    writer: Any = None,
    decompose: bool = True,
    cache: Any = None,
) -> Result:
    """Drive the browser toward a goal, optionally planning first.

    With a `cache` (a `cache.PlanCache`), a goal that already succeeded against this
    page is replayed from the stored plan: one snapshot and zero model calls.

    Jev decides one action at a time from the current state and cannot hold a plan,
    so a compound goal is split into ordered subgoals before it starts. That is the
    measured accuracy fix: "compute 7 times 8" made it press `7, 8, x, =`, while
    "press 7" is unambiguous. With no text model configured the goal is used
    unchanged and everything still works.
    """
    started = time.perf_counter()
    start_url = target.url

    if cache is not None:
        # One snapshot buys an exact cache key. Keying on the goal alone would let a
        # plan recorded on one page replay against a different page that happens to
        # share the goal. The replay needs this snapshot anyway, so it costs nothing.
        can_write = writer is not None and writer.available
        probe = snapshot(driver, target, goal, can_write=can_write)
        # The key must be the page the plan STARTS from. Storing under the URL the
        # run ended on means the next run — which starts where this one began — never
        # matches, and the cache silently never hits.
        start_url = probe.url
        stored = cache.get(f"{start_url}|{goal}", goal)
        if stored:
            replayed = replay(
                driver, target, goal, stored, act=act, settle=settle, writer=writer,
                observation=probe,
            )
            if replayed.outcome == "replayed":
                return replayed
            cache.drop(f"{start_url}|{goal}", goal)  # stale: fall through and re-plan

    if writer is None or not decompose:
        result = _run_one(
            driver, target, goal, chooser, act=act, max_steps=max_steps,
            min_confidence=min_confidence, settle=settle, writer=writer,
        )
        if cache is not None and act and result.outcome == "done":
            cache.put(f"{start_url}|{goal}", goal, plan_from(result))
        return result

    summary = ""
    try:
        summary = read(driver, target)
    except DriverError:
        pass
    subgoals = writer.decompose(goal, summary)

    if len(subgoals) < 2:
        return _run_one(
            driver, target, goal, chooser, act=act, max_steps=max_steps,
            min_confidence=min_confidence, settle=settle, writer=writer,
        )

    combined = Result(goal=goal)
    remaining = max_steps
    for subgoal in subgoals:
        if remaining <= 0:
            break
        part = _run_one(
            driver, target, subgoal, chooser, act=act, max_steps=remaining,
            min_confidence=min_confidence, settle=settle, writer=writer,
        )
        combined.steps.extend(part.steps)
        combined.snapshots += part.snapshots
        combined.url = part.url
        remaining -= max(1, len(part.steps))
        combined.outcome = part.outcome
        if part.outcome not in ("done",):
            break

    combined.seconds = time.perf_counter() - started
    combined.subgoals = subgoals
    if cache is not None and act and combined.outcome == "done":
        cache.put(f"{start_url}|{goal}", goal, plan_from(combined))
    return combined
