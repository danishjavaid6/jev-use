'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../bin/facebook-agent.js'), 'utf8');

function fixture() {
  let now = 0;
  let identity = 'account1';
  let currentUrl = 'https://www.facebook.com/';
  const checkpoint = { stage: 'started' };
  const events = [];
  const context = {
    Date: { now: () => now }, Math, URL,
    state: { jevWorkflow: { account1: checkpoint } },
    page: {
      url: () => currentUrl,
      evaluate: async () => 900,
      getByRole: role => role === 'link' ? { count: async () => 1, nth() { return this; }, isVisible: async () => true, getAttribute: async () => '/reels/' }
        : { first() { return this; }, waitFor: async () => {} },
      mouse: { wheel: async (_, distance) => events.push(['scroll', distance]) },
      waitForTimeout: async ms => { now += ms; },
      getByText: () => ({ first() { return this; }, isVisible: async () => false, waitFor: async () => events.push(['chooser']) }),
      locator: () => ({ first() { return this; }, isVisible: async () => false, waitFor: async () => {} }),
    },
    facebookPages: {
      activeAccountId: async () => identity,
      login: async (options, record) => { record.accountId = identity; events.push(['login', options.login_button]); },
      assertAccount: async record => { if (record.accountId !== identity) throw Error('wrong identity'); },
      mfaRequired: async () => false,
    },
    workflow: {
      exactAccountName: name => new RegExp('^' + name + '$'),
      begin: () => checkpoint,
      dismissPagePrompts: async () => {},
      settleAlerts: async ({ seconds }) => { now += seconds * 1000; events.push(['settle', seconds]); },
      navigate: async url => { currentUrl = url; events.push(['navigate', url]); },
      fillPage: async options => events.push(['fill', options.pageName]),
      beforeCreate: () => { checkpoint.stage = 'submission_reserved'; },
      confirmCreated: async () => { checkpoint.stage = 'created'; },
      logout: async () => { checkpoint.stage = 'logged_out'; identity = null; },
      loggedOut: () => { checkpoint.stage = 'logged_out'; },
      listSavedAccounts: async () => ['Account One', 'Account Two'],
    },
  };
  vm.runInNewContext(source + '\nglobalThis.agent = facebookAgent;', context);
  return { context, checkpoint, events, setIdentity: id => { identity = id; },
    step: (action, options = {}) => context.agent.step({ action, run_id: 'stable-run', account: 'account1', ...options }) };
}

test('explicit restart reconciles only the same signed-out precreation account and retains history', async () => {
  const f = fixture();
  f.setIdentity(null);
  f.context.page.getByText = () => ({ first() { return this; }, isVisible: async () => true });
  const old = { runId: 'old', account: 'account1', accountId: 'verified', stage: 'home_ready' };
  f.context.state.jevWorkflow.old = old;
  await f.step('login', { restart_from_run: 'old', account_id: 'verified' });
  assert.equal(old.stage, 'home_ready');
  assert.equal(f.checkpoint.restartFromRun, 'old');
});

test('restart refuses all creation stages, mismatched identities and absent chooser', async () => {
  for (const stage of ['submission_reserved', 'submitting', 'created', 'logged_out']) {
    const f = fixture(); f.setIdentity(null);
    f.context.page.getByText = () => ({ first() { return this; }, isVisible: async () => true });
    f.context.state.jevWorkflow.old = { runId: 'old', account: 'account1', accountId: 'verified', stage };
    await assert.rejects(f.step('login', { restart_from_run: 'old', account_id: 'verified' }), /forbids restarting/);
  }
  const f = fixture(); f.setIdentity(null);
  f.context.state.jevWorkflow.old = { runId: 'old', account: 'account1', accountId: 'verified', stage: 'home_ready' };
  await assert.rejects(f.step('login', { restart_from_run: 'old', account_id: 'verified' }), /signed-out chooser/);
  await assert.rejects(f.step('login', { restart_from_run: 'old', account_id: 'wrong' }), /same verified/);
});

