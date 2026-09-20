# jev-use

**Browser use where Jev makes the decisions.**

```
browser_profiles   which Chrome is open, and whether CDP works
browser_use        drive the page toward a goal; Jev picks each action
browser_read       return the page's text so your agent can answer questions
```

Any harness — Command Code, Claude Code, Cursor, Codex — can call these over MCP.
The harness brings the reasoning, this server brings the browser, Jev brings the
decisions. The model never invents an action: it picks one id from a set this server
built from the live DOM, and the choice is validated against the snapshot it came
from before anything is dispatched.

## Setup

Works on Linux, macOS and Windows. Two steps, once — plus the driver.

**Linux / macOS**

```bash
uv sync                       # or: pip install -e .
cp .env.example .env          # then set TYPESAFE_API_KEY

cmd mcp add --scope user jev-use -- "$PWD/.venv/bin/python" -m jev_use.mcp_server

# the /browser-use entry point (Command Code surfaces skills, not MCP prompts)
mkdir -p ~/.commandcode/skills/browser-use
cp skills/browser-use/SKILL.md ~/.commandcode/skills/browser-use/SKILL.md
```

**Windows (PowerShell)**

```powershell
uv sync
Copy-Item .env.example .env    # then set TYPESAFE_API_KEY

cmd mcp add --scope user jev-use -- "$PWD\.venv\Scripts\python.exe" -m jev_use.mcp_server

New-Item -ItemType Directory -Force "$env:USERPROFILE\.commandcode\skills\browser-use" | Out-Null
Copy-Item skills\browser-use\SKILL.md "$env:USERPROFILE\.commandcode\skills\browser-use\SKILL.md"
```

**The driver, once per machine.** It is a separate Rust binary and it has a
*native installer per platform* — `install.sh` has no Windows handling at all,
which is why handing a Windows user the curl command is a dead end:

```bash
# Linux / macOS
/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -c "irm https://cua.ai/driver/install.ps1 | iex"
```

`jev_use.driver.find_driver()` resolves it through `CUA_DRIVER_COMMAND`, then
PATH, then the directory each installer actually uses — which matters on
Windows, where `install.ps1` appends to the *User* PATH and a running process
cannot see that change.

Where each platform keeps Chrome's profile, and the one-time launcher:

| | profile root | launcher |
|---|---|---|
| Linux | `~/.config/google-chrome` | `scripts/enable-cdp.sh` |
| macOS | `~/Library/Application Support/Google/Chrome` | `scripts/enable-cdp.sh` |
| Windows | `%LOCALAPPDATA%\Google\Chrome\User Data` | `scripts/enable-cdp.ps1` |

All of that lives in `jev_use/host.py` — the single file that knows the
difference between the three. Everything else in `jev_use` is platform-agnostic.

**Why the third step copies instead of using your profile in place.** Chrome 153
refuses to open a remote-debugging port on the default data directory:

```
$ google-chrome --remote-debugging-port=9222 --user-data-dir=~/.config/google-chrome
DevTools remote debugging requires a non-default data directory.
```

That is a security change (a CDP port on your default profile is a cookie-theft
primitive) and it means the obvious approach is impossible — Chrome exits immediately.
The supported route is a **copy in a non-default location**, which is verified to
work. Your cookies travel with the copy so the logins are there, but the copy is a
**snapshot**: a sign-in made to the original afterwards does not reach it. That is why
`browser_open` takes `refresh`.

Separately, even with a CDP port, Cua's platform matrix lists existing-profile
attachment as proven for Chrome on Linux X11, macOS, Windows and Sway — **not
GNOME/Mutter**. That is why the engine is built on the driver's `page` tool with an
explicit `cdp_port` (see *Why `page`*) rather than on `browser_prepare`.

## Use

In any chat, either name the tool or use the prompt:

> Use jev-use `browser_use` to open the usage page, then `browser_read` it.

