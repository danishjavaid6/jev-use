---
name: mobile-use
description: Read and operate the user's Android phone through adb.
---

Use jev-use tools. Keep goals, calls and output short. Run device listing,
reading, location inspection, and actions sequentially; the MCP server runs one
tool at a time. Await each result before starting the next call.

## Facebook account location jobs

For checking every saved Facebook account, use `android_facebook(action="audit")`
when the live tool schema exposes it. Set `chunk_size=2` unless a smaller chunk
is needed to fit the harness timeout. Keep calls sequential; when `complete=false`,
pass the returned `resume_token` unchanged on the next audit call. Results are
cumulative, so preserve prior verified entries. If an audit response contains
a blocker, inspect the phone once and follow the returned detail. If a switch
finished late or the phone is still logging in as the requested account, resume
with the same token and `retry_current=true` (CLI: `--retry-current`). This waits
for that login, verifies the active identity, and reads without selecting again;
an identity mismatch stays blocked. After inspection, if that account must be
skipped, pass the same token with
`continue_after_blocker=true` (CLI: `--continue-after-blocker`) to skip that
uncertain account and proceed with later accounts. The skipped account remains
unverified; never replay its selection automatically. For one account, or while
`audit` is unavailable, use the deterministic workflow below:

1. `android_devices()` once, then keep its serial.
2. `android_facebook(serial="...", action="accounts")` once. Use only returned
   account names. `state=accounts` means the list was observed and verified.
3. For each requested account, sequentially call
   `android_facebook(serial="...", action="location", account="EXACT_NAME")`.
   This handles menu navigation, selection, waiting for login, opening a fresh
   location page, and verification of the active identity before and after the
   read. It requires no decision model and leaves Facebook on the verified Menu.
4. Quote `location` only when `state=location` and `identity_verified=true`.
   Do not copy a result to another account or treat notifications/VPN location as
   primary-location evidence. Keep results keyed by returned `account`.

`locked`, `network_error`, `login`, and `authentication_required` require the stated user action. For
`identity_mismatch`, `timeout`, `unsupported_ui`, or `account_ambiguous`, inspect
once with `android_read(include_screenshot=true)`. Use the guarded audit recovery
above for an observed delayed login; otherwise stop. Do not try a manual
guessed-tap fallback, repeat a timed-out account selection, or lower model
confidence. The operation may have taken effect. Unknown layouts fail without
guessing. A partial list is never reported as complete. Preserve already confirmed
results on interruptions and resume when a token is available.

Primary location is Facebook's inferred value; these read tools do not set it.
A request to update a profile city or make a location-tagged post is a separate
UI action. Do not invent a primary-location write operation, assume fraudulent
intent, or decline solely because accounts share a requested city. If “post
location” is ambiguous, clarify which UI action the user wants.

The running MCP process must be reloaded after installing updated tools. If
`android_facebook` is absent from the live tool catalog, use the built-in
`jev-use call android_facebook --serial SERIAL --action audit --chunk-size 2`
fallback. Continue with `--resume-token TOKEN` from the returned response. Use
`--continue-after-blocker` only after inspecting/resolving the blocker. The
tool-call fields are `chunk_size`, `resume_token`, `retry_current`, and
`continue_after_blocker`. The recovery and skip flags are mutually exclusive.
Do not revert to model-guessed taps after an unsupported layout.

## Other phone tasks

1. `android_devices()` once. No device: ask to connect it. Unauthorized: ask to
   unlock and accept USB debugging. Several devices: pass `serial` explicitly.
2. Read the current screen with `android_read(serial="...")`. It includes
   tappable control descriptions, even when an icon has no accessibility label.
3. Perform the requested action with
   `android_use(serial="...", goal="...", act=true)`.
   Use `decompose=false` for a single step. Then `android_read` to verify.
   For a known control, use `goal="tap EXACT_DESCRIPTION"`, `max_steps=1`,
   `decompose=false`, and `use_cache=false`. Exact unique descriptions are selected
   without a model guess. Re-read after navigation; descriptions are not durable
   IDs. For unlabelled icons, inspect the actual screen to establish which control
   serves the goal; do not infer an icon's meaning from its position alone or
   repeatedly rephrase a goal after low confidence. Do not lower confidence to
   force a guess.
4. For Facebook's account location use `android_location`. Report `location`
   only when `state=location`; `state=login` means sign-in is required.

If MCP tools are missing, search once, then use the built-in shell fallback:

```text
jev-use call android_devices
jev-use call android_read
jev-use call android_use --goal "open Settings" --act
```

Use `--serial <id>` when needed and `--json-file <file>` for advanced arguments.
Do not write helper clients, install packages, or loop over failed calls.
A "Request not started" busy response means this call was rejected, not that
its requested action is running. Wait for the named original call to finish.
Do not poll by starting another action/read or assume an unchanged screen proves
a hang. If the original call times out, report its error and inspect once after
the server is available; never repeat an uncertain state-changing action.
Retry a transient failure once; otherwise report it and stop.
The phone must be unlocked. Typing requires a configured text model and supports
ASCII only. scrcpy is optional. Quote what the screen shows; do not invent values.
