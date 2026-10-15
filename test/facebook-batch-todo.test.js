'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const todo = require('../skills/facebook-create-pages/scripts/batch-todo.js');

const profiles = [
  { id: 'p1', name: 'Profile 1' },
  { id: 'p2', name: 'Profile 2' },
];

test('init orders profiles and next reports the first pending account', () => {
  const state = todo.initTodo({ batch_id: 'B1', profiles });
  todo.addAccounts(state, 'p1', [{ account: 'A1', page_name: 'Page One' }]);
  assert.equal(state.profiles[0].status, 'open');
  assert.deepEqual(todo.nextWork(state).account, 'A1');
});

test('mark advances through accounts then completes the profile', () => {
  const state = todo.initTodo({ batch_id: 'B1', profiles });
  todo.addAccounts(state, 'p1', [
    { account: 'A1', page_name: 'Page One' },
    { account: 'A2', page_name: 'Page Two' },
  ]);
  todo.mark(state, 'p1', 'A1', 'logged_out');
  assert.equal(todo.nextWork(state).account, 'A2');
  todo.mark(state, 'p1', 'A2', 'skipped', 'missing_password');
  assert.equal(state.profiles[0].status, 'done');
  assert.equal(todo.nextWork(state).profile_id, 'p2');
  assert.equal(todo.nextWork(state).status, 'needs_enumeration');
  todo.markProfile(state, 'p2', 'done');
  assert.equal(todo.nextWork(state), null);
  assert.equal(todo.summary(state).complete, true);
});

test('profile with no accounts asks for enumeration instead of skipping', () => {
  const state = todo.initTodo({ batch_id: 'B1', profiles });
  assert.equal(todo.nextWork(state).status, 'needs_enumeration');
});

test('passwords are refused in accounts, details and saved state', () => {
  const state = todo.initTodo({ batch_id: 'B1', profiles });
  assert.throws(() => todo.addAccounts(state, 'p1', [{ account: 'A1', page_name: 'P', password: 'x' }]), /password/);
  todo.addAccounts(state, 'p1', [{ account: 'A1', page_name: 'P' }]);
  assert.throws(() => todo.mark(state, 'p1', 'A1', 'skipped', 'password=hunter2'), /password/);
  state.profiles[0].accounts[0].detail = 'leaked password';
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'todo-')), 'todo.json');
  assert.throws(() => todo.save(file, state), /password/);
});

test('save and load round-trip keeps ordering and statuses', () => {
  const state = todo.initTodo({ batch_id: 'B1', profiles });
  todo.addAccounts(state, 'p2', [{ account: 'B1', page_name: 'Page B' }]);
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'todo-')), 'todo.json');
  todo.save(file, state);
  const restored = todo.load(file);
  assert.equal(restored.profiles[1].accounts[0].account, 'B1');
  assert.equal(todo.summary(restored).accounts_total, 1);
});

test('unknown ids and statuses are rejected', () => {
  const state = todo.initTodo({ batch_id: 'B1', profiles });
  todo.addAccounts(state, 'p1', [{ account: 'A1', page_name: 'P' }]);
  assert.throws(() => todo.mark(state, 'nope', 'A1', 'logged_out'), /unknown profile/);
  assert.throws(() => todo.mark(state, 'p1', 'nope', 'logged_out'), /unknown account/);
  assert.throws(() => todo.mark(state, 'p1', 'A1', 'bogus'), /unknown account status/);
  assert.throws(() => todo.initTodo({ batch_id: 'B1', profiles: [{ id: 'p1' }, { id: 'p1' }] }), /duplicate/);
});
