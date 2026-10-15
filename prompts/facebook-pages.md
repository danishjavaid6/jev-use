# Start or resume the Facebook Page workflow

Give the agent this prompt after reloading the updated jev-use MCP connection.
Supply credentials privately at runtime; do not save them in this prompt file.

```text
Use $facebook-create-pages. Read its SKILL.md and follow the structured
browser_facebook actions through one persistent jev-use MCP connection.

Process only the GoLogin profiles/accounts I authorize for this run. Use the
runtime credentials and stable Page-name/category mapping I supply. Discover
exact profile IDs; resume from status and existing checkpoints before acting.
Do not repeat completed or uncertain creation attempts.

First call browser_facebook_run status to recover the last scope, cursor,
pending action, saved account mapping, and checkpoint history. Resume the saved
run only when this request authorizes work. For a new job, start a run with
profile_numbers for a single profile/list, profile_start/profile_end for a range,
or observed profile_ids for named profiles, exactly matching my instruction.
The user chooses the profiles for each job; do not reuse an old range when a
different selection is requested. Pass its run_id
to browser_open and all structured steps. If I request Profiles 23 to 29, work
only those exact profiles and stop at 29; never broaden to all profiles. After
an interruption, inspect the cursor account's current status before acting.
If I say stop, pause the saved run and cease new actions.

For each account: login; settle alerts 20 seconds; browse Home 60 seconds,
Reels 60 seconds, Home 60 seconds with varied pauses and scrolls; open Page
creation; settle alerts 20 seconds; fill; create once; verify success; return
Home; browse 30 seconds; logout and verify the chooser. Finish all unprocessed
accounts, save/close the profile, then advance. Target 5-6 minutes without
shortening required browsing; report actual time and blockers.

If something unexpected happens, inspect current state, handle supported
popups, and resume the returned next_action. Preserve IDs, journals, and
creation guards. Never bypass MFA/CAPTCHA/restrictions or guess passwords.
Record verified successes and blocked/pending work. If I stop creation,
stop publishing immediately and perform only requested cleanup/maintenance.
```

The complete maintained instructions are in
[the skill](../skills/facebook-create-pages/SKILL.md).