> `/browser-use check my Command Code usage and Cloudflare R2 billing`

`/browser-use` comes from the **skill** installed at
`~/.commandcode/skills/browser-use/`, because Command Code surfaces skills as slash
commands but does not surface MCP prompts. The server also exposes the same guidance
as the MCP prompt `browser-use` for harnesses that do support prompts.

Either way the guidance is identical: discover → open if needed → drive → **read** →
report, with an explicit instruction to call `browser_open` when nothing is drivable
rather than stopping or silently substituting a different browser.

`browser_use` orbits the page; `browser_read` is what answers a question about what a
page says. Calling only `browser_use` and expecting a summary is the mistake that made
a real task look like it "wasn't doing anything".

```bash
# dry run — decides and validates, clicks nothing
python -m jev_use.mcp_server   # normally driven by the harness, not by hand
```

Verified end to end on a CDP-enabled Chrome:

```
browser_use(goal="open the link to iana.org", act=true)
  > acted: click_element a "Learn more" (conf 0.96)
    done
  url=https://www.iana.org/help/example-domains
  outcome=done actions=1 snapshots=2 seconds=3.88

browser_read()
  url=https://www.iana.org/help/example-domains
  Domains / Protocols / Numbers / About ...
  As described in RFC 2606 and RFC 6761, a number of domains such as ...
```

## Why `page`

The driver has two browser surfaces. The typed `browser_*` tools need
`browser_prepare`, which needs a provable accessibility binding; a long-running Chrome
on GNOME/Wayland exposes **one** accessibility element where a fresh one exposes ~160,
so it refuses with `browser_binding_ambiguous` however many times you retry.

`page` takes an **explicit `cdp_port`**. No window binding, no accessibility tree, no
existing-profile grant, no compositor cooperation.

**One consequence you should know about.** `page` mutations are gated on the driver's
**unrestricted** permission mode with launch-time risk acceptance. Setting only
`CUA_DRIVER_ENABLE_LEGACY_PAGE_MUTATIONS=1` is *not* enough — measured, the driver
answers *"legacy page mutation is unbounded and requires unrestricted mode with trusted
launch-time risk acceptance"*. `jev_use/driver.py` therefore sets both.

Containment: this server spawns its own `cua-driver mcp` per tool call and owns its
stdio, so unrestricted applies to a process that only ever serves these three browser
tools and that nothing else can reach. It is still a real widening — the driver would
no longer refuse a dangerous action. That is why every script we send is generated by
us and **page text is never interpolated into it**.

## Speed

Measured on this machine:

```
snapshot (one execute_javascript)      ~150 ms
click    (one execute_javascript)      ~600 ms
AT-SPI accessibility path, for comparison   567-750 / 1350 ms
```

A Jev decision is ~200-400 ms. The driver's MCP protocol floor is 4 ms, so almost all
of the cost is the work itself, not transport.

* **Poll, never sleep.** After a click we poll for the URL to change, bounded. A
  snapshot is 150 ms, so this returns the moment the page moves instead of burning a
  fixed settle.
* **Page-scoped candidates.** A DOM snapshot returns page elements, not browser chrome.
  On `example.com` the accessibility path offered 29 targets, **28 of them toolbar
  buttons**; the DOM offers the page.

### Things that look like optimisations and are not

* **`semantic_v2` snapshots are 7x slower** (1537 ms vs 210 ms). Use the default.
* **Tighter AX bounds are slower**, not faster: `max_elements=60` took 1309 ms and
  returned 1 element against 736 ms and 29 elements for the default.

## Getting answers out of a page

`browser_read` returns raw text — 10–20 KB for a dashboard, which your model then has
to summarize into prose. `browser_extract` asks instead, and returns values:

