'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../bin/workflow-helpers.js'), 'utf8');
function fixture(videoPresent = true) {
  let now = 0;
  let clicks = 0;
  const video = { muted: false, paused: true, readyState: 4, async play() { this.paused = false; } };
  const sandbox = { state: {}, dialogs: { acceptNext: async () => {} }, Date: { now: () => now }, page: {
    mouse: { wheel: async () => {} },
    waitForTimeout: async ms => { now += ms; },
    locator: () => ({ first() { return this; }, nth() { return this; }, count: async () => videoPresent ? 1 : 0,
      evaluate: async callback => callback(video), click: async options => { if (!options?.trial) clicks++; },
      filter() { return this; }, waitFor: async () => {} })
  } };
  vm.runInNewContext(source + '\nglobalThis.workflow = workflow;', sandbox);
  return { ...sandbox, clicks: () => clicks };
}

test('requires thirty seconds of observed playback before reserving creation', async () => {
  const { workflow, state } = fixture();
  workflow.begin('run1', 'account1');
  assert.throws(() => workflow.beforeCreate('Test Page'), /browseFeed/);
  const browsing = await workflow.browseFeed();
  assert.equal(browsing.elapsedMs, 30000);
  assert.equal(browsing.playingObserved, true);
  assert.equal(browsing.scrolls, 10);
  workflow.beforeCreate('Test Page');
  assert.equal(workflow.status().stage, 'submission_reserved');
  await assert.rejects(workflow.submitCreation({ selector: 'button' }), /submission=/);
  state.jevWorkflow[state.jevWorkflowActive].stage = 'submitting';
  state.jevSubmissionPermits = [state.jevWorkflowActive];
  await workflow.submitCreation({ selector: 'button' });
  await assert.rejects(workflow.submitCreation({ selector: 'button' }), /only be attempted once/);
  await workflow.confirmCreated({ selector: '[role=alert]', expectedText: 'Success! Test Page created' });
  assert.equal(workflow.status().stage, 'created');
  assert.throws(() => workflow.beforeCreate('Test Page'), /already created/);
});

test('reports missing playback and prevents proceeding as if videos played', async () => {
  const { workflow } = fixture(false);
  workflow.begin('run2', 'account2');
  const browsing = await workflow.browseFeed();
  assert.equal(browsing.playingObserved, false);
  assert.equal(workflow.status().stage, 'browse_incomplete');
  assert.throws(() => workflow.beforeCreate('Test Page'), /browseFeed/);
});

test('saved login can bypass the password prompt', async () => {
  const fixtureState = fixture();
  fixtureState.page.locator = selector => ({ waitFor: async () => {
    if (selector === '#password') throw new Error('no password prompt');
  } });
  assert.equal(await fixtureState.workflow.waitForLogin({password:'#password',signedIn:'#account'}), 'signed_in');
});

test('a filled Page-name input alone is not creation proof', async () => {
  const { workflow } = fixture();
  workflow.begin('run3', 'account3');
  await workflow.browseFeed();
  workflow.beforeCreate('Test Page');
  workflow.status().stage = 'submitting';
  await assert.rejects(workflow.confirmCreated({selector:'input',expectedText:'Test Page'}), /creation notice/);
  assert.equal(workflow.status().stage, 'submitting');
});


test('validation failures leave the reservation retryable without any click', async () => {
  const f = fixture();
  f.workflow.begin('validation', 'account');
  await f.workflow.browseFeed();
  f.workflow.beforeCreate('Test Page');
  f.page.locator = () => ({ click: async () => { throw new Error('selector missing'); } });
  await assert.rejects(f.workflow.validateCreation({selector:'button'}), /selector missing/);
  assert.equal(f.workflow.status().stage, 'submission_reserved');
  assert.equal(f.clicks(), 0);
});

test('read-only inspection and begin preserve a reservation', async () => {
  const f = fixture();
  f.workflow.begin('read', 'account');
  await f.workflow.browseFeed();
  f.workflow.beforeCreate('Test Page');
  assert.equal(f.workflow.begin('read', 'account').stage, 'submission_reserved');
  assert.equal(f.workflow.status().stage, 'submission_reserved');
});


test('logout accepts the wizard and verifies the chooser after navigation destroys context', async () => {
  const f = fixture();
  f.workflow.begin('logout', 'account');
  f.workflow.status().stage = 'created';
  let accepted = 0;
  let loggedOut = false;
  let verified = false;
  f.dialogs.acceptNext = async () => { accepted++; };
  f.page.goto = async url => { assert.equal(url, 'https://www.facebook.com/'); };
  f.page.locator = () => ({ isVisible: async () => false, waitFor: async () => { assert.equal(loggedOut, true); verified = true; } });
  f.page.getByRole = (role, options) => {
    assert.equal(role, 'button');
    assert.equal(options.name, 'Your profile');
    return { waitFor: async () => {}, click: async () => {} };
  };
  f.page.getByText = () => ({ last() { return this; }, waitFor: async () => {}, click: async () => { loggedOut = true; throw new Error('Execution context was destroyed due to navigation'); } });
  assert.equal((await f.workflow.logout()).stage, 'logged_out');
  assert.equal(accepted, 2);
  assert.equal(verified, true);
});

