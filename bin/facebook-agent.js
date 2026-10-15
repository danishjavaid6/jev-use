// Structured actions: the calling agent owns every transition and the batch loop.
// Secrets stay in the invocation. Only non-secret progress enters the journal.
const facebookAgent = {
  recordCompleted(checkpoint) {
    if (checkpoint.stage !== 'logged_out') return;
    checkpoint.completedAt ??= Date.now();
    if (Number.isFinite(checkpoint.loginStartedAt)) checkpoint.elapsedMs = checkpoint.completedAt - checkpoint.loginStartedAt;
  },
  next(checkpoint) {
    const recovery = checkpoint.loginRecoveries?.at(-1);
    if (recovery?.completedAt != null && recovery.settledAt == null && ['signed_in', 'home_ready', 'browsed', 'form_open', 'form_ready'].includes(checkpoint.stage)) return 'wait_home';
    const steps = { started: 'login', signed_in: 'wait_home', home_ready: 'browse',
      browsed: 'open_form', form_open: 'wait_form', form_ready: 'fill',
      submission_reserved: 'create', submitting: 'confirm', created: 'home',
      logged_out: 'accounts', mfa_required: 'inspect' };
    if (checkpoint.stage === 'created' && checkpoint.agentHome) {
      return checkpoint.postCreateBrowsed ? 'logout' : 'browse_after';
    }
    return steps[checkpoint.stage] || 'inspect';
  },
  browsePlan(checkpoint) {
    if (!checkpoint.agentBrowsePlan) {
      // Old Home-only progress never counts as Reels viewing.
      const previous = checkpoint.agentBrowseMs || 0;
      checkpoint.agentBrowsePlan = [
        { surface: 'home', targetMs: 60000, elapsedMs: Math.min(60000, previous) },
        { surface: 'reels', targetMs: 60000, elapsedMs: 0 },
        { surface: 'home', targetMs: 60000, elapsedMs: Math.min(60000, Math.max(0, previous - 60000)) },
      ];
      if (previous) checkpoint.browseMigration = { previousHomeMs: previous };
    }
    return checkpoint.agentBrowsePlan;
  },
  async browseSurface(surface) {
    const url = new URL(page.url());
    const facebook = /(^|\.)facebook\.com$/.test(url.hostname);
    if (surface === 'home') {
      if (!facebook || !['/', '/home.php'].includes(url.pathname)) await workflow.navigate('https://www.facebook.com/');
      await page.getByRole('main').first().waitFor({ state: 'visible', timeout: 10000 });
      return;
    }
    if (!facebook || !/^\/reels?(?:\/|$)/.test(url.pathname)) {
      const links = page.getByRole('link', { name: /^Reels$/i });
      let destination;
      for (let index = 0; index < await links.count(); index++) {
        const link = links.nth(index);
        if (!await link.isVisible().catch(() => false)) continue;
        const href = await link.getAttribute('href');
        if (!href) continue;
        const observed = new URL(href, page.url());
        if (/^https:$/.test(observed.protocol) && /(^|\.)facebook\.com$/.test(observed.hostname) && /^\/reels?(?:\/|$)/.test(observed.pathname)) {
          destination = observed.href; break;
        }
      }
      if (!destination) throw new Error('reels_link_unavailable: inspect an observed Reels navigation link; Home scrolling cannot replace Reels');
      await workflow.navigate(destination);
    }
    await page.locator('video').first().waitFor({ state: 'visible', timeout: 10000 });
  },
  pacing(surface, previous) {
    const ranges = surface === 'reels' ? [[2500, 5500], [6500, 11000], [12500, 20000]] : [[400, 1600], [2200, 5200], [6500, 12000]];
    let pace = Math.floor(Math.random() * ranges.length);
    if (pace === previous) pace = (pace + 1 + Math.floor(Math.random() * 2)) % ranges.length;
    const [low, high] = ranges[pace];
    return { pace, pause: Math.round(low + Math.random() * (high - low)) };
  },
  async browse(checkpoint, after, seconds) {
    const plan = after ? [{ surface: 'home', targetMs: 30000, elapsedMs: checkpoint.agentAfterMs || 0 }] : this.browsePlan(checkpoint);
    const phase = plan.find(item => item.elapsedMs < item.targetMs);
    if (!phase) return;
    await this.browseSurface(phase.surface);
    await facebookPages.assertAccount(checkpoint);
    const started = Date.now();
    const budget = Math.min(seconds * 1000, phase.targetMs - phase.elapsedMs);
    const deadline = started + budget;
    checkpoint.browseStats ||= { scrolls: 0, upwardScrolls: 0, pauses: 0 };
    const stats = checkpoint.browseStats;
    while (Date.now() < deadline) {
      const iteration = Date.now();
      await workflow.dismissPagePrompts();
      const { pace, pause } = this.pacing(phase.surface, stats.lastPace);
      stats.lastPace = pace;
      const height = phase.surface === 'reels' ? await page.evaluate(() => window.innerHeight) : 0;
      const magnitude = phase.surface === 'reels' ? Math.round(Math.max(300, height || 900) * (0.7 + Math.random() * 0.7))
        : Math.round(100 + Math.pow(Math.random(), 1.5) * 1300);
      const upward = Math.random() < (phase.surface === 'reels' ? 0.08 : 0.18);
      await page.mouse.wheel(0, upward ? -magnitude : magnitude);
      stats.scrolls++;
      if (upward) stats.upwardScrolls++;
      const wait = Math.min(pause, Math.max(0, deadline - Date.now()));
      await page.waitForTimeout(wait);
      stats.pauses++;
      stats.minPauseMs = Math.min(stats.minPauseMs ?? wait, wait);
      stats.maxPauseMs = Math.max(stats.maxPauseMs ?? wait, wait);
      stats.minScrollPx = Math.min(stats.minScrollPx ?? magnitude, magnitude);
      stats.maxScrollPx = Math.max(stats.maxScrollPx ?? magnitude, magnitude);
      phase.elapsedMs += Math.min(deadline, Date.now()) - iteration;
      if (after) checkpoint.agentAfterMs = phase.elapsedMs;
      else checkpoint.agentBrowseMs = plan.reduce((total, item) => total + item.elapsedMs, 0);
    }
    if (plan.every(item => item.elapsedMs >= item.targetMs)) {
      if (after) checkpoint.postCreateBrowsed = true;
      else { checkpoint.browsed = true; checkpoint.stage = 'browsed'; }
    }
  },
  async step(options) {
    const { action, run_id, account } = options;
    const checkpoint = workflow.begin(run_id, account);
    const result = () => ({ ...checkpoint, next_action: this.next(checkpoint),
      browse_remaining_seconds: Math.max(0, Math.ceil((180000 - (checkpoint.agentBrowseMs || 0)) / 1000)),
      after_remaining_seconds: Math.max(0, Math.ceil((30000 - (checkpoint.agentAfterMs || 0)) / 1000)) });
    if (action === 'status') {
      const passwordPromptVisible = await page.locator('input[name="pass"]').first().isVisible().catch(() => false);
      const chooserVisible = await page.getByText(/^Log into Facebook$/i).first().isVisible().catch(() => false);
      const passwordRejected = await page.getByText('The password you entered is incorrect.', { exact: true }).first().isVisible().catch(() => false);
      const identity = await facebookPages.activeAccountId();
      const pendingRecovery = checkpoint.loginRecoveries?.at(-1);
      if (pendingRecovery && pendingRecovery.completedAt == null && identity === checkpoint.accountId) pendingRecovery.completedAt = Date.now();
      if (identity && identity === checkpoint.accountId && checkpoint.stage !== 'started') delete checkpoint.loginFailure;
      const url = new URL(page.url());
      const expectedIdentity = checkpoint.accountId || options.account_id;
      const accountMismatch = Boolean(identity && expectedIdentity && identity !== expectedIdentity);
      const kind = identity ? (/\/pages\/create/.test(url.pathname) ? 'creation_form' : 'signed_in')
        : passwordPromptVisible ? 'password_form' : chooserVisible ? 'account_chooser' : 'unknown';
      let nextAction = checkpoint.stage === 'started' && !identity && !passwordPromptVisible && chooserVisible && checkpoint.loginCardClicked &&
        !checkpoint.loginSubmitted && !checkpoint.loginRetry ? 'retry_login' : this.next(checkpoint);
      if (accountMismatch) nextAction = 'inspect';
      else if (pendingRecovery && pendingRecovery.completedAt == null) nextAction = passwordRejected ? 'inspect' : 'login';
      else if (pendingRecovery?.completedAt != null && pendingRecovery.settledAt == null) nextAction = 'wait_home';
      else if (kind === 'account_chooser' && checkpoint.accountId && ['signed_in', 'home_ready', 'browsed', 'form_open', 'form_ready'].includes(checkpoint.stage) && !checkpoint.pageName) nextAction = 'login';
      else if (identity && kind !== 'creation_form' && ['form_open', 'form_ready'].includes(checkpoint.stage)) nextAction = 'open_form';
      else if (kind === 'creation_form' && ['signed_in', 'home_ready', 'browsed'].includes(checkpoint.stage)) nextAction = 'home';
      return { ...result(), next_action: nextAction,
        current_state: { kind, url: url.origin + url.pathname, accountMismatch,
          identityMatches: expectedIdentity ? identity === expectedIdentity : null },
        live_login: { identityPresent: Boolean(identity), passwordPromptVisible, chooserVisible, passwordRejected } };
    }
    if (action === 'correct_password') {
      const fail = (code, message) => {
        checkpoint.loginFailure = { code, at: Date.now() };
        throw new Error(message);
      };
      if (checkpoint.stage !== 'started' || checkpoint.accountId || !checkpoint.loginSubmitted || checkpoint.passwordCorrection) {
        return fail('correction_not_available', 'Password correction requires a rejected prior login and permits one corrected submission');
      }
      if (await facebookPages.activeAccountId()) return fail('already_signed_in', 'An account is already signed in; inspect identity instead');
      const input = page.locator('input[name="pass"]').first();
      const rejection = page.getByText('The password you entered is incorrect.', { exact: true }).first();
      const passwordVisible = await input.isVisible();
      if (!passwordVisible) {
        // Restarting a GoLogin browser can return to the saved-account chooser.
        // A user-supplied correction may start one new login there while keeping
        // the old submission as history and all creation guards intact.
        const chooser = page.getByText(/^Log into Facebook$/i).first();
        const card = typeof workflow.savedAccountCard === 'function' ? await workflow.savedAccountCard({ accountName: options.account_name || account, accountId: options.account_id }) : page.getByRole('button').filter({ hasText: workflow.exactAccountName(options.account_name || account) });
        if (!await chooser.isVisible() || !await card.isVisible()) return fail('no_matching_chooser', 'Require the matching saved account on a signed-out chooser before correction');
        await page.waitForTimeout(1500);
        if (await facebookPages.activeAccountId() || await input.isVisible() || !await chooser.isVisible()) {
          return fail('login_state_changed', 'Login state changed during inspection; observe again before correction');
        }
        checkpoint.passwordCorrection = { at: Date.now(), reason: 'user correction from signed-out chooser',
          previousCardClicked: Boolean(checkpoint.loginCardClicked), previousSubmitted: true };
        checkpoint.loginSubmitted = false;
        checkpoint.loginCardClicked = false;
        try { await facebookPages.login({ ...options, login_button: true }, checkpoint, 45000); }
        catch {
          if (await facebookPages.mfaRequired(checkpoint)) return result();
          return fail('corrected_login_incomplete', 'Corrected login did not complete; inspect status and the browser. Credentials omitted.');
        }
        checkpoint.stage = 'signed_in';
        delete checkpoint.loginFailure;
        return result();
      }
      if (!await rejection.isVisible()) return fail('no_explicit_rejection', 'Require a visible password input and explicit incorrect-password notice before correction');
      const matched = await page.evaluate(expected => {
        const input = document.querySelector('input[name="pass"]');
        const normalize = text => String(text || '').replace(/\s+/g, ' ').trim();
        for (let node = input?.parentElement, depth = 0; node && node !== document.body && depth < 8; node = node.parentElement, depth++) {
          const lines = String(node.innerText || '').split(/\n/).map(normalize);
          if (lines.includes(normalize(expected))) return true;
        }
        return false;
      }, options.account_name || account);
      if (!matched) return fail('wrong_password_form', 'Password form does not identify the requested account; inspect before correction');
      // Reserve this correction before its click, without retaining the secret.
      // A lost response is recovered by login's identity wait, never resubmission.
      checkpoint.passwordCorrection = { at: Date.now(), reason: 'user correction after explicit rejection' };
      try {
        await input.fill(options.password);
        await dialogs.acceptNext();
        await page.getByRole('button', { name: /^Log in$/i }).click();
        await facebookPages.login({ ...options, login_button: true }, checkpoint, 45000);
      } catch {
        if (await facebookPages.mfaRequired(checkpoint)) return result();
        return fail('corrected_login_incomplete', 'Corrected login did not complete; inspect status and the browser. Credentials omitted.');
      }
      checkpoint.stage = 'signed_in';
      delete checkpoint.loginFailure;
      return result();
    }
    if (action === 'retry_login') {
      if (checkpoint.stage !== 'started' || checkpoint.accountId || checkpoint.loginSubmitted || checkpoint.loginRetry ||
          !checkpoint.loginCardClicked) {
        throw new Error('A login retry requires one inspected failed card selection without password submission; preserve the checkpoint');
      }
      if (await facebookPages.activeAccountId()) throw new Error('An account is already signed in; inspect identity instead');
      const input = page.locator('input[name="pass"]').first();
      if (await input.isVisible()) throw new Error('Password form is already open; resume login with its runtime password');
      await page.getByText(/^Log into Facebook$/i).first().waitFor({ state: 'visible', timeout: 5000 });
      checkpoint.loginRetry = { at: Date.now(), reason: 'inspected chooser dismissal' };
      checkpoint.loginCardClicked = false;
      return result();
    }
    if (action === 'accounts') {
      if (!/^https:\/\/(?:[^/]+\.)?facebook\.com(?:\/|$)/.test(page.url())) await workflow.navigate('https://www.facebook.com/');
      if (await facebookPages.activeAccountId()) throw new Error('An account is still signed in; resume its checkpoint before enumerating accounts');
      await page.getByText(/^Log into Facebook$/i).first().waitFor({ state: 'visible', timeout: 15000 });
      const accounts = (await workflow.listSavedAccounts()).filter(name => !/^(Use another profile|Add account|Remove account|Log in|Create new account)$/i.test(name));
      const cards = typeof workflow.listSavedAccountCards === 'function' ? await workflow.listSavedAccountCards() : undefined;
      return { accounts, cards, checkpoint: result() };
    }
    // The bridge already validated identity, journaled and performed the sole click.
    if (action === 'create') {
      if (checkpoint.stage !== 'submitting') throw new Error('Creation was not journaled');
      return result();
    }
    if (action === 'open_form' && ['form_open', 'form_ready'].includes(checkpoint.stage)) {
      await facebookPages.assertAccount(checkpoint);
      await workflow.navigate('https://www.facebook.com/pages/create/');
      checkpoint.stage = 'form_open';
      checkpoint.recoveries ||= [];
      checkpoint.recoveries.push({ action: 'open_form', at: Date.now() });
      return result();
    }
    if (action === 'home' && ['signed_in', 'home_ready', 'browsed'].includes(checkpoint.stage)) {
      await facebookPages.assertAccount(checkpoint);
      await workflow.navigate('https://www.facebook.com/');
      checkpoint.recoveries ||= [];
      checkpoint.recoveries.push({ action: 'home', at: Date.now() });
      return result();
    }
    const resumeLogin = action === 'login' && checkpoint.accountId && ['signed_in', 'home_ready', 'browsed', 'form_open', 'form_ready'].includes(checkpoint.stage) && !checkpoint.pageName;
    if (resumeLogin) {
      const activeIdentity = await facebookPages.activeAccountId();
      const pendingRecovery = checkpoint.loginRecoveries?.at(-1);
      const existingRecovery = pendingRecovery && pendingRecovery.completedAt == null;
      const passwordVisible = await page.locator('input[name="pass"]').first().isVisible();
      const chooserVisible = await page.getByText(/^Log into Facebook$/i).first().isVisible();
      if (options.account_id !== checkpoint.accountId || (activeIdentity && activeIdentity !== checkpoint.accountId) ||
        (!existingRecovery && (activeIdentity || !chooserVisible || passwordVisible))) {
        throw new Error('Precreation login recovery requires the matching verified account on a signed-out chooser');
      }
      checkpoint.loginRecoveries ||= [];
      if (!existingRecovery) {
        checkpoint.loginRecoveries.push({ at: Date.now(), stage: checkpoint.stage, previousSubmitted: Boolean(checkpoint.loginSubmitted), previousCardClicked: Boolean(checkpoint.loginCardClicked) });
        checkpoint.loginSubmitted = false;
        checkpoint.loginCardClicked = false;
      }
    }
    if (action !== this.next(checkpoint) && !resumeLogin) throw new Error(`Expected ${this.next(checkpoint)}, received ${action}; inspect status, do not replay completed actions`);
    if (action === 'login') {
      const restartRun = options.restart_from_run || checkpoint.restartFromRun;
      const historical = restartRun && Object.values(state.jevWorkflow).find(other =>
        other.runId === restartRun && (other.account === account || other.accountId === options.account_id));
      if (options.restart_from_run) {
        if (!historical || historical.accountId !== options.account_id || checkpoint.stage !== 'started' ||
          checkpoint.loginSubmitted || await facebookPages.activeAccountId() ||
          !await page.getByText(/^Log into Facebook$/i).first().isVisible() ||
          await page.locator('input[name="pass"]').first().isVisible()) {
          throw new Error('Restart requires the same verified historical account on a signed-out chooser before login');
        }
        const safeStages = ['started', 'signed_in', 'home_ready', 'browsed', 'form_open', 'form_ready'];
        for (const other of Object.values(state.jevWorkflow)) {
          if (other !== checkpoint && (other.account === account || other.accountId === options.account_id) &&
            (!safeStages.includes(other.stage) || other.pageName || other.submission || other.created || other.creation || other.submitting)) {
            throw new Error('Historical creation reservation or uncertain attempt forbids restarting this account');
          }
        }
        checkpoint.restartFromRun = options.restart_from_run;
        checkpoint.restartReconciledAt = Date.now();
        historical.restartReconciledBy = { runId: run_id, account };
      }
      for (const other of Object.values(state.jevWorkflow)) {
        if (other !== checkpoint && other.accountId && other.stage !== 'logged_out') {
          const successor = other.restartReconciledBy && Object.values(state.jevWorkflow).find(record =>
            record.runId === other.restartReconciledBy.runId && record.account === other.restartReconciledBy.account &&
            record.restartFromRun === other.runId && record.accountId === other.accountId && record.stage === 'logged_out');
          if (successor && ['started', 'signed_in', 'home_ready', 'browsed', 'form_open', 'form_ready'].includes(other.stage) && !other.pageName) continue;
          if (other === historical && checkpoint.restartFromRun === other.runId &&
            ['started', 'signed_in', 'home_ready', 'browsed', 'form_open', 'form_ready'].includes(other.stage) && !other.pageName) continue;
          throw new Error('Finish the previous account and verify logout first');
        }
      }
      if (!/^https:\/\/(?:[^/]+\.)?facebook\.com(?:\/|$)/.test(page.url())) await workflow.navigate('https://www.facebook.com/');
      const loginStarted = Date.now();
      checkpoint.loginStartedAt ??= loginStarted;
      try { await facebookPages.login({ ...options, login_button: true }, checkpoint, 45000); }
      catch (error) {
        if (error.code === 'ambiguous_saved_account') checkpoint.loginFailure = { code: error.code, at: Date.now() };
        if (await facebookPages.mfaRequired(checkpoint)) return result();
        // Do not expose backend errors containing password-fill parameters.
        throw new Error('Login did not complete. Inspect the browser and status; credentials have been omitted.');
      }
      if (!resumeLogin) checkpoint.stage = 'signed_in';
      if (resumeLogin) checkpoint.loginRecoveries.at(-1).completedAt = Date.now();
      checkpoint.timings ||= {};
      checkpoint.timings.login = (checkpoint.timings.login || 0) + Date.now() - loginStarted;
      delete checkpoint.loginFailure;
      return result();
    }
    // Recover logout when the previous call lost its response after signing out.
    if (action === 'logout' && !await facebookPages.activeAccountId()) {
      await page.getByText('Use another profile', { exact: true }).waitFor({ state: 'visible', timeout: 15000 });
      workflow.loggedOut();
      this.recordCompleted(checkpoint);
      return result();
    }
    await facebookPages.assertAccount(checkpoint);
    const started = Date.now();
    switch (action) {
      case 'wait_home':
      case 'wait_form':
        await workflow.settleAlerts({ seconds: 20 });
        if (action === 'wait_home' && checkpoint.loginRecoveries?.at(-1)?.completedAt != null && checkpoint.loginRecoveries.at(-1).settledAt == null) checkpoint.loginRecoveries.at(-1).settledAt = Date.now();
        else checkpoint.stage = action === 'wait_home' ? 'home_ready' : 'form_ready';
        break;
      case 'browse': await this.browse(checkpoint, false, options.seconds ?? 20); break;
      case 'open_form':
        await workflow.navigate('https://www.facebook.com/pages/create/');
        checkpoint.stage = 'form_open';
        break;
      case 'fill':
        if (checkpoint.pageName && checkpoint.pageName !== options.page_name) throw new Error('Keep the original Page name');
        checkpoint.pageName = options.page_name;
        await workflow.fillPage({ pageName: options.page_name, category: options.category, bio: options.bio || '', timeout: 30000 });
        workflow.beforeCreate(options.page_name);
        break;
      case 'confirm': await workflow.confirmCreated(); break;
      case 'home':
        await workflow.navigate('https://www.facebook.com/');
        checkpoint.agentHome = true;
        break;
      case 'browse_after': await this.browse(checkpoint, true, options.seconds ?? 20); break;
      case 'logout': await workflow.logout(); break;
    }
    checkpoint.timings ||= {};
    checkpoint.timings[action] = (checkpoint.timings[action] || 0) + Date.now() - started;
    this.recordCompleted(checkpoint);
    return result();
  }
};
