# jev-use

**Browser and Android use where Jev makes the decisions.**

```
browser_profiles   which Chrome is open, and whether CDP works
browser_open       launch a profile from a private copy, with a CDP endpoint
browser_use        drive the page toward a goal; Jev picks each action
browser_extract    ask typed questions about the page, get typed answers
browser_read       return the page's text so your agent can answer questions
browser_read_many  read several URLs at once, one tab each, in parallel

android_devices    which phones are attached over adb
android_use        drive the phone toward a goal; Jev picks each action
android_read       return the screen's text, read from the view hierarchy
android_location   the location Facebook attributes to the signed-in account
```

Any MCP-capable harness — Command Code, Claude Code, Cursor, Codex, OpenCode,
Windsurf, Gemini CLI, VS Code, and anything else — can call these over MCP.
The harness brings the reasoning, this server brings the device, Jev brings the
decisions. The model never invents an action: it picks one id from a set this server
built from the live DOM or the live view hierarchy, and the choice is validated
against the snapshot it came from before anything is dispatched.

## Setup

Works on Linux, macOS and Windows.

**One command.** `install.sh` / `install.ps1` do everything below — the npm
install, the runtime, the driver, the MCP registration and both skills — then ask
for your Jev key and report what is left:

```bash
# Linux / macOS
bash -c "$(curl -fsSL https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.sh)"
```

```powershell
# Windows (PowerShell)
iex (irm https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.ps1)
```

`bash -c "$(curl ...)"` rather than `curl | bash` on purpose: piping into bash
makes stdin the script, so the key prompt could not read the terminal.

Supply the key up front with `--key=<key>` instead of being prompted (Windows:
`install.ps1 -Key <key>`), or export `TYPESAFE_API_KEY` first. It is
idempotent — running it again repairs rather than reinstalls. Both scripts are
short enough to read before you run them.

**Global install.** `npm install -g <package>` runs the same install through its
`postinstall`, so a plain global install is enough on a fresh device — then
`jev-use install` to repair, or to pick up a harness you installed later. It
installs into npm's global directory when that is writable, and falls back to a
user-owned prefix when it needs an elevated shell, so it does not stop to ask for
admin. On Windows, `install.sh` refuses under Git Bash/MSYS and points at
`install.ps1`: the POSIX bootstrap would run against the Windows node/npm and
build a half-broken environment whose real breakage only shows up later.

**Private repo: the one-liners above do not work, and adding collaborators does
not change that.** `raw.githubusercontent.com` is fetched without credentials, so
it answers 404 for a private repo — to a collaborator, and to you. Access has to
come from an authenticated client on that machine:

```bash
gh auth login                       # once; this also wires up git's credential helper

bash -c "$(gh api repos/hamzajavaid2005/jev-use/contents/install.sh \
  -H 'Accept: application/vnd.github.raw')"
```

Or do what that script does when the archive URL 404s — pack over git, which is
the step that can use those credentials, and install the tarball it produces:

```bash
npm pack git+https://github.com/hamzajavaid2005/jev-use.git
npm install -g ./jev-use-*.tgz
jev-use install --key=<key>
```

**Do not use `npm install -g git+https://…` directly.** On npm 10 — what ships
with Node 22 — that symlinks the package to a clone inside npm's own cache
directory, so `postinstall` cannot find `bin/jev-use.js` and the install stops
with `MODULE_NOT_FOUND`. And when it does get far enough, it is worse than
loud: the lifecycle scripts run *inside that clone*, so the path it registers is
the one npm deletes. Measured on a real machine — all four harness configs held
`~/.npm/_cacache/tmp/git-cloneXXXX/bin/jev-use-mcp.js`, already deleted, and the
only symptom was a harness with no tools and nothing saying why. `install.sh`
packs first for exactly this reason, and the installer now refuses to register
from a temporary path at all.

**To let anyone install it, the repo has to stop being private.** Collaborator
access means an explicit invite and a login per person, which is not
distribution. Make the repo public, or publish under a name you own and pass
`--npm=<name>` — plain `jev-use` on the public registry belongs to someone
else's package.

**Or by hand.** Two steps, once — plus the driver.

**Linux / macOS**

```bash
uv sync                       # or: pip install -e .
cp .env.example .env          # then set TYPESAFE_API_KEY

cmd mcp add --scope user jev-use -- "$PWD/.venv/bin/python" -m jev_use.mcp_server

# the /browser-use and /mobile-use entry points (Command Code surfaces skills, not
# MCP prompts). `jev-use install` does this for every harness it finds.
for skill in browser-use mobile-use; do
  mkdir -p ~/.commandcode/skills/$skill
  cp skills/$skill/SKILL.md ~/.commandcode/skills/$skill/SKILL.md
done
```

