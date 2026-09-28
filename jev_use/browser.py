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

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from . import host
from .choosers import Decision, deterministic_decision, validate
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
  // Clear the previous scan's markers FIRST. Without this, an element that was
  // tagged last time but is filtered this time keeps its old index, which can
  // collide with a fresh one — and click_js, which looks a ref up with
  // querySelector, would then find the stale element first and act on the wrong
  // node.
  document.querySelectorAll('[data-jev-ref]').forEach((old) => old.removeAttribute('data-jev-ref'));
  // Accepted ancestors are tracked in a Set, so nothing is written to the DOM until
  // a candidate is final and the nested-duplicate check never consults a marker.
  const accepted = new Set();
  const out = [];
  let i = 0;
  for (const e of Array.from(document.querySelectorAll(sel))) {
    const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.top > innerHeight) continue;
    const style = getComputedStyle(e);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const text = (e.innerText || e.value || e.getAttribute('aria-label')
             || e.getAttribute('placeholder') || e.getAttribute('name')
             || e.getAttribute('href') || '').trim().replace(/\s+/g, ' ').slice(0, 90);
    const tag = e.tagName.toLowerCase();
    // A link with no label and no real destination is decoration, not a target.
    const href = e.getAttribute('href') || '';
    if (tag === 'a' && !text && (!href || href === '#' || href.startsWith('javascript:'))) continue;
    // A control nested inside an already-accepted control with the same label is
    // the same target twice (an icon inside its button, a span inside its link).
    // Offering both splits the confidence vote for no gain. Walk to the nearest
    // accepted ancestor; only that one is compared.
    let anc = e.parentElement, nested = false;
    while (anc) {
      if (accepted.has(anc)) {
        const ptext = (anc.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 90);
        if (ptext && ptext === text) nested = true;
        break;
      }
      anc = anc.parentElement;
    }
    if (nested) continue;
    // A coarse position, so two genuinely different controls that happen to share
    // a label can be told apart without dropping either one.
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const pos = (cy < innerHeight / 3 ? 'top' : cy > 2 * innerHeight / 3 ? 'bottom' : 'middle')
              + '-' + (cx < innerWidth / 3 ? 'left' : cx > 2 * innerWidth / 3 ? 'right' : 'centre');
    e.setAttribute('data-jev-ref', String(i));
    accepted.add(e);
    out.push({
      ref: i,
      tag: tag,
      fillable: e.matches('input,textarea,select') || e.isContentEditable === true,
      enabled: !e.disabled && e.getAttribute('aria-disabled') !== 'true',
      pos: pos,
      text: text,
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


def navigate_js(url: str) -> str:
    """Navigate, clearing any `beforeunload` handler first.

    A page can register `onbeforeunload`; a navigation that triggers its prompt
    blocks `Runtime.evaluate` until the dialog is answered, and nothing here answers
    it — the call then hangs until the harness times out. Clearing the handler just
    before navigating suppresses a prompt we would have had to accept anyway, and
    changes nothing else about the page we are leaving.
    """
    return (
        "(() => {"
        "try { window.onbeforeunload = null; } catch (_) {}"
        f"location.href = {json.dumps(url)};"
        "return JSON.stringify({ok:true});"
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

#: Cap on the option set, matching the Android engine. Past this the choice stops
#: being meaningful and the confidence score stops being readable. Reached only on
#: very large pages, and only after the goal-relevant controls are kept.
MAX_CANDIDATES = 120


def _goal_terms(goal: str) -> set[str]:
    """Words worth matching a control's label against, ignoring short filler."""
    return {t for t in re.findall(r"[a-z0-9]+", (goal or "").lower()) if len(t) >= 3}


def _rank_candidates(
    candidates: list[dict[str, Any]], goal: str = ""
) -> list[dict[str, Any]]:
    """Make the option set executable, mutually exclusive, and bounded.

    Three rules:

    * **Disabled controls are dropped.** A disabled button cannot be activated, so
      offering it is a choice the run can only refuse — and, once picked, a wasted
      step. `TABLE_JS` records `enabled` for exactly this decision.
    * **Duplicates are disambiguated, not dropped.** A repeated label gets a coarse
      positional hint so both stay selectable; dropping one would remove a real
      target. This mirrors the desktop engine, which appends a position for exactly
      this reason.
    * **The set is capped, goal-first.** A page can offer hundreds of controls, and
      the Android engine caps at 120 for the same reason. The list is reordered only
      when the cap is actually exceeded — by fillable, labelled, and overlap with the
      goal — so an ordinary page keeps its natural order.
    """
    candidates = [c for c in candidates if c.get("enabled", True)]

    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate["description"]] = counts.get(candidate["description"], 0) + 1
    for candidate in candidates:
        if counts[candidate["description"]] > 1:
            hint = candidate.get("pos") or f'element {candidate["id"]}'
            candidate["description"] = f'{candidate["description"]} ({hint})'

    if len(candidates) <= MAX_CANDIDATES:
        return candidates

    terms = _goal_terms(goal)

    def score(candidate: dict[str, Any]) -> int:
        value = 0
        if candidate["fillable"]:
            value += 4
        if candidate["label"].strip():
            value += 2
        if candidate["enabled"]:
            value += 1
        label = candidate["label"].lower()
        if terms and any(term in label for term in terms):
            value += 8
        return value

    # Stable: candidates of equal score keep document order.
    return sorted(candidates, key=score, reverse=True)[:MAX_CANDIDATES]


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
        goal: str = "",
    ):
        self.can_write = can_write
        self.target = target
        self.url = payload.get("url") or ""
        self.title = payload.get("title") or ""
        self.navigate_url = navigate_url
        self.pid = target.pid
        self.window_id = target.window_id
        self._candidates = _rank_candidates(
            [
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
                    "enabled": bool(el.get("enabled", True)),
                    "pos": el.get("pos", ""),
                    "frame": None,
                }
                for el in payload.get("elements", [])
            ],
            goal,
        )
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

    def fingerprint(self) -> str:
        """A stable signature of the controls a plan could act on — the cache key.

        Deliberately the interactive structure (control roles and labels) and not
        page text: a clock, a cart count or a relative timestamp would change it on
        every visit and turn every replay into a miss. Two visits to the same page
        with the same controls are the same plan target, which is exactly the
        condition under which a stored plan is still valid.
        """
        basis = sorted(f'{c["role"]}|{c["label"]}' for c in self._candidates)
        digest = hashlib.sha1("\n".join(basis).encode("utf-8")).hexdigest()[:12]
        return f"{self.url}|{self.title}|{digest}"


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
    return Observation(
        target,
        payload,
        match.group(0) if match else None,
        can_write=can_write,
        goal=goal,
    )


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