test('agent calls the entire workflow with measured browsing chunks and two waits', async () => {
  const f = fixture();
  assert.equal((await f.step('login', { password: 'not-persisted' })).next_action, 'wait_home');
  await f.step('wait_home');
  for (let i = 0; i < 8; i++) assert.equal((await f.step('browse')).next_action, 'browse');
  assert.equal((await f.step('browse')).next_action, 'open_form');
  assert.equal(f.checkpoint.agentBrowseMs, 180000);
  await f.step('open_form');
  await f.step('wait_form');
  await f.step('fill', { page_name: 'A Page', category: 'Artist' });
  assert.equal(f.checkpoint.stage, 'submission_reserved');
  f.checkpoint.stage = 'submitting'; // Only the bridge may journal and click.
  assert.equal((await f.step('create')).next_action, 'confirm');
  await f.step('confirm');
  await f.step('home');
  assert.equal((await f.step('browse_after')).next_action, 'browse_after');
  assert.equal((await f.step('browse_after')).next_action, 'logout');
  assert.equal(f.checkpoint.agentAfterMs, 30000);
  assert.equal((await f.step('logout')).stage, 'logged_out');
  assert.deepEqual(f.events.filter(e => e[0] === 'settle'), [['settle', 20], ['settle', 20]]);
  assert.ok(!JSON.stringify(f.checkpoint).includes('not-persisted'));
  assert.equal(f.checkpoint.loginStartedAt, 0);
  assert.equal(f.checkpoint.completedAt, 250000);
  assert.equal(f.checkpoint.elapsedMs, 250000);
});

test('logout recovery preserves a durable login start and leaves legacy totals unknown', async () => {
  for (const start of [123, undefined]) {
    const f = fixture();
    f.checkpoint.stage = 'created'; f.checkpoint.agentHome = true; f.checkpoint.postCreateBrowsed = true;
    if (start !== undefined) f.checkpoint.loginStartedAt = start;
    f.setIdentity(null);
    await f.step('logout');
    assert.equal(f.checkpoint.completedAt, 0);
    assert.equal(f.checkpoint.elapsedMs, start === undefined ? undefined : -123);
  }
});

test('signed-out precreation recovery retains progress and refuses uncertain submission', async () => {
  const f = fixture();
  f.setIdentity(null);
  f.context.page.getByText = () => ({ first() { return this; }, isVisible: async () => true });
  Object.assign(f.checkpoint, { stage: 'home_ready', accountId: 'known', loginSubmitted: true, agentBrowseMs: 60000, loginStartedAt: 0 });
  f.context.facebookPages.login = async (_options, record) => { record.loginSubmitted = true; record.accountId = 'known'; };
  await f.step('login', { account_id: 'known' });
  assert.equal(f.checkpoint.stage, 'home_ready');
  assert.equal(f.checkpoint.agentBrowseMs, 60000);
  assert.equal(f.checkpoint.loginStartedAt, 0);
  assert.equal(f.checkpoint.loginRecoveries[0].completedAt, 0);
  assert.equal(f.context.agent.next(f.checkpoint), 'wait_home');
  f.setIdentity('known');
  await f.step('wait_home');
  assert.equal(f.checkpoint.stage, 'home_ready');
  assert.equal(f.checkpoint.agentBrowseMs, 60000);
  assert.equal(f.checkpoint.loginRecoveries[0].settledAt, 20000);
  f.checkpoint.loginRecoveries.push({ at: 1 });
  f.setIdentity(null);
  f.checkpoint.loginSubmitted = true;
  f.context.facebookPages.login = async (_options, record) => {
    assert.equal(record.loginSubmitted, true, 'pending submission must never be reset');
    throw new Error('identity still pending');
  };
  await assert.rejects(f.step('login', { account_id: 'known' }), /Login did not complete/);
  f.checkpoint.stage = 'submitting';
  await assert.rejects(f.step('login', { account_id: 'known' }), /Expected confirm/);
});