**Windows (PowerShell)**

```powershell
uv sync
Copy-Item .env.example .env    # then set TYPESAFE_API_KEY

cmd mcp add --scope user jev-use -- "$PWD\.venv\Scripts\python.exe" -m jev_use.mcp_server

foreach ($skill in "browser-use","mobile-use") {
  New-Item -ItemType Directory -Force "$env:USERPROFILE\.commandcode\skills\$skill" | Out-Null
  Copy-Item "skills\$skill\SKILL.md" "$env:USERPROFILE\.commandcode\skills\$skill\SKILL.md"
}
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

> `/mobile-use turn on Airplane mode`

**The phone needs none of the setup above.** Everything in this section is about
giving Chrome a CDP port; Android goes through adb, so the requirement is just
`adb` on PATH, the phone on USB (or `adb connect`), and USB debugging accepted.
Both skills are installed by the npm package; `adb` itself is a distro package
(`android-tools-adb` on Debian/Ubuntu, `android-platform-tools` on macOS).

`/browser-use` and `/mobile-use` come from the **skills** installed at
`~/.commandcode/skills/{browser-use,mobile-use}/`, because Command Code surfaces skills
as slash commands but does not surface MCP prompts. The server also exposes the same
guidance as the MCP prompts `browser-use` and `mobile-use` for harnesses that do support
prompts. `jev-use install` installs both skills into every harness skills directory it
finds — Command Code, Claude Code, Codex, OpenCode, and the shared `~/.agents/skills`.

**Any harness.** MCP is the whole interface, so it does not matter which one you
use. `jev-use harnesses` lists every harness this knows how to register with, its
config file, and whether it was detected. `jev-use install --harness=<id,...>`
restricts registration to the ids you name — and forces them even when detection
did not fire, which is the escape hatch for a harness this has an adapter for but
no evidence of. For anything *else*, `jev-use harnesses --print` emits the one
block any MCP-capable harness needs, and the global install puts `jev-use-mcp` on
PATH so a harness can be pointed at the bare command instead of an absolute path.

Either way the guidance is identical: discover → open if needed → drive → **read** →
report, with an explicit instruction to call `browser_open` when nothing is drivable
rather than stopping or silently substituting a different browser.

`browser_use` orbits the page; `browser_read` is what answers a question about what a
page says. Calling only `browser_use` and expecting a summary is the mistake that made
a real task look like it "wasn't doing anything". The same split holds on the phone:
`android_use` changes the screen, `android_read` reports it.

`act=false` is the dry run on both, and it is what makes Android safe to point at a
real phone — it decides and validates and dispatches nothing at all.

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

* **Poll, never sleep — and poll something cheap.** After a click we wait for the
  page to move, bounded. The wait used to re-run the whole ~150 ms candidate table on
  every iteration (about eight across a 3 s settle) only to compare `.url`; it now
  reads `location.href`, the element count, the text length and a cheap digest of
  every form control's value. It returns early only on *evidence*: the page must
  first change (URL or DOM), then hold quiet for several probes, and `readyState`
  must be `complete` — not merely past `loading`, because `interactive` still means
  scripts and data are arriving. Stillness on its own proves nothing (a click that
  starts a slow request leaves the page perfectly still), and a URL change that is
  still loading is a half-loaded document. Typing is settled the same way, since
  validation, autocomplete and same-URL updates all follow a keystroke — and the
  form-value term is what makes a normal entry register as a change at all, since it
  never touches `body.innerText`. A page that never changes waits out `settle`, which
  is the old, safe behaviour.
* **Page-scoped candidates.** A DOM snapshot returns page elements, not browser chrome.
  On `example.com` the accessibility path offered 29 targets, **28 of them toolbar
  buttons**; the DOM offers the page.
* **Reading N pages is one call, not N.** `browser_read_many` opens a tab per URL and
  reads them together, so the page loads overlap and the harness pays one turn instead
  of one per page. Measured: three pages in **2.6 s** (tabs opened and closed around
  it). `browser_use` still drives one page at a time — stepping through a site is a
  sequence of decisions, not a fan-out.

### Things that look like optimisations and are not

* **`semantic_v2` snapshots are 7x slower** (1537 ms vs 210 ms). Use the default.
* **Tighter AX bounds are slower**, not faster: `max_elements=60` took 1309 ms and
  returned 1 element against 736 ms and 29 elements for the default.

### Android speed

Measured on a Huawei RNE-L21 over USB. One snapshot used to cost **2912 ms**; it is
now **2122 ms**, and where the rest went is worth recording because two of the three
fixes were pure waste:

| primitive | cost |
|---|---|
| `uiautomator dump` | **2037 ms** — the real work |
| `wm size` | **800 ms** — for a value that never changes |
| `dumpsys window` | 52 ms |
| `exec-out cat` | 39 ms |
| adb floor (`shell true`) | 21 ms |

* **Cache `wm size`, keyed on rotation.** It was 27% of every snapshot, re-asked to
  learn a constant. The rotation comes free from the dump we already have, so a
  rotated device cannot be handed stale dimensions.
* **Never snapshot inside the settle poll.** The poll only asks "has the foreground
  app changed?" and answered it with a full hierarchy dump — ~2.9 s per attempt,
  most of a step's budget spent to read one string. It polls a ~50 ms signal now.
* **Android settles for 1.5 s, not 3.** Most Android actions navigate *within* an
  app, so the foreground never changes and the poll cannot fire; a long settle is
  then dead time. What actually settles the screen is the dump itself, which blocks
  on UI idle — measured at **3.0-3.4 s in a transition against 2.1 s still**.

Net, on a real one-action run: **17.5 s → 9.9 s**, and the per-decision floor is now
~3.2-4.1 s, of which 2.1 s is the dump.

**The floor is `uiautomator`, and it is per-dump, not per-startup.** Two dumps inside
one `adb shell` took 4063 ms and four took 8084 ms, so each one pays full price — the
cost is the UI-idle sync, not process startup. A persistent on-device uiautomator
would therefore buy nothing, and `--compressed` saves 3%. Going meaningfully below
~2 s per snapshot needs a different mechanism entirely (a companion accessibility
service, or capture-plus-vision), not more tuning.

`dumpsys activity top` is 77 ms and *does* contain a view hierarchy, and it is
deliberately unused: it dumps one activity's raw View tree, so it sees nothing inside
a WebView, Compose or Flutter surface and would report an empty container where the
real UI is.

**Persistent `adb` was investigated and rejected.** The `adb` *server* already
persists across calls; the per-command cost is spawning the `adb` *client* (the 21 ms
floor in the table above), paid once per tap, swipe, keyevent and poll. There is no
supported persistent-client mode — the interactive `adb shell` stream is the only
alternative, and it does not suit `uiautomator`, whose dump is a one-shot CLI that
must run to completion. Against a 2.1 s dump, saving ~21 ms per call is noise, so it
is deliberately not pursued.

### What the loop no longer pays for

Beyond the per-primitive numbers, several costs were structural — paid per run or per
decision rather than per primitive — and each is removed:

* **The driver is a session, not a per-call process.** `browser_use`, `browser_read`
  and `browser_extract` on one page used to spawn and tear down their own
  `cua-driver`, re-running discovery (`ps`, up to two `/json/version` probes) and a
  `list_windows` bind each time. One healthy driver and bind is kept and reused, and
  dropped only when the child dies, the requested profile changes, or a call fails.
* **A dump already taken is reused.** The browser's cache-key snapshot now seeds the
  first step on a miss, and the Android planner's dump seeds the loop instead of being
  re-taken — on Android that is ~2.1 s that no longer happens twice.
* **A dead question is gone.** `key` is asked only when `press_key` is on the table,
  which the browser and Android engines never do, so their decisions no longer carry a
  six-option question that could not affect the answer.
* **The candidate set is bounded and disambiguated.** Duplicate labels get a position
  hint rather than being dropped, nested duplicates and label-less links are filtered
  in the DOM, and a very large page is capped at 120 controls, goal-relevant first.
* **Unambiguous first steps skip the model.** `back`, `home`, a scroll, a bare URL,
  and a goal that names exactly one control are decided in code. The model is kept for
  the choices that are genuinely ambiguous, and only on the first step, so a sticky
  goal cannot be re-selected into a loop.
* **Cache keys fingerprint the screen.** The browser keys on URL, title and the
  interactive structure; Android keys on the window signature rather than the bare
  app. Page text is deliberately excluded, so a clock or a count does not turn every
  replay into a miss. Neither is a full screen hash — an ordinary Android tab or
  fragment swap keeps the same window — so a stale plan is caught structurally
  instead: every stored description must resolve to **exactly one** element *legal
  for its operation* on the freshly dumped screen and then pass the same `validate`
  a live decision does, a plan that resolves to none or to an ambiguous many aborts
  rather than guessing, and an Android plan must **start** with a described target —
  a plan that opens with `back`/`home`/`scroll`/`navigate` would act before anything
  had validated the screen, so it is refused outright. When the screen cannot be
  identified at all, caching is bypassed for that run rather than writing every
  unreadable screen to one shared key. A click that fails to land aborts the replay
  too, rather than being ignored and reported as success. The replay still skips the
  ~2.1 s hierarchy dump after its *final* action, but keeps the cheap settle, so it
  does not report success while a transition is still underway.

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

## Android

Same loop, different device. There is no phone equivalent of the Chrome copy
dance: adb is the automation channel and it is already there, so the whole setup
is "plug it in and accept the debugging prompt".

```
android_devices    which phones are attached, and whether each is usable
android_use        drive toward a goal; Jev picks each action
android_read       the screen's text, read from the view hierarchy
android_location   what location Facebook attributes to the signed-in account
```

**Why this talks to adb instead of to scrcpy's window.** scrcpy is a *viewer* — it
renders the phone into a single OpenGL surface, so anything reading it through
accessibility or the window tree sees one opaque element. That is precisely the
GNOME/Wayland dead end that made the desktop surface unusable here, and pointing
a window-based driver at scrcpy reproduces it exactly. The channel *under* scrcpy
is adb, so that is what this speaks. Two things fall out of it:

* **scrcpy is not required.** It works with the phone on USB and adb running,
  scrcpy open or not — and with it open you can *watch* the agent work.
* **Coordinates need no scaling.** `uiautomator` reports bounds in the device's
  own space and `input tap` consumes the same space, so a tap is arithmetic
  rather than a mapping problem.

The snapshot is `uiautomator dump` — the live view hierarchy with text, content
descriptions, classes and bounds. That is what makes the model's choice a
*closed set* rather than a coordinate guess.

**Two things measured on real hardware**, both of which changed the code:

* **`/sdcard` is a trap.** On EMUI, `uiautomator dump /sdcard/x.xml` reports
  *"UI hierchary dumped to: /sdcard/x.xml"* and the file is not there. A naive
  implementation reads that as a blank screen. The dump goes to
  `/data/local/tmp` instead, which always exists and needs no permissions.
* **Ambiguous options poison the confidence.** A lock screen offered two stacked
  cards in the same region, both rendered as `framelayout - middle-centre`. Jev
  answered **0.37** — under the threshold — and the run refused to act. The model
  was not unsure about the goal; it could not tell which of two identical options
  it was choosing between. Descriptions now escalate from position to exact tap
  point, so every option is unique.

**Reading a page that only exists inside an app.** `android_location` reports the
location Facebook attributes to the signed-in account, and getting there needed the
one route that works. An `https://facebook.com/...` intent is *not* it — the app
publishes no App Link for it, so Android hands the URL to Chrome; naming the package
is refused (`unable to resolve Intent`), and starting the webview activity directly
is blocked by `com.facebook.permission.prod.FB_APP_COMMUNICATION`. The app's own
`fb://facewebmodal/f?href=<encoded url>` deep link opens its internal webview, which
is what makes the page readable as an app screen rather than a browser tab. The URL
is cache-busted on every call, because the webview keeps the last page and a second
check after switching accounts would otherwise be served the first account's answer.

