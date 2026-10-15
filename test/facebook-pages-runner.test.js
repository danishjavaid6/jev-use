'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../skills/facebook-create-pages/scripts/account-runner.js'), 'utf8');
const config = { run_id:'run1', account:'Saved account', page_name:'Test Page', password:'fixture-only' };
function fixture({ withDialogs = false } = {}) {
  const events = [];
  const record = {stage:'started'};
  let identity = null;
  let now = 0;
  let matched = true;
  let loginDelay = 0;
  let clickedAt = null;
  const sandbox = {
    Date: {now: () => now},
    ...(withDialogs ? { dialogs: { acceptNext: async () => events.push(['accept-dialog']) } } : {}),
    page: {
      url: () => 'https://www.facebook.com/',
      evaluate: async (callback, name) => {
        if (clickedAt !== null && now - clickedAt >= loginDelay) identity='account-123';
        return name ? (identity ? {id:identity,matched} : null) : identity;
      },
      waitForTimeout:async ms => {now+=ms;},
      locator: () => ({first() {return this;},isVisible:async () => false}),
      getByRole: () => ({filter() {return this;},isVisible:async () => !identity,
        click:async () => {clickedAt=now;events.push(['login']);}}),
      getByText: () => ({waitFor:async () => events.push(['chooser'])})
    },
    workflow: {
      begin: () => record,
      navigate: async url => events.push(['goto',url]),
      settleAlerts: async options => { assert.equal(options.seconds,20); events.push(['settle']); },
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
  return {...sandbox, events, record, setIdentity: value => {identity=value;}, setMatched: value => {matched=value;}, setDelay: value => {loginDelay=value;}};
}

test('prepares then confirms and logs out without retaining the password', async () => {
  const f = fixture();
  assert.equal((await f.runner.prepare(config)).stage, 'submission_reserved');
  assert.equal(f.record.accountId,'account-123');
  assert.deepEqual(f.events.map(event=>event[0]), ['login','browse','goto','fill','reserve']);
  assert.ok(!JSON.stringify(f.record).includes(config.password));
  assert.ok(!JSON.stringify(f.record).includes('never-return-this'));
  f.record.stage='submitting'; // Bridge journals and performs the one creation click.
  assert.equal((await f.runner.finish(config)).stage,'logged_out');
  assert.deepEqual(f.events.slice(-2).map(event=>event[0]),['confirm','logout']);
});

test('parses the primary location across Facebook text-layout variants', () => {
  const f = fixture();
  assert.equal(f.runner.parsePrimaryLocation('Your primary location is near:\nLahore, Punjab 54\nPrimary location is determined by information we use'), 'Lahore, Punjab 54');
  assert.equal(f.runner.parsePrimaryLocation('Your primary location is near: Cebu City, Central Visayas'), 'Cebu City, Central Visayas');
  assert.equal(f.runner.parsePrimaryLocation('Your primary location is near:\nLoading\u2026'), null);
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


test('verified already-signed-in account skips chooser and manual checkpoint migration', async () => {
  const f=fixture(); f.setIdentity('account-123');
  await f.runner.prepare(config);
  assert.equal(f.record.accountId,'account-123');
  assert.ok(!f.events.some(event=>event[0]==='login'));
});

test('slow remembered login waits for identity without repeated clicks or reloads', async () => {
  const f=fixture(); f.setDelay(35000);
  await f.runner.prepare(config);
  assert.equal(f.events.filter(event=>event[0]==='login').length,1);
  assert.equal(f.record.accountId,'account-123');
});

test('an explicit 30-second post-creation browse runs before logout', async () => {
  const f = fixture();
  f.record.stage='submitting'; f.setIdentity('account-123'); f.record.accountId='account-123'; f.record.browsed=true;
  await f.runner.finish({...config, post_success_browse_seconds: 30});
  assert.deepEqual(f.events.map(event=>event[0]), ['confirm','goto','logout']);
});

test('the default post-creation browse skips the home scroll', async () => {
  const f = fixture();
  f.record.stage='submitting'; f.setIdentity('account-123'); f.record.accountId='account-123'; f.record.browsed=true;
  await f.runner.finish(config);
  assert.ok(!f.events.some(event=>event[0]==='goto'));
  assert.equal(f.record.stage,'logged_out');
});

test('arms native dialog handling before saved-card selection', async () => {
  const f = fixture({ withDialogs: true });
  f.setDelay(500);
  await f.runner.prepare(config);
  assert.deepEqual(f.events.slice(0, 2).map(event => event[0]), ['accept-dialog', 'login']);
});

test('wrong or unverified existing identity never reaches browsing or creation', async () => {
  const f=fixture(); f.setIdentity('other-account'); f.setMatched(false);
  await assert.rejects(f.runner.prepare(config), /identity did not become verifiable/);
  assert.equal(f.record.accountId,undefined);
  assert.equal(f.events.length,0);
});

test('a matching display name cannot override an explicit different account ID', async () => {
  const f = fixture();
  f.setIdentity('another-id');
  f.setMatched(true);
  await assert.rejects(f.runner.login({ ...config, account_id: 'requested-id' }, f.record, 1000), /differs from the explicitly verified/);
  assert.equal(f.record.accountId, undefined);
  assert.equal(f.record.loginFailure.code, 'wrong_active_account');
  assert.equal(f.events.length, 0);
});

test('a pending login is resumed without selecting the saved card again', async () => {
  const f=fixture(); f.record.loginCardClicked=true;
  await assert.rejects(f.runner.prepare(config), /identity did not become verifiable/);
  assert.ok(!f.events.some(event=>event[0]==='login'));
  assert.equal(f.record.loginObservation.cardSelectionAttempted,true);
  assert.equal(f.record.loginObservation.identityPresent,false);
  assert.equal(f.record.loginObservation.savedCardVisible,true);
  assert.equal(f.record.loginObservation.passwordPromptVisible,false);
  assert.equal(f.record.loginObservation.passwordSubmissionAttempted,false);
  assert.ok(!JSON.stringify(f.record).includes(config.password));
});

test('saved-card navigation loss resumes waiting for identity without another card click', async () => {
  const f = fixture();
  let clicks = 0;
  f.page.getByRole = () => ({ filter() { return this; }, isVisible: async () => true,
    click: async () => { clicks++; f.setIdentity('account-123'); throw Error('Execution context was destroyed during navigation'); } });
  assert.equal(await f.runner.login(config, f.record, 1000), 'signed_in');
  assert.equal(clicks, 1);
  assert.equal(f.record.accountId, 'account-123');
});

test('an inert saved card reports observations without inferring expired sessions or retrying', async () => {
  const f=fixture();
  f.setDelay(120000);
  await assert.rejects(f.runner.login(config,f.record,1500), /within 1.5 seconds/);
  assert.equal(f.events.filter(event=>event[0]==='login').length,1);
  assert.equal(f.record.loginObservation.elapsedMs,1500);
  assert.equal(f.record.loginObservation.identityPresent,false);
  assert.equal(f.record.accountId,undefined);
});

test('identity verification requires the requested name on a link to the cookie identity', async () => {
  let now=0;
  const checkpoint={};
  const sandbox={
    Date:{now:()=>now},URL,
    location:{hostname:'www.facebook.com',href:'https://www.facebook.com/'},
    document:{cookie:'c_user=123; unrelated=fixture',querySelectorAll:()=>[
      {href:'https://www.facebook.com/profile.php?id=999',innerText:'Saved account'},
      {href:'https://www.facebook.com/profile.php?id=123',innerText:'Another account'}
    ]},
    page:{evaluate:async(callback,arg)=>callback(arg),waitForTimeout:async ms=>{now+=ms;},
      locator:()=>({first(){return this;},isVisible:async()=>false}),
      getByRole:()=>({filter(){return this;},isVisible:async()=>false})}
  };
  vm.runInNewContext(source+'\nglobalThis.runner=facebookPages;',sandbox);
  await assert.rejects(sandbox.runner.login(config,checkpoint,1000),/did not become verifiable/);
  assert.equal(checkpoint.accountId,undefined);
  sandbox.document.querySelectorAll=()=>[{href:'https://www.facebook.com/profile.php?id=123',innerText:' Saved   account '}];
  assert.equal(await sandbox.runner.login(config,checkpoint,1000),'signed_in');
  assert.equal(checkpoint.accountId,'123');
});

test('password form verification stays inside its dialog and accepts only the requested name or public ID', async () => {
  const sandbox = { URL, location: { href: 'https://www.facebook.com/' },
    page: { evaluate: async (callback, argument) => callback(argument) },
    document: { querySelector: () => ({ closest: () => ({ innerText: 'Another account\nPassword', querySelectorAll: () => [] }) }) },
  };
  vm.runInNewContext(source + '\nglobalThis.runner = facebookPages;', sandbox);
  assert.equal(await sandbox.runner.passwordAccountMatches(config), false);
  sandbox.document.querySelector = () => ({ closest: () => ({ innerText: ' Saved   account \nPassword', querySelectorAll: () => [] }) });
  assert.equal(await sandbox.runner.passwordAccountMatches(config), true);
  sandbox.document.querySelector = () => ({ closest: () => ({ innerText: 'Password', querySelectorAll: () => [{ href: 'https://www.facebook.com/profile.php?id=123' }] }) });
  assert.equal(await sandbox.runner.passwordAccountMatches({ ...config, account_id: '123' }), true);
  assert.equal(await sandbox.runner.passwordAccountMatches({ ...config, account_id: '456' }), false);
  sandbox.document.querySelector = () => ({ closest: () => ({ innerText: 'Saved account\nPassword', querySelectorAll: () => [{ href: 'https://www.facebook.com/profile.php?id=999' }] }) });
  assert.equal(await sandbox.runner.passwordAccountMatches({ ...config, account_id: '123' }), false);
});

test('an unexpected password form cannot receive credentials before account verification', async () => {
  const f = fixture();
  let fills = 0;
  f.page.locator = () => ({ first() { return this; }, isVisible: async () => true, fill: async () => fills++ });
  f.runner.passwordAccountMatches = async () => false;
  await assert.rejects(f.runner.login(config, f.record, 1000), /does not visibly identify/);
  assert.equal(fills, 0);
  assert.equal(f.record.loginSubmitted, undefined);
  assert.equal(f.record.loginFailure.code, 'wrong_or_unverified_password_form');
  f.runner.passwordAccountMatches = async () => true;
  f.page.locator = () => ({ first() { return this; }, isVisible: async () => true,
    fill: async () => fills++, press: async () => f.setIdentity('account-123') });
  assert.equal(await f.runner.login(config, f.record, 1000), 'signed_in');
  assert.equal(fills, 1);
  assert.equal(f.record.loginSubmitted, true);
});

test('a blocked Log in button preserves an unsubmitted attempt and resumes with a sole actual click', async () => {
  const f = fixture();
  let actualClicks = 0;
  let blocked = true;
  f.runner.passwordAccountMatches = async () => true;
  f.page.locator = () => ({ first() { return this; }, isVisible: async () => true, fill: async () => {} });
  f.page.getByRole = () => ({ filter() { return this; }, isVisible: async () => false,
    click: async options => {
      if (options?.trial) { if (blocked) throw Error('overlay blocks action'); return; }
      actualClicks++; f.setIdentity('account-123');
    } });
  const options = { ...config, login_button: true };
  await assert.rejects(f.runner.login(options, f.record, 1000), /not actionable/);
  assert.equal(f.record.loginSubmitted, undefined);
  assert.equal(f.record.loginFailure.code, 'login_button_not_actionable');
  assert.equal(actualClicks, 0);
  blocked = false;
  await f.runner.login(options, f.record, 1000);
  assert.equal(actualClicks, 1);
  assert.equal(f.record.loginSubmitted, true);
  assert.ok(!JSON.stringify(f.record).includes(config.password));
});
