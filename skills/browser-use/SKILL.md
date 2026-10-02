---
name: browser-use
description: Use Chrome or GoLogin to read sites and perform browser actions.
---

Use jev-use tools. Keep calls and output short.
Chrome and GoLogin open visibly by default. CDP actions do not move the user's
system mouse or keyboard; keep using the returned port while the user works in
other windows. Never bring the browser to the foreground or use OS input.
Known URLs use dedicated background tabs without activating them; the user can
select those tabs to watch. Launching a visible browser may briefly take focus.
Always pass `headless=false` when opening Chrome or GoLogin unless the user
explicitly requests headless. `background` is a legacy option, not headless mode.
An already-running headless browser cannot become visible through an action:
explain that it needs closing and reopening; do not silently reuse it if the
user wants a visible browser. Never kill an unrelated browser to do this.
Do not operate the same page the user is actively editing.

1. Call `browser_profiles` once. Reuse the matching live CDP port.
2. If closed, call `browser_open(profile="name", headless=false)` once and use its returned port.
   Use `vendor="gologin"` for GoLogin; never copy it into Chrome.
   For a public task with no requested account, use Chrome's Default profile.
   For an account task with several possible profiles, ask which account.
3. Reading a known URL: `browser_read(port=N, url="https://...")`.
   This navigates and reads in one call, with no decision-model request.
   Several URLs: `browser_read_many(port=N, urls=[...])`.
4. Exact action: `browser_action(port=N, action="click", label="Save")` or
   `action="fill", label="Email", text="user@example.com"`. Scroll uses
   `action="scroll_down"`. This acts once and returns text without a Jev call.
   For any repeated login, account switch, form, wizard, or checkout workflow,
   **MUST use `browser_script`** with one prepared BetterWright Playwright
   snippet per account. It attaches to the returned port, does not launch
   another browser, and avoids a model round-trip per click. Inspect once if
   selectors are unknown, then write the script and run it. Do not switch to
   agent-browser for the same page. Install it once with
   `bun install -g betterwright` and verify the script result.
   `browser_script` dismisses recognized cookie/promotional overlays and the
   Facebook `Sign in as` chooser shown with a `Close` button by default. It
   verifies that the chooser disappears before running the script. Set
   `dismiss_overlays=false` to keep it open. Password forms and unrelated dialogs
   stay open. If this popup appears midway through a script, repeat the same
   scoped dialog close and wait for it to disappear before continuing.
   Browser-owned FedCM account chooser alerts are monitored over CDP on the
   selected tab and dismissed when `dismiss_overlays=true`. Other browser UI is
   not guaranteed to be accessible; report warnings and request manual dismissal
   if it still blocks the task. Never repeatedly click behind a popup.
   For native JavaScript dialogs, prepare `dialogs.dismissNext()` or
   `dialogs.acceptNext()` before the action that opens it.
   Scripts run through Node.js 22+ (no Bun PATH dependency), retain `state` and the
   selected tab between calls, and use a hard timeout in seconds. If multiple tabs
   exist on first attachment, pass `page_url` with the exact observed workflow URL.
   Omit it afterward to retain the same tab through navigation.
   Do not mix script and interactive tools during a prepared workflow: interactive
   tools detach the script worker. Switching tools can also select another tab.
   On a script attachment error, stop and report the error. Never infer that CDP
   allows only one websocket, never launch a second shell driver as a workaround,
   and never fall back to dozens of goal-based clicks. After a timeout, inspect
   state before retrying: a submission may already have committed.
   `settle` is SECONDS, limited to 0–10: use `5`, never `5000`. Stop after two
   failed actions on the same control and inspect the real target.
   Prepared scripts include a `workflow` helper for repeated account jobs:
   `workflow.begin(runId, account)`, `await workflow.browseFeed({seconds:30})`,
   `workflow.beforeCreate(pageName)`, `await workflow.submitCreation({selector})`,
   `await workflow.confirmCreated({selector,
   expectedText})`, and `workflow.loggedOut()`. Use these checkpoints so a
   timeout or logout failure cannot silently submit a duplicate Page. The helper
   waits for conditions rather than fixed sleeps and accepts a signed-in marker
   when a saved account skips the password prompt.
   For repeated Facebook Page-creation tasks, use these helpers. After confirming
   the selected account is logged in, scroll its feed and play visible videos for
   30 seconds BEFORE opening the creation form. If playback is not observed,
   inspect the visible Videos/Reels entry and complete that step; do not report
   playback that did not occur. Skip only if the user explicitly asks to skip it.
   Reserve creation with `workflow.beforeCreate` and END THAT SCRIPT CALL. The
   next call uses `workflow.submitCreation`, waits for an exact creation notice,
   and uses `workflow.confirmCreated`. This extra boundary saves the reservation
   to disk before the click, protecting against hard timeouts. Never bypass the
   helper with a raw Create Page click after a reservation. Use a stable run ID
   across retries and the same account ID; changing it defeats duplicate checks.
   Start each phase with `workflow.begin` using the same IDs, including after
   reconnecting. Inspect and fill the form BEFORE reserving the submission; the
   immediate next script should submit and confirm, not inspect or refill.
   When attaching to a profile opened elsewhere, supply a stable non-secret
   `checkpoint_scope` on every script call. GoLogin profiles opened by this MCP
   server use their profile ID automatically. Do not use passwords as IDs.
   `confirmCreated` must observe a creation notice with the exact submitted Page
   name, or the new Page's actual ID/URL. Search matches and the filled name input
   alone are not proof. Record `loggedOut` only after verifying the chooser returned.
   Keep default overlay dismissal enabled: the screenshot's Sign in as chooser
   is a blocking credential selector, separate from Facebook's password form.
   On Windows its scoped Close button is also handled through UI Automation.
   Do not disable dismissal just because the task includes login.
   If the target is ambiguous or needs several steps without a prepared script, use
   `browser_use(port=N, goal="...", act=true)`: Jev chooses from real controls.
   Keep goals short; use `decompose=false` for a single step. Verify the result.
5. After a GoLogin task call `browser_close()` to save its state.

If MCP tools are missing, search once, then use the built-in shell fallback:

```text
jev-use call browser_profiles
jev-use call browser_open --profile Default --no-headless
jev-use call browser_read --port 9222 --url https://example.com
jev-use call browser_action --port 9222 --action click --label Save
jev-use call browser_script --port 9222 --json-file workflow.json
jev-use call browser_use --port 9222 --goal "open billing" --act
```

Use the actual returned port. Ordinary shell arguments work on Windows too.
For advanced arguments use `--json-file <file>`.
Do not create MCP clients, CDP scripts, install packages, edit registry entries,
kill browsers, or repeatedly search for missing tools. If a call fails, retry
once only when the error is transient. Otherwise return the error and stop.
If MCP registration is missing, recommend
`jev-use install --harness=commandcode` and restart the app; use the fallback
for this task. Never spend the user's task debugging the installation.
