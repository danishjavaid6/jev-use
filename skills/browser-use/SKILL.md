---
name: browser-use
description: Use Chrome or GoLogin to read sites and perform browser actions.
---

Use jev-use tools. Keep calls and output short.
Chrome opens in the background by default. Known URLs use a dedicated background
tab; CDP actions do not move the user's system mouse or keyboard. Keep using the
returned port while the user works in other windows. Set `background=false` only
if the user wants to watch Chrome. GoLogin uses its own launcher; `headless=true`
is optional there. Do not operate the same page the user is actively editing.

1. Call `browser_profiles` once. Reuse the matching live CDP port.
2. If closed, call `browser_open(profile="name")` once and use its returned port.
   Use `vendor="gologin"` for GoLogin; never copy it into Chrome.
   For a public task with no requested account, use Chrome's Default profile.
   For an account task with several possible profiles, ask which account.
3. Reading a known URL: `browser_read(port=N, url="https://...")`.
   This navigates and reads in one call, with no decision-model request.
   Several URLs: `browser_read_many(port=N, urls=[...])`.
4. Clicking, filling or scrolling: `browser_use(port=N, goal="...", act=true)`.
   Keep the goal short; use `decompose=false` for a single step. Read afterward
   to verify. Report only observed facts.
5. After a GoLogin task call `browser_close()` to save its state.

If MCP tools are missing, search once, then use the built-in shell fallback:

```text
jev-use call browser_profiles
jev-use call browser_open --profile Default
jev-use call browser_read --port 9222 --url https://example.com
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
