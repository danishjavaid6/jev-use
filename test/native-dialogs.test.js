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
