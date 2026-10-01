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
   Browser-owned credential popups may be absent from the page DOM. If a
   screenshot shows a blocking popup but the dialog locator finds nothing,
   stop and request manual dismissal; never repeatedly click behind the popup.
   For
   native JavaScript dialogs, prepare `dialogs.dismissNext()` or
   `dialogs.acceptNext()` immediately before the action that opens the dialog.
   When the MCP server is loaded, call `browser_script` directly. Do not run
   `jev-use call browser_script` from the shell: that starts a second process
   which cannot release the MCP server's existing CDP connection and can cause
   a `connectOverCDP` timeout.
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