**Limits.** Typing is ASCII only: `adb input text` cannot deliver non-ASCII, and a
non-ASCII value is *refused* rather than mangled. A locked phone has no useful view
hierarchy, and there is no unlock support. Some UI is genuinely not tappable per
`uiautomator` — the text is visible to the model, but it is not offered as a target.

## Layout

```
jev_use/browser.py     the browser engine: discovery, snapshot, read, Jev loop
jev_use/android.py     the Android engine: adb, view hierarchy, taps
jev_use/host.py        the one file that knows the platform
jev_use/choosers.py    JevChooser (live) and MockChooser (offline)
jev_use/driver.py      cua-driver MCP stdio client + the env contract
jev_use/mcp_server.py  the MCP surface, browser and Android
jev_use/profiles.py    profile registry, copy-and-launch, and the CLI
jev_use/text_model.py  the planner and writer Jev structurally cannot be
install.sh, install.ps1  the one-command bootstrap (curl|bash, irm|iex)
bin/, lib/             the npm installer (lib/harnesses/ registers with each harness)
scripts/enable-cdp.sh  thin wrapper over profiles.py (--list / --open NAME)
skills/browser-use/    the same guidance as an agent skill (/browser-use)
skills/mobile-use/     ditto, for the phone (/mobile-use)
tests/                 python tests, test/ node tests
```

`jev_use/candidates.py`, `loop.py` and `cli.py` are the retired desktop surface. They
are unregistered from MCP and kept on disk only so the work is not lost; nothing in the
browser or Android path imports them. `cli.py` stays reachable as the `jev-desktop`
console script — not `jev-use`, which the npm package owns, and it does not run on
Windows because the AT-SPI code behind it is Linux-only.

## Test

```bash
python -m pytest -q      # the engines
npm test                 # the installer
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
