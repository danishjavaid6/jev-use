'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { NativeDialogs } = require('../bin/native-dialogs');

function fixture() {
  const sent = [];
  const socket = { addEventListener() {}, send(raw) { sent.push(JSON.parse(raw)); } };
  const watcher = new NativeDialogs(socket, 'chosen-tab');
  watcher.sessionId = 'chosen-session';
  watcher.enabled = true;
  return { watcher, sent };
}

test('dismisses only the selected tab native account chooser', async () => {
  const { watcher, sent } = fixture();
  watcher.receive({ method: 'FedCm.dialogShown', sessionId: 'chosen-session', params: { dialogType: 'AccountChooser', dialogId: 'dialog1' } });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].method, 'FedCm.dismissDialog');
  assert.deepEqual(sent[0].params, { dialogId: 'dialog1' });
  assert.equal(sent[0].sessionId, 'chosen-session');
  watcher.receive({ id: sent[0].id, result: {} });
  await Promise.resolve();
  assert.deepEqual(watcher.dismissed, ['FedCm.AccountChooser']);
});

test('leaves other tabs, password flows and disabled dismissal untouched', () => {
  const { watcher, sent } = fixture();
  watcher.receive({ method: 'FedCm.dialogShown', sessionId: 'other-session', params: { dialogType: 'AccountChooser', dialogId: 'd' } });
  watcher.receive({ method: 'FedCm.dialogShown', sessionId: 'chosen-session', params: { dialogType: 'ConfirmIdpLogin', dialogId: 'd' } });
  watcher.enabled = false;
  watcher.receive({ method: 'FedCm.dialogShown', sessionId: 'chosen-session', params: { dialogType: 'AccountChooser', dialogId: 'd' } });
  assert.equal(sent.length, 0);
});

test('Windows chooser starts a scoped helper and stops when dismissal is disabled', () => {
  const { EventEmitter } = require('node:events');
  const { PassThrough } = require('node:stream');
  const { WindowsChooser } = require('../bin/windows-chooser');
  let command;
  let args;
  let killed = false;
  const chooser = new WindowsChooser(14771, (cmd, argv) => {
    command = cmd; args = argv;
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.kill = () => { killed = true; };
    return child;
  }, 'win32');
  chooser.toggle(true);
  assert.equal(command, 'powershell.exe');
  assert.equal(args.at(-1), '14771');
  assert.ok(args.some(arg => arg.endsWith('dismiss-account-chooser.ps1')));
  chooser.toggle(false);
  assert.equal(killed, true);
});

test('Windows popup inspection becomes ready only after the helper completes its first pass', async () => {
  const { EventEmitter } = require('node:events');
  const { PassThrough } = require('node:stream');
  const { WindowsChooser } = require('../bin/windows-chooser');
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.kill = () => {};
  const chooser = new WindowsChooser(14771, () => child, 'win32');
  chooser.toggle(true);
  let finished = false;
  const ready = chooser.waitUntilReady(1000).then(() => { finished = true; });
  await Promise.resolve();
  assert.equal(finished, false);
  child.stdout.write('{"dismissed":"Windows.ProfileChooser"}\n{"ready":true}\n');
  await ready;
  assert.equal(chooser.ready, true);
  assert.deepEqual(chooser.dismissed, ['Windows.ProfileChooser']);
  chooser.toggle(false);
  assert.equal(chooser.ready, false);
});

test('each action requires a fresh clear native-popup inspection', async () => {
  const { EventEmitter } = require('node:events');
  const { PassThrough } = require('node:stream');
  const { WindowsChooser } = require('../bin/windows-chooser');
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.kill = () => {};
  const chooser = new WindowsChooser(14771, () => child, 'win32');
  chooser.toggle(true);
  child.stdout.write('{"ready":true}\n{"inspected":true,"blocked":false,"kinds":[]}\n');
  let finished = false;
  const pending = chooser.ensureClear(1000).then(value => { finished = true; return value; });
  await Promise.resolve();
  assert.equal(finished, false, 'old clear state must not authorize a new action');
  child.stdout.write('{"inspected":true,"blocked":false,"kinds":[]}\n');
  assert.equal((await pending).blocked, false);
  const blocked = chooser.ensureClear(1000);
  await Promise.resolve();
  child.stdout.write('{"inspected":true,"blocked":true,"kinds":["native_modal"]}\n');
  await assert.rejects(blocked, /popup blocks the action.*native_modal/);
  chooser.toggle(false);
});

test('popup startup has an independent budget and clears an obsolete readiness warning', async () => {
  const { EventEmitter } = require('node:events');
  const { PassThrough } = require('node:stream');
  const { WindowsChooser } = require('../bin/windows-chooser');
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.kill = () => {};
  const chooser = new WindowsChooser(14771, () => child, 'win32');
  chooser.toggle(true);
  await chooser.waitUntilReady(1);
  assert.match(chooser.warning, /did not become ready/);
  child.stdout.write('{"ready":true}\n');
  assert.equal(chooser.warning, null);
  let startupBudget;
  chooser.waitUntilReady = async timeout => { startupBudget = timeout; };
  const pending = chooser.ensureClear(100);
  await Promise.resolve();
  child.stdout.write('{"inspected":true,"blocked":false,"kinds":[]}\n');
  await pending;
  assert.equal(startupBudget, undefined, 'fresh inspection timeout must not shorten startup');
  chooser.toggle(false);
});

test('SDK attachments use separate bookkeeping profiles while retaining the GoLogin endpoint', () => {
  const { attachmentOptions } = require('../bin/betterwright-bridge');
  const first = attachmentOptions('ws://127.0.0.1:1234/devtools/browser/test');
  const second = attachmentOptions('ws://127.0.0.1:1234/devtools/browser/test');
  assert.notEqual(first.profile, second.profile);
  assert.match(first.profile, /^jev-[a-f0-9-]+$/);
  assert.deepEqual(first.provider, second.provider);
  assert.equal(first.vault, false);
  assert.equal(first.credentialCapture, false);
});

test('popup helper failure preserves its sanitized exception type for recovery diagnostics', async () => {
  const { EventEmitter } = require('node:events');
  const { PassThrough } = require('node:stream');
  const { WindowsChooser } = require('../bin/windows-chooser');
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.kill = () => {};
  const chooser = new WindowsChooser(14771, () => child, 'win32');
  chooser.toggle(true);
  child.stdout.write('{"exceptionType":"System.InvalidOperationException"}\n{"warning":"Popup helper stopped"}\n');
  child.emit('exit', 1);
  await assert.rejects(chooser.ensureClear(100), /UIA: System.InvalidOperationException/);
  assert.equal(chooser.inspection, undefined, 'a failed inspection cannot manufacture a clear result');
});