```json
browser_extract({
  "questions": {
    "logged_out": {"type":"noul",  "instructions":"Is this a sign-in page?"},
    "runs":       {"type":"score", "instructions":"How many total runs are reported?",
                   "criteria":["none","under 100","100 to 1000","over 1000"]},
    "pressure":   {"type":"score", "instructions":"How close is the most-used limit to exhaustion?",
                   "criteria":["under 25%","25-50%","50-90%","over 90%"]},
    "model":      {"type":"choice","instructions":"Which model is in the history?",
                   "criteria":{"deepseek":"deepseek-v4-flash","muse":"muse-spark","other":"other"}}
  }
})
```

Measured against a real usage page — every answer correct, **1.31 s** for four
questions including process startup:

```
logged_out:      0.03                  not a sign-in page
runs_bucket:     2.0  conf 1.00        100 to 1000   (251 runs)
limit_pressure:  0.1  conf 0.90        under 25%     (19% on the 5-hour limit)
most_used:       deepseek  conf 0.96
```

All questions are evaluated in parallel in one call, so six cost about what one does.
It is also more trustworthy than summarizing: **Jev cannot generate a number it did
not read**, so a figure in the answer came from the page.

## Repeating a task costs nothing

A successful run is reduced to a plan of element *descriptions* — never refs, which
are snapshot-scoped integers — and cached against the page it started from. The next
identical run replays it with **one snapshot and zero model calls**:

```
RUN 1 (plans)    outcome=done      actions=1 snapshots=2   7.17s
RUN 2 (replays)  outcome=replayed  actions=1 snapshots=2   0.47s
```

Keyed on `url|goal`, so the same goal on a different page cannot replay the wrong
plan. A description that no longer resolves aborts rather than guessing, and drops
the entry. `use_cache: false` disables it.

## Planning and typing

Jev returns typed decisions and never generates strings. That is the point, and it
leaves exactly two gaps. One optional OpenAI-compatible endpoint
(`JEV_USE_TEXT_MODEL`, works with OpenRouter / Ollama / vLLM / llama.cpp / anything
speaking `/chat/completions`) fills both:

**Planning fixes accuracy.** Jev decides one action at a time from the current state
and cannot hold a plan, which is why "compute 7 times 8" made it press `7, 8, ×, =`.
A compound goal is now split into ordered subgoals before the loop starts, and each
subgoal is unambiguous on its own. That is the same fix the shipping projects use —
paulsmith splits compound goals first, vinilana's harness turns goals into
"verifiable subgoals". `decompose: false` turns it off for a goal that is already one
step.

**Typing becomes possible.** `type_text` fills a form field: Jev chooses *which*
field from a text-only target head, and the helper writes the string. The JS goes
through the prototype's native value setter and then dispatches `input` and `change`,
because setting `.value` alone is invisible to React and friends.

Both degrade gracefully. With no text model configured, the goal is used unchanged
and `type_text` is **not offered at all** — offering an operation the model can never
satisfy would just create an impossible choice.

## Reading a page

`browser_read` **waits for the page to arrive.** That sounds trivial and is the thing
that cost a real run the most time: an agent read a still-empty SPA three times, blamed
a "stuck Production/Error filter", tried an explicit status param, and opened a
different tab — all because the read returned the shell it found.

The wait settles on the **DOM element count**, not on text and not on page length.
Getting there took three bugs, each found by measuring:

| signal | why it failed |
|---|---|
| text length | the Cloudflare nav cleared 1600 chars before one billing figure rendered |
| text stillness | a ticking clock never settles, so Vercel timed out and returned its nav |
| empty counts as settled | Cloudflare read back 0 characters |

Element count stops growing the moment data lands, and is indifferent to a clock.
Empty text is never treated as settled.

Measured on the pages that failed:

```
dash.cloudflare.com  (R2)      0 chars  ->  2062 chars, "Class A Operations"
github .../commits   0 chars  ->  3457 chars, the commit messages
vercel.com/dashboard           0 chars  ->   271 chars, the nav only
```

