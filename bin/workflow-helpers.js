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
  async loginSavedAccount({ accountName, password, accountSelector, signedInSelector, timeout = 20000 }) {
    const card = accountSelector ? page.locator(accountSelector) : page.getByRole('button').filter({ hasText: accountName });
    await card.waitFor({ state: 'visible', timeout });
    await card.click();
    const input = page.locator('input[name="pass"]').first();
    const signedIn = signedInSelector ? page.locator(signedInSelector) : page.getByRole('button', { name: 'Your profile', exact: true });
    const winner = await Promise.any([
      input.waitFor({ state: 'visible', timeout }).then(() => 'password'),
      signedIn.waitFor({ state: 'visible', timeout }).then(() => 'signed_in')
    ]).catch(() => { throw new Error('Login did not reach a password prompt or signed-in marker; inspect once'); });
    if (winner === 'password') {
      await input.fill(password);
      await input.press('Enter');
      await signedIn.waitFor({ state: 'visible', timeout });
    }
    return winner;
  },
  async fillPage({ pageName, bio, category = 'Reel creator', nameSelector, categorySelector, optionSelector, bioSelector, timeout = 10000 }) {
    const name = nameSelector ? page.locator(nameSelector) : page.getByRole('textbox', { name: /^Page name/i });
    const categories = categorySelector ? page.locator(categorySelector) : page.getByRole('combobox', { name: /^Category/i });
    const description = bioSelector ? page.locator(bioSelector) : page.getByRole('textbox', { name: /^Bio/i });
    await name.fill(pageName);
    await categories.fill(category);
    const option = optionSelector ? page.locator(optionSelector) : page.getByText(category, { exact: true }).last();
    await option.waitFor({ state: 'visible', timeout });
    await option.click();
    await description.fill(bio);
    await this.creationControl().click({ trial: true, timeout });
    return { formReady: true };
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
  creationControl(selector) {
    return selector ? page.locator(selector) : page.getByRole('button', { name: 'Create Page', exact: true });
  },
  async validateCreation({ selector, timeout = 5000 } = {}) {
    const checkpoint = this.status();
    if (checkpoint.stage !== 'submission_reserved') throw new Error(`Creation is ${checkpoint.stage}; inspect existing results, never manually click again`);
    // Trial performs actionability checks without clicking or consuming the reservation.
    await this.creationControl(selector).click({ trial: true, timeout });
  },
  async submitCreation({ selector, timeout = 10000 } = {}) {
    const checkpoint = this.status();
    const key = state.jevWorkflowActive;
    const permits = state.jevSubmissionPermits || [];
    if (checkpoint.stage !== 'submitting' || !permits.includes(key)) {
      throw new Error('Use browser_script submission={run_id,account} after beforeCreate. Creation can only be attempted once; inspect uncertain results, never use a raw click fallback.');
    }
    state.jevSubmissionPermits = permits.filter(item => item !== key);
    await this.creationControl(selector).click({ timeout });
  },
  async confirmCreated({ selector, expectedText, pageUrl = null, pageId = null, timeout = 30000 } = {}) {
    const checkpoint = this.status();
    if (checkpoint.stage === 'created' || checkpoint.stage === 'logged_out') return checkpoint;
    if (checkpoint.stage !== 'submitting') throw new Error('No pending creation to confirm');
    let confirmation;
    if (selector && expectedText) {
      if (!expectedText.includes(checkpoint.pageName)) throw new Error('Confirmation must include the exact submitted Page name');
      if (!/was created|you.ve created|page created|success/i.test(expectedText) && !pageUrl && !pageId) throw new Error('Require a creation notice or the observed new Page ID/URL, not a search result or filled input');
      confirmation = page.locator(selector).filter({ hasText: expectedText });
    } else {
      const name = checkpoint.pageName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      confirmation = page.getByText(new RegExp(`(?:you.ve created.*${name}|${name}.*was created)`, 'i')).last();
    }
    await confirmation.waitFor({ state: 'visible', timeout });
    checkpoint.confirmation = { text: expectedText || await confirmation.innerText(), pageUrl, pageId };
    checkpoint.stage = 'created';
    return checkpoint;
  },
  async logout({ chooserSelector = 'text=Use another profile', profileSelector, logoutSelector, timeout = 20000 } = {}) {
    if (this.status().stage !== 'created') throw new Error('Confirm creation before logout');
    // Optional wizard: accept leaving once; never invent tokenless logout URLs.
    await dialogs.acceptNext();
    await page.goto('https://www.facebook.com/', { waitUntil: 'domcontentloaded', timeout });
    const chooser = page.locator(chooserSelector);
    if (!await chooser.isVisible()) {
      const profile = profileSelector ? page.locator(profileSelector) : page.getByRole('button', { name: 'Your profile', exact: true });
      await profile.waitFor({ state: 'visible', timeout });
      await profile.click();
      const logout = logoutSelector ? page.locator(logoutSelector) : page.getByText(/^Log out$/i).last();
      await logout.waitFor({ state: 'visible', timeout });
      await dialogs.acceptNext();
      try { await logout.click({ timeout }); }
      catch (error) {
        if (!/execution context.*destroyed|navigation|target.*closed/i.test(error.message)) throw error;
      }
    }
    await chooser.waitFor({ state: 'visible', timeout });
    return this.loggedOut();
  },
  loggedOut() {
    const checkpoint = this.status();
    if (checkpoint.stage !== 'created') throw new Error('Confirm creation before recording logout');
    checkpoint.stage = 'logged_out';
    return checkpoint;
  }
};
