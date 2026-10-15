# Structured actions and manual recovery

Use this reference only for focused recovery or maintenance. The default agent loop
is `browser_facebook_next`, described in the parent SKILL.md.

Use the user's requested profiles and accounts. Loading this skill does not
start publication. If the user stops Page creation, stop creating Pages and
continue only the requested inspection, maintenance, or cleanup.

## Tools and startup

Use one persistent **jev-use MCP connection**. GoLogin owns the browser;
Betterwright performs structured actions. Do not use a batch driver script,
launch another browser, change fingerprints/proxies, or clear cookies.

| Tool | Purpose |
| --- | --- |
| `browser_profiles` | Discover exact live GoLogin IDs and profile names once. |
| `browser_facebook_run` | Restore durable scope/progress; start a bounded run, explicitly resume, or pause. No browser actions or passwords. |
| `browser_open` | Open one allowed profile with `vendor="gologin"`, `headless=false`, and the saved `run_id`; retain its port. |
| `browser_facebook` | One structured workflow action; no code argument. |
| `browser_use` | One focused Jev inspection for unfamiliar UI, retaining the same tab. |
| `browser_close` | Stop/save the owned profile before opening the next one. |

Before a new session run `jev-use doctor`. After upgrading, reload the harness's
MCP connection: the current Facebook schema supports 60-second browse chunks,
`tabs`, `page_target_id`, and `browser_facebook_run`. Doctor checks a fresh launcher, not a cached agent
connection. Missing tools require loading that connection. Do not use the
one-shot `jev-use call browser_open` fallback: its exit cleanup closes the profile.
Do not create a holder process or ad hoc MCP client.

Process ranges numerically; preserve an explicit list's requested order. Open
only the selected profiles' exact discovered IDs. Keep
one stable `run_id`, stable account keys, and a non-secret account/Page mapping.
Use observed saved cards and public account IDs, never guessed identities.
Preserve progress in a non-secret inventory: profile/account IDs, Page name,
stage, creation evidence, elapsed time, and blocker reason. Passwords belong
only in runtime `login`/`correct_password` arguments, never files or logs.

## Durable state and user scope

Before opening a profile, call `browser_facebook_run(action="status")` to read
the last run. This is local inspection and does not resume publication. Use its
saved `run_id` for an instruction to continue that batch. Read `status`,
`profiles`, `cursor`, `pending_action`, `interrupted_action`, `last_action`,
`saved_accounts`, and each profile's `checkpoints` before selecting work.

**The user's current instruction chooses the profiles.** There is no fixed
profile or default range. A saved cursor must never override a newly requested
selection. "Continue/resume" uses the saved selection; a new selection needs
its own scoped job while retaining prior account/Page history. If a genuinely
new job has no profile selection, ask which profiles instead of selecting all.

For a new job, create a stable ID and translate the user's instruction into one
selection mode in `start`:

| User instruction | Selection arguments |
| --- | --- |
| "Only Profile 7" | `profile_numbers: [7]` |
| "Profiles 10 to 15" | `profile_start: 10, profile_end: 15` |
| "Profiles 2, 7, and 19" | `profile_numbers: [2, 7, 19]` |
| A custom-named profile | Discover it and pass only its observed `profile_ids`. |

For example, when the user selected only Profile 7:

```json
{"action":"start","run_id":"my-batch","profile_numbers":[7]}
```

Use only one selection mode; never add profiles between separate list entries.
The exact IDs in `profiles` are the allowed set, including for sparse lists.
Missing or duplicate profiles are errors; never substitute the first available
profile. The saved selection and exact IDs are immutable for that run. If the user
requests a different selection, use a separate scoped job and retain old journals;
do not enlarge the old job. A new user request to create additional Pages uses a
new stable job ID even for the same profiles/accounts. Each job creates one new
Page per observed account; old successes do not complete the new request.
Never change IDs merely to retry an uncertain submission in an existing job.
Never interpret "23 to 29" as all profiles or continue with 30 after 29.

For an authorized continuation of a paused/interrupted run, call `resume` with
its saved `run_id`, then open its allowed profile and inspect account `status`
before taking the next action. Inspect the saved cursor account first when it
has unfinished work; then process remaining eligible accounts within the saved
range. Retain blocked/deferred work, rather than silently forgetting it when
advancing profiles. Accounts completed in this job are skipped. A `submitting`
checkpoint in this job permits confirmation only. A separately authorized new
job leaves older attempts untouched and creates its own additional Page; it does
not retry, delete or mark those older attempts complete. Keep Page details stable
per job and all prior records available for audit.

Each structured action saves its intent before execution and its result after
execution. If the agent/connection dies, `pending_action` remains. Treat this
as uncertain until explicit resume plus live inspection; never replay it blindly.
Saved ports and cursor/checkpoint state are observations, not proof of current
browser UI. Browser journals preserve partial Home/Reels progress and creation
guards. Always use the current returned port and verify the account identity.

On the user's stop instruction call `pause` when the connection permits, cease
new actions, and stop the worker. A pause blocks further structured actions;
already-submitted actions may still complete and must be inspected on resume.
Do not interpret a maintenance request or reading saved state as authorization
to resume publication. Keep passwords solely in runtime login arguments.

## Account flow