# -- settling ---------------------------------------------------------------

# The settle poll asks exactly one question — "has the page moved?" — so it must
# not rebuild the candidate table to ask it. `TABLE_JS` is ~150 ms of
# querySelectorAll, getComputedStyle and a DOM write per element; the old poll ran
# it on every iteration and threw away everything except `.url`. This probe is a
# few milliseconds. `text` is the *length* of the visible text, so a text-only
# update is visible without shipping the text itself.
SETTLE_JS = (
    "JSON.stringify({url: location.href, state: document.readyState, "
    "nodes: document.getElementsByTagName('*').length, "
    "text: document.body ? document.body.innerText.length : 0, "
    # `fields` is the summed length of the values in every form control. Typing
    # changes an input's `.value`, which never appears in `body.innerText`, so
    # without this a normal field entry looked like "no change" and settling after
    # typing burned the whole timeout.
    "fields: (() => { let n = 0; "
    "for (const f of document.querySelectorAll('input,textarea,select')) "
    "{ if (f.value) n += f.value.length + 1; } return n; })()})"
)

#: How many consecutive quiet probes are required *after* a change is seen. Each
#: probe is ~200 ms apart, so this is the quiet window in units of 200 ms. One was
#: too short: a spinner or a second async update can land well after 200 ms, so the
#: page looks quiet and then moves again.
SETTLE_QUIET_PROBES = 3


