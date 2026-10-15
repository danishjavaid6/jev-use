---
name: facebook-create-pages
description: Create Facebook Pages across user-selected GoLogin profiles using a bounded next-step tool, durable checkpoints, popup handling, and verified logout.
---

Use one persistent jev-use MCP connection. The user's instruction selects the
profiles and authorizes work. Reading this skill or maintaining the project does
not start publication. Do not launch a script/holder process or another worker.

## Start or resume

1. Run `jev-use doctor` before a new session. Reload the MCP connection after
   upgrading; a fresh doctor probe does not update a cached connection. Required
   tools: `browser_facebook_run`, `browser_facebook_next`.
2. Read `browser_facebook_run(action="status")` to recover prior scope and progress.
   Distinguish a continuation from a new request for additional Pages. "Resume"
   uses the saved `run_id`; "create again" or "create more" starts a new job,
   even when the selected profiles/accounts are the same. A fresh job requests
   one additional Page in each observed account. Never start publication for a
   maintenance-only instruction.
3. For a new job, call `browser_facebook_run(action="start", run_id=STABLE_ID, ...)`
   with exactly ONE user-selected mode: `profile_numbers=[7]` for one profile,
   `profile_numbers=[2,7,19]` for a list, `profile_start=23, profile_end=29` for a
   range, or observed `profile_ids` for custom names. Discover named IDs with
   `browser_profiles`. No profile selection is assumed. A new selection uses its
   own run; retain old journals and creation evidence.
4. Call `browser_facebook_next(run_id=RUN)`. Repeat only as directed below. It
   owns opening/closing GoLogin, observed account selection, live status, popup
   checks and ONE bounded workflow action per call. Do not choose ports, account
   keys, action sequences, or tabs yourself in this mode.

Records belong to `(profile, run_id, account)`. Skip only accounts completed in
the current job. Prior Pages never satisfy a new request: the same account can
create another Page in another authorized job. Keep one stable run ID through
all retries of a job; never invent new IDs to retry its uncertain Create click.
Older unfinished requests remain recorded separately and are not replayed,
marked complete, or deleted when another Page is requested. The response's
`history` counts them separately from current-job progress. Page details stay
stable within a job; choose the user's new Page details for the next request.

The controller creates one dedicated Facebook tab and retains its exact target
ID. Other restored Facebook tabs are left untouched. Do not override that tab
or attach by URL. A lost tab with a reserved/pending creation requires inspection,
not a replacement tab or another Create click. One live MCP worker owns the run
until its profile is closed/saved; another worker receives a blocker.

## The small-agent loop

| Returned status | Agent action |
| --- | --- |
| `continue` | Call the exact `next_call` immediately. Do not add sleeps. `requires_fresh_inspection` means the next call inspects before acting. |
| `needs_input` | Supply only the `required` fields for the returned account, retaining `run_id`. Use already supplied user inputs; ask only for missing information. |
| `needs_resume` | Read run status. For an authorized continuation, call `resume`, then call next again. Never replay the interrupted action directly. |
| `paused` | Stop. Resume only when the user's current instruction authorizes continuation. |
| `blocked` | Inspect the reason. If offered, call `browser_facebook_vision` once for read-only diagnosis, then report unresolved work. Do not loop or escape guards with scripts/new run IDs. |
| `completed` | Report completion; the tool verified creation evidence, logout, inventories and profile closure. |

Live-status reads get up to three attempts inside one `next` call, with short
1s/2s delays and SDK reconnection to the same verified tab. No login, fill,
Create or logout action is replayed by this recovery. A temporary unknown UI is
reobserved too. `live_status_checks` reports attempt count, reconnections and
sanitized causes when recovery was needed. A pause interrupts further attempts.
After all three reads fail, use the offered diagnosis once and stop; do not
start another job or keep retrying indefinitely.

Supply a common password through the runtime `password` argument once when
requested (or on the first next call). It stays only in this MCP process, scoped
to the run, and expires after 30 minutes idle; pause, completion or process restart
removes it. After restart, resupply the user's known password when requested.
Never put credentials in files, logs, Page details, or run-state tools. For
rejection, use `corrected_password` only for an explicit user correction. Do not
guess, keep trying the same rejected password, or bypass MFA/CAPTCHA.

When asked for `page_name` and `category`, use the user's stable mapping for the
returned profile/account. Optional `bio` applies to that account. The backend
persists these non-secret details before filling; never invent a different name
after a failed fill or uncertain submission.

On stop, call `browser_facebook_run(action="pause", run_id=RUN)` and cease calls.
Call `browser_close` to save/close the profile owned by this connection when
safe; report any closure failure explicitly. A blocked job also needs this
cleanup. Never leave a browser open without reporting it.
A submitted action may already have committed: preserve state for inspection on
resume. Only one worker may own a run. If the agent/connection dies, resume from
saved state rather than starting over. An uncertain submission in this job
must be inspected/confirmed, never clicked again. A separately authorized new
job creates an additional Page without changing an older job's reservation.
An unfinished account in the current run still requires reconciliation before
switching to another account.

## What the tool executes

Login -> settle alerts 20 seconds -> Home 60 seconds -> Reels 60 seconds ->
Home 60 seconds -> open Page creation -> settle alerts 20 seconds -> fill ->
create once -> confirm creation -> Home 30 seconds -> logout and verify chooser.
Scroll distances, directions and pauses vary. Reels requires observed navigation
and visible video. Target 5-6 minutes per account; never shorten required waits to
meet that target. Network/account challenges can extend it.

Supported browser/native popups are checked around actions and during long steps.
Unknown or uninspectable popups stop interaction. Vision uses Jev 1.14 and the
image header for experimental, read-only screen/popup diagnosis. Its viewport
capture cannot see browser chrome; its coordinates never authorize clicks.
Identity, submission and success still require live structured evidence.

A blocked account means the job is unfinished. Report the profile/account,
reason, verified successes, pending work and actual timings. Never call a filled
form or created Page with pending logout complete. Repeated failures stop after
two attempts; repairing a blocker may require a focused structured action.
While the controller owns a profile, raw browser actions and lower-level
mutations are refused. For explicitly requested manual recovery, pause and
close/save first, then resume the authorized run and reopen with `browser_open`
for the documented structured recovery action. Never do this as an automatic
fallback after a blocker.

An unmatched password form can return `reset_login_view`. The backend returns
to the chooser once only when no password was submitted. Submitted login and
creation attempts keep their original guards. Vision failures report `phase`
(`tab_selection`, `capture`, or `analysis`) so report the actual failing stage.

For maintenance or a focused manual recovery, read
[structured actions](references/structured-actions.md). For an explicitly
requested older scripted workflow, read [legacy mode](references/legacy-runner.md).
