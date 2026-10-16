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
  async navigate(url, { timeout = 45000 } = {}) {
    // Commit waits for the destination response, not Facebook's slow SPA hydration.
    // The next step waits for its actual control/identity before acting.
    await dialogs.acceptNext();
    return page.goto(url, { waitUntil: 'commit', timeout });
  },
  async waitForLogin({ password, signedIn, timeout = 20000 }) {
    // The caller supplies observed selectors. A saved session may skip the password.
    const winner = await Promise.any([
      page.locator(password).waitFor({ state: 'visible', timeout }).then(() => 'password'),
      page.locator(signedIn).waitFor({ state: 'visible', timeout }).then(() => 'signed_in')
    ]).catch(() => { throw new Error('Neither password prompt nor signed-in marker appeared; inspect once instead of repeating clicks'); });
    return winner;
  },
  async visibleControl(candidates, timeout) {
    return Promise.any(candidates.map(async candidate => {
      await candidate.waitFor({ state: 'visible', timeout });
      return candidate;
    })).catch(() => { throw new Error('No known control became visible; inspect once for an observed selector override'); });
  },
  profileControl(selector, timeout) {
    return this.visibleControl(selector ? [page.locator(selector)] : [
      page.getByRole('button', { name: 'Your profile', exact: true }),
      page.locator('[aria-label="Your profile"]')
    ], timeout);
  },
  exactAccountName(accountName) {
    const words = String(accountName).trim().split(/\s+/).map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    return new RegExp('^\\s*' + words.join('\\s+') + '\\s*$');
  },
  async publicAccountIds(card) {
    return card.evaluate(element => {
      const ids = new Set();
      for (const attribute of ['data-userid', 'data-user-id', 'data-profileid', 'data-profile-id']) {
        const value = element.getAttribute(attribute);
        if (/^\d+$/.test(value || '')) ids.add(value);
      }
      for (const link of element.querySelectorAll('a[href]')) {
        try {
          const url = new URL(link.href, location.href);
          if (!/(^|\.)facebook\.com$/.test(url.hostname)) continue;
          const id = url.pathname === '/profile.php' ? url.searchParams.get('id') : url.pathname.match(/^\/(\d+)\/?$/)?.[1];
          if (/^\d+$/.test(id || '')) ids.add(id);
        } catch {}
      }
      return [...ids];
    });
  },
  async savedAccountCard({ accountName, accountId }) {
    const candidates = page.getByRole('button').filter({ hasText: this.exactAccountName(accountName) });
    if (typeof candidates.count !== 'function' || await candidates.count() <= 1) return candidates;
    if (accountId) {
      const matches = [];
      for (let index = 0; index < await candidates.count(); index++) {
        if ((await this.publicAccountIds(candidates.nth(index))).includes(accountId)) matches.push(index);
      }
      if (matches.length === 1) return candidates.nth(matches[0]);
    }
    const error = new Error('Multiple saved accounts have this exact name; inspect their observed public account IDs before selecting');
    error.code = 'ambiguous_saved_account';
    throw error;
  },
  async loginSavedAccount({ accountName, password, accountSelector, signedInSelector, timeout = 20000 }) {
    const card = await this.visibleControl(accountSelector ? [page.locator(accountSelector)] : [
      page.getByRole('button').filter({ hasText: this.exactAccountName(accountName) }),
      page.locator('div[role="button"]').filter({ hasText: this.exactAccountName(accountName) })
    ], timeout);
    await card.click();
    const input = page.locator('input[name="pass"]').first();
    const signedIn = this.profileControl(signedInSelector, timeout);
    const winner = await Promise.any([
      input.waitFor({ state: 'visible', timeout }).then(() => 'password'),
      signedIn.then(() => 'signed_in')
    ]).catch(() => { throw new Error('Login did not reach a password prompt or signed-in marker; inspect once'); });
    if (winner === 'password') {
      await input.fill(password);
      await input.press('Enter');
      await signedIn;
    }
    return winner;
  },
  async fillPage({ pageName, bio, category = 'Reel creator', nameSelector, categorySelector, optionSelector, bioSelector, timeout = 10000 }) {
    const name = await this.visibleControl(nameSelector ? [page.locator(nameSelector)] : [page.getByRole('textbox', { name: /^Page name/i }), page.getByLabel(/^Page name/i)], timeout);
    const categories = await this.visibleControl(categorySelector ? [page.locator(categorySelector)] : [page.getByRole('combobox', { name: /^Category/i }), page.getByLabel(/^Category/i)], timeout);
    const description = await this.visibleControl(bioSelector ? [page.locator(bioSelector)] : [
      page.getByRole('textbox', { name: /^Bio/i }),
      page.getByLabel(/^Bio/i),
      page.locator('textarea[name*="bio" i], textarea[placeholder*="bio" i]')
    ], timeout);
    await name.fill(pageName);
    await categories.fill(category);
    // Facebook renders the suggestion in a listbox. Prefer the option itself;
    // the visible text leaf can sit under a pointer-blocking overlay.
    const option = optionSelector ? page.locator(optionSelector) : page.getByRole('option', { name: category, exact: true });
    try {
      if (!optionSelector && (typeof option.count !== 'function' || await option.count() === 0)) throw new Error('category option not present');
      await option.waitFor({ state: 'visible', timeout: Math.min(timeout, 3000) });
      await option.click({ timeout: Math.min(timeout, 3000) });
    } catch {
      const fallback = page.getByText(category, { exact: true }).last();
      await fallback.waitFor({ state: 'visible', timeout });
      await fallback.click({ timeout: Math.min(timeout, 3000) });
    }
    await description.fill(bio);
    await this.dismissPagePrompts();
    await this.creationControl().click({ trial: true, timeout });
    return { formReady: true };
  },
  async dismissPagePrompts() {
    // Handle permission/notification banners rendered inside the page without
    // touching arbitrary content. Browser chrome prompts are handled by the
    // bridge's native-dialog watcher.
    const prompts = page.locator('[role="dialog"], [aria-modal="true"]');
    const count = await prompts.count().catch(() => 0);
    for (let index = 0; index < count; index++) {
      const prompt = prompts.nth(index);
      if (typeof prompt.isVisible !== 'function' || !await prompt.isVisible().catch(() => false)) continue;
      for (const label of [/^(Close|Dismiss|Not now|No thanks|Block)$/i]) {
        const button = prompt.getByRole('button', { name: label }).first();
        if (await button.isVisible().catch(() => false)) {
          await button.click({ timeout: 1500 }).catch(() => {});
          break;
        }
      }
    }
  },
  async settleAlerts({ seconds = 20 } = {}) {
    // Passive watch after login and after reaching the creation form: the CDP
    // bridge auto-accepts page-owned JS alerts while overlay dismissal is on,
    // so here we repeatedly clear in-page permission/notification prompts
    // (e.g. "Turn on notifications") without touching other content.
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > 60) throw new Error('Alert settle must be 0–60 seconds');
    const started = Date.now();
    const deadline = started + seconds * 1000;
    let dismissed = 0;
    while (Date.now() < deadline) {
      const before = Date.now();
      try {
        const prompts = page.locator('[role="dialog"], [aria-modal="true"]');
        if (await prompts.count().catch(() => 0)) {
          await this.dismissPagePrompts();
          dismissed++;
        }
      } catch { /* A rerendered dialog is retried on the next pass. */ }
      await page.waitForTimeout(Math.min(1000, Math.max(0, deadline - Date.now())));
      if (Date.now() === before) break; // Clock did not advance; avoid a hot loop.
    }
    return { settledMs: Date.now() - started, promptsDismissed: dismissed };
  },
  async listSavedAccounts({ timeout = 15000 } = {}) {
    // Read-only enumeration of the "Log into Facebook" saved-account chooser.
    // Returns visible card texts in DOM order; the caller matches them against
    // its account inventory. Never clicks, fills, or consumes reservations.
    await page.getByText(/^Log into Facebook$/i).first().waitFor({ state: 'visible', timeout }).catch(() => {});
    const cards = page.locator('div[role="button"]');
    const count = await cards.count().catch(() => 0);
    const names = [];
    for (let index = 0; index < count; index++) {
      const text = await cards.nth(index).innerText().catch(() => '');
      const name = String(text || '').replace(/\s+/g, ' ').trim();
      if (name) names.push(name);
    }
    return names;
  },
  async listSavedAccountCards() {
    const cards = page.locator('div[role="button"]');
    const observed = [];
    for (let index = 0; index < await cards.count(); index++) {
      const card = cards.nth(index);
      if (!await card.isVisible().catch(() => false)) continue;
      const name = String(await card.innerText()).replace(/\s+/g, ' ').trim();
      if (!name || /^(Use another profile|Add account|Remove account|Log in|Create new account)$/i.test(name)) continue;
      observed.push({ name, publicAccountIds: await this.publicAccountIds(card) });
    }
    return observed;
  },
  async browseFeed({ seconds = 30, discoverVideoSurface = false } = {}) {
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > 180) throw new Error('Feed duration must be 0–180 seconds');
    const checkpoint = this.status();
    if (checkpoint.browsed || checkpoint.stage === 'created' || checkpoint.stage === 'logged_out') return checkpoint.browsing;
    const started = Date.now();
    let deadline = started + seconds * 1000;
    let navigationMs = 0;
    let scrolls = 0;
    let playingObserved = false;
    let videoSurfaceTried = false;
    while (Date.now() < deadline) {
      // Observe visible feed videos only. Start muted for autoplay, then unmute
      // and retry play so Facebook's player reports genuine playback reliably.
      const videos = page.locator('video:visible');
      const count = await videos.count();
      for (let index = 0; index < count; index++) {
        try {
          const playing = await videos.nth(index).evaluate(video => {
            video.muted = true;
            const playing = !video.paused && video.readyState >= 2;
            // play() can remain pending during buffering; never await it here.
            if (video.paused) video.play().catch(() => {});
            return playing;
          });
          playingObserved ||= playing;
        } catch { /* A scrolled-away video can disappear; inspect on the next pass. */ }
      }
      // Observe with human-like, uneven pacing: random pauses and scroll
      // distances (mostly down, occasionally back up) instead of fixed steps.
      const pause = 1500 + Math.random() * 3000;
      await page.waitForTimeout(Math.min(pause, Math.max(0, deadline - Date.now())));
      for (let index = 0; index < count; index++) {
        try {
          playingObserved ||= await videos.nth(index).evaluate(video => !video.paused && video.readyState >= 2);
          if (!playingObserved) {
            playingObserved ||= await videos.nth(index).evaluate(video => {
              video.muted = false;
              video.volume = 0;
              if (video.paused) video.play().catch(() => {});
              return !video.paused && video.readyState >= 2;
            });
          }
        }
        catch { /* Feed rerendered; retry on the next sample. */ }
      }
      if (discoverVideoSurface && !playingObserved && !videoSurfaceTried && Date.now() - started >= 6000) {
        videoSurfaceTried = true;
        const link = page.getByRole('link', { name: /^(Reels|Videos)$/i }).first();
        if (await link.count()) {
          const href = await link.getAttribute('href');
          if (href) {
            const target = new URL(href, page.url());
            if (target.protocol === 'https:' && /(^|\.)facebook\.com$/.test(target.hostname)) {
              const navigationStarted = Date.now();
              await this.navigate(target.href);
              const elapsed = Date.now() - navigationStarted;
              navigationMs += elapsed;
              deadline += elapsed; // Loading a different surface is not browsing time.
            }
          }
        }
      }
      const distance = 300 + Math.floor(Math.random() * 500);
      if (page.mouse?.wheel) {
        if (Math.random() < 0.85) await page.mouse.wheel(0, distance).catch(() => {});
        else await page.mouse.wheel(0, -Math.floor(distance / 2)).catch(() => {});
      }
      scrolls++;
      // The randomized playback observation above supplies the requested browsing time.
    }
    checkpoint.browsed = Date.now() - started - navigationMs >= 30000 && playingObserved;
    checkpoint.browsing = { elapsedMs: Date.now() - started, navigationMs, scrolls, playingObserved };
    checkpoint.stage = checkpoint.browsed ? 'browsed' : 'browse_incomplete';
    return checkpoint.browsing;
  },
  beforeCreate(pageName) {
    const checkpoint = this.status();
    if (['submission_reserved', 'submitting', 'created', 'logged_out'].includes(checkpoint.stage)) {
      throw new Error(`Creation already ${checkpoint.stage} for this run/account. Inspect the saved checkpoint; do not submit again.`);
    }
    if (!checkpoint.browsed) throw new Error('Complete workflow.browseFeed with the configured duration before creating the Page');
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
  async logout({ chooserSelector = 'text=Use another profile', profileSelector, logoutSelector, timeout = 45000 } = {}) {
    if (this.status().stage !== 'created') throw new Error('Confirm creation before logout');
    // Optional wizard: accept leaving once; never invent tokenless logout URLs.
    await this.navigate('https://www.facebook.com/', { timeout });
    const chooser = page.locator(chooserSelector);
    if (!await chooser.isVisible()) {
      const profile = await this.profileControl(profileSelector, timeout);
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