All `browser_facebook` calls include `port`, `run_id`, and `account`. On first
attachment, the tool prefers a unique saved-account chooser among Facebook tabs.
Subsequent calls retain the selected real tab and its popup watcher. Avoid
`browser_read`, which can create a separate read tab. If attachment is ambiguous,
call `action="tabs"` for observed IDs, inspect once, then supply the selected
`page_target_id` on the next action. Do not select by duplicate URL alone or
close unrelated tabs. Omit tab-selection arguments after attachment.

1. Restore/start the scoped run as above. Open its exact allowed GoLogin ID
   using `browser_open(..., run_id=RUN)`. Structured actions require the profile
   to be owned by this MCP connection; a supplied port alone cannot verify scope.
   Call `accounts` with `account="__inventory__"` and record all observed cards.
   If already signed in, resume that account's checkpoint with `status` first.
2. For each unprocessed account call `status`, then follow its returned
   `next_action`. Use the table below; do not restart a fixed sequence after a
   failure. A verified account ID takes precedence over a matching display name.
3. After verified `logged_out`, enumerate the chooser again and process the next
   unprocessed card. Only advance when the chooser is actually reachable.
4. Close/save the profile after its accounts are completed or recorded as
   blocked/skipped, then open the next requested profile. A close/save error
   retains ownership: do not open another profile or claim saving succeeded.

| Current stage / returned action | Do this |
| --- | --- |
| `started` / `login` | Supply observed `account_name`, verified `account_id` when available, and runtime password. The tool verifies the matching password form and actual signed-in identity. |
| `signed_in` / `wait_home` | Wait the required 20 seconds and dismiss supported alerts. |
| `home_ready` / `browse` | Repeat default 60-second chunks: **Home 60 seconds -> Reels 60 seconds -> Home 60 seconds**. Follow remaining progress. |
| `browsed` / `open_form` | Open Facebook Page creation. |
| `form_open` / `wait_form` | Wait another 20 seconds for alerts. |
| `form_ready` / `fill` | Supply stable `page_name`, observed/valid `category`, optional `bio`; select its category suggestion. |
| `submission_reserved` / `create` | Call `create` once. The tool validates actionability and identity, journals the attempt, then clicks. |
| `submitting` / `confirm` | Confirm actual creation. Never call Create again, including after a timeout. |
| `created` / `home` | Return Home after verified success. |
| `browse_after` | Browse Home for 30 seconds; one default chunk is capped to the remaining time. |
| `logout` | Log out and verify the saved-account chooser. |
| `logged_out` | Completed; do not create another Page for this account. |
| `inspect` / `mfa_required` | Inspect once, record the external challenge or exact blocker; do not bypass it. |

The browse tool varies scroll distances, directions, and pauses across short,
medium, and long ranges. Reels has longer viewing pauses. It follows an observed
Reels link and requires a visible video; Home cannot substitute for missing
Reels. Missing/blocked Reels navigation returns a blocker and preserves completed
Home time. Browsing performs no likes, comments, shares, or follows.

Target approximately 5-6 minutes per account from login through verified logout,
excluding profile startup. Required waits/browsing total 250 seconds. Navigation,
login and model/tool overhead can exceed the target; report actual elapsed time.
Do not shorten requested waits, invent success, or add sleeps between tool calls.

## Popups, recovery, and uncertain actions

The bridge checks browser chrome before and after actions and monitors long
steps. Results include `browserDialogs` and `nativeDialogsDismissed`. Supported
Sign in as credential choosers, notification prompts, and page JS alerts are
handled separately from Facebook password forms. A blocked/unavailable popup
inspector stops interaction. Unknown native dialogs require inspection; do not
click behind them or close arbitrary controls.

After an error or lost response, call `status` with the **same IDs**. Consider
`current_state`, `live_login`, checkpoint stage, and popup result together.
`loginObservation` describes a prior attempt and can be stale.

- A matching open password form with `loginSubmitted=false` resumes `login`
  with the runtime password. `retry_login` permits one stalled card selection
  retry only when its guard proves no password submission or active identity.
- A submitted login resumes waiting/inspection, never password replay. Use
  `correct_password` only once for an explicitly supplied correction after
  rejection or its supported stable-chooser recovery; never guess passwords.
- A verified signed-out chooser during precreation can resume the same account
  using `login` and its verified ID. Completed progress is retained and a fresh
  20-second alert settle is required after successful reauthentication.
- If status returns `home` because the creation form opened too early, return
  Home and finish browsing. If it returns `open_form` after navigation loss,
  reopen/settle/fill; completed browsing is retained.
- `submitting` allows confirmation only. `created` allows completion/logout.
  Missing success evidence, a disabled button, a search match, or a Page error
  does not permit another Create click. Capture exact success notice or observed
  Page identity; leave URL/ID unknown when not exposed.
- Preserve historical journals. Do not change run/account IDs to escape guards.
  An explicit user request to redo precreation permits `restart_from_run` only
  with the same verified ID and signed-out chooser. Reserved, submitting,
  created, and logged-out accounts cannot be restarted this way.
- After one focused inspection/correction, record an unresolved blocker. Skip
  missing credentials, MFA/CAPTCHA, restrictions, or unsupported UI accurately.
  Continue accounts only if the chooser is reachable; otherwise save/close the
  blocked profile and record its remaining work before advancing.

Report verified Pages and logout, blocked/pending accounts, captured evidence,
and actual elapsed times. Do not call a filled form or pending logout complete.

For callers explicitly requesting the older scripted prepare/finish mode only,
read [references/legacy-runner.md](legacy-runner.md). It is not the
structured agent workflow above.
