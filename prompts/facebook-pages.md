# Start or resume with a small agent

Reload the updated jev-use MCP connection first. Supply credentials privately at
runtime; never save them in this file. Replace the selection with the actual
profiles the user requested.

```text
Use $facebook-create-pages. Read SKILL.md. Work through one persistent jev-use
MCP connection, using browser_facebook_next as the execution interface.

Read browser_facebook_run status. For this authorized continuation, resume its
saved run if needed. For a new job, start a stable run with exactly the profiles
I selected: one profile, numeric range, explicit list, or observed named IDs.
Never let a saved cursor override a new selection. Preserve prior journals.

Call browser_facebook_next with that run_id. When status is continue, execute
its exact next_call. When needs_input, supply the requested fields for the
returned account using my credentials and stable Page-name/category mapping.
Ask only for inputs I have not provided. Runtime password remains in server
memory, never files. After restart, resupply it when requested. Only use an
explicit user correction for corrected_password.

For needs_resume, read run status and resume this authorized job, then call
next. For blocked, use offered read-only vision diagnosis once and report the
unresolved blocker. Do not loop, use raw scripts, guess passwords, change run
IDs to escape guards, or bypass MFA/CAPTCHA. Only report completed when the
tool returns completed. If I say stop, pause the run and cease new actions.

The backend owns account/profile selection, popup checks, current-state
recovery, Home/Reels/Home browsing with varied pacing, single creation,
confirmation, postcreation browsing, logout and profile saving. Preserve all
required waits. Report actual results, timings and unfinished work honestly.
```

See [the skill](../skills/facebook-create-pages/SKILL.md) for status handling.
