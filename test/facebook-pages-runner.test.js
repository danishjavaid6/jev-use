'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../skills/facebook-create-pages/scripts/account-runner.js'), 'utf8');
const config = { run_id:'run1', account:'Saved account', page_name:'Test Page', password:'fixture-only' };
function fixture() {
  const events = [];
  const record = {stage:'started'};
  let identity = null;
  const sandbox = {
    context: { cookies: async () => identity ? [{name:'c_user',value:identity},{name:'xs',value:'never-return-this'}] : [] },
    page: { evaluate: async () => identity, goto: async url => events.push(['goto',url]), getByText: () => ({waitFor:async () => events.push(['chooser'])}) },
    workflow: {
      begin: () => record,
      loginSavedAccount: async options => { assert.equal(options.password, 'fixture-only'); identity='account-123'; events.push(['login']); },
      browseFeed: async options => { assert.equal(options.seconds,30); record.browsed=true; events.push(['browse']); },
      fillPage: async () => events.push(['fill']),
      beforeCreate: name => { record.pageName=name; record.stage='submission_reserved'; events.push(['reserve']); return record; },
      validateCreation: async () => events.push(['validate']),
      confirmCreated: async () => { record.stage='created'; events.push(['confirm']); },
      logout: async () => { record.stage='logged_out'; identity=null; events.push(['logout']); return record; },
      loggedOut: () => { record.stage='logged_out'; return record; }
    }
  };
  vm.runInNewContext(source + '\nglobalThis.runner = facebookPages;', sandbox);
  return {...sandbox, events, record, setIdentity: value => {identity=value;}};
}

test('prepares then confirms and logs out without retaining the password', async () => {
  const f = fixture();
  assert.equal((await f.runner.prepare(config)).stage, 'submission_reserved');
  assert.equal(f.record.accountId,'account-123');
  assert.deepEqual(f.events.map(event=>event[0]), ['goto','login','browse','goto','fill','reserve']);
  assert.ok(!JSON.stringify(f.record).includes(config.password));
  assert.ok(!JSON.stringify(f.record).includes('never-return-this'));
  f.record.stage='submitting'; // Bridge journals and performs the one creation click.
  assert.equal((await f.runner.finish(config)).stage,'logged_out');
  assert.deepEqual(f.events.slice(-2).map(event=>event[0]),['confirm','logout']);
});

test('an existing reservation validates without logging in, browsing or creating again', async () => {
  const f = fixture();
  f.record.stage='submission_reserved'; f.record.accountId='account-123'; f.setIdentity('account-123');
  await f.runner.prepare(config);
  assert.deepEqual(f.events.map(event=>event[0]), ['validate']);
});

test('does not log out a different signed-in account', async () => {
  const f = fixture();
  f.record.stage='created'; f.record.accountId='account-123'; f.setIdentity('other-account');
  await assert.rejects(f.runner.finish(config),/does not match/);
  assert.equal(f.events.length,0);
});

test('resumes a logout that already took effect before the prior tool returned', async () => {
  const f = fixture();
  f.record.stage='created'; f.record.accountId='account-123';
  assert.equal((await f.runner.finish(config)).stage,'logged_out');
  assert.deepEqual(f.events.map(event=>event[0]), ['chooser']);
});

test('stops when no feed video playback was observed', async () => {
  const f = fixture();
  f.workflow.browseFeed = async () => {f.record.browsed=false;};
  await assert.rejects(f.runner.prepare(config), /without observed video/);
  assert.ok(!f.events.some(event=>event[0]==='fill' || event[0]==='reserve'));
});