test('status completes a recovery only on matching identity then requires fresh settle', async () => {
  const f = fixture();
  Object.assign(f.checkpoint, { stage: 'home_ready', accountId: 'account1', agentBrowseMs: 60000, loginRecoveries: [{ at: 0 }] });
  assert.equal((await f.step('status')).next_action, 'wait_home');
  assert.equal(f.checkpoint.loginRecoveries[0].completedAt, 0);
  assert.equal(f.checkpoint.agentBrowseMs, 60000);
  f.checkpoint.loginRecoveries = [{ at: 1 }];
  f.setIdentity('wrong');
  assert.equal((await f.step('status')).next_action, 'inspect');
  assert.equal(f.checkpoint.loginRecoveries[0].completedAt, undefined);
});

test('status reconciles a lost login response only for the exact observed account ID', async () => {
  const f = fixture();
  f.checkpoint.loginSubmitted = true;
  assert.equal((await f.step('status', { account_id: 'account1' })).next_action, 'wait_home');
  assert.equal(f.checkpoint.accountId, 'account1');
  assert.equal(f.checkpoint.stage, 'signed_in');
  const wrong = fixture();
  assert.equal((await wrong.step('status', { account_id: 'different' })).next_action, 'inspect');
  assert.equal(wrong.checkpoint.stage, 'started');
  const neverAttempted = fixture();
  await neverAttempted.step('status', { account_id: 'account1' });
  assert.equal(neverAttempted.checkpoint.stage, 'started', 'reading an identity cannot manufacture a login attempt');
});

test('a mismatched password form recovers once before any password submission', async () => {
  const f = fixture(); f.setIdentity(null);
  f.context.page.locator = () => ({ first() { return this; }, isVisible: async () => true });
  f.context.facebookPages.passwordAccountMatches = async () => false;
  assert.equal((await f.step('status')).next_action, 'reset_login_view');
  await f.step('reset_login_view');
  assert.deepEqual(f.events.at(-1), ['navigate', 'https://www.facebook.com/login/']);
  assert.equal(f.checkpoint.loginSubmitted, undefined);
  assert.equal((await f.step('status')).next_action, 'inspect');
  await assert.rejects(f.step('reset_login_view'), /no submitted password/);
  const submitted = fixture(); submitted.setIdentity(null);
  submitted.checkpoint.loginSubmitted = true;
  submitted.context.page.locator = f.context.page.locator;
  submitted.context.facebookPages.passwordAccountMatches = async () => false;
  assert.equal((await submitted.step('status')).next_action, 'inspect');
  await assert.rejects(submitted.step('reset_login_view'), /no submitted password/);
});

test('a partially filled precreation form can reauthenticate without changing its Page name', async () => {
  const f = fixture(); f.setIdentity(null);
  Object.assign(f.checkpoint, { stage: 'form_ready', accountId: 'known', pageName: 'Stable Page', loginSubmitted: true });
  f.context.page.getByText = () => ({ first() { return this; }, isVisible: async () => true });
  f.context.facebookPages.login = async (_options, record) => { record.accountId = 'known'; };
  assert.equal((await f.step('status')).next_action, 'login');
  await f.step('login', { account_id: 'known' });
  assert.equal(f.checkpoint.pageName, 'Stable Page');
  assert.equal(f.checkpoint.stage, 'form_ready');
});

test('rejects premature creation flow, account switching and wrong active identity', async () => {
  const f = fixture();
  await assert.rejects(f.step('fill'), /Expected login/);
  await assert.rejects(f.step('create'), /not journaled/);
  f.context.state.jevWorkflow.other = { accountId: 'other', stage: 'created' };
  await assert.rejects(f.step('login'), /previous account/);
  delete f.context.state.jevWorkflow.other;
  await f.step('login');
  f.setIdentity('different');
  await assert.rejects(f.step('wait_home'), /wrong identity/);
});