class PageSignature(NamedTuple):
    """The cheap, candidate-free reading the settle poll compares."""

    url: str
    state: str
    nodes: int
    text: int
    fields: int


def _page_signature(driver: Driver, target: Target) -> PageSignature:
    """A cheap reading of the page, without building the candidate table."""
    payload = _parse_js_payload(_js(driver, target, SETTLE_JS))
    return PageSignature(
        payload.get("url") or "",
        payload.get("state") or "",
        int(payload.get("nodes") or 0),
        int(payload.get("text") or 0),
        int(payload.get("fields") or 0),
    )


def _dom_changed(before: PageSignature, now: PageSignature) -> bool:
    """Whether the DOM part of a signature moved (ignores url and readyState)."""
    return (now.nodes, now.text, now.fields) != (before.nodes, before.text, before.fields)


def _wait_for_settle(
    driver: Driver, target: Target, before: PageSignature, settle: float
) -> str | None:
    """Wait for the page to move after an action, cheaply.

    `before` is the signature taken immediately before the action. Returns the new
    URL when one was seen, else `None`.

    The wait ends early, but only on evidence:

    * the page must first CHANGE — a URL change or a DOM change (element count +
      text length) — and then
    * hold still for `SETTLE_QUIET_PROBES` consecutive probes, and
    * be fully loaded: `readyState == "complete"`, not merely `!= "loading"`.
      `interactive` fires while scripts and data are still arriving, so accepting it
      would settle a page that has not finished becoming itself.

    Requiring the change FIRST is the whole point, and it is the bug the first cut
    of this function had. Stillness on its own is not evidence: a click that fires
    an async request leaves the page perfectly still for the first few hundred
    milliseconds, so treating "two equal counts" as settled handed Jev the
    pre-update page.

    The `readyState` guard is the second bug: returning the instant the URL changed
    handed Jev a half-loaded document. A client-side route change keeps `complete`
    (the document never reloads) and is governed by the DOM-quiet rule instead.
    """
    deadline = time.perf_counter() + settle
    changed = False
    quiet = 0
    previous: PageSignature | None = None
    while time.perf_counter() < deadline:
        try:
            signature = _page_signature(driver, target)
        except DriverError:
            time.sleep(0.2)
            continue
        if signature.url and signature.url != before.url:
            changed = True
        if _dom_changed(before, signature):
            changed = True
        # Only `complete` is settled. `loading` and `interactive` both mean the
        # document is still arriving, so nothing is settled until they end.
        if changed and signature.state == "complete":
            if previous is not None and not _dom_changed(previous, signature):
                quiet += 1
                if quiet >= SETTLE_QUIET_PROBES:
                    return signature.url if signature.url != before.url else None
            else:
                quiet = 0
            previous = signature
        time.sleep(0.2)
    return None


def navigate_and_settle(driver: Driver, target: Target, url: str, settle: float) -> None:
    """Point the page at `url` and wait for it to actually arrive.

    The baseline signature is read from the LIVE page immediately before navigating.
    Comparing against the target's cached `url` was wrong: it can be stale — the page
    may have changed outside Jev between calls — or empty on a freshly attached
    target, in which case the first probe already looked different and the wait
    returned before the requested page had loaded.
    """
    before = _page_signature(driver, target)
    _js(driver, target, navigate_js(url))
    _wait_for_settle(driver, target, before, settle)


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


