---
name: facebook-create-pages
description: Create Facebook Pages across saved accounts in an existing GoLogin profile using a reusable runner, verified creation checkpoints, and logout between accounts.
---

Use this skill for the user's authorized Page-creation task. Keep GoLogin as the
browser owner. Use the jev-use MCP tools and their returned port; never start a
second browser, clear cookies, change proxy/fingerprint settings, or use a shell
CDP driver while MCP is attached.

The executable [scripts/account-runner.js](scripts/account-runner.js) is bundled
and injected into `browser_script` as `facebookPages`. Invoke it directly; do not
rewrite the login, category, creation, wizard, or logout logic for each account.
No live mutation is authorized merely by loading this skill.

## Setup once

1. Load `browser_profiles`, `browser_open`, `browser_script`, `browser_close`.
2. Find the exact requested GoLogin profile; open with `vendor="gologin"` and
   `headless=false` if needed. Reuse its port throughout the task.
3. Inspect the saved-account chooser once. Use the observed accounts and the
   requested count. Create one stable run ID and an account/Page-name mapping.
   If names are left to you, choose them once and keep them on retries.
4. Reuse the task-supplied password only for login; do not save credentials in
   the runner, run IDs, journals, screenshots, or files.

## Two calls per account

Use the same config and checkpoint scope in both calls. `account` is a stable
account identifier or the observed saved-account name; `account_name` is needed
only when those differ. The runner checks the active identity on resumption.

First call: `browser_script(port=N, timeout=150, code=...)`:

```js
return await facebookPages.prepare({
  run_id: RUN_ID,
  account: ACCOUNT,
  page_name: PAGE_NAME,
  bio: BIO,
  password: TASK_PASSWORD
});
```

Replace uppercase variables with task values in the invocation, without storing
the password in an external file. Keep default `dismiss_overlays=true`; the
Sign in as credential chooser is distinct from the Facebook password form.

Preparation selects the saved account, handles either password or remembered
login, scrolls/plays the feed for 30 seconds, fills the Page form, verifies it is
ready, and saves `submission_reserved` without creating the Page. If the user
changes the browsing requirement, adapt that step deliberately rather than
claiming it occurred. No observed playback means pause for one focused inspection.

If `submission_reserved`, second call:

```text
browser_script:
  port: N
  timeout: 120
  submission: {run_id: RUN_ID, account: ACCOUNT}
  code: return await facebookPages.finish({run_id: RUN_ID, account: ACCOUNT, page_name: PAGE_NAME});
```

The submission argument checks actionability first, journals the attempt, and
clicks Create Page's button role once (works for `div[role="button"]`). The runner
then waits for the exact delayed success notice, accepts the optional wizard's
beforeunload, logs out through the account menu, and verifies the chooser returns.
Continue to the next account only after `logged_out`.

## Resume and unexpected UI

- `logged_out`: account complete; do not create again.
- `created`: run `facebookPages.finish` WITHOUT `submission` to finish logout.
- `submitting`: run `facebookPages.finish` WITHOUT `submission` to confirm and
  log out. If the notice is gone, inspect the actual Page identity once and use
  an observed confirmation selector/URL/ID. An unchanged URL, disabled button,
  search match, or "already manage a Page" error does not authorize another click.
- `submission_reserved`: inspection does not consume it. A missing selector
  fails before clicking, so correct it once and retry the submission argument.
- Other stages: correct the observed problem and retry preparation with the SAME
  run ID/account. Never erase checkpoints or generate new IDs to bypass a lock.

Selector overrides in the runner are only for demonstrated UI changes:
`account_selector`, `signed_in_selector`, `name_selector`, `category_selector`,
`option_selector`, `bio_selector`, `create_selector`; finish accepts `confirmation`
and `logout` options from the workflow helpers. Pass `create_selector` in the
submission argument as `selector` too. Do not guess selectors or URLs.
For externally opened profiles, keep a stable non-secret `checkpoint_scope` on
all calls; profiles opened by this MCP server use the GoLogin ID automatically.
If an older checkpoint lacks `accountId`, inspect identity before migrating it;
never infer that it is the currently signed-in account.

Stop after one focused inspection/correction if the issue remains unresolved.
Hand off MFA/CAPTCHA/account restrictions or unsupported native dialogs. Never
bypass creation guards with a manual click, probe hidden APIs, clear cookies, or
perform a logout URL experiment. Batch known steps rather than narrate each click.

## Finish

Call `browser_close` to save the GoLogin profile. Report each account's Page name,
confirmed stage, captured Page URL/ID when available, and unfinished work. Include
elapsed time; do not call pending creation or pending logout complete.