test('uncertain submission can only proceed to confirmation', async () => {
  const f = fixture();
  f.checkpoint.stage = 'submitting';
  assert.equal((await f.step('status')).next_action, 'confirm');
  await assert.rejects(f.step('fill', { page_name: 'Another Page' }), /Expected confirm/);
});

test('another historical reservation allows only a verified different account on a signed-out chooser', async () => {
  for (const blocked of [false, 'same_id', 'same_name', 'password_form', 'signed_in', 'current_run', 'unknown_id']) {
    const f = fixture(); f.setIdentity(blocked === 'signed_in' ? 'other' : null);
    f.context.page.getByText = () => ({ first() { return this; }, isVisible: async () => true });
    f.context.page.locator = () => ({ first() { return this; }, isVisible: async () => blocked === 'password_form' });
    const old = { runId: blocked === 'current_run' ? 'stable-run' : 'old', account: blocked === 'same_name' ? 'account1' : 'Other', accountId: blocked === 'same_id' ? 'requested' : 'other', stage: 'submission_reserved', pageName: 'Reserved Page' };
    f.context.state.jevWorkflow.old = old;
    const action = f.step('login', { account_id: blocked === 'unknown_id' ? undefined : 'requested' });
    if (blocked) {
      await assert.rejects(action, /previous account/);
      assert.equal(f.checkpoint.loginFailure.code, 'unfinished_account_requires_reconciliation');
      assert.equal(f.events.some(e => e[0] === 'login'), false);
    } else await action;
    assert.equal(old.stage, 'submission_reserved');
    assert.equal(old.pageName, 'Reserved Page');
  }
});

test('a scoped new Page request allows the same account while preserving older journals', async () => {
  for (const stage of ['started', 'form_ready', 'submission_reserved', 'submitting', 'created', 'logged_out']) {
    for (const hasId of [false, true]) {
      const f = fixture(); f.setIdentity(null);
      const old = { runId: 'old', account: 'account1', stage, pageName: 'Previous Page', ...(hasId ? {accountId: 'requested'} : {}) };
      const original = JSON.stringify(old);
      f.context.state.jevWorkflow.old = old;
      await f.step('login', { account_id: 'requested', new_page_per_account: true });
      assert.equal(f.events.filter(e => e[0] === 'login').length, 1);
      assert.equal(JSON.stringify(old), original);
    }
  }
});

test('a new Page request cannot switch unfinished accounts in the same run even without an ID', async () => {
  for (const hasId of [false, true]) {
    const f = fixture(); f.setIdentity(null);
    f.context.state.jevWorkflow.other = { runId: 'stable-run', account: 'other', stage: 'submitting', ...(hasId ? {accountId: 'other'} : {}) };
    await assert.rejects(f.step('login', { new_page_per_account: true }), /previous account/);
    assert.equal(f.events.some(e => e[0] === 'login'), false);
  }
});

test('a failed scroll does not count unperformed browsing time', async () => {
  const f = fixture();
  await f.step('login');
  await f.step('wait_home');
  f.context.page.mouse.wheel = async () => { throw Error('disconnected'); };
  await assert.rejects(f.step('browse'), /disconnected/);
  assert.equal((await f.step('status')).browse_remaining_seconds, 180);
});

test('browsing visits Home, Reels, then Home and resumes each surface without timing credit from navigation', async () => {
  const f = fixture();
  await f.step('login'); await f.step('wait_home');
  await f.step('browse', { seconds: 60 });
  assert.equal(f.checkpoint.agentBrowsePlan[0].elapsedMs, 60000);
  await f.step('browse', { seconds: 20 });
  assert.equal(f.checkpoint.agentBrowsePlan[1].elapsedMs, 20000);
  await f.step('browse', { seconds: 40 });
  assert.equal(f.checkpoint.agentBrowsePlan[1].elapsedMs, 60000);
  assert.equal(f.checkpoint.stage, 'home_ready');
  await f.step('browse', { seconds: 60 });
  assert.equal(f.checkpoint.stage, 'browsed');
  assert.equal(f.checkpoint.agentBrowseMs, 180000);
  assert.deepEqual(f.events.filter(event => event[0] === 'navigate').map(event => event[1]),
    ['https://www.facebook.com/reels/', 'https://www.facebook.com/']);
});