def _action_result(driver: Driver, target: Target, javascript: str) -> dict[str, Any]:
    """The parsed result of an action script.

    Only an UNPARSEABLE reply is tolerated, and only because it has a benign cause:
    a click that navigates can unload the page out from under the eval, so there is
    no JSON left to read.

    A `DriverError` from the driver call itself — the CDP connection failed, the
    driver refused, the child exited — is a real failure and must propagate. Catching
    it here would report a broken driver as a completed action, which is worse than
    the ignored `ok:false` this replaced.
    """
    raw = _js(driver, target, javascript)
    try:
        return _parse_js_payload(raw)
    except DriverError:
        return {"ok": True}


def type_into(driver: Driver, target: Target, ref: str, text: str) -> None:
    result = _action_result(driver, target, type_js(int(ref), text))
    if not result.get("ok"):
        raise DriverError(f"type failed: {result.get('reason', 'unknown')}")


def execute(driver: Driver, target: Target, decision: Decision, observation: Observation) -> None:
    if decision.kind == "type_text":
        raise ValueError("type_text needs the text resolved first; use run()")
    if decision.kind == "click_element":
        result = _action_result(driver, target, click_js(int(decision.element_id or 0)))
        if not result.get("ok"):
            raise DriverError(f"click failed: {result.get('reason', 'unknown')}")
        return
    if decision.kind in ("scroll_down", "scroll_up"):
        delta = SCROLL_PIXELS if decision.kind == "scroll_down" else -SCROLL_PIXELS
        _js(driver, target, scroll_js(delta))
        return
    if decision.kind == "navigate" and observation.navigate_url:
        _js(driver, target, navigate_js(observation.navigate_url))
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
    observation: Observation | None = None,
) -> Result:
    """One goal, one Jev loop. No planning here — see run().

    `observation` seeds the first step: `run()` may already hold a fresh snapshot
    (the one it took to build the cache key), and re-taking it would be a wasted
    ~150 ms. It is consumed once and every later step re-observes.
    """
    started = time.perf_counter()
    result = Result(goal=goal)
    history: list[str] = []
    can_write = writer is not None and writer.available
    pending = observation

    for index in range(max_steps):
        if pending is not None:
            observation = pending
            pending = None
        else:
            observation = snapshot(driver, target, goal, can_write=can_write)
        result.snapshots += 1
        result.url = observation.url

        # First step, unambiguous goal: decide it in code rather than paying for a
        # model round trip. Later steps always go to the chooser.
        fixed = deterministic_decision(goal, observation) if index == 0 else None
        chosen = fixed if fixed is not None else chooser.choose(goal, observation, history)
        decision = validate(chosen, observation)
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
            # Typing is settled too: validation, autocomplete, a submit handler or a
            # same-URL React update can all follow a keystroke, and the next step's
            # snapshot would otherwise race them. Scroll and wait are not.
            settles = decision.kind in ("click_element", "navigate", "type_text")
            # The baseline is taken BEFORE the action, and only when it will be used:
            # waiting against a pre-action signature is what lets the poll tell "the
            # page changed" from "the page has not started changing yet".
            before = _page_signature(driver, target) if settles else None
            try:
                if decision.kind == "type_text" and text is not None:
                    type_into(driver, target, decision.element_id or "0", text)
                else:
                    execute(driver, target, decision, observation)
            except DriverError as exc:
                result.steps.append(
                    Step(index, decision, observation, False, f"failed: {exc}")
                )
                result.outcome = "action_failed"
                break
            if settles:
                # Poll a cheap signal, never the candidate table. The next step
                # re-observes anyway, so the poll only decides how long to wait.
                _wait_for_settle(driver, target, before, settle)
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
            # Resolve only within the elements LEGAL for this operation, so a cached
            # click cannot land on a non-clickable element or typing on a field.
            legal = observation.targets_for(kind)
            matches = [c for c in legal if c["description"] == wanted]
            if len(matches) != 1:
                # Exactly one, or it is not replayable. Zero means the target is gone;
                # more than one means the description is ambiguous and silently
                # taking the first could act on the wrong control.
                reason = (
                    f"nothing described {wanted!r}"
                    if not matches
                    else f"{len(matches)} elements match {wanted!r}"
                )
                result.steps.append(
                    Step(index, Decision(kind=kind, source="cache"), observation, False,
                         f"replay_miss: {reason}")
                )
                result.outcome = "replay_miss"
                result.seconds = time.perf_counter() - started
                return result
            decision = Decision(
                kind=kind, element_id=matches[0]["id"], confidence=1.0, source="cache"
            )

        # A cached action passes the same gate a live one does.
        decision = validate(decision, observation)
        if not decision.accepted:
            result.steps.append(
                Step(index, decision, observation, False, f"replay_miss: {decision.rejection}")
            )
            result.outcome = "replay_miss"
            result.seconds = time.perf_counter() - started
            return result

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
            has_next = index < len(plan) - 1
            # A state-changing action is settled even when it is LAST, so a final
            # click does not return `replayed` while the transition is still underway.
            # Only the expensive re-dump is skipped when nothing later needs it.
            settles = kind in ("click_element", "navigate", "type_text")
            before = _page_signature(driver, target) if settles else None
            try:
                if kind == "type_text" and text is not None:
                    type_into(driver, target, decision.element_id or "0", text)
                else:
                    execute(driver, target, decision, observation)
            except DriverError as exc:
                # A stale plan whose click no longer lands must abort, not report
                # success. run() drops the entry and re-plans from a fresh snapshot.
                result.steps.append(
                    Step(index, decision, observation, False, f"replay_miss: {exc}")
                )
                result.outcome = "replay_miss"
                result.seconds = time.perf_counter() - started
                return result
            if settles:
                # The settle is cheap (three ~5 ms probes); the returned URL keeps the
                # report honest for a final action, which gets no snapshot.
                moved = _wait_for_settle(driver, target, before, settle)
                if moved:
                    result.url = moved
            # Re-observe before the next entry, whatever the URL did. A same-URL
            # in-place change leaves the previous observation stale, so the next
            # description would resolve against elements that no longer exist or
            # now mean something else.
            if has_next:
                observation = snapshot(driver, target, goal, can_write)
                result.snapshots += 1
                result.url = observation.url
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
    start_key = f"{target.url}|{goal}"
    probe: Observation | None = None

    if cache is not None:
        # One snapshot buys an exact cache key. Keying on the goal alone would let a
        # plan recorded on one page replay against a different page that happens to
        # share the goal. The replay needs this snapshot anyway, so it costs nothing
        # — and on a miss it seeds the first step rather than being thrown away.
        can_write = writer is not None and writer.available
        probe = snapshot(driver, target, goal, can_write=can_write)
        # The key is the fingerprint of the page the plan STARTS from: URL, title and
        # the interactive structure. Storing under the URL the run ended on means the
        # next run — which starts where this one began — never matches, and the cache
        # silently never hits.
        start_key = f"{probe.fingerprint()}|{goal}"
        stored = cache.get(start_key, goal)
        if stored:
            replayed = replay(
                driver, target, goal, stored, act=act, settle=settle, writer=writer,
                observation=probe,
            )
            if replayed.outcome == "replayed":
                return replayed
            cache.drop(start_key, goal)  # stale: fall through and re-plan
            # The replay may have executed part of the plan before the miss, so the
            # probe describes a screen that no longer exists. Re-observing is the only
            # safe start for the re-plan.
            probe = None

    if writer is None or not decompose:
        result = _run_one(
            driver, target, goal, chooser, act=act, max_steps=max_steps,
            min_confidence=min_confidence, settle=settle, writer=writer,
            observation=probe,
        )
        if cache is not None and act and result.outcome == "done":
            cache.put(start_key, goal, plan_from(result))
        return result

    # `read()` above waited for the page to render, so the cache probe — taken before
    # that wait, and before the planner ran — may predate the very controls the plan
    # will act on. The decomposition path starts from a FRESH observation rather than
    # that stale one. (The probe is still reused on the non-decompose path below,
    # where nothing waited on the page.)
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
        cache.put(start_key, goal, plan_from(combined))
    return combined
