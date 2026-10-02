// Runs inside the existing BetterWright sandbox; never starts another browser.
const facebookPages = {
  version: '1',
  config(options) {
    for (const key of ['run_id', 'account', 'page_name']) {
      if (typeof options[key] !== 'string' || !options[key].trim()) throw new Error(`${key} must be a non-empty string`);
    }
    return options;
  },
  async activeAccountId() {
    // Read only the non-secret identity cookie. Never return authentication cookies.
    return page.evaluate(() => {
      if (!/(^|\.)facebook\.com$/.test(location.hostname)) return null;
      return document.cookie.split(';').map(item => item.trim()).find(item => item.startsWith('c_user='))?.slice(7) || null;
    });
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
      await page.goto('https://www.facebook.com/', { waitUntil: 'domcontentloaded', timeout: 20000 });
      await workflow.loginSavedAccount({
        accountName: config.account_name || config.account,
        password: config.password,
        accountSelector: config.account_selector,
        signedInSelector: config.signed_in_selector
      });
      checkpoint.accountId = await this.activeAccountId();
      if (!checkpoint.accountId) throw new Error('Login did not produce a signed-in Facebook identity');
    }
    await workflow.browseFeed({ seconds: 30 });
    if (!checkpoint.browsed) throw new Error('Feed browsing completed without observed video playback; inspect Videos/Reels once before continuing');
    await page.goto('https://www.facebook.com/pages/create/', { waitUntil: 'domcontentloaded', timeout: 20000 });
    await workflow.fillPage({
      pageName: config.page_name,
      bio: config.bio || 'Short-form reels and video content.',
      category: config.category || 'Reel creator',
      nameSelector: config.name_selector,
      categorySelector: config.category_selector,
      optionSelector: config.option_selector,
      bioSelector: config.bio_selector
    });
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
      await workflow.confirmCreated(config.confirmation || {});
    }
    if (checkpoint.stage !== 'created') throw new Error(`Cannot finish stage ${checkpoint.stage}; prepare or inspect without another creation click`);
    return workflow.logout(config.logout || {});
  }
};