test('missing Reels navigation is a recoverable blocker and never claims Home time as Reels', async () => {
  const f = fixture();
  await f.step('login'); await f.step('wait_home'); await f.step('browse', { seconds: 60 });
  const roles = f.context.page.getByRole;
  f.context.page.getByRole = role => role === 'link' ? { count: async () => 0 } : roles(role);
  await assert.rejects(f.step('browse', { seconds: 60 }), /reels_link_unavailable/);
  assert.equal(f.checkpoint.agentBrowseMs, 60000);
  assert.equal(f.checkpoint.agentBrowsePlan[1].elapsedMs, 0);
  assert.equal(f.checkpoint.browsed, undefined);
  f.context.page.getByRole = roles;
  await f.step('browse', { seconds: 60 });
  assert.equal(f.checkpoint.agentBrowsePlan[1].elapsedMs, 60000);
});

test('scrolling changes tempo, distance, direction and keeps a completed phase on a later failure', async () => {
  const f = fixture();
  let seed = 123456;
  f.context.Math = Object.assign(Object.create(Math), { random: () => ((seed = (seed * 16807) % 2147483647) / 2147483647) });
  await f.step('login'); await f.step('wait_home'); await f.step('browse', { seconds: 60 });
  const stats = f.checkpoint.browseStats;
  assert.ok(stats.maxPauseMs - stats.minPauseMs > 4000);
  assert.ok(stats.maxScrollPx - stats.minScrollPx > 400);
  assert.ok(stats.upwardScrolls > 0);
  f.context.page.mouse.wheel = async () => { throw Error('scroll disconnected'); };
  await assert.rejects(f.step('browse', { seconds: 60 }), /disconnected/);
  assert.equal(f.checkpoint.agentBrowseMs, 60000);
});

test('logout recovers after losing the prior response', async () => {
  const f = fixture();
  Object.assign(f.checkpoint, { stage: 'created', agentHome: true, postCreateBrowsed: true });
  f.setIdentity(null);
  assert.equal((await f.step('logout')).stage, 'logged_out');
  assert.deepEqual(f.events, [['chooser']]);
});

test('a verified failed card selection may retry once after popup inspection', async () => {
  const f = fixture();
  f.setIdentity(null);
  Object.assign(f.checkpoint, { loginCardClicked: true, loginObservation: {
    identityPresent: false, passwordSubmissionAttempted: false,
    passwordPromptVisible: false, savedCardVisible: true,
  } });
  assert.equal((await f.step('retry_login')).next_action, 'login');
  assert.equal(f.checkpoint.loginCardClicked, false);
  await assert.rejects(f.step('retry_login'), /requires one inspected/);
});

test('login recovery never clears a submitted password or active identity', async () => {
  const f = fixture();
  Object.assign(f.checkpoint, { loginCardClicked: true, loginSubmitted: true,
    loginObservation: { savedCardVisible: true, passwordSubmissionAttempted: true } });
  await assert.rejects(f.step('retry_login'), /requires one inspected/);
  assert.equal(f.checkpoint.loginSubmitted, true);
});

function correctionFixture() {
  const f = fixture();
  f.setIdentity(null);
  f.checkpoint.loginSubmitted = true;
  f.context.page.locator = () => ({ first() { return this; }, isVisible: async () => true,
    fill: async () => f.events.push(['fill-correction']) });
  f.context.page.getByText = () => ({ first() { return this; }, isVisible: async () => true });
  f.context.page.evaluate = async () => true;
  f.context.page.getByRole = () => ({ click: async () => { f.events.push(['submit-correction']); f.setIdentity('account1'); } });
  f.context.dialogs = { acceptNext: async () => {} };
  return f;
}

