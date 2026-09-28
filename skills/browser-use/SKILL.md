---
name: browser-use
description: Drive the user's own browser to do a task and report what the pages say — usage pages, dashboards, billing, repos, deployments. Use when the user asks you to open a site, check a page, or find something behind a login.
---

# browser-use

Use the `jev-use` MCP server. It drives the user's **own** Chrome — the profile with
their logins — and Jev makes every in-page decision.

You bring the reasoning and the reporting. Jev brings the clicks. You never pick a
DOM element; you describe a destination.

## Order of operations

**1. `browser_profiles` first.** Costs ~0.15s and lists running browsers *and*
profiles on disk, by their real names.

* Something already drivable (`cdp:port`) that matches what the user meant → use it.
* Nothing drivable → `browser_open(profile="<name>")`. That copies the profile into a
  private directory and launches it with a CDP endpoint. Warn if the profile is large
  (0.5–1.5 GB); the copy is made once and reused.
* Not sure which profile they mean and several match → **ask**. Never substitute a
  different browser: the point is that this runs against the profile with their logins.

**2. `browser_use` to get there.** Pass `url` when you know it.

```
browser_use(url="https://github.com/vercel/next.js/commits/canary",
            goal="show the latest commits", act=true)
```

Jev chooses every click and scroll. Describe the destination, not the clicks. Leave
`decompose` on unless the goal is already one step: Jev decides one action at a time
and cannot hold a plan, so a compound goal is split into ordered subgoals first, and
sequential tasks are where it otherwise goes wrong.

**3. Get the answer.** `browser_use` **orbits** the page; it does not report what the
page says. Two ways to actually get the content:

* **`browser_extract` — preferred when you want specific values.** Ask typed
  questions and get typed answers. This is cheaper (no 20 KB of text in your context)
  and more trustworthy, because Jev cannot generate a number it did not read:

  ```json
  {"questions": {
     "logged_out": {"type":"noul",  "instructions":"Is this a sign-in page?"},
     "tokens":     {"type":"score", "instructions":"How many tokens are used?",
                    "criteria":["none","under 1M","1M-100M","over 100M"]},
     "plan":       {"type":"choice","instructions":"Which plan is shown?",
                    "criteria":{"free":"Free tier","goat":"Paid GOAT plan","other":"Other"}}
  }}
  ```

* **`browser_read`** when you need the raw text — an unfamiliar page, a commit
  message, deciding what to ask next.

**Many URLs? Read them together, not one at a time.** If the task spans several pages
(a list of sites, a batch of tickets, the same page for N accounts), do **not** call
`browser_read` in a loop — that is N tool calls and N turns. Use:

```
browser_read_many(urls=["https://a.example/contact",
                        "https://b.example/contact"],
                  concurrency=3)
```

It opens one tab per URL, reads them in parallel, and returns the text of each in the
order you asked, labelled with its URL. A page that fails is reported in its own
section rather than failing the whole call. Prefer it whenever you already know the
URLs — and if you only need specific fields, go straight to the page with
`browser_use(url=...)` then `browser_extract`, rather than navigating through the
site with clicks.

**4. Report precisely.** Quote the values you saw or extracted: numbers, names, dates.
If a page needed a login and the session had expired, say so plainly rather than
guessing at the contents.

## Shape of a good answer

Per section — what you opened, what it said, what you could not reach:

```
Command Code usage — reached /settings/usage
  97.6M tokens this billing month, 251 runs
  5-hour limit 19% (resets in 4h), weekly 8%, monthly 4%

Cloudflare R2 billing — could not reach: the page redirected to a product
  page and the session was logged out. Not guessing at the numbers.
```

## When it will not work

* **No CDP endpoint.** `browser_open` opens one from a copy.
* **An expired session.** There is no generic way to log in — the server can fill
  fields only when a text model is configured and the goal supplies the value. If a
  login wall appears, say so.
* **Typing.** `type_text` needs a configured text model (`JEV_USE_TEXT_MODEL`); without
  one, only clicking, scrolling and reading are available.
* **`browser_use` drives one page at a time.** Stepping through a site is sequential;
  only `browser_read_many` fans out across tabs (up to 8).
* **A "driver did not answer" timeout.** The page raised a JavaScript dialog
  (`alert`/`confirm`/`beforeunload`) that blocked the call. Just retry — the server
  kills the stuck driver and starts a fresh one. Do **not** restart Chrome or reopen
  the profile to recover; that loses the page you were on for no reason.

## Do not

* Do not call `browser_use` repeatedly hoping a different result appears.
* Do not summarise a page you never read.
* Do not report a task as done because a tool call returned success — read the page.