test('form filling selects the exact category and only trial-clicks Create Page', async () => {
  const f = fixture();
  let categorySelected = false;
  let trialChecked = false;
  f.page.getByRole = (role, options) => ({ waitFor: async () => {}, fill: async () => {}, click: async options => { assert.equal(options.trial, true); trialChecked = true; } });
  f.page.getByLabel = (...args) => f.page.getByRole('textbox', { name: args[0] });
  f.page.getByText = category => ({ last() { return this; }, waitFor: async () => {}, click: async () => { assert.equal(category, 'Reel creator'); categorySelected = true; } });
  await f.workflow.fillPage({ pageName:'Fixture Page', bio:'Fixture bio' });
  assert.equal(categorySelected, true);
  assert.equal(trialChecked, true);
});

test('form labels work when role selectors cannot see the fields', async () => {
  const f = fixture();
  const filled = [];
  f.page.getByRole = (role) => role === 'button'
    ? {click: async options => assert.equal(options.trial, true)}
    : {waitFor: async () => {throw new Error('missing role');}};
  f.page.getByLabel = label => ({waitFor: async () => {}, fill: async value => filled.push(value)});
  f.page.getByText = () => ({last() {return this;},waitFor: async () => {},click: async () => {}});
  await f.workflow.fillPage({pageName:'Label Page',bio:'Description'});
  assert.deepEqual(filled, ['Label Page','Reel creator','Description']);
});

test('checks later visible videos without blocking on a buffering play promise', async () => {
  const f = fixture();
  const videos = [
    {paused:true,readyState:0,play: () => new Promise(() => {})},
    {paused:false,readyState:4,play: async () => {}}
  ];
  f.page.locator = () => ({count:async () => videos.length,nth:index => ({evaluate:async callback => callback(videos[index])})});
  f.workflow.begin('multiple-videos','account');
  const browsing = await f.workflow.browseFeed();
  assert.equal(browsing.elapsedMs,30000);
  assert.equal(browsing.playingObserved,true);
});

test('login detects an aria-labeled profile even without a button role', async () => {
  const f = fixture();
  let selected = false;
  f.page.getByRole = (role, options) => options?.name
    ? {waitFor: async () => {throw new Error('no role');}}
    : {filter() {return this;},waitFor:async () => {},click:async () => {selected=true;}};
  f.page.locator = selector => ({filter() {return this;},first() {return this;},
    waitFor:async () => {if (selector.includes('pass')) throw new Error('no password');},
    click:async () => {selected=true;}});
  assert.equal(await f.workflow.loginSavedAccount({accountName:'Account',password:'unused'}),'signed_in');
  assert.equal(selected,true);
});

test('feed fallback follows one observed Reels link and records actual playback', async () => {
  const f=fixture(); let opened=false; const navigations=[];
  const video={paused:false,readyState:4};
  f.page.url=()=> 'https://www.facebook.com/';
  f.page.getByRole=()=>({first() {return this;},count:async()=>1,getAttribute:async()=>'/reel/observed-id/'});
  f.page.locator=()=>({count:async()=>opened?1:0,nth:()=>({evaluate:async callback=>callback(video)})});
  f.workflow.navigate=async url=>{navigations.push(url);opened=true;};
  // URL is a standard sandbox primitive in BetterWright.
  const originalURL=global.URL;
  // The helper's VM uses a separate realm; inject its URL through a new fixture below.
  const sandbox={state:f.state,page:f.page,dialogs:f.dialogs,Date:f.Date,URL:originalURL};
  vm.runInNewContext(source+'\nglobalThis.workflow=workflow;',sandbox);
  sandbox.workflow.navigate=f.workflow.navigate;
  sandbox.workflow.begin('reels-fallback','account');
  const browsing=await sandbox.workflow.browseFeed({discoverVideoSurface:true});
  assert.equal(browsing.playingObserved,true);
  assert.deepEqual(navigations,['https://www.facebook.com/reel/observed-id/']);
});

test('navigation waits for commit instead of the slow SPA load event', async () => {
  const f=fixture(); let accepted=false;
  f.dialogs.acceptNext=async()=>{accepted=true;};
  f.page.goto=async (url,options)=>{assert.equal(accepted,true);assert.equal(options.waitUntil,'commit');assert.equal(options.timeout,45000);return url;};
  assert.equal(await f.workflow.navigate('https://www.facebook.com/'),'https://www.facebook.com/');
});