test('explicit password rejection permits one user correction without resetting the checkpoint', async () => {
  const f = correctionFixture();
  assert.equal((await f.step('correct_password', { password: 'private-fixture' })).stage, 'signed_in');
  assert.equal(f.checkpoint.loginSubmitted, true);
  assert.ok(f.checkpoint.passwordCorrection);
  assert.deepEqual(f.events.slice(0, 2), [['fill-correction'], ['submit-correction']]);
  assert.ok(!JSON.stringify(f.checkpoint).includes('private-fixture'));
  await assert.rejects(f.step('correct_password', { password: 'another-fixture' }), /permits one/);
});

test('password correction refuses uncertainty, wrong identity and a mismatched form', async () => {
  const f = correctionFixture();
  f.context.page.getByText = () => ({ first() { return this; }, isVisible: async () => false });
  await assert.rejects(f.step('correct_password', { password: 'fixture' }), /explicit incorrect-password/);
  assert.equal(f.checkpoint.passwordCorrection, undefined);
  f.context.page.getByText = () => ({ first() { return this; }, isVisible: async () => true });
  f.context.page.evaluate = async () => false;
  await assert.rejects(f.step('correct_password', { password: 'fixture' }), /does not identify/);
  f.setIdentity('different');
  await assert.rejects(f.step('correct_password', { password: 'fixture' }), /already signed in/);
  assert.equal(f.events.length, 0);
});

test('a corrected credential can resume from a signed-out chooser after a profile restart', async () => {
  const f = correctionFixture();
  f.checkpoint.loginCardClicked = true;
  f.context.page.locator = () => ({ first() { return this; }, isVisible: async () => false });
  f.context.page.getByRole = () => ({ filter() { return this; }, isVisible: async () => true });
  f.context.facebookPages.login = async (options, record) => {
    assert.equal(record.loginSubmitted, false);
    assert.equal(record.loginCardClicked, false);
    assert.equal(options.password, 'private-fixture');
    record.loginSubmitted = true;
    record.accountId = 'account1';
  };
  assert.equal((await f.step('correct_password', { password: 'private-fixture' })).stage, 'signed_in');
  assert.equal(f.checkpoint.passwordCorrection.previousSubmitted, true);
  assert.ok(!JSON.stringify(f.checkpoint).includes('private-fixture'));
});

test('status reports current UI separately from stale checkpoint observations', async () => {
  const f = fixture();
  f.setIdentity(null);
  f.checkpoint.loginObservation = { passwordPromptVisible: true };
  const status = await f.step('status');
  assert.equal(status.loginObservation.passwordPromptVisible, true);
  assert.equal(status.live_login.passwordPromptVisible, false);
});

test('resuming an open form from Home recovers navigation without replaying login or browsing', async () => {
  const f = fixture();
  Object.assign(f.checkpoint, { stage: 'form_ready', accountId: 'account1', agentBrowseMs: 180000 });
  const status = await f.step('status');
  assert.equal(status.current_state.kind, 'signed_in');
  assert.equal(status.next_action, 'open_form');
  assert.equal((await f.step('open_form')).next_action, 'wait_form');
  assert.equal(f.checkpoint.agentBrowseMs, 180000);
  assert.deepEqual(f.events, [['navigate', 'https://www.facebook.com/pages/create/']]);
});

test('current state never authorizes replaying an uncertain creation', async () => {
  const f = fixture();
  Object.assign(f.checkpoint, { stage: 'submitting', accountId: 'account1' });
  assert.equal((await f.step('status')).next_action, 'confirm');
  await assert.rejects(f.step('open_form'), /Expected confirm/);
  f.setIdentity('different');
  assert.equal((await f.step('status')).next_action, 'inspect');
});

test('verified resumed login does not keep reporting an obsolete login failure', async () => {
  const f = fixture();
  Object.assign(f.checkpoint, { stage: 'home_ready', accountId: 'account1', loginFailure: { code: 'old_failure' } });
  assert.equal((await f.step('status')).loginFailure, undefined);
});
