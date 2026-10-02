// Injected into BetterWright's sandbox. No Node or credentials in persisted state.
const workflow = {
  begin(runId, account) {
    if (!runId || !account) throw new Error('workflow.begin requires a stable run ID and account');
    state.jevWorkflow ||= {};
    const key = JSON.stringify([String(runId), String(account)]);
    state.jevWorkflow[key] ||= { runId: String(runId), account: String(account), stage: 'started' };
    state.jevWorkflowActive = key;
    return state.jevWorkflow[key];
  },
  status() {
    const checkpoint = state.jevWorkflow?.[state.jevWorkflowActive];
    if (!checkpoint) throw new Error('Call workflow.begin first');
    return checkpoint;
  },
  async waitForLogin({ password, signedIn, timeout = 20000 }) {
    // The caller supplies observed selectors. A saved session may skip the password.
    const winner = await Promise.any([
      page.locator(password).waitFor({ state: 'visible', timeout }).then(() => 'password'),
      page.locator(signedIn).waitFor({ state: 'visible', timeout }).then(() => 'signed_in')
    ]).catch(() => { throw new Error('Neither password prompt nor signed-in marker appeared; inspect once instead of repeating clicks'); });
    return winner;
  },
  async browseFeed({ seconds = 30 } = {}) {
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > 60) throw new Error('Feed duration must be 0–60 seconds');
    const checkpoint = this.status();
    if (checkpoint.browsed || checkpoint.stage === 'created' || checkpoint.stage === 'logged_out') return checkpoint.browsing;
    const started = Date.now();
    const deadline = started + seconds * 1000;
    let scrolls = 0;
    let playingObserved = false;
    while (Date.now() < deadline) {
      // Observe visible feed videos only. Muted playback avoids unexpected audio.
      const visible = page.locator('video:visible').first();
      if (await visible.count()) {
        try {
          await visible.evaluate(async video => {
            video.muted = true;
            await video.play();
          });
          playingObserved ||= await visible.evaluate(video => !video.paused && video.readyState >= 2);
        } catch { /* Autoplay/site restrictions: report, never invent playback. */ }
      }
      await page.mouse.wheel(0, 550);
      scrolls++;
      // Deliberate user-requested browsing time, not a form/navigation settle wait.
      await page.waitForTimeout(Math.min(3000, Math.max(0, deadline - Date.now())));
    }
    checkpoint.browsed = Date.now() - started >= 30000 && playingObserved;
    checkpoint.browsing = { elapsedMs: Date.now() - started, scrolls, playingObserved };
    checkpoint.stage = checkpoint.browsed ? 'browsed' : 'browse_incomplete';
    return checkpoint.browsing;
  },
  beforeCreate(pageName) {
    const checkpoint = this.status();
    if (['submission_reserved', 'submitting', 'created', 'logged_out'].includes(checkpoint.stage)) {
      throw new Error(`Creation already ${checkpoint.stage} for this run/account. Inspect the saved checkpoint; do not submit again.`);
    }
    if (!checkpoint.browsed) throw new Error('Complete workflow.browseFeed({seconds:30}) before creating the Page');
    checkpoint.pageName = String(pageName);
    checkpoint.stage = 'submission_reserved';
    return checkpoint;
  },
  async submitCreation({ selector, timeout = 10000 }) {
    const checkpoint = this.status();
    const key = state.jevWorkflowActive;
    const permits = state.jevSubmissionPermits || [];
    if (checkpoint.stage !== 'submitting' || !permits.includes(key)) {
      throw new Error('Persist workflow.beforeCreate in a separate script call first. A reserved submission can only be attempted once; inspect uncertain results before continuing.');
    }
    state.jevSubmissionPermits = permits.filter(item => item !== key);
    await page.locator(selector).click({ timeout });
  },
  async confirmCreated({ selector, expectedText, pageUrl = null, pageId = null, timeout = 20000 }) {
    const checkpoint = this.status();
    if (checkpoint.stage !== 'submitting') throw new Error('No pending creation to confirm');
    if (!expectedText || !expectedText.includes(checkpoint.pageName)) throw new Error('Confirmation must include the exact submitted Page name');
    if (!/was created|you.ve created|page created|success/i.test(expectedText) && !pageUrl && !pageId) throw new Error('Require a creation notice or the observed new Page ID/URL, not a search result or filled input');
    const confirmation = page.locator(selector).filter({ hasText: expectedText });
    await confirmation.waitFor({ state: 'visible', timeout });
    checkpoint.confirmation = { text: expectedText, pageUrl, pageId };
    checkpoint.stage = 'created';
    return checkpoint;
  },
  loggedOut() {
    const checkpoint = this.status();
    if (checkpoint.stage !== 'created') throw new Error('Confirm creation before recording logout');
    checkpoint.stage = 'logged_out';
    return checkpoint;
  }
};
