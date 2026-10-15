# jev-use

Global browser and Android tools for agent harnesses. Chrome runs in the
background; Jev chooses controls for ambiguous tasks. Exact actions and page
reads run directly without a decision-model request.

## Install once per device

Requires Node 18+. The installer provisions Python, the CDP client, optional
Jev SDK, and the driver fallback. It registers detected harnesses in their
user configuration and installs `/browser-use` and `/mobile-use` globally.
No repository folder needs to be open when using the tools.

Linux/macOS:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.sh)"
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.ps1 | iex
```

Repair/register Command Code:

```text
jev-use install --harness=commandcode
jev-use doctor
```

`doctor` performs an MCP handshake and lists tools, then tests Command Code's
registered launcher (including its Windows shell behavior). It reports startup
errors instead of treating a config file as proof of a connection. A successful
check verifies the launcher, not the desktop app's live connection; use `/mcp`
or **Test connections** in the app after restarting it. MCP startup never installs
or repairs Python; if the runtime is missing, run `jev-use install` first.

Set keys once:

```text
jev-use install --key=<typesafe-key>
jev-use install --gologin-token=<gologin-token>
```

Restart the harness after installation. Supported registration adapters:
Command Code, Claude Code, Codex, Cursor, OpenCode, Windsurf, Gemini CLI, VS Code.
Slash-command discovery depends on the harness's skill support. Shared skills
are installed under `~/.agents/skills` (Windows: `%USERPROFILE%\.agents\skills`).
Harnesses installed later need another `jev-use install` to register MCP.

```text
jev-use harnesses
jev-use harnesses --print
```

The second command prints configuration for other MCP-capable harnesses.
For a private repository, installers need GitHub access. To install a specific
revision, download `install.ps1` and run it with `-Ref <commit>`; on POSIX use
the bootstrap's `--ref=<commit>` option. Do not install the unrelated public
registry package named `jev-use`. Install this repository's archive or a packed
tarball. Avoid direct npm git installs that can register temporary cache paths.

## Choose the shortest tool path

Call `browser_profiles` once. Reuse the matching port, or call `browser_open`
with the requested profile. Use its returned port in later calls.

| Need | Tool | Uses Jev? |
| --- | --- | --- |
| Read a known URL | `browser_read(port=N, url="https://...")` | No |
| Read several URLs in parallel | `browser_read_many(port=N, urls=[...])` | No |
| Click an exact label | `browser_action(port=N, action="click", label="Save")` | No |
| Fill an exact field | `browser_action(port=N, action="fill", label="Email", text="...")` | No |
| Scroll/back | `browser_action(port=N, action="scroll_down")` or `action="back"` | No |
| Prepared multi-step workflow | `browser_script(port=N, code="...")` | No |
| Ambiguous or multi-step goal | `browser_use(port=N, goal="...", act=true)` | Yes, when needed |
| Extract typed answers | `browser_extract(port=N, questions={...})` | Yes |

`browser_action` acts once and returns page text. Click/fill require exactly one
matching visible control; ambiguous labels are refused. `browser_use` validates
Jev's choice against real controls; exact deterministic decisions and cached
plans bypass Jev. Use `decompose=false` for one-step goals. Compound planning
and generated text require a configured `JEV_USE_TEXT_MODEL`; an unavailable
planner is skipped. Literal text supplied to `browser_action` needs no writer.

For repeated Facebook Page creation, use `/facebook-create-pages` (or
`$facebook-create-pages` in Codex). The default is `browser_facebook_next`:
the agent repeatedly calls one tool with a stable run ID. The backend chooses
the next authorized profile, observed account and bounded action from fresh
browser state and durable journals. Betterwright performs actions in GoLogin.
No caller-written JavaScript or batch script is needed.

After starting/resuming the scoped run below, call:

```json
{"run_id":"my-batch"}
```

Pass that object to `browser_facebook_next`. On `continue`, repeat its exact
`next_call`. On `needs_input`, supply the requested runtime password or current
account's Page details. The common password stays in this MCP process only,
expires after 30 minutes idle, and is cleared on pause/completion/restart.
Page details are durable; credentials must be resupplied after restart.
`needs_resume` requires reading run status and an authorized `resume`.
`paused` and `blocked` stop the loop; `completed` requires all observed accounts'
creation evidence, verified logout and profile closure. Repeated failures stop
after two attempts. Submission guards apply to each job's own Page attempt.

Each new authorized job requests **one additional Page per observed account**
in exactly the user's selected profiles. Accounts may already have Pages from
earlier jobs; that history does not complete or block the new request. To create
another Page in the same accounts, start a new stable `run_id`. To resume an
interrupted job, retain its existing ID: completed accounts in that job are
skipped and uncertain submissions are confirmed rather than clicked again.
Every job keeps separate Page details, confirmation, timing and logout records.
Older unresolved attempts remain visible in history without being retried or
silently marked successful. Current progress/results count only this job;
`history` reports previous confirmed Pages and unresolved creation attempts.

Transient live-status failures are checked up to three times per next-step
call, with 1s/2s delays and a fresh SDK connection to the exact same tab. An
unknown UI is reobserved before acting. Recovery keeps the browser, journal and
submission guards intact; it never replays login, fill, Create or logout.
`live_status_checks` reports attempts, reconnections and sanitized cause codes.
Each status attempt has a 25-second deadline. Pause stops further attempts;
after three failures the controller stops for diagnosis and profile cleanup.

`browser_facebook_vision(run_id="my-batch")` adds experimental, read-only Jev
1.14 screen classification, popup presence and a possible Close-button box.
It sends `TypeSafe-Danger-Zone-Images: true`, with the image-capable SDK and
Pillow supplied by the `live` extra. Viewport screenshots cannot show native
browser-chrome popups; native dialog handlers remain necessary. Vision does
not authorize clicks or substitute for account identity/creation evidence.

The lower-level `browser_facebook` actions below remain available for focused
manual recovery and maintenance; smaller agents should use the next-step tool.
The controller pins a dedicated tab instead of choosing among restored duplicate
Facebook tabs. It persists that tab's identity and validates it against the live
browser on reconnect. Lost creation tabs stop for inspection. A worker lease
prevents two MCP processes from operating the same run, and raw/low-level
mutations are refused while the controller owns the profile. Pause and save/close
before deliberately switching to manual recovery.

Vision reports whether failure occurred during tab selection, screenshot capture,
or TypeSafe analysis. An opt-in integration test exercises actual Chrome capture
and the live image API on isolated fixture UI (`JEV_TEST_CHROME` and
`JEV_TEST_VISION=1`, with `TYPESAFE_API_KEY` in the test process environment).

Use the [start/resume prompt](prompts/facebook-pages.md) for the full agent-driven
batch. Before a new session, run `jev-use doctor`: when GoLogin is configured it
also checks the Facebook recovery schema, Betterwright CLI/SDK, and Jev inspection
dependency without launching a browser. Reload the harness's MCP connection after
upgrading; a successful fresh-launch check does not refresh an older live server.

For lower-level manual control, open each requested profile with `browser_open`.
For the default next-step mode, the tool opens and closes profiles itself.

First restore the saved batch with `browser_facebook_run(action="status")`.
For a new job, call `browser_facebook_run(action="start", run_id="my-batch",
profile_start=23, profile_end=29)`. This records only the exact requested range
and live GoLogin IDs. Pass that `run_id` to `browser_open` and every structured
Facebook step. Scoped profile opens and structured steps reject profiles outside
the recorded range. Structured steps require ownership on the same MCP
connection, so a guessed port cannot bypass the profile check.

The user selects the profiles for each job; 23–29 is only an example. Use
`profile_numbers=[7]` for one profile, `profile_numbers=[2,7,19]` for a list,
or `profile_ids=["<observed-id>"]` for custom-named profiles. Supply one selection
mode. A sparse list never includes the profiles between its entries. A newly
requested selection takes precedence over the previous batch's saved cursor;
create a separate scoped job and retain the old journals.

Run state persists outside the workspace under the workflow checkpoint directory
(`JEV_USE_WORKFLOW_DIR` overrides it). It records the last account/action, partial
progress, and pending attempts across agent or MCP restarts. `status` reads saved
state without launching a browser. Use `resume` for an authorized continuation,
then inspect live account status; use `pause` on stop. A pending attempt is
uncertain until inspected. The range cannot silently expand under the same run
ID, and completed or uncertain creation journals are retained.

`accounts → login → wait_home → browse → open_form → wait_form → fill → create → confirm → home → browse_after → logout`

Every call takes `port`, `run_id`, and `account`. Use the same identifiers on
retries. `accounts` can use `account="__inventory__"`; use an observed account
identifier for subsequent calls. `login` takes the runtime `password` and
optional observed `account_name`/verified `account_id`. `fill` takes `page_name`,
`category`, and optional `bio`. For example, after reaching `form_ready`:

```json
{"action":"fill","port":9222,"run_id":"my-batch","account":"observed-account","page_name":"My Page","category":"Artist","bio":"Art and illustrations."}
```

Use the actual returned port. Follow `next_action` in each response. Both wait
steps last 20 seconds. Repeat `browse` in chunks of at most 60 seconds until
Home 60 seconds, Reels 60 seconds, then Home 60 seconds have accumulated; repeat
`browse_after` until 30 seconds of Home browsing have accumulated. Scroll
distances, directions, and pauses vary, with longer pauses on Reels. An observed
Reels link and visible video are required; missing Reels cannot count as Home
scrolling. Progress and remaining time survive reconnects.
The default 60-second chunk reduces model/tool overhead to three calls before
creation and one 30-second call afterward. Responses retain the active account's
checkpoint and popup diagnostics; other accounts' journals remain on disk.

The target is approximately **5–6 minutes per account**, from login through
verified logout. The requested waits/browsing total 250 seconds; browser work
and model/tool overhead account for the rest. Facebook delays or account
challenges can take longer. Do not cut browsing or skip confirmation to meet
the target.

GoLogin requires the persistent MCP server. Do not use the one-shot
`jev-use call browser_open` fallback for this workflow: process-exit cleanup
stops the profile before the next CLI call. If the agent cannot see the tools,
fix/reload the harness MCP connection. OpenCode uses
`~/.config/opencode/opencode.json` (or `$XDG_CONFIG_HOME/opencode`) on Windows
too; `opencode debug paths` reports the active location. Restart its session
after changing the registration or installed skills.

After verified logout, enumerate again and process every remaining account.
Only then call `browser_close` to save the profile and open the next one. Keep
a non-secret batch inventory and stable Page names; never place passwords in
files. Missing credentials and blocked accounts must be recorded as unfinished
or skipped, not successful. MFA/CAPTCHA may need human handling.

Call `status` after an interrupted call. A `submitting` checkpoint requires
`confirm`, never another creation attempt. Creation verifies the active account,
checks the button, and journals the attempt before clicking. Supported Windows
notification/credential prompts and selected-tab JavaScript dialogs use the
existing native handlers; inspect any returned native-dialog warning. Unknown
or localized native UI may need manual dismissal.

The legacy `facebookPages.prepare`/`finish` runner remains available through
`browser_script` for callers explicitly choosing prepared scripts.

Windows native account choosers are deduplicated by their UI Automation runtime
IDs, and the bridge waits for the first native-popup inspection before acting.
After inspecting and dismissing a popup that blocked account selection, use
`retry_login` once with the same checkpoint if the chooser remains visible and
no password was submitted. If the password form appeared, resume `login` directly.
For an explicit user-supplied password correction after Facebook's visible
incorrect-password notice, `correct_password` permits one corrected submission
on that account's form while preserving the existing run and creation journal.
It also handles a verified signed-out account chooser after a profile restart.
`status.live_login` reports current UI separately from the saved previous login
observation; `loginFailure.code` supplies non-secret recovery diagnostics.
When no password has been submitted in the current attempt (`loginSubmitted`
is false), resume normal `login` with the supplied password even if a correction
was reserved. `current_state` reports the live page and account mismatch; use it
with the checkpoint to recover supported navigation instead of restarting the flow.

The native dialog watcher runs throughout the workflow. Before and after each
structured action it requires a fresh clear popup inspection. Supported browser
alerts are dismissed; remaining or uninspectable native popups stop progress and
are reported via `browserDialogs`. Page-owned password forms remain available
for login. Each SDK attachment has separate bookkeeping so it does not contend
with an unrelated default Betterwright profile lock.

Precreation sign-outs recover under the same checkpoint and verified account ID,
preserving completed browsing. Successful reauthentication has a fresh alert
settle; lost responses never authorize another password or Create submission.
Exact account names and observed public IDs disambiguate saved cards. A supplied
verified ID cannot be overridden by a matching display name. Password forms must
identify the requested account before filling. Stop/save failures retain profile
ownership and prevent opening another profile over unfinished work.

For a known workflow, `browser_script` runs prepared Playwright through the
BetterWright SDK, avoiding a model request per click. It uses Node.js 22+ and the
installed BetterWright package; Bun is no longer required at execution time.
GoLogin still launches and saves the original profile. Call `browser_close`
afterward to save its state.

The SDK connection, selected tab and `state` persist between script calls. If
several tabs exist at first attachment, supply `page_url` with the exact existing
workflow URL; omit it afterward to follow the same tab through navigation.
Switching to interactive tools detaches the script worker, so keep a prepared
workflow on the script path. A script failure is an MCP error, not a success.
Do not replay a timed-out submission without checking whether it committed.

Scripts dismiss recognized page overlays, the scoped `Sign in as` chooser,
and the Google One Tap bubble. The selected tab also has native FedCM account chooser monitoring via
CDP. Set `dismiss_overlays=false` to preserve these choosers. Password forms,
other tabs and unrelated native dialogs are untouched. Unsupported native dialog
monitoring is reported as a warning; other browser-owned UI may require manual
dismissal. Prepare `dialogs.dismissNext()` or `dialogs.acceptNext()` before
an action that opens a JavaScript dialog.

Inspect selectors once, batch deterministic stretches, and observe when the form
structure changes. A quick connection check requires no page mutation:

```js
return { url: page.url(), title: await page.title(), stateAvailable: !!state };
```

Script deadlines are in seconds (1–900, default 120). `browser_use.settle` is
also in seconds, limited to 0–10; `5000` is rejected immediately. The MCP server
answers heartbeat and schema requests while a tool runs and refuses overlapping
actions instead of silently queueing duplicate submissions. Attachment errors must be diagnosed from the returned error; they do not prove CDP supports only one websocket.

Prepared scripts expose a checkpointed `workflow` helper for repeated account
jobs. It supports `workflow.begin`, condition-based login detection,
`workflow.browseFeed({seconds:180})`, `workflow.settleAlerts({seconds:20})`, `workflow.beforeCreate`, `workflow.submitCreation`, exact creation
confirmation, and `workflow.loggedOut`. Checkpoints are written atomically so a
timeout or logout failure stops a duplicate submission. The 3-minute randomized
browsing period with 20-second alert settles is used for repeated Facebook
Page-creation jobs, and is never added to
ordinary browser tasks. The helper reports whether video playback was observed;
scrolling alone is not reported as successful playback.

Use `workflow.loginSavedAccount` for the saved-account chooser (password or
remembered session) and `workflow.fillPage` for the observed form. Call
`workflow.beforeCreate(pageName)` after the form is actionable, then submit via
`browser_script`'s `submission:{run_id,account}` argument. Reads preserve the
reservation. An invalid selector fails before any click or consumption of the
reservation. The tool checks actionability using a trial click, saves the attempt
on disk, then clicks the button role once. The snippet runs after the click:

```js
await workflow.confirmCreated();
return await workflow.logout();
```

Confirmation waits for a delayed creation notice containing the exact Page name.
Logout accepts the wizard's beforeunload and uses the account menu, never clears
cookies or invents logout tokens. After a submission, only re-check confirmation
or logout; a disabled button or unchanged URL does not authorize another click.
Use stable run/account IDs on retries and checkpoint_scope for externally opened
profiles. The helper selectors can be overridden using observed DOM evidence.

On Windows, a browser-owned `Sign in as` chooser may be outside the page DOM.
The script worker starts a scoped PowerShell UI Automation helper that invokes
`Close` only when it finds that heading inside the same browser process. It does
not send global keystrokes or click unrelated windows; warnings are returned if
the helper cannot access the dialog.

Reads default to 6,000 characters per page; `max_chars` can raise this to 20,000.
The harness receives 15 tool schemas, loaded skill instructions, and tool results;
it does not receive the project source. Actual context usage depends on the
harness's tool discovery and tokenizer.

## Background work

`browser_open` launches a visible browser by default. Only explicit `headless=true`
opts into headless mode, for Chrome or GoLogin. Legacy `background=true` no longer
hides the browser. The shell fallback accepts `--no-headless` for visible mode.
An existing headless session must be closed and reopened to show a window.
Opening a visible window may briefly take focus; ongoing
CDP actions do not bring it to the foreground. Known URL tasks use a dedicated background tab; parallel reads pin
each tab by ID. CDP actions do not move the system mouse or send OS keystrokes,
so the user can work in other windows. Do not automate the same page the user
is editing. Sites that open native dialogs may still need user interaction.

Chrome profiles are copies in a private data directory because Chrome blocks
CDP on its default directory. Copies are snapshots; `refresh=true` updates them.
Copying a large profile is a one-time cost, not a model delay. On Windows,
installed Chrome takes precedence over PATH/registry entries that may point to
Orbita. `JEV_USE_BROWSER_BINARY` selects an explicit browser executable.

GoLogin uses its own SDK and Orbita with the profile's fingerprint, proxy and
cookies. Use `browser_open(profile="name", vendor="gologin")`, then
`browser_close()` to stop and save the profile. Its native launch is preserved;
`headless=true` requests no visible window. Cloud debugger URLs are unsupported;
this transport expects a local CDP port. Get a token from
<https://app.gologin.com/#/personalArea/TokenApi>.

## Android

Install Android platform-tools (`adb`) and accept USB debugging on an unlocked
phone. `scrcpy` is optional. Start with `android_devices`, then `android_read` or
`android_use(goal="...", act=true)`. Read again to verify actions.

Pass `serial` when multiple devices are attached. `android_location` reads
Facebook's account location; quote it only when `state=location`. Typing supports
ASCII and generated text requires a text model. Phone automation can run while
the user works on the PC, but shares the phone's visible screen with manual use.

For Facebook account batches, use `android_facebook(action="audit")`. It
enumerates saved accounts and processes them sequentially in bounded chunks.
For an authorized login → primary-location read → Public location check-in →
logout job, use `android_facebook_flow` with the exact account/place,
`publish=true`, and a stable `run_id`. It follows observed controls without a
decision model per tap and journals submission before posting. Reuse its
`resume_token` to continue bounded calls; `complete=true` confirms both the post
and logout. The check-in is separate from Facebook's inferred primary location.
Continue with the returned resume token while `complete=false`; keep each call
sequential. If a blocker stops the audit, inspect the phone and follow its
detail. For an observed delayed login, pass the same token with
`retry_current=true` (CLI: `--retry-current`). Recovery verifies the currently
active account and reads its location without selecting the saved account again.
Use `continue_after_blocker=true` (CLI: `--continue-after-blocker`) instead if
the inspected account must be skipped. The two flags are mutually exclusive.
The skipped account remains unverified.
Each result includes the account, location, identity verification, state, and
elapsed time. Quote a location only when `state=location` and
`identity_verified=true`. The workflow returns explicit blocked states for
locks, connection failures, sign-in, and unsupported layouts. It does not edit
location or publish posts. For a single account, `action="location"` remains
available.

To summarize captured audit calls, save one JSON object per line in a JSONL
file. Each line can be the tool response directly or a JSON object with the
response under `response` and optional `wall_seconds`, `tool_calls`, and
`model_calls` numbers from the harness. Then run:

```text
python scripts/benchmark_facebook_audit.py audit-responses.jsonl
```

The report counts unique verified accounts, reported location retries, per-account
elapsed time, audit call time, and known wall time. Audit responses include
cumulative results, so the script counts each account once. Enumeration time is
shown separately because it is already included in the first audit call. Record
wall time around each tool call to
make it useful. It leaves model/provider latency unknown when the harness does
not report it; the wall-minus-call figure is unattributed time, not an inference
measurement. Compare a live complete run with the 43m20s historical session
only after reporting verified completion count. The 5x improvement in the plan
is an engineering target, not a measured result.

`android_read(include_screenshot=true)` returns both tappable descriptions and
the phone image for unfamiliar UI. Exact unique descriptions can be selected
with `android_use(goal="tap EXACT_DESCRIPTION", act=true, max_steps=1,
decompose=false, use_cache=false)`. Reload the MCP connection after updating so
the new tool schemas and handlers are active.

## Missing MCP tools

Use the built-in fallback instead of asking the model to write clients:

```text
jev-use call browser_profiles
jev-use call browser_open --profile Default
jev-use call browser_read --port 9222 --url https://example.com
jev-use call browser_action --port 9222 --action click --label Save
jev-use call android_devices
jev-use call android_use --goal "open Settings" --act
```

Use the actual returned port. `--json-file <path>` supplies advanced arguments
without shell quoting problems. The fallback starts a process per command;
native MCP reuses a session and is faster for repeated calls. Search for missing
tools once, use the fallback, and report persistent failures instead of repair
loops. `doctor` checks configuration and installed paths; it does not prove the
app connected its MCP session.

If `jev-use` is not on PATH but this repository checkout is available, run the
same fallback from the repository root with `.venv/bin/python -m jev_use call <tool> ...`
using the checkout's environment, or the interpreter where jev-use is installed. The Python
console script also accepts the `call` subcommand directly.

Facebook audits can start from the saved-account sign-in screen: the workflow
tries one uniquely observed saved card, verifies the resulting active identity,
then enumerates the full in-app picker. A saved card alone never establishes a
valid session. Password, verification, expired-session, and pending-login
blockers remain explicit; an uncertain selection is never automatically replayed.

## Development

```text
pip install -e '.[dev,harness]'
python -m pytest -q
npm test
```

Source and historical design notes: [docs/architecture.md](docs/architecture.md).
The npm install excludes retired desktop modules and development tests.
