// Runs inside the existing BetterWright sandbox; never starts another browser.
const facebookPages = {
  version: '2',
  config(options) {
    for (const key of ['run_id', 'account', 'page_name']) {
      if (typeof options[key] !== 'string' || !options[key].trim()) throw new Error(`${key} must be a non-empty string`);
    }
    return options;
  },
  async measure(checkpoint, step, operation) {
    const started = Date.now();
    try { return await operation(); }
    finally {
      checkpoint.timings ||= {};
      checkpoint.timings[step] = (checkpoint.timings[step] || 0) + Date.now() - started;
    }
  },
  async activeAccountId() {
    // Read only the non-secret identity cookie. Never return authentication cookies.
    return page.evaluate(() => {
      if (!/(^|\.)facebook\.com$/.test(location.hostname)) return null;
      return document.cookie.split(';').map(item => item.trim()).find(item => item.startsWith('c_user='))?.slice(7) || null;
    });
  },
  async login(config, checkpoint, timeout = 60000) {
    const accountName = config.account_name || config.account;
    const deadline = Date.now() + timeout;
    const input = page.locator('input[name="pass"]').first();
    const card = config.account_selector ? page.locator(config.account_selector)
      : page.getByRole('button').filter({ hasText: accountName });
    while (Date.now() < deadline) {
      const identity = await page.evaluate(expectedName => {
        if (!/(^|\.)facebook\.com$/.test(location.hostname)) return null;
        const id = document.cookie.split(';').map(item => item.trim()).find(item => item.startsWith('c_user='))?.slice(7);
        if (!id) return null;
        const normalize = text => text.replace(/\s+/g, ' ').trim();
        const selfLinks = Array.from(document.querySelectorAll('a[href]')).filter(link => {
          const url = new URL(link.href, location.href);
          return /(^|\.)facebook\.com$/.test(url.hostname) &&
            (url.searchParams.get('id') === id || url.pathname === '/' + id || url.pathname === '/' + id + '/');
        });
        return { id, matched: selfLinks.some(link => normalize(link.innerText || '') === normalize(expectedName)) };
      }, accountName).catch(error => {
        if (/execution context.*destroyed|navigation|cannot find context/i.test(error.message)) return null;
        throw error;
      });
      if (identity && (identity.matched || config.account_id === identity.id)) {
        checkpoint.accountId = identity.id;
        return 'signed_in';
      }
      // A cookie alone never authorizes creating a Page for an unverified account.
      if (!identity && !checkpoint.loginSubmitted && await input.isVisible().catch(() => false)) {
        if (!config.password) throw new Error('Password prompt appeared but no task password was supplied');
        await input.fill(config.password);
        checkpoint.loginSubmitted = true;
        await input.press('Enter');
      } else if (!identity && !checkpoint.loginCardClicked && await card.isVisible().catch(() => false)) {
        checkpoint.loginCardClicked = true;
        await card.click();
      }
      await page.waitForTimeout(500); // Bounded polling for hydration/identity, not a reload loop.
    }
    throw new Error('Login identity did not become verifiable within 60 seconds; inspect once. Keep this checkpoint and do not seed identity manually.');
  },
  async assertAccount(checkpoint) {
    if (!checkpoint.accountId || await this.activeAccountId() !== checkpoint.accountId) {
      throw new Error('Signed-in account does not match this checkpoint; inspect identity before continuing');
    }
  },
  async prepare(options) {
    const config = this.config(options);
    const checkpoint = workflow.begin(config.run_id, config.account);
    if (checkpoint.pageName && checkpoint.pageName !== config.page_name) throw new Error('Page name differs from the saved run; keep the original mapping');
    if (['submitting', 'created', 'logged_out'].includes(checkpoint.stage)) return checkpoint;
    if (checkpoint.stage === 'submission_reserved') {
      await this.assertAccount(checkpoint);
      await workflow.validateCreation({ selector: config.create_selector });
      return checkpoint;
    }
    if (checkpoint.accountId) {
      await this.assertAccount(checkpoint);
    } else {
      // Keep the current tab: an existing session or delayed login can finish without reloading.
      if (!/^https:\/\/(?:[^/]+\.)?facebook\.com(?:\/|$)/.test(page.url())) {
        await workflow.navigate('https://www.facebook.com/');
      }
      await this.measure(checkpoint, 'loginMs', () => this.login(config, checkpoint));
    }
    await this.measure(checkpoint, 'browsingMs', () => workflow.browseFeed({ seconds: 30, discoverVideoSurface: true }));
    if (!checkpoint.browsed) throw new Error('Feed browsing completed without observed video playback; inspect Videos/Reels once before continuing');
    if (!page.url().startsWith('https://www.facebook.com/pages/create')) await this.measure(checkpoint, 'formNavigationMs', () => workflow.navigate('https://www.facebook.com/pages/create/'));
    await this.measure(checkpoint, 'formFillMs', () => workflow.fillPage({
      timeout: 45000,
      pageName: config.page_name,
      bio: config.bio || 'Short-form reels and video content.',
      category: config.category || 'Reel creator',
      nameSelector: config.name_selector,
      categorySelector: config.category_selector,
      optionSelector: config.option_selector,
      bioSelector: config.bio_selector
    }));
    return workflow.beforeCreate(config.page_name);
  },
  async finish(options) {
    const config = this.config(options);
    const checkpoint = workflow.begin(config.run_id, config.account);
    if (checkpoint.pageName && checkpoint.pageName !== config.page_name) throw new Error('Page name differs from the saved run; keep the original mapping');
    if (checkpoint.stage === 'logged_out') return checkpoint;
    if (checkpoint.stage === 'created' && !await this.activeAccountId()) {
      await page.getByText('Use another profile', { exact: true }).waitFor({ state: 'visible', timeout: 20000 });
      return workflow.loggedOut();
    }
    // Older jobs without an identity checkpoint require explicit identity inspection.
    await this.assertAccount(checkpoint);
    if (checkpoint.stage === 'submitting') {
      await this.measure(checkpoint, 'confirmationMs', () => workflow.confirmCreated(config.confirmation || {}));
    }
    if (checkpoint.stage !== 'created') throw new Error(`Cannot finish stage ${checkpoint.stage}; prepare or inspect without another creation click`);
    return this.measure(checkpoint, 'logoutMs', () => workflow.logout(config.logout || {}));
  }
};