The Vercel landing page genuinely renders nothing but navigation until you click
through to a project — that is the page's behaviour, not a waiting bug, and it is
what `browser_use` is for.

## The decision contract

Jev is a *decision point, not a planner* — it evaluates each question against the state
alone. One `system_one` call carries two questions in parallel:

| question | type | answers |
|---|---|---|
| `operation` | Choice | click_element / type_text / scroll_up / scroll_down / wait / done / impossible (+ navigate when the goal names a URL) |
| `click_target` | Choice | one id per clickable element |
| `text_target` | Choice | one id per **fillable** field — only offered when typing is possible |

Target heads are per-operation and contain only legal elements. One shared list would
let the model pick a text field for a click, and padding an option set with illegal
choices is exactly what makes a confidence score unreadable.

Rules, all of them learned from real failures:

* **Only supported operations are offered.** With nothing clickable, `click_element` is
  withheld rather than offered with an empty target set.
* **Gate on the weakest answer.** Measured: for "compute 9 times 6" the operation came
  back at confidence **1.00** while the target distribution was `9=0.45` vs `π=0.36` —
  a coin flip the operation's confidence completely hid. `Decision.confidence` is now
  the minimum across answered questions, so that run refuses instead of misclicking.
* **The goal's URL is extracted by regex, in code** — never generated by the model.
* **`navigate` exists only when the goal contains a URL**, so it is never a free choice.
* **Free text is absent.** Jev has no string generation; a caller that needs typing
  supplies the literal.

The model is pinned to `jev-1.13.0`, not `jev-latest`, because a moving version changes
answers underneath a tuned confidence threshold.

## Design rules this code enforces

* **No page text ever reaches the injected script.** Refs are integers we generate.
  `click_js` coerces to `int` and fails closed on anything else.
* **Refs are snapshot-scoped.** `validate()` rejects any id absent from the current
  snapshot, and every step re-snapshots.
* **Fail fast with the fix.** With no CDP endpoint, tools return the exact remediation
  *and* the list of browsers found — no probing loop, no retry storm. The worst failure
  mode of the old design was discovering the problem ten turns in.
* **A driver refusal is guidance, not a stack trace.** `DriverError` is returned as
  readable text so the caller can act on it.

## Layout

```
jev_use/browser.py     the engine: discovery, snapshot, read, Jev loop
jev_use/choosers.py    JevChooser (live) and MockChooser (offline)
jev_use/driver.py      cua-driver MCP stdio client + the env contract
jev_use/mcp_server.py  the three-tool MCP surface
jev_use/profiles.py    profile registry, copy-and-launch, and the CLI
jev_use/text_model.py  the planner and writer Jev structurally cannot be
scripts/enable-cdp.sh  thin wrapper over profiles.py (--list / --open NAME)
skills/browser-use/    the same guidance as an agent skill
tests/                 92 tests
```

`jev_use/candidates.py`, `loop.py` and `cli.py` are the retired desktop surface. They
are unregistered from MCP and kept on disk only so the work is not lost; nothing in the
browser path imports them.

## Test

```bash
python -m pytest -q
```

## Limits

* **The one-time relaunch is a hard prerequisite.** Until it runs, every call fails fast.
* **`page` is a "legacy" tool.** Cua prefers the typed `browser_*` surface and notes the
  legacy path "does not provide the typed browser surface's exact binding or
  existing-profile grant guarantees" — which is precisely why it works here. If Cua
  removes `page`, this engine needs rewriting onto direct CDP.
* Clicking, scrolling and navigation only. Forms, file uploads and multi-tab are not
  implemented. Typing needs a literal from the caller.
* **One port per Chrome.** Use a second port for a second profile;
  `browser_profiles` reports what it finds.
* Jev reads literally and is not a calculator — it cannot hold a plan across steps.
  Tasks needing a sequence ("press 7, then multiply, then 8") should be decomposed
  first; that is what the shipping projects do.
